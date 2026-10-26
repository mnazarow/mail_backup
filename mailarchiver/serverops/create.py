"""
Создание учётных записей Axigen списком.

Источник — строки «адрес; имя; фамилия» (или «Фамилия Имя <адрес>», как в
адресной книге) либо сотрудники из раздела «Сотрудники», у которых указан
адрес в домене сервера, а учётной записи на сервере ещё нет. Пароли —
случайные (файлом для раздачи, скачивается один раз) или один общий временный;
можно потребовать сменить пароль при первом входе, задать квоту и сразу
завести ящики в архиве.

Проверка показывает, что будет с каждой строкой, и ничего не меняет;
создание идёт заданием «Действия с ящиками на сервере», по одной учётной
записи, с проверкой после каждой (учётная запись открывается).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..errors import JobCancelled, MailArchiverError, ValidationError
from ..logging_setup import get_logger
from ..models import AuthType, JobStatus, JobType
from . import base

log = get_logger("serverops.create")

KEY = "ax_create"
LABEL = "Создать учётные записи"
MAX_LINES = 5000
_ANGLE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")


def parse_lines(text: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Строки списка → (учётные записи, нераспознанные строки)."""
    items, bad = [], []
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if len(lines) > MAX_LINES:
        raise ValidationError(f"За раз — не больше {MAX_LINES} строк.")
    for line in lines:
        raw = line.strip()
        m = _ANGLE.search(raw)
        if m:
            email = m.group(1)
            words = _ANGLE.sub(" ", raw).replace('"', " ").split()
            last = words[0] if words else ""
            first = words[1] if len(words) > 1 else ""
        else:
            parts = [p.strip() for p in re.split(r"[;\t,]", raw)]
            email = parts[0] if parts else ""
            first = parts[1] if len(parts) > 1 else ""
            last = parts[2] if len(parts) > 2 else ""
        email = email.strip().lower()
        if "@" not in email:
            bad.append({"line": raw[:200], "text": "нет адреса"})
            continue
        items.append({"email": email, "first": first[:100], "last": last[:100]})
    return items, bad


def _from_employees(svc, st) -> List[Dict[str, str]]:
    from .axigen_ops import split_full_name
    out = []
    for emp in svc.db.list_employees(status="active", limit=100000):
        email = (emp["email"] or "").strip().lower()
        if "@" not in email:
            continue
        names = split_full_name(emp["full_name"] or "", "last_first")
        out.append({"email": email, "first": names.get("firstName", ""), "last": names.get("lastName", "")})
    return out


