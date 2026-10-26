"""
Действия раздела «Ящики на сервере» с учётными записями подключённых почтовых серверов.

Те же действия, что для Axigen (закрыть вход, пароли, квота, псевдонимы, ФИО,
удаление с передачей адреса коллеге…), плюс автоответ и пересылка — через
драйвер подключения (:mod:`mailarchiver.mailservers`). Действие доступно
учётной записи, только если её сервер это умеет (возможность драйвера) и в
подключении разрешены изменения; иначе ящик пропускается с причиной.

После каждого изменения учётная запись перечитывается с сервера (если драйвер
умеет читать одну учётную запись) — раздел показывает то, что на сервере на
самом деле, а не то, что просили.

Удаление — только при полном архиве, как у Axigen, но проверка — по IMAP: ящик
должен копироваться без пароля сотрудника (через подключение или вход
администратора почты), чтобы после закрытия входа в него можно было войти;
архив ставится на бессрочное удержание, ящик копируется ещё раз, и каждое
письмо каждой папки на сервере сверяется с архивом (папка, UIDVALIDITY, UID,
файл на диске). Хоть одного письма нет — учётная запись не удаляется.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import JobCancelled, MailArchiverError, ValidationError
from ..mailservers import service as mss
from ..mailservers import store as msstore
from ..mailservers.base import Cap, NotSupported, check_email, norm_email as normalize_email
from ..models import AuthType
from ..util import human_size, utcnow_iso
from . import base
from . import targets as tg

#: Что нужно серверу для действия (ключ действия → возможность драйвера).
NEEDS: Dict[str, str] = {
    "ax_block": Cap.BLOCK, "ax_unblock": Cap.BLOCK, "ax_password": Cap.PASSWORD, "ax_must_change": Cap.MUST_CHANGE,
    "ax_quota": Cap.QUOTA, "ax_alias_add": Cap.ALIASES, "ax_alias_remove": Cap.ALIASES, "ax_contact": Cap.CONTACT,
    "ax_add_mailboxes": Cap.READ, "ax_refresh": Cap.READ, "ax_delete": Cap.DELETE, "ax_forward": Cap.ALIASES,
    "srv_autoreply": Cap.AUTOREPLY, "srv_forwarding": Cap.FORWARD,
}
#: Действия, которые есть только у Axigen.
AXIGEN_ONLY = ("ax_services_off", "ax_services_on", "ax_account_type", "ax_publish", "ax_purge")
#: Не меняют сервер — разрешение изменений не нужно.
READ_ONLY = ("ax_add_mailboxes", "ax_refresh")
#: Работают и с учётной записью, удалённой на сервере.
FOR_DELETED = ("ax_forward", "ax_refresh")


def label(t: tg.Target) -> str:
    try:
        return mss.driver_class(t.conn.type).label
    except ValidationError:
        return t.conn.type


def has(t: tg.Target, cap: str) -> bool:
    try:
        return mss.driver_class(t.conn.type).can(cap)
    except ValidationError:
        return False


def _session(ctx, t: tg.Target, summary: str, kind: str = "write"):
    return mss.session(ctx.svc, t.conn, user=ctx.user, kind=kind, summary=summary)


def _reread(ctx, t: tg.Target, drv) -> Optional[Dict[str, Any]]:
    """Перечитать учётную запись (тем же драйвером) и обновить снимок; None — не умеет или нет её."""
    from ..mailservers.base import normalize_account
    try:
        raw = drv.get_account(t.email)
    except MailArchiverError:
        return None
    if raw is None:
        return None
    acc = normalize_account(raw)
    if acc["email"] != t.email:
        # сервер нашёл адрес как псевдоним ДРУГОЙ учётной записи — это не она (её, видимо, уже нет)
        return None
    msstore.upsert_account(ctx.db, t.conn.id, acc)
    return msstore.get_account(ctx.db, t.conn.id, acc["email"])


def bound_problem(t: tg.Target, acc) -> str:
    """Ящик архива — точно ящик этой учётной записи на ЭТОМ сервере? (пусто — да).

    Связь «по логину» может указывать на ящик с прежнего сервера (переезд на Microsoft 365, а ящик копировался
    со старого Zimbra): сверять с ним полноту архива или сохранять в него новый пароль нельзя."""
    if acc is None:
        return "ящика нет в архиве"
    if acc.auth_type == AuthType.SERVER:
        return "" if getattr(acc, "server_id", None) == t.conn.id else \
            f"ящик «{acc.name}» копируется через другое подключение"
    host = (acc.host or "").strip().lower()
    if t.host and host == t.host:
        return ""
    return (f"ящик архива «{acc.name}» копируется с сервера {acc.host or '—'}, а IMAP-сервер подключения "
            f"«{t.conn.name}» — {t.host or 'не задан'}: это может быть другой сервер")


def _partial(ctx, action, t: tg.Target, notes: List[str], exc: BaseException) -> Tuple[str, str]:
    """Часть изменений сделана, потом ошибка: сделанное — в аудит, ящик — «ошибка» с перечнем сделанного."""
    done = "; ".join(notes)
    msg = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
    ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + action.key, f"{t.email}: сделано частично: {done}"[:300])
    ctx.data.setdefault("audited", set()).add(t.key)
    return "fail", f"{done}; затем ошибка: {msg}"


def _emit(ctx, t: tg.Target):
    def emit(level: str, text: str) -> None:
        if ctx.job is not None:
            ctx.job.event(level, f"{t.email}: {text}"[:600])
    return emit


# ---------------------------------------------------------------------------
#  Общая проверка
# ---------------------------------------------------------------------------
def base_check(action, ctx, t: tg.Target) -> str:
    key = action.key
    if key in AXIGEN_ONLY:
        return f"«{action.label}» есть только у Axigen, а это {label(t)}"
    cap = NEEDS.get(key)
    if cap is None:
        return f"«{action.label}» для учётных записей {label(t)} не выполняется"
    if t.deleted and key not in FOR_DELETED:
        return "учётная запись удалена на сервере"
    if not has(t, cap):
        return f"{label(t)}: {Cap.LABELS.get(cap, cap)} — этот сервер так не умеет (или его API этого не позволяет)"
    if key not in READ_ONLY and getattr(action, "axigen_change", False) and not t.conn.allow_changes:
        return f"изменения на сервере «{t.conn.name}» не разрешены (раздел «Почтовые серверы» → подключение)"
    return ""


def check(action, ctx, t: tg.Target) -> str:
    reason = base_check(action, ctx, t)
    if reason:
        return reason
    fn = CHECKS.get(action.key)
    return fn(action, ctx, t) if fn else ""


def plan(action, ctx, t: tg.Target) -> str:
    fn = PLANS.get(action.key)
    return fn(action, ctx, t) if fn else "будет выполнено"


def apply(action, ctx, t: tg.Target) -> str:
    """Сразу выполняемые действия (``immediate``) для учётной записи подключения."""
    if action.key == "ax_add_mailboxes":
        return apply_add_mailboxes(action, ctx, t)
    raise ValidationError(f"«{action.label}» для учётных записей {label(t)} не выполняется.")


def execute(action, ctx, t: tg.Target) -> Tuple[str, str]:
    fn = EXECS.get(action.key)
    if fn is None:
        return "skip", base_check(action, ctx, t) or "не выполняется"
    try:
        return fn(action, ctx, t)
    except NotSupported as exc:
        return "skip", exc.message


# ---------------------------------------------------------------------------
#  Вход и пароли
# ---------------------------------------------------------------------------
def _block_check(action, ctx, t):
    if t.server.get("login_blocked") is True and not ctx.params.get("random_password"):
        return "вход уже закрыт"
    return ""


def _block_plan(action, ctx, t):
    parts = []
    if ctx.params.get("final_copy", True) and t.mailbox is not None and t.mailbox.enabled:
        parts.append("последняя копия")
    parts.append("вход уже закрыт" if t.server.get("login_blocked") else "вход будет закрыт")
    if ctx.params.get("random_password"):
        parts.append("пароль сменится на случайный" if has(t, Cap.PASSWORD) else
                     f"пароль не сменится ({label(t)} пароли не меняет)")
    if t.mailbox is not None and t.mailbox.auth_type == AuthType.PASSWORD and t.mailbox.enabled:
        parts.append("⚠ копирование по паролю сотрудника после этого может не пройти")
    return ", ".join(parts)


def _final_copy(ctx, t, emit) -> Tuple[bool, str, str]:
    """Последняя копия ящика (если есть и включён). → (удалось, почему нет, заметка)."""
    from .imap_ops import _Hold, fresh_copy
    acc = t.mailbox
    if acc is None or not acc.enabled:
        return True, "", ""
    if bound_problem(t, acc):
        return True, "", f"последняя копия не делалась: {bound_problem(t, acc)}"
    with _Hold(ctx, acc):
        ok, why = fresh_copy(ctx, acc, emit)
    return ok, why, "последняя копия сделана" if ok else ""


def _save_mailbox_password(ctx, t, password: str) -> str:
    acc = t.mailbox
    if acc is None or acc.auth_type != AuthType.PASSWORD:
        return ""
    if bound_problem(t, acc):
        return f"в карточку ящика «{acc.name}» пароль НЕ сохранён: {bound_problem(t, acc)}"
    fresh = ctx.db.get_account(acc.id) or acc
    fresh.password = password
    ctx.db.update_account(fresh, update_password=True, update_oauth_secret=False, update_oauth_token=False)
    ctx.db.set_login_status(acc.id, "", "")
    return "новый пароль сохранён в карточке ящика"


def _block_exec(action, ctx, t):
    notes: List[str] = []
    if ctx.params.get("final_copy", True):
        ok, why, note = _final_copy(ctx, t, _emit(ctx, t))
        if not ok:
            return "skip", f"последняя копия перед блокировкой не удалась — вход НЕ закрыт: {why}"
        if note:
            notes.append(note)
    with _session(ctx, t, f"Закрыть вход {t.email}") as drv:
        text = drv.set_login(t.email, False, keep_receiving=bool(ctx.params.get("keep_receiving", True)))
        notes.insert(0, text or "вход закрыт")
        msstore.update_fields(ctx.db, t.conn.id, t.email, login_blocked=True, blocked_at=utcnow_iso(),
                              blocked_by=ctx.user)
        if ctx.params.get("random_password"):
            if drv.can(Cap.PASSWORD):
                password = drv.random_password()
                try:
                    drv.set_password(t.email, password, must_change=False)
                except MailArchiverError as exc:
                    _reread(ctx, t, drv)
                    return _partial(ctx, action, t, notes, exc)
                saved = _save_mailbox_password(ctx, t, password)
                notes.append("пароль сменён на случайный" + (f"; {saved}" if saved else " (нигде не сохранён)"))
            else:
                notes.append(f"пароль не сменён: {label(t)} пароли через API не меняет")
        row = _reread(ctx, t, drv)
    if row is not None and row.get("login_blocked") is False:
        return "fail", "; ".join(notes) + "; но сервер показывает, что вход открыт — проверьте в консоли сервера"
    if row is not None:
        msstore.update_fields(ctx.db, t.conn.id, t.email, login_blocked=True)
    return "ok", "; ".join(notes)


def _unblock_check(action, ctx, t):
    return "вход не закрыт" if t.server.get("login_blocked") is False else ""


def _unblock_exec(action, ctx, t):
    with _session(ctx, t, f"Открыть вход {t.email}") as drv:
        text = drv.set_login(t.email, True)
        msstore.update_fields(ctx.db, t.conn.id, t.email, login_blocked=False, blocked_at="", blocked_by="")
        row = _reread(ctx, t, drv)
    if row is not None and row.get("login_blocked") is True:
        return "fail", "сервер показывает, что вход по-прежнему закрыт — проверьте в консоли сервера"
    return "ok", text or "вход открыт"


def _password_check(action, ctx, t):
    if ctx.params.get("mode") == "same":
        password = ctx.params.get("password") if ctx.job is None else \
            ctx.db.secret.decrypt(ctx.params.get("password_enc") or "")
        try:
            mss.driver(ctx.svc, t.conn).check_password(password or "")
        except ValidationError as exc:
            return exc.message
    return ""


def _password_plan(action, ctx, t):
    mode = ctx.params.get("mode")
    text = {"random_file": "случайный пароль — в файл для раздачи", "random_hidden": "случайный пароль",
            "same": "общий пароль"}.get(mode, "новый пароль")
    if ctx.params.get("must_change"):
        text += "; смена пароля при следующем входе" if has(t, Cap.MUST_CHANGE) else \
            f"; смену пароля при входе {label(t)} не требует"
    if t.mailbox is not None and t.mailbox.auth_type == AuthType.PASSWORD and not bound_problem(t, t.mailbox):
        text += "; сохранится в карточке ящика"
    return text


def _password_exec(action, ctx, t):
    from . import secrets
    mode = ctx.params.get("mode")
    op_id = ctx.data.get("op_id")
    to_file = mode == "random_file" and bool(op_id)
    notes: List[str] = []
    with _session(ctx, t, f"Смена пароля {t.email}") as drv:
        if mode == "same":
            password = ctx.db.secret.decrypt(ctx.params.get("password_enc") or "")
        else:
            password = drv.random_password()
        drv.check_password(password)
        if to_file:
            secrets.save(ctx.db, op_id, t.email, password, name=t.name)
        must = bool(ctx.params.get("must_change")) and drv.can(Cap.MUST_CHANGE)
        try:
            text = drv.set_password(t.email, password, must_change=must)
        except Exception:
            if to_file:
                secrets.set_note(ctx.db, op_id, t.email, "смена не подтверждена сервером — проверьте вход")
            raise
        notes.append(text or "пароль сменён")
        if to_file:
            notes.append("пароль — в файле для раздачи (История → операция)")
        if ctx.params.get("must_change") and not must:
            notes.append(f"смену пароля при входе {label(t)} не требует")
        saved = _save_mailbox_password(ctx, t, password)
        if saved:
            notes.append(saved)
        if must:
            msstore.update_fields(ctx.db, t.conn.id, t.email, must_change=True)
    return "ok", "; ".join(notes)


def _must_change_plan(action, ctx, t):
    return "при следующем входе — смена пароля" if ctx.params.get("value") == "yes" else "требование снимется"


def _must_change_exec(action, ctx, t):
    value = ctx.params.get("value") == "yes"
    with _session(ctx, t, f"Смена пароля при входе {t.email}") as drv:
        text = drv.set_must_change(t.email, value)
        msstore.update_fields(ctx.db, t.conn.id, t.email, must_change=value)
        _reread(ctx, t, drv)
    return "ok", text or ("при следующем входе потребуется сменить пароль" if value else "требование снято")


# ---------------------------------------------------------------------------
#  Квота, псевдонимы, ФИО
# ---------------------------------------------------------------------------
def _quota_check(action, ctx, t):
    new = int(ctx.params["quota_mb"]) * 1024 * 1024
    quota = (t.server.get("quota_kb") or 0) * 1024
    used = t.server.get("used_bytes")
    if ctx.params.get("only_raise") and quota and quota >= new:
        return f"квота и так {human_size(quota)} — не меньше новой"
    if used is not None and used > new:
        return f"занято {human_size(used)} — больше новой квоты: учётная запись перестала бы принимать письма"
    return ""


def _quota_plan(action, ctx, t):
    quota = t.server.get("quota_kb")
    return f"квота: {human_size(quota * 1024) if quota else 'не задана'} → " \
           f"{human_size(int(ctx.params['quota_mb']) * 1024 * 1024)}"


def _quota_exec(action, ctx, t):
    new = int(ctx.params["quota_mb"]) * 1024 * 1024
    before = t.server.get("quota_kb")
    with _session(ctx, t, f"Квота {t.email}: {new} байт") as drv:
        row = _reread(ctx, t, drv)
        if row is not None:
            if row.get("used_bytes") is not None and row["used_bytes"] > new:
                return "skip", f"занято {human_size(row['used_bytes'])} — больше новой квоты"
            if ctx.params.get("only_raise") and row.get("quota_kb") and row["quota_kb"] * 1024 >= new:
                return "skip", f"квота и так {human_size(row['quota_kb'] * 1024)}"
            before = row.get("quota_kb")
        text = drv.set_quota(t.email, new)
        msstore.update_fields(ctx.db, t.conn.id, t.email, quota_kb=new // 1024)
        after = _reread(ctx, t, drv)
    if after is not None and after.get("quota_kb") and after["quota_kb"] != new // 1024:
        return "fail", f"сервер показывает квоту {human_size(after['quota_kb'] * 1024)} вместо {human_size(new)}"
    out = text or f"квота: {human_size(before * 1024) if before else 'не задана'} → {human_size(new)}"
    if int(ctx.params.get("threshold") or 0):
        out += f"; предупреждение о заполнении {label(t)} так не настраивает"
    return "ok", out


def _taken(ctx, t) -> set:
    cache = ctx.data.setdefault("srv_taken", {})
    if t.conn.id not in cache:
        taken = set()
        for row in msstore.list_accounts(ctx.db, t.conn.id, include_deleted=False):
            taken.add(row["email"])
            taken.update(row.get("aliases") or [])
        cache[t.conn.id] = taken
    return cache[t.conn.id]


def _alias_name(ctx, t) -> str:
    from .axigen_ops import render_alias
    employee = ctx.db.get_employee_by_email(t.email)
    row = dict(t.server, name=t.email.split("@", 1)[0])
    return render_alias(ctx.params.get("template") or "", row, employee)


def _alias_add_check(action, ctx, t):
    from ..axigen.directory import valid_account_name
    name = _alias_name(ctx, t)
    if not name:
        return "не заполнены имя и фамилия (ни у учётной записи, ни в «Сотрудниках»)"
    if not valid_account_name(name):
        return f"«{name}» — недопустимое имя псевдонима"
    address = f"{name}@{t.email.split('@', 1)[1]}"
    if address in (t.server.get("aliases") or []) or address == t.email:
        return f"псевдоним {address} уже есть"
    if address in _taken(ctx, t):
        return f"адрес {address} уже занят другой учётной записью или псевдонимом"
    claimed = ctx.data.setdefault("claimed", {})
    other = claimed.get(address)
    if other and other != t.key:
        return f"такой же псевдоним {address} получается у другой выбранной учётной записи"
    claimed[address] = t.key
    return ""


def _alias_add_plan(action, ctx, t):
    return f"псевдоним {_alias_name(ctx, t)}@{t.email.split('@', 1)[1]}"


def _alias_add_exec(action, ctx, t):
    address = f"{_alias_name(ctx, t)}@{t.email.split('@', 1)[1]}"
    with _session(ctx, t, f"Псевдоним {address} для {t.email}") as drv:
        after = drv.add_alias(t.email, address)
        if after is not None:
            msstore.update_fields(ctx.db, t.conn.id, t.email, aliases=[a for a in after if a != t.email])
        row = _reread(ctx, t, drv)
    if row is not None and address not in (row.get("aliases") or []):
        return "fail", f"сервер не показал новый псевдоним {address} — проверьте в консоли сервера"
    return "ok", f"добавлен псевдоним {address}"


def _alias_remove_list(ctx, t) -> List[str]:
    have = list(t.server.get("aliases") or [])
    if ctx.params.get("which") == "all":
        return have
    domain = t.email.split("@", 1)[1]
    wanted = set()
    for raw in re.split(r"[\s,;]+", ctx.params.get("aliases") or ""):
        raw = raw.strip().lower()
        if raw:
            wanted.add(raw if "@" in raw else f"{raw}@{domain}")
    return [a for a in have if a in wanted]


def _alias_remove_check(action, ctx, t):
    return "" if _alias_remove_list(ctx, t) else "таких псевдонимов у учётной записи нет"


def _alias_remove_plan(action, ctx, t):
    return "удалятся: " + ", ".join(_alias_remove_list(ctx, t))


def _alias_remove_exec(action, ctx, t):
    remove = _alias_remove_list(ctx, t)
    done: List[str] = []
    with _session(ctx, t, f"Удаление псевдонимов {t.email}") as drv:
        row = _reread(ctx, t, drv)
        if row is not None:
            remove = [a for a in remove if a in (row.get("aliases") or [])]
            if not remove:
                return "skip", "этих псевдонимов на сервере уже нет"
        try:
            for alias in remove:
                after = drv.remove_alias(t.email, alias)
                done.append(alias)
                if after is not None:
                    msstore.update_fields(ctx.db, t.conn.id, t.email, aliases=[a for a in after if a != t.email])
        except MailArchiverError as exc:
            _reread(ctx, t, drv)
            if done:
                return _partial(ctx, action, t, ["удалены: " + ", ".join(done)], exc)
            raise
        _reread(ctx, t, drv)
    return "ok", "удалены: " + ", ".join(done)


#: Поля «ФИО, отдел, должность» действия Axigen → общие поля драйвера.
_CONTACT_MAP = {"firstName": "first_name", "lastName": "last_name", "department": "department",
                "position": "title", "businessPhone": "phone", "company": "company", "office": "office"}
_CONTACT_TITLES = {"first_name": "имя", "last_name": "фамилия", "department": "отдел", "title": "должность",
                   "phone": "телефон", "company": "организация", "office": "офис", "display_name": "имя для показа",
                   "mobile": "мобильный"}


def _contact_wanted(action, ctx, t) -> Dict[str, str]:
    raw = action._wanted(ctx, t)
    fields = set(mss.driver_class(t.conn.type).contact_fields)
    out = {}
    for key, value in raw.items():
        mapped = _CONTACT_MAP.get(key)
        if mapped and mapped in fields:
            out[mapped] = value
    return out


def _contact_change(action, ctx, t) -> Dict[str, str]:
    wanted = _contact_wanted(action, ctx, t)
    overwrite = bool(ctx.params.get("overwrite"))
    return {k: v for k, v in wanted.items()
            if (t.server.get(k) or "") != v and (overwrite or not (t.server.get(k) or ""))}


def _contact_check(action, ctx, t):
    if ctx.params.get("source") == "employees" and ctx.db.get_employee_by_email(t.email) is None:
        return "сотрудника с этим адресом нет в разделе «Сотрудники»"
    if not _contact_wanted(action, ctx, t):
        return "нечего записывать (у сотрудника не заполнены нужные поля, или сервер их не принимает)"
    if not _contact_change(action, ctx, t):
        return "поля уже заполнены" if not ctx.params.get("overwrite") else "всё уже так"
    return ""


def _contact_plan(action, ctx, t):
    change = _contact_change(action, ctx, t)
    return "; ".join(f"{_CONTACT_TITLES.get(k, k)}: {v}" for k, v in change.items())


def _contact_exec(action, ctx, t):
    change = _contact_change(action, ctx, t)
    if not change:
        return "skip", "всё уже так"
    with _session(ctx, t, f"Контактные данные {t.email}") as drv:
        text = drv.set_contact(t.email, change)
        msstore.update_fields(ctx.db, t.conn.id, t.email, **change)
        row = _reread(ctx, t, drv)
    out = text or ("записано: " + "; ".join(f"{_CONTACT_TITLES.get(k, k)}: {v}" for k, v in change.items()))
    if row is not None:
        unshown = [_CONTACT_TITLES.get(k, k) for k, v in change.items() if (row.get(k) or "") != v]
        if unshown:
            out += f"; после записи сервер показал другое: {', '.join(unshown)} — проверьте в консоли сервера"
    return "ok", out


# ---------------------------------------------------------------------------
#  Архив, перечитать
# ---------------------------------------------------------------------------
def _add_mailboxes_check(action, ctx, t):
    if t.mailbox is not None:
        return f"уже копируется: ящик «{t.mailbox.name}»"
    if not has(t, Cap.IMAP_ADMIN):
        return (f"{label(t)}: копирование без паролей сотрудников не поддерживается — заведите ящик с паролем "
                f"вручную («Почтовые ящики»)")
    from ..mailservers.mailboxes import NO_COPY_KINDS, mailbox_problem
    if t.server.get("kind") in NO_COPY_KINDS:
        return NO_COPY_KINDS[t.server["kind"]]
    problem = mailbox_problem(t.conn)
    if problem:
        return f"{label(t)}: вход в ящики без паролей не настроен — {problem}"
    return ""


def apply_add_mailboxes(action, ctx, t) -> str:
    from ..mailservers.mailboxes import add_mailboxes
    items = add_mailboxes(ctx.svc, t.conn, [t.email], user=ctx.user, enable=bool(ctx.params.get("enable", True)))
    item = items[0] if items else {"status": "skip", "text": "не заведён"}
    if item["status"] != "ok":
        raise ValidationError(item["text"])
    return item["text"]


def _refresh_exec(action, ctx, t):
    row = mss.refresh_account(ctx.svc, t.conn, t.email, user=ctx.user)
    if row is None:
        return "ok", "учётной записи на сервере больше нет — помечена удалённой (архив сохранён)"
    parts = []
    if row.get("used_bytes") is not None:
        parts.append(f"занято {human_size(row['used_bytes'])}")
    if row.get("quota_kb"):
        parts.append(f"квота {human_size(row['quota_kb'] * 1024)}")
    parts.append("вход закрыт" if row.get("login_blocked") else
                 ("вход открыт" if row.get("login_blocked") is False else "вход: неизвестно"))
    if row.get("autoreply"):
        parts.append("автоответ включён")
    if row.get("forward_to"):
        parts.append("пересылка: " + ", ".join(row["forward_to"]))
    return "ok", "перечитана: " + ", ".join(parts)


# ---------------------------------------------------------------------------
#  Удаление (только при полном архиве) и передача адреса коллеге
# ---------------------------------------------------------------------------
def colleague(ctx, t, address: str) -> Optional[Dict]:
    """Учётная запись коллеги в том же подключении (по адресу или псевдониму), не удалённая."""
    address = (address or "").strip().lower()
    for row in msstore.list_accounts(ctx.db, t.conn.id, include_deleted=False):
        if row["email"] == address or address in (row.get("aliases") or []):
            return row
    return None


def transfer_addresses(t, colleague_email: str, with_aliases: bool, aliases: Optional[List[str]] = None) -> List[str]:
    domain = colleague_email.split("@", 1)[1]
    have = list(aliases if aliases is not None else (t.server.get("aliases") or []))
    out = [t.email] + (have if with_aliases else [])
    return [a for a in out if a.split("@", 1)[-1] == domain]


def transfer_problem(ctx, t, address: str, with_aliases: bool) -> str:
    if not has(t, Cap.ALIASES):
        return f"{label(t)} псевдонимы через API не меняет — передать адрес нельзя"
    row = colleague(ctx, t, address)
    if row is None:
        return f"учётной записи {address} нет на сервере «{t.conn.name}» (по последней сверке)"
    target = row["email"]
    if target == t.email:
        return "коллега — та же учётная запись"
    if t.email.split("@", 1)[1] != target.split("@", 1)[1]:
        return f"коллега {target} в другом домене — псевдоним передаётся только в своём домене"
    busy = []
    for other in msstore.list_accounts(ctx.db, t.conn.id, include_deleted=False):
        if other["email"] in (t.email, target):
            continue
        for a in transfer_addresses(t, target, with_aliases):
            if a == other["email"] or a in (other.get("aliases") or []):
                busy.append(f"{a} занят учётной записью {other['email']}")
    if busy:
        return "; ".join(busy[:3])
    if all(a in (row.get("aliases") or []) for a in transfer_addresses(t, target, with_aliases)):
        return "адреса уже переданы этому коллеге"
    return ""


def transfer(ctx, t, address: str, with_aliases: bool, drv) -> List[str]:
    """Адреса учётной записи (удалённой) → псевдонимы коллеги. Каждый — сразу в аудит и в снимок."""
    row = colleague(ctx, t, address)
    if row is None:
        raise ValidationError(f"учётной записи коллеги {address} нет")
    target = row["email"]
    added: List[str] = []
    have = list(row.get("aliases") or [])
    live = (ctx.data.get("live_aliases") or {}).get(t.key)
    for a in transfer_addresses(t, target, with_aliases, live):
        if a in have:
            continue
        after = drv.add_alias(target, a)
        added.append(a)
        have = [x for x in (after if after is not None else have + [a]) if x != target]
        msstore.update_fields(ctx.db, t.conn.id, target, aliases=have)
        ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + "ax_forward", f"{a} → {target}"[:300])
        ctx.data.setdefault("audited", set()).add(t.key)
    if added:
        msstore.merge_info(ctx.db, t.conn.id, t.email, {"forwarded_to": target})
    return added


def _imap_login_after_block(svc, acc) -> str:
    """Можно ли войти в ящик, когда вход сотрудника закрыт (пусто — можно)."""
    if acc.auth_type in (AuthType.SERVER, AuthType.MASTER):
        return tg.imap_problem(svc, acc)
    return ("ящик копируется по паролю сотрудника — после закрытия входа сверить его с архивом нечем; переведите "
            "ящик на копирование через подключение (без пароля)")


def _delete_check(action, ctx, t):
    acc = t.mailbox
    if acc is None:
        return "ящика нет в архиве — удалять учётную запись нельзя (письма пропали бы)"
    problem = _imap_login_after_block(ctx.svc, acc)
    if problem:
        return problem
    problem = bound_problem(t, acc)
    if problem:
        return f"полноту архива проверить нечем: {problem}"
    if t.server.get("login_blocked") is not True:
        return "вход на сервере не закрыт — сначала «Заблокировать вход»"
    to = (ctx.params.get("forward_to") or "").strip().lower()
    if to:
        row = colleague(ctx, t, to)
        if row is not None and row["email"] == t.email:
            return "это коллега, которому передаются адреса, — её не удаляем"
        problem = transfer_problem(ctx, t, to, bool(ctx.params.get("forward_aliases", True)))
        if problem:
            return f"адрес коллеге передать нельзя: {problem}"
    return ""


def _delete_plan(action, ctx, t):
    stats = ctx.totals.get(t.mailbox.id) or {}
    text = (f"удержание архива, последняя копия, сверка всех папок по IMAP, удаление; в архиве писем "
            f"{int(stats.get('messages', 0))}")
    to = (ctx.params.get("forward_to") or "").strip().lower()
    if to:
        row = colleague(ctx, t, to)
        target = row["email"] if row else to
        text += f"; затем {', '.join(transfer_addresses(t, target, bool(ctx.params.get('forward_aliases', True))))} → " \
                f"{target}"
    return text


def imap_coverage(ctx, acc, emit, state: Optional[Dict[str, Tuple[int, int]]] = None) -> Tuple[bool, str]:
    """Каждое письмо каждой папки ящика на сервере — в архиве? → (да, что не так).

    ``state`` — сюда записывается (UIDVALIDITY, UIDNEXT) каждой папки на момент сверки: перед удалением
    :func:`imap_changed` проверит, что с тех пор писем не прибавилось."""
    from .imap_ops import _cancelled, connect, copied_to_archive, missing_in_archive
    gaps: List[str] = []
    conn = connect(ctx, acc)
    try:
        for f in conn.list_folders():
            _cancelled(ctx)
            if not f.selectable:
                continue
            info = conn.select(f.name, readonly=True)
            if state is not None:
                state[f.name] = (int(info.get("uidvalidity") or 0), int(info.get("uidnext") or 0))
            if not int(info.get("exists") or 0):
                continue
            uids = conn.search_uids(info)
            if not uids:
                continue
            if not copied_to_archive(ctx.svc, acc, f):
                gaps.append(f"«{f.name}»: папка не копируется в архив (исключена в настройках), писем {len(uids)}")
                continue
            missing = missing_in_archive(ctx, acc, f.name, info.get("uidvalidity"), uids)
            if missing:
                gaps.append(f"«{f.name}»: нет в архиве {len(missing)} из {len(uids)}")
        if conn.bad_folder_names:
            gaps.append("папки с неверной кодировкой имени не прочитаны: " + ", ".join(conn.bad_folder_names[:5]))
    finally:
        conn.close()
    return not gaps, "; ".join(gaps[:10])


def imap_changed(ctx, acc, state: Dict[str, Tuple[int, int]]) -> str:
    """Прибавились ли письма (или папки с письмами) с момента сверки (пусто — нет)."""
    from .imap_ops import connect
    changed: List[str] = []
    conn = connect(ctx, acc)
    try:
        for f in conn.list_folders():
            if not f.selectable:
                continue
            st = conn.folder_status(f.name)
            if st is None:
                info = conn.select(f.name, readonly=True)
                st = {"uidvalidity": info.get("uidvalidity"), "uidnext": info.get("uidnext"),
                      "messages": info.get("exists")}
            now = (int(st.get("uidvalidity") or 0), int(st.get("uidnext") or 0))
            was = state.get(f.name)
            if was is None:
                if int(st.get("messages") or 0):
                    changed.append(f"новая папка «{f.name}»")
            elif now != was:
                changed.append(f"«{f.name}»")
    finally:
        conn.close()
    return ", ".join(changed[:5])


def _delete_exec(action, ctx, t):
    from ..accountops import after_hold_set
    from ..employees import HOLD_FOREVER
    from .imap_ops import _Hold, fresh_copy
    acc = t.mailbox
    emit = _emit(ctx, t)
    to = (ctx.params.get("forward_to") or "").strip().lower()
    with_aliases = bool(ctx.params.get("forward_aliases", True))
    previous_hold = (acc.hold_until, acc.hold_reason)
    hold_set = False
    deleted = False
    keep_hold = False
    notes: List[str] = []
    try:
        if acc.hold_until != HOLD_FOREVER:
            # удержание — ДО последней копии: со сроком хранения копия не взяла бы старые письма
            ctx.db.set_account_hold(acc.id, HOLD_FOREVER, "manual")
            acc = ctx.db.get_account(acc.id) or acc
            hold_set = True
        with _Hold(ctx, acc):
            ok, why = fresh_copy(ctx, acc, emit)
            if not ok:
                return "skip", f"последняя копия не удалась — учётная запись НЕ удалена: {why}"
            acc = ctx.db.get_account(acc.id) or acc
            problem = bound_problem(t, acc) or _imap_login_after_block(ctx.svc, acc)
            if problem:
                return "skip", f"полноту архива проверить нечем — учётная запись НЕ удалена: {problem}"
            state: Dict[str, Tuple[int, int]] = {}
            covered, gaps = imap_coverage(ctx, acc, emit, state)
            if not covered:
                return "skip", f"не все письма ящика есть в архиве — учётная запись НЕ удалена: {gaps}"
            with _session(ctx, t, f"Удаление учётной записи {t.email}") as drv:
                row = _reread(ctx, t, drv)
                if row is None:
                    return "skip", "учётной записи на сервере уже нет"
                if row.get("login_blocked") is not True:
                    return "skip", "пока шла последняя копия, вход на сервере открыли — учётная запись не удалена"
                if to:
                    ctx.data.setdefault("live_aliases", {})[t.key] = list(row.get("aliases") or [])
                    if colleague(ctx, t, to) is None:
                        return "skip", f"адрес передать некому: учётной записи коллеги {to} нет — учётная запись " \
                                       f"не удалена"
                    if drv.get_account(colleague(ctx, t, to)["email"]) is None:
                        return "skip", f"учётной записи коллеги {to} на сервере нет — адрес передать некому, " \
                                       f"учётная запись не удалена"
                # сверка шла минуты, а почта в ящик приходит и при закрытом входе: пришло новое — не удаляем
                changed = imap_changed(ctx, acc, state)
                if changed:
                    return "skip", (f"пока шла сверка, в ящик пришли новые письма ({changed}) — учётная запись НЕ "
                                    f"удалена; повторите удаление (будет снята ещё одна копия)")
                keep_hold = True    # с этого момента результат может оказаться «удалена» — удержание не снимаем
                try:
                    text = drv.delete_account(t.email)
                except JobCancelled:
                    raise
                except MailArchiverError as exc:
                    # 502/504 или обрыв: сервер мог удалить, но не ответить — перечитать
                    try:
                        live = drv.get_account(t.email)
                    except MailArchiverError:
                        return "fail", (f"удаление: результат неизвестен ({exc.message}) — архив оставлен на "
                                        f"бессрочном удержании; проверьте учётную запись на сервере "
                                        f"(«Перечитать») и повторите удаление, если она есть")
                    if live is not None and normalize_email(live.get("email")) == t.email:
                        keep_hold = False
                        raise
                    text = f"сервер ответил ошибкой ({exc.message}), но учётной записи больше нет — удалена"
                deleted = True
            notes.append(text or "учётная запись удалена на сервере")
    except (JobCancelled, MailArchiverError):
        raise
    finally:
        if not deleted and hold_set and not keep_hold:
            ctx.db.set_account_hold(acc.id, previous_hold[0], previous_hold[1] or "manual")
    msstore.note_deleted(ctx.db, t.conn.id, t.email)
    if acc.enabled:
        ctx.db.set_account_enabled(acc.id, False)
        notes.append("копирование ящика выключено")
    if hold_set:
        after_hold_set(ctx.svc, acc.id)
        notes.append("архив поставлен на бессрочное удержание")
    stats = ctx.db.message_totals_by_account().get(acc.id) or {}
    notes.append(f"в архиве писем: {int(stats.get('messages', 0))} ({human_size(int(stats.get('bytes', 0)))})")
    text = "; ".join(notes)
    # необратимо — в аудит сразу, что бы ни случилось с передачей адреса
    ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + action.key, f"{t.email}: {text}"[:300])
    ctx.data.setdefault("audited", set()).add(t.key)
    if not to:
        return "ok", text
    try:
        with _session(ctx, t, f"Передача адреса {t.email} → {to}") as drv:
            added = transfer(ctx, t, to, with_aliases, drv)
    except Exception as exc:  # noqa: BLE001
        ctx.data.setdefault("item_extra", {})[t.key] = {"next_action": "ax_forward"}
        return "fail", (f"{text}; адрес коллеге НЕ передан: {getattr(exc, 'message', exc)}. Учётная запись уже "
                        f"удалена — передайте адрес действием «Передать адрес коллеге» («Повторить для неудачных» "
                        f"подставит его сам)")
    target = (colleague(ctx, t, to) or {}).get("email", to)
    return "ok", f"{text}; письма на {', '.join(added) or t.email} теперь приходят {target}"


def _forward_check(action, ctx, t):
    if not t.deleted:
        return "учётная запись ещё есть на сервере — сначала удалите её (там же можно сразу передать адрес)"
    return transfer_problem(ctx, t, ctx.params.get("to") or "", bool(ctx.params.get("with_aliases", True)))


def _forward_plan(action, ctx, t):
    row = colleague(ctx, t, ctx.params.get("to") or "")
    target = row["email"] if row else ctx.params.get("to")
    return f"{', '.join(transfer_addresses(t, target, bool(ctx.params.get('with_aliases', True))))} → {target}"


def _forward_exec(action, ctx, t):
    with _session(ctx, t, f"Передача адреса {t.email}") as drv:
        # снимок может отставать (или сверка ошиблась): адрес живой учётной записи коллеге не отдаём
        live = drv.get_account(t.email)
        if live is not None and normalize_email(live.get("email")) == t.email:
            mss.refresh_account(ctx.svc, t.conn, t.email, user=ctx.user)
            return "skip", "учётная запись на сервере есть — адрес передаётся только после её удаления"
        added = transfer(ctx, t, ctx.params.get("to") or "", bool(ctx.params.get("with_aliases", True)), drv)
    if not added:
        return "skip", "адреса уже переданы"
    target = (colleague(ctx, t, ctx.params.get("to") or "") or {}).get("email", ctx.params.get("to"))
    return "ok", f"письма на {', '.join(added)} теперь приходят {target}"


# ---------------------------------------------------------------------------
#  Автоответ и пересылка (есть у подключённых серверов, у Axigen — нет)
# ---------------------------------------------------------------------------
def _autoreply_check(action, ctx, t):
    return ""


def _autoreply_plan(action, ctx, t):
    if ctx.params.get("mode") == "off":
        return "автоответ выключится" if t.server.get("autoreply") is not False else "автоответ и так выключен"
    period = ""
    if ctx.params.get("start") or ctx.params.get("end"):
        period = f" ({ctx.params.get('start') or '…'} — {ctx.params.get('end') or '…'})"
    return f"автоответ «{(ctx.params.get('subject') or '').strip() or 'без темы'}»{period}"


def _autoreply_exec(action, ctx, t):
    on = ctx.params.get("mode") != "off"
    with _session(ctx, t, f"Автоответ {t.email}: {'вкл' if on else 'выкл'}") as drv:
        text = drv.set_autoreply(t.email, enabled=on, subject=ctx.params.get("subject") or "",
                                 text=ctx.params.get("text") or "", start=ctx.params.get("start") or "",
                                 end=ctx.params.get("end") or "")
        msstore.update_fields(ctx.db, t.conn.id, t.email, autoreply=on)
        row = _reread(ctx, t, drv)
    if row is not None and row.get("autoreply") is not None and bool(row["autoreply"]) != on:
        return "fail", "сервер показывает автоответ " + ("выключенным" if on else "включённым")
    return "ok", text or ("автоответ включён" if on else "автоответ выключен")


def forward_targets(ctx) -> List[str]:
    out = []
    for raw in re.split(r"[\s,;]+", ctx.params.get("addresses") or ""):
        if raw.strip():
            out.append(check_email(raw, "Адрес пересылки"))
    return out


def _forwarding_check(action, ctx, t):
    if ctx.params.get("mode") == "off":
        return ""
    if t.email in forward_targets(ctx):
        return "пересылать письма самому себе нельзя"
    return ""


def _forwarding_plan(action, ctx, t):
    if ctx.params.get("mode") == "off":
        return "пересылка выключится"
    keep = "копия остаётся в ящике" if ctx.params.get("keep_copy", True) else "в ящике писем не останется"
    return f"пересылка на {', '.join(forward_targets(ctx))}; {keep}"


def _forwarding_exec(action, ctx, t):
    addresses = [] if ctx.params.get("mode") == "off" else forward_targets(ctx)
    with _session(ctx, t, f"Пересылка {t.email}") as drv:
        text = drv.set_forwarding(t.email, addresses=addresses, keep_copy=bool(ctx.params.get("keep_copy", True)))
        msstore.update_fields(ctx.db, t.conn.id, t.email, forward_to=addresses)
        _reread(ctx, t, drv)
    return "ok", text or (f"пересылка на {', '.join(addresses)}" if addresses else "пересылка выключена")


CHECKS: Dict[str, Callable] = {
    "ax_block": _block_check, "ax_unblock": _unblock_check, "ax_password": _password_check,
    "ax_quota": _quota_check, "ax_alias_add": _alias_add_check, "ax_alias_remove": _alias_remove_check,
    "ax_contact": _contact_check, "ax_add_mailboxes": _add_mailboxes_check, "ax_delete": _delete_check,
    "ax_forward": _forward_check, "srv_autoreply": _autoreply_check, "srv_forwarding": _forwarding_check,
}
PLANS: Dict[str, Callable] = {
    "ax_block": _block_plan, "ax_unblock": lambda a, c, t: "вход откроется", "ax_password": _password_plan,
    "ax_must_change": _must_change_plan, "ax_quota": _quota_plan, "ax_alias_add": _alias_add_plan,
    "ax_alias_remove": _alias_remove_plan, "ax_contact": _contact_plan,
    "ax_add_mailboxes": lambda a, c, t: "будет заведён ящик (копирование без пароля сотрудника)",
    "ax_refresh": lambda a, c, t: "будет перечитана", "ax_delete": _delete_plan, "ax_forward": _forward_plan,
    "srv_autoreply": _autoreply_plan, "srv_forwarding": _forwarding_plan,
}
EXECS: Dict[str, Callable] = {
    "ax_block": _block_exec, "ax_unblock": _unblock_exec, "ax_password": _password_exec,
    "ax_must_change": _must_change_exec, "ax_quota": _quota_exec, "ax_alias_add": _alias_add_exec,
    "ax_alias_remove": _alias_remove_exec, "ax_contact": _contact_exec, "ax_refresh": _refresh_exec,
    "ax_delete": _delete_exec, "ax_forward": _forward_exec, "srv_autoreply": _autoreply_exec,
    "srv_forwarding": _forwarding_exec,
}
