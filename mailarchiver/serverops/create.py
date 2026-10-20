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


def run(svc, user: dict, params: Dict[str, Any], *, preview: bool = True, confirm: str = "") -> Dict[str, Any]:
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
    username = (user or {}).get("username") or "admin"
    ready = [i for i in items if i["status"] == "ok"]
    counts = {"ok": len(ready), "skip": len(items) - len(ready), "fail": 0}
    result = {"ok": True, "preview": preview, "items": items, "counts": counts, "warnings": warnings,
              "total": len(items), "danger": 1, "op_id": None, "jobs": []}
    if preview:
        result["summary"] = f"{LABEL}: будет создано {len(ready)} из {len(items)}."
        return result
    if not ready:
        raise ValidationError("Создавать нечего.")
    options = {"password_mode": mode, "must_change": bool(params.get("must_change")), "quota_mb": quota_mb,
               "add_to_archive": bool(params.get("add_to_archive", True)),
               "items": [{"email": i["email"], "first": i["first"], "last": i["last"]} for i in ready]}
    if mode == "same":
        options["password_enc"] = svc.db.secret.encrypt(params.get("password") or "")
    safe = {"source": params.get("source") or "text", "password_mode": mode, "must_change": options["must_change"],
            "quota_mb": quota_mb, "add_to_archive": options["add_to_archive"]}
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
    summary = f"{LABEL}: в задании {len(ready)} из {len(items)}. Задание №{job_id}."
    svc.db.finish_bulk_op(op_id, ok=len(ready), skipped=counts["skip"], failed=0, jobs=1, summary=summary,
                          results=rows)
    svc.db.add_audit(username, base.HISTORY_PREFIX + KEY, f"операция №{op_id}: {summary}"[:900])
    result.update({"op_id": op_id, "jobs": [job_id], "summary": summary})
    return result


def handle_job(ctx) -> Dict[str, Any]:
    """Задание: создать учётные записи по одной."""
    from ..axigen import actions
    from ..axigen import store as axstore
    from ..axigen.common import AxigenError, load_settings
    from ..axigen.jobs import add_mailboxes
    from ..axigen.service import logged_session
    from . import secrets
    svc = ctx.services
    params = ctx.params
    options = params.get("options") or {}
    who = params.get("requested_by") or "admin"
    op_id = params.get("op_id")
    st = load_settings(svc).require()
    actions.require_changes_allowed(st)
    items = list(options.get("items") or [])
    mode = options.get("password_mode") or "random_file"
    common = svc.db.secret.decrypt(options.get("password_enc") or "") if mode == "same" else ""
    quota_kb = int(options.get("quota_mb") or 0) * 1024
    ctx.event("INFO", f"{LABEL}: {len(items)}.")
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
        password = common or actions.random_password()
        notes: List[str] = []
        status = "ok"
        created = False
        to_file = mode == "random_file" and bool(op_id)
        progress.mark(svc.db, op_id, email, progress.RUNNING)
        try:
            with logged_session(svc, username=who, kind="write", summary=f"Создание учётной записи {email}",
                                st=st) as cli:
                # пароль — в файл для раздачи ДО создания: что бы ни случилось дальше, у учётной записи не
                # окажется пароля, которого никто не знает
                if to_file:
                    secrets.save(svc.db, op_id, email, password,
                                 name=" ".join(x for x in (it.get("first"), it.get("last")) if x))
                try:
                    actions.create_account(cli, email, password, first_name=it.get("first", ""),
                                           last_name=it.get("last", ""))
                except Exception as exc:
                    if to_file:
                        if "уже есть" in str(getattr(exc, "message", exc)):
                            secrets.forget(svc.db, op_id, email)        # сервер точно не создал
                        else:
                            secrets.set_note(svc.db, op_id, email, "создание не подтверждено сервером — проверьте, "
                                                                   "есть ли учётная запись")
                    raise
                created = True
                notes.append("создана")
                svc.db.add_audit(who, base.HISTORY_PREFIX + KEY, email)
                if to_file:
                    notes.append("пароль — в файле для раздачи")
                if quota_kb:
                    try:
                        actions.set_quota(cli, email, quota_kb)
                        notes.append(f"квота {quota_kb // 1024} МБ")
                    except (AxigenError, MailArchiverError) as exc:
                        notes.append(f"квота не задана ({exc.message})")
                if options.get("must_change"):
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
            if options.get("add_to_archive"):
                added = add_mailboxes(svc, [email], user=who, auth=AuthType.AXIGEN_FTP)
                if added and added[0]["status"] == "ok":
                    notes.append("ящик заведён в архиве")
        except (AxigenError, MailArchiverError) as exc:
            if created:
                status = "fail"
                notes.append(f"затем ошибка: {exc.message}")
            else:
                status = "skip" if "уже есть" in exc.message else "fail"
                notes = [exc.message]
        except Exception as exc:  # noqa: BLE001
            log.exception("Создание учётной записи %s упало", email)
            status = "fail"
            text = f"{type(exc).__name__}: {exc}"
            notes = notes + [f"затем ошибка: {text}"] if created else [text]
        stats[status] += 1
        item_status = {"ok": "success", "skip": "skipped", "fail": "failed"}[status]
        results.append({"name": email, "key": email, "status": item_status, "detail": "; ".join(notes)[:1000]})
        # создана, но потом ошибка (квота, ящик в архиве) — повтор создания не нужен: учётная запись уже есть
        progress.mark(svc.db, op_id, email, progress.SUCCESS if created else item_status)
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