def _plan(svc, params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    from ..axigen import store as axstore
    from ..axigen.common import load_settings, split_email
    from ..axigen.directory import valid_account_name, valid_domain
    st = load_settings(svc)
    source = params.get("source") or "text"
    if source == "employees":
        items, bad = _from_employees(svc, st), []
    else:
        items, bad = parse_lines(params.get("text") or "")
    rows = axstore.list_accounts(svc.db)
    taken = {}
    domains = set()
    for row in rows:
        domains.add(row["domain"])
        if not row.get("deleted_at"):
            taken[row["email"]] = f"уже есть учётная запись {row['email']}"
            for alias in row.get("aliases") or []:
                taken[alias] = f"адрес занят псевдонимом учётной записи {row['email']}"
    out: List[Dict[str, Any]] = []
    seen = set()
    have_account = 0
    for b in bad:
        out.append({"email": b["line"], "status": "skip", "text": b["text"]})
    for it in items:
        email = it["email"]
        local, domain = split_email(email)
        status, text = "ok", ""
        if email in seen:
            status, text = "skip", "повторяется в списке"
        elif not valid_domain(domain) or not valid_account_name(local):
            status, text = "skip", "недопустимый адрес"
        elif not st.domain_allowed(domain):
            status, text = "skip", f"домен {domain} не входит в домены из настроек Axigen"
        elif domains and domain not in domains:
            status, text = "skip", f"домена {domain} нет на сервере (по последней сверке)"
        elif email in taken:
            if source == "employees":
                # у большинства сотрудников учётная запись уже есть — не засоряем ими проверку
                have_account += 1
                seen.add(email)
                continue
            status, text = "skip", taken[email]
        seen.add(email)
        if status == "ok":
            name = " ".join(x for x in (it.get("first"), it.get("last")) if x)
            text = "будет создана" + (f" — {name}" if name else "")
        out.append({"email": email, "first": it.get("first", ""), "last": it.get("last", ""), "status": status,
                    "text": text})
    warnings = []
    if not rows:
        warnings.append("Сверки с Axigen ещё не было — проверить, какие учётные записи уже есть, не по чему; "
                        "существующие сервер всё равно не пересоздаст (будет отказ).")
    if source == "employees" and not items:
        warnings.append("В разделе «Сотрудники» нет действующих сотрудников с адресом.")
    if have_account:
        warnings.append(f"Сотрудники, у которых учётная запись на сервере уже есть, в списке не показаны: "
                        f"{have_account}.")
    return out, warnings


def _plan_server(svc, params: Dict[str, Any], conn) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Как :func:`_plan`, но для подключённого почтового сервера (по его последней сверке)."""
    from ..mailservers import store as msstore
    from ..mailservers.base import EMAIL_RE
    source = params.get("source") or "text"
    if source == "employees":
        items, bad = _from_employees(svc, None), []
    else:
        items, bad = parse_lines(params.get("text") or "")
    rows = msstore.list_accounts(svc.db, conn.id)
    taken: Dict[str, str] = {}
    domains = set()
    for row in rows:
        domains.add(row["domain"])
        if not row.get("deleted_at"):
            taken[row["email"]] = f"уже есть учётная запись {row['email']}"
            for alias in row.get("aliases") or []:
                taken[alias] = f"адрес занят псевдонимом учётной записи {row['email']}"
    out: List[Dict[str, Any]] = [{"email": b["line"], "status": "skip", "text": b["text"]} for b in bad]
    seen = set()
    have_account = 0
    for it in items:
        email = it["email"]
        domain = email.partition("@")[2]
        status, text = "ok", ""
        if email in seen:
            status, text = "skip", "повторяется в списке"
        elif not EMAIL_RE.match(email):
            status, text = "skip", "недопустимый адрес"
        elif domains and domain not in domains:
            status, text = "skip", f"домена {domain} нет на сервере «{conn.name}» (по последней сверке)"
        elif email in taken:
            if source == "employees":
                have_account += 1
                seen.add(email)
                continue
            status, text = "skip", taken[email]
        seen.add(email)
        if status == "ok":
            name = " ".join(x for x in (it.get("first"), it.get("last")) if x)
            text = "будет создана" + (f" — {name}" if name else "")
        out.append({"email": email, "first": it.get("first", ""), "last": it.get("last", ""), "status": status,
                    "text": text})
    warnings = []
    if not rows:
        warnings.append(f"Сверки с «{conn.name}» ещё не было — проверить, какие учётные записи уже есть, не по чему; "
                        f"существующие сервер всё равно не пересоздаст (будет отказ).")
    if source == "employees" and not items:
        warnings.append("В разделе «Сотрудники» нет действующих сотрудников с адресом.")
    if have_account:
        warnings.append(f"Сотрудники, у которых учётная запись на сервере уже есть, в списке не показаны: "
                        f"{have_account}.")
    return out, warnings


def _server_conn(svc, server_id: int):
    """Подключение, в котором создаются учётные записи (проверка: включено, умеет, изменения разрешены)."""
    from ..mailservers import service as mss
    from ..mailservers.base import Cap
    conn = mss.get(svc, server_id)
    cls = mss.driver_class(conn.type)
    if not conn.enabled:
        raise ValidationError(f"Подключение «{conn.name}» выключено.", hint="Раздел «Почтовые серверы».")
    if not cls.can(Cap.CREATE):
        raise ValidationError(f"{cls.label}: создавать учётные записи через API этот сервер не позволяет.")
    if not conn.allow_changes:
        raise ValidationError(f"Изменения на сервере «{conn.name}» не разрешены.",
                              hint="Раздел «Почтовые серверы» → подключение → «Разрешить изменения на сервере».")
    return conn, cls


def run(svc, user: dict, params: Dict[str, Any], *, preview: bool = True, confirm: str = "") -> Dict[str, Any]:
    server_id = int(params.get("server_id") or 0)
    if server_id:
        return _run_server(svc, user, params, server_id, preview=preview)
    from ..axigen import actions
    from ..axigen.common import load_settings
    st = load_settings(svc)
    if not st.enabled:
        raise ValidationError("Работа с сервером Axigen выключена.", hint="Настройки → Сервер Axigen.")
    try:
        st.require()
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(getattr(exc, "message", str(exc)), hint=getattr(exc, "hint", None)) from exc
    actions.require_changes_allowed(st)
    mode = params.get("password_mode") or "random_file"
    if mode not in ("random_file", "same"):
        raise ValidationError("Пароли — случайные (файлом) или один общий.")
    if mode == "same":
        actions.check_password(params.get("password") or "")
    quota_mb = int(params.get("quota_mb") or 0)
    if quota_mb and not 1 <= quota_mb <= 4 * 1024 * 1024:
        raise ValidationError("Квота — от 1 МБ до 4 ТБ (или 0 — как у домена).")
    items, warnings = _plan(svc, params)
    return _launch(svc, user, params, items, warnings, mode, quota_mb, preview=preview, where="")


def _run_server(svc, user: dict, params: Dict[str, Any], server_id: int, *, preview: bool) -> Dict[str, Any]:
    from ..mailservers import service as mss
    from ..mailservers.base import Cap
    conn, cls = _server_conn(svc, server_id)
    mode = params.get("password_mode") or "random_file"
    if mode not in ("random_file", "same"):
        raise ValidationError("Пароли — случайные (файлом) или один общий.")
    if mode == "same":
        mss.driver(svc, conn).check_password(params.get("password") or "")
    quota_mb = int(params.get("quota_mb") or 0)
    if quota_mb and not 1 <= quota_mb <= 4 * 1024 * 1024:
        raise ValidationError("Квота — от 1 МБ до 4 ТБ (или 0 — как задано на сервере).")
    items, warnings = _plan_server(svc, params, conn)
    if quota_mb and not cls.can(Cap.QUOTA):
        warnings.append(f"{cls.label}: квоту через API не задать — будет та, что на сервере по умолчанию.")
    if params.get("must_change") and not cls.can(Cap.MUST_CHANGE):
        warnings.append(f"{cls.label}: смену пароля при первом входе сервер не требует (API этого не позволяет).")
    if params.get("add_to_archive", True):
        if not cls.can(Cap.IMAP_ADMIN):
            warnings.append(f"{cls.label}: копирование без паролей сотрудников не поддерживается — ящики в архиве "
                            f"не заведутся сами (заведите их с паролем в «Почтовых ящиках»).")
        else:
            problem = mss.driver(svc, conn).imap_admin_problem()
            if problem:
                warnings.append(f"{cls.label}: вход в ящики без паролей не настроен ({problem}) — ящики в архиве "
                                f"не заведутся сами.")
    return _launch(svc, user, params, items, warnings, mode, quota_mb, preview=preview, where=f"«{conn.name}»",
                   server_id=server_id)


def _launch(svc, user: dict, params: Dict[str, Any], items: List[Dict[str, Any]], warnings: List[str], mode: str,
            quota_mb: int, *, preview: bool, where: str, server_id: int = 0) -> Dict[str, Any]:
    username = (user or {}).get("username") or "admin"
    ready = [i for i in items if i["status"] == "ok"]
    counts = {"ok": len(ready), "skip": len(items) - len(ready), "fail": 0}
    result = {"ok": True, "preview": preview, "items": items, "counts": counts, "warnings": warnings,
              "total": len(items), "danger": 1, "op_id": None, "jobs": []}
    if preview:
        result["summary"] = f"{LABEL}{' на ' + where if where else ''}: будет создано {len(ready)} из {len(items)}."
        return result
    if not ready:
        raise ValidationError("Создавать нечего.")
    options = {"password_mode": mode, "must_change": bool(params.get("must_change")), "quota_mb": quota_mb,
               "add_to_archive": bool(params.get("add_to_archive", True)),
               "items": [{"email": i["email"], "first": i["first"], "last": i["last"]} for i in ready]}
    if server_id:
        options["server_id"] = server_id
    if mode == "same":
        options["password_enc"] = svc.db.secret.encrypt(params.get("password") or "")
    safe = {"source": params.get("source") or "text", "password_mode": mode, "must_change": options["must_change"],
            "quota_mb": quota_mb, "add_to_archive": options["add_to_archive"]}
    if server_id:
        safe["server_id"] = server_id
    op_id = svc.db.create_bulk_op(username, base.HISTORY_PREFIX + KEY, LABEL, safe, len(items))
    from . import progress
    progress.started(svc.db, op_id)                 # до очереди — см. base.run
    job_id = svc.queue.enqueue(JobType.SERVER_ACTION, None,
                               {"action": KEY, "keys": [], "options": options, "requested_by": username,
                                "op_id": op_id}, priority=4, max_attempts=1, created_by=username)
    rows = [{"id": i["email"], "key": i["email"], "name": i["email"], "username": i["email"],
             "status": "ok" if i["status"] == "ok" else "skip",
             "detail": "в задании «Действия с ящиками на сервере»" if i["status"] == "ok" else i["text"],
             **({"job_id": job_id} if i["status"] == "ok" else {})} for i in items]
    summary = f"{LABEL}{' на ' + where if where else ''}: в задании {len(ready)} из {len(items)}. Задание №{job_id}."
    svc.db.finish_bulk_op(op_id, ok=len(ready), skipped=counts["skip"], failed=0, jobs=1, summary=summary,
                          results=rows)
    svc.db.add_audit(username, base.HISTORY_PREFIX + KEY, f"операция №{op_id}: {summary}"[:900])
    result.update({"op_id": op_id, "jobs": [job_id], "summary": summary})
    return result


class _AxigenCreator:
    """Создание учётных записей на Axigen (CLI)."""

    def __init__(self, svc, who: str, options: Dict[str, Any]) -> None:
        from ..axigen import actions
        from ..axigen.common import load_settings
        self.svc, self.who, self.options = svc, who, options
        self.st = load_settings(svc).require()
        actions.require_changes_allowed(self.st)
        self.quota_kb = int(options.get("quota_mb") or 0) * 1024

    def random_password(self) -> str:
        from ..axigen import actions
        return actions.random_password()

    def create(self, it: Dict[str, str], password: str, before, notes: List[str], state: Dict[str, bool]) -> None:
        from ..axigen import actions
        from ..axigen import store as axstore
        from ..axigen.common import AxigenError
        from ..axigen.jobs import add_mailboxes
        from ..axigen.service import logged_session
        svc, email, quota_kb = self.svc, it["email"], self.quota_kb
        with logged_session(svc, username=self.who, kind="write", summary=f"Создание учётной записи {email}",
                            st=self.st) as cli:
            before()
            actions.create_account(cli, email, password, first_name=it.get("first", ""), last_name=it.get("last", ""))
            state["created"] = True
            notes.append("создана")
            svc.db.add_audit(self.who, base.HISTORY_PREFIX + KEY, email)
            if quota_kb:
                try:
                    actions.set_quota(cli, email, quota_kb)
                    notes.append(f"квота {quota_kb // 1024} МБ")
                except (AxigenError, MailArchiverError) as exc:
                    notes.append(f"квота не задана ({exc.message})")
            if self.options.get("must_change"):
                try:
                    actions.set_account_flag(cli, email, "mustChangePass", "yes")
                    notes.append("при первом входе — смена пароля")
                except (AxigenError, MailArchiverError) as exc:
                    notes.append(f"требование сменить пароль не записано ({exc.message})")
            services = None
            try:
                services = actions.get_services(cli, email)
            except AxigenError:
                pass
        local, domain = actions.parse_email(email)
        axstore.upsert_created(svc.db, email=email, domain=domain, name=local, first_name=it.get("first", ""),
                               last_name=it.get("last", ""), services=services)
        if quota_kb:
            axstore.update_fields(svc.db, email, quota_kb=quota_kb)
        if self.options.get("add_to_archive"):
            added = add_mailboxes(svc, [email], user=self.who, auth=AuthType.AXIGEN_FTP)
            if added and added[0]["status"] == "ok":
                notes.append("ящик заведён в архиве")


class _ServerCreator:
    """Создание учётных записей на подключённом почтовом сервере (драйвер подключения)."""

    def __init__(self, svc, who: str, options: Dict[str, Any]) -> None:
        from ..mailservers import service as mss
        self.svc, self.who, self.options = svc, who, options
        self.conn, self.cls = _server_conn(svc, int(options["server_id"]))
        self.quota = int(options.get("quota_mb") or 0) * 1024 * 1024
        self.mss = mss

    def random_password(self) -> str:
        return self.mss.driver(self.svc, self.conn).random_password()

    def create(self, it: Dict[str, str], password: str, before, notes: List[str], state: Dict[str, bool]) -> None:
        from ..mailservers import store as msstore
        from ..mailservers.base import Cap, normalize_account
        from ..mailservers.mailboxes import add_mailboxes
        svc, email, conn = self.svc, it["email"], self.conn
        must = bool(self.options.get("must_change")) and self.cls.can(Cap.MUST_CHANGE)
        quota = self.quota if self.cls.can(Cap.QUOTA) else None
        with self.mss.session(svc, conn, user=self.who, kind="write", summary=f"Создание учётной записи {email}") as drv:
            drv.check_password(password)
            before()
            raw = drv.create_account(email, first_name=it.get("first", ""), last_name=it.get("last", ""),
                                     password=password, must_change=must, quota_bytes=quota or None)
            state["created"] = True
            notes.append("создана")
            if isinstance(raw, dict) and raw.get("warning"):
                # создана, но не всё вышло (Microsoft 365: лицензия не выдана) — сказать прямо в строке
                notes.append(f"⚠ {raw['warning']}")
            svc.db.add_audit(self.who, base.HISTORY_PREFIX + KEY, f"{email} — «{conn.name}»")
            if quota:
                notes.append(f"квота {quota // (1024 * 1024)} МБ")
            elif self.quota:
                notes.append(f"квоту {self.cls.label} через API не задаёт")
            if must:
                notes.append("при первом входе — смена пароля")
            elif self.options.get("must_change"):
                notes.append(f"смену пароля при входе {self.cls.label} не требует")
            try:
                fresh = drv.get_account(email)
            except MailArchiverError:
                fresh = None
        row = fresh or raw or {"email": email, "first_name": it.get("first", ""), "last_name": it.get("last", "")}
        row = dict(row)
        row.setdefault("email", email)
        msstore.upsert_account(svc.db, conn.id, normalize_account(row))
        if self.options.get("add_to_archive"):
            added = add_mailboxes(svc, conn, [email], user=self.who)
            if added and added[0]["status"] == "ok":
                notes.append("ящик заведён в архиве")
            elif added:
                notes.append(f"ящик в архиве не заведён: {added[0]['text']}")


def handle_job(ctx) -> Dict[str, Any]:
    """Задание: создать учётные записи по одной."""
    from ..axigen.common import AxigenError
    from . import secrets
    svc = ctx.services
    params = ctx.params
    options = params.get("options") or {}
    who = params.get("requested_by") or "admin"
    op_id = params.get("op_id")
    creator = _ServerCreator(svc, who, options) if options.get("server_id") else _AxigenCreator(svc, who, options)
    items = list(options.get("items") or [])
    mode = options.get("password_mode") or "random_file"
    common = svc.db.secret.decrypt(options.get("password_enc") or "") if mode == "same" else ""
    where = f" на «{creator.conn.name}»" if options.get("server_id") else ""
    ctx.event("INFO", f"{LABEL}{where}: {len(items)}.")
    from . import progress
    progress.started(svc.db, op_id)
    results: List[Dict] = []
    stats = {"ok": 0, "skip": 0, "fail": 0}
    cancelled_at = None
    for index, it in enumerate(items):
        if ctx.is_cancelled():
            # остановка службы — не отмена (очередь сама отметит задание); отмена администратором — итог
            # по уже созданным учётным записям сохраняется
            if not svc.db.is_cancel_requested(ctx.job_id):
                raise JobCancelled("Задание отменено.")
            cancelled_at = index
            break
        ctx.progress(index, len(items), it["email"])
        email = it["email"]
        password = common or creator.random_password()
        notes: List[str] = []
        status = "ok"
        state = {"created": False}
        to_file = mode == "random_file" and bool(op_id)
        progress.mark(svc.db, op_id, email, progress.RUNNING)

        def before() -> None:
            # пароль — в файл для раздачи ДО создания: что бы ни случилось дальше, у учётной записи не
            # окажется пароля, которого никто не знает
            if to_file:
                secrets.save(svc.db, op_id, email, password,
                             name=" ".join(x for x in (it.get("first"), it.get("last")) if x))
        try:
            try:
                creator.create(it, password, before, notes, state)
            except Exception as exc:
                if to_file and not state["created"]:
                    if _already_exists(exc) or isinstance(exc, ValidationError):
                        # уже есть или строка отклонена до обращения к серверу — сервер точно не создал
                        secrets.forget(svc.db, op_id, email)
                    else:
                        secrets.set_note(svc.db, op_id, email, "создание не подтверждено сервером — проверьте, "
                                                               "есть ли учётная запись")
                raise
            if to_file:
                notes.insert(1, "пароль — в файле для раздачи")
        except (AxigenError, MailArchiverError) as exc:
            if state["created"]:
                status = "fail"
                notes.append(f"затем ошибка: {exc.message}")
            else:
                status = "skip" if _already_exists(exc) else "fail"
                notes = [exc.message]
        except Exception as exc:  # noqa: BLE001
            log.exception("Создание учётной записи %s упало", email)
            status = "fail"
            text = f"{type(exc).__name__}: {exc}"
            notes = notes + [f"затем ошибка: {text}"] if state["created"] else [text]
        stats[status] += 1
        item_status = {"ok": "success", "skip": "skipped", "fail": "failed"}[status]
        results.append({"name": email, "key": email, "status": item_status, "detail": "; ".join(notes)[:1000]})
        # создана, но потом ошибка (квота, ящик в архиве) — повтор создания не нужен: учётная запись уже есть
        progress.mark(svc.db, op_id, email, progress.SUCCESS if state["created"] else item_status)
        ctx.event({"ok": "INFO", "skip": "WARNING", "fail": "ERROR"}[status], f"{email}: {'; '.join(notes)}"[:600])
    if cancelled_at is not None:
        summary = (f"{LABEL}: отменено — создано {stats['ok']}, пропущено {stats['skip']}, ошибок {stats['fail']}; "
                   f"не обработано {len(items) - cancelled_at} (из {len(items)}).")
        ctx.event("WARNING", summary)
        return {"final_status": JobStatus.CANCELLED, "summary": summary, "ok": stats["ok"],
                "skipped": stats["skip"], "failed": stats["fail"], "items": results}
    ctx.progress(len(items), len(items), "Готово")
    summary = f"{LABEL}: создано {stats['ok']}, пропущено {stats['skip']}, ошибок {stats['fail']} (из {len(items)})."
    final = JobStatus.SUCCESS if not stats["fail"] else (JobStatus.PARTIAL if stats["ok"] else JobStatus.FAILED)
    return {"final_status": final, "summary": summary, "ok": stats["ok"], "skipped": stats["skip"],
            "failed": stats["fail"], "items": results}


def _already_exists(exc: BaseException) -> bool:
    from ..mailservers.base import AlreadyExists
    return isinstance(exc, AlreadyExists) or "уже есть" in str(getattr(exc, "message", exc))
