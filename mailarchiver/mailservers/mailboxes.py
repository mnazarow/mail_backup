"""Ящики архива для учётных записей подключения («Завести ящики в архиве», сверка с «заводить новые»)."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from ..models import Account, AuthType
from . import store
from .base import Cap
from .service import driver_class


#: Учётные записи, для которых ящик в архиве не заводится (вид — как сообщил драйвер).
NO_COPY_KINDS = {
    "system": "служебная учётная запись сервера (спам, карантин, адресная книга) — ящик не заводится",
    "no_mailbox": "у учётной записи нет почтового ящика",
    "unlicensed": "у учётной записи нет лицензии с почтой — почтового ящика нет",
    "archived": "учётная запись архивирована — войти в её ящик нельзя",
}


def existing_links(svc, conn: store.Connection) -> Dict[str, Account]:
    """Адрес учётной записи подключения → уже связанный с ней ящик архива."""
    from ..serverops import targets as tg
    out: Dict[str, Account] = {}
    for t in tg.load(svc):
        if t.server is not None and t.server_id == conn.id and t.mailbox is not None:
            out[t.email] = t.mailbox
    return out


def mailbox_problem(conn: store.Connection) -> str:
    """Почему для учётных записей подключения нельзя завести ящики «без паролей» (пусто — можно)."""
    from .service import imap_target_problem
    cls = driver_class(conn.type)
    if not cls.can(Cap.IMAP_ADMIN):
        return ""
    drv = cls(conn, None)
    host, _port, security = drv.imap_server()
    return drv.imap_admin_problem() or imap_target_problem(drv, host, security)


def new_mailbox(svc, conn: store.Connection, row: Dict, *, template: Optional[Dict] = None) -> Optional[Account]:
    """Ящик архива для учётной записи: копирование через подключение (без пароля сотрудника)."""
    cls = driver_class(conn.type)
    if not cls.can(Cap.IMAP_ADMIN):
        return None
    if mailbox_problem(conn):
        return None
    drv = cls(conn, None)
    host, port, security = drv.imap_server()
    template = template or {}
    full = (row.get("display_name") or " ".join(x for x in (row.get("first_name") or "", row.get("last_name") or "")
                                                 if x)).strip()
    acc = Account(name=(full or row["email"])[:200], host=host, port=int(port or 993), username=row["email"],
                  password="", auth_type=AuthType.SERVER, security=security or "ssl", enabled=True,
                  folder_include=list(template.get("folder_include") or []),
                  folder_exclude=list(template.get("folder_exclude") or []),
                  notes=f"Заведён по учётной записи сервера «{conn.name}» ({cls.label}).",
                  retention_days=int(template.get("retention_days", -1)))
    acc.server_id = conn.id
    return acc


def add_mailboxes(svc, conn: store.Connection, emails: Sequence[str], *, user: str,
                  event: Optional[Callable[[str, str], None]] = None, enable: bool = True,
                  preview: bool = False) -> List[Dict]:
    from ..axigen.jobs import mailbox_template
    cls = driver_class(conn.type)
    rows = {r["email"]: r for r in store.list_accounts(svc.db, conn.id)}
    linked = existing_links(svc, conn)
    template = mailbox_template(svc)
    out: List[Dict] = []
    for email in emails:
        email = (email or "").strip().lower()
        row = rows.get(email)
        if row is None:
            out.append({"email": email, "status": "skip", "text": "нет в последней сверке"})
            continue
        if row.get("deleted_at"):
            out.append({"email": email, "status": "skip", "text": "удалена на сервере"})
            continue
        if row.get("kind") in NO_COPY_KINDS:
            # служебные (спам, карантин), без почтового ящика — копировать нечего или незачем
            out.append({"email": email, "status": "skip", "text": NO_COPY_KINDS[row["kind"]]})
            continue
        if email in linked:
            out.append({"email": email, "status": "skip", "text": f"уже копируется: ящик «{linked[email].name}»"})
            continue
        acc = new_mailbox(svc, conn, row, template=template)
        if acc is None:
            problem = mailbox_problem(conn)
            out.append({"email": email, "status": "skip",
                        "text": (f"{cls.label}: вход в ящики без паролей не настроен — {problem}" if problem else
                                 f"{cls.label}: копирование без пароля сотрудника не поддерживается — заведите ящик "
                                 f"с паролем вручную («Почтовые ящики»)")})
            continue
        acc.enabled = enable
        if preview:
            out.append({"email": email, "status": "ok", "text": f"будет заведён ящик «{acc.name}»"})
            continue
        acc.id = svc.db.create_account(acc)
        out.append({"email": email, "status": "ok", "text": f"заведён ящик «{acc.name}»", "account_id": acc.id})
        svc.db.add_audit(user, "account_create", f"{acc.name} ({email}) — по учётной записи «{conn.name}»")
        if event:
            event("INFO", f"{email}: заведён ящик архива «{acc.name}»")
    return out
