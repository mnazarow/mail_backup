"""API почтового клиента раздела «Почта» (1.17.0): ``/api/webmail/...``.

Кто что может:

* **сотрудник** (вход паролем ящика) — свои ящики (ящики своей карточки): читать, проверять почту, писать,
  менять ящик на сервере; выключенный администратором ящик — только читать;
* **администратор** — любой ящик: читать и проверять почту; писать и менять ящик — если это разрешает
  настройка ``webmail.admin_actions`` (каждое действие — в аудит);
* **оператор** — ничего (писем он не видит).

Пока клиент выключен (``webmail.enabled``), работают только чтение списка и папок архива.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import webmail as wm
from .. import webmail_tools as wt
from ..errors import ValidationError
from ..models import AuthType
from ..util import human_size
from . import auth as auth_mod

router = APIRouter(prefix="/api/webmail")


def _svc(request: Request):
    return request.app.state.services


def _user(request: Request) -> dict:
    user = auth_mod.require_user(request)
    if user.get("role") not in ("admin", "mailbox"):
        raise HTTPException(403, "Письма доступны администратору и самому сотруднику.")
    return user


def _is_mailbox(user: dict) -> bool:
    return user.get("role") == "mailbox"


def _account(svc, user: dict, account_id: int):
    if _is_mailbox(user) and int(account_id) not in auth_mod.mailbox_ids(user):
        raise HTTPException(403, "Доступ разрешён только к своим почтовым ящикам")
    acc = svc.db.get_account(int(account_id))
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    return acc


def _client_problem(svc, user: dict, acc) -> str:
    """Почему с ящиком нельзя работать на сервере ('' — можно)."""
    if not bool(svc.rt("webmail", "enabled")):
        return "Почтовый клиент выключен («Настройки → Почтовый клиент»): «Почта» показывает только архив."
    if not acc.enabled:
        return f"Ящик «{acc.name}» выключен — письма из архива можно читать, а менять ящик на сервере нельзя."
    if acc.secret_broken and acc.auth_type in (AuthType.PASSWORD, AuthType.AXIGEN_FTP, AuthType.OAUTH2):
        return f"Пароль ящика «{acc.name}» не расшифровывается текущим ключом — введите его заново в карточке ящика."
    if acc.auth_type == AuthType.AXIGEN_FTP and not acc.password:
        return (f"Ящик «{acc.name}» копируется через FTP Backup Axigen без пароля — войти в него по IMAP нельзя. "
                f"Задайте ящику пароль, чтобы работать с ним в клиенте.")
    return ""


def _act_problem(svc, user: dict, acc) -> str:
    problem = _client_problem(svc, user, acc)
    if problem:
        return problem
    if user.get("role") == "admin" and not bool(svc.rt("webmail", "admin_actions")):
        return ("Администратору писать письма и менять ящики сотрудников запрещено настройкой «Администратор может "
                "писать и менять ящики» («Настройки → Почтовый клиент»).")
    return ""


def _require_act(svc, user: dict, acc) -> None:
    problem = _act_problem(svc, user, acc)
    if problem:
        raise HTTPException(403, problem)


def _require_online(svc, user: dict, acc) -> None:
    problem = _client_problem(svc, user, acc)
    if problem:
        raise HTTPException(403, problem)


def _owner(svc, user: dict) -> str:
    """Чьи черновики: у сотрудника — общие для всех его ящиков (вошёл паролем любого из них)."""
    if _is_mailbox(user):
        acc = svc.db.get_account(int(user["account_id"]))
        if acc is not None and acc.employee_id:
            return f"mailbox:emp:{int(acc.employee_id)}"
        return f"mailbox:acc:{int(user['account_id'])}"
    return f"{user.get('role')}:{user.get('username')}"


def _actor(user: dict) -> str:
    return str(user.get("username") or "")


# ---------------------------------------------------------------------------
#  Состояние и папки
# ---------------------------------------------------------------------------
@router.get("/status")
def webmail_status(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    online = _client_problem(svc, user, acc)
    act = _act_problem(svc, user, acc)
    send = act or (wm.send_problem(svc, acc) if not act else "")
    ident = wm.get_identity(svc, acc)
    return {
        "enabled": bool(svc.rt("webmail", "enabled")),
        "account_id": int(acc.id),
        "online": not online, "online_problem": online,
        "can_act": not act, "act_problem": act,
        "can_send": not send, "send_problem": send,
        "mark_read": _is_mailbox(user) and bool(svc.rt("webmail", "mark_read")) and not act,
        "auto_refresh_s": int(svc.rt("webmail", "auto_refresh_s") or 0),
        "max_attachments_mb": int(svc.rt("webmail", "max_attachments_mb") or 25),
        "is_admin": user.get("role") == "admin",
        "undo_send_s": int(svc.rt("webmail", "undo_send_s") or 0),
        "rules_interval_min": int(svc.rt("webmail", "rules_interval_min") or 0),
        "identity": {"display_name": ident["display_name"], "address": ident["address"],
                     "signature": ident["signature"], "from_choices": ident["from_choices"]},
        "autoreply_active": wt.get_autoreply(svc, int(acc.id))["active"],
    }


@router.get("/folders")
def webmail_folders(request: Request, account_id: int, refresh: bool = False, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    online = not _client_problem(svc, user, acc)
    # Ящик, в который сервер последний раз не пустил, сам не трогаем: повторные неудачные входы с адреса
    # архива сервер может заблокировать. Список с сервера — только по кнопке «↻».
    auto = online and acc.login_status != "auth_error"
    return wm.folders(svc, acc, refresh=refresh and online, online=auto or (refresh and online))


class SyncBody(BaseModel):
    account_id: int
    folder: str = Field(min_length=1, max_length=1000)
    folders: bool = False
    #: проверка по кнопке (а не по таймеру): идёт и после неудачного входа
    manual: bool = False


@router.post("/sync")
def webmail_sync(request: Request, body: SyncBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_online(svc, user, acc)
    if acc.login_status == "auth_error" and not body.manual:
        return {"folder": body.folder, "skipped": "auth_error", "new": 0, "gone": 0, "flags": 0, "pending": 0,
                "total": None, "unseen": None, "login_error": acc.login_error}
    res = wm.sync_folder(svc, acc, body.folder)
    if body.folders:
        try:
            wm.refresh_folders(svc, acc)
        except ValidationError:
            pass
    if body.folder == wm.special_folders(svc, acc).get("inbox", "INBOX"):
        # правила и автоответ — сразу, не дожидаясь проверки по расписанию
        try:
            res["rules"] = wt.process_new(svc, acc)
        except ValidationError as exc:
            res["rules"] = {"errors": [exc.message]}
    return res


# ---------------------------------------------------------------------------
#  Список писем
# ---------------------------------------------------------------------------
@router.get("/messages")
def webmail_messages(request: Request, account_id: int, folder: str, offset: int = 0, limit: int = 50,
                     sort: str = "date_desc", filter: str = "all", show_gone: bool = False,  # noqa: A002
                     user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    from ..imap.backup import BackupEngine
    data = wm.list_messages(svc, int(acc.id), folder, offset=offset, limit=limit, sort=sort, filt=filter,
                            show_gone=show_gone)
    out = []
    for r in data["rows"]:
        subject, from_addr, has_attach = r["subject"], r["from_addr"], r["has_attach"]
        if subject is None:      # копии, снятые до 1.0: тема и отправитель дозаполняются при первом показе
            try:
                raw = svc.store.read_message(int(acc.id), r["stored_path"])
                subject, from_addr, has_attach = BackupEngine._extract_meta(raw)  # noqa: SLF001
                svc.db.set_message_headers(r["id"], subject, from_addr, has_attach)
            except Exception:  # noqa: BLE001
                subject, from_addr, has_attach = "", "", 0
        flags = [f for f in (r["flags"] or "").split(",") if f]
        keywords = [f for f in flags if not f.startswith("\\") and f not in _SYSTEM_KEYWORDS]
        out.append({
            "id": r["id"], "folder": r["folder"], "subject": subject or "(без темы)", "from": from_addr or "",
            "to": (r["peer"] or "") if r["dir"] == 2 else "", "date": r["internaldate"],
            "size": int(r["size"] or 0), "size_h": human_size(r["size"] or 0),
            "has_attach": bool(has_attach), "seen": "\\Seen" in flags, "flagged": "\\Flagged" in flags,
            "answered": "\\Answered" in flags, "forwarded": "$Forwarded" in flags, "draft": "\\Draft" in flags,
            "gone": bool(r["gone"]), "keywords": keywords,
        })
    return {"messages": out, "total": data["total"], "gone": data["gone"], "offset": data["offset"],
            "limit": data["limit"], "folder": folder}


_SYSTEM_KEYWORDS = {"$Forwarded", "$MDNSent", "$Junk", "$NotJunk", "Junk", "NonJunk", "NotJunk", "$Phishing",
                    "$SubmitPending", "$Submitted", "Forwarded", "$Answered"}


@router.get("/ids")
def webmail_ids(request: Request, account_id: int, folder: str, filter: str = "all",  # noqa: A002
                show_gone: bool = False, user: dict = Depends(_user)):
    """Все письма папки (с тем же отбором) — «Выбрать все письма папки»."""
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return wm.list_ids(svc, int(acc.id), folder, filt=filter, show_gone=show_gone)


# ---------------------------------------------------------------------------
#  Действия над письмами
# ---------------------------------------------------------------------------
class IdsBody(BaseModel):
    account_id: int
    ids: List[int] = Field(min_length=1, max_length=wm.MAX_ACTION_IDS)


class FlagsBody(IdsBody):
    add: List[str] = Field(default_factory=list, max_length=10)
    remove: List[str] = Field(default_factory=list, max_length=10)


@router.post("/flags")
def webmail_flags(request: Request, body: FlagsBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.set_flags(svc, acc, body.ids, add=body.add, remove=body.remove)


class MoveBody(IdsBody):
    folder: str = Field(min_length=1, max_length=1000)


@router.post("/move")
def webmail_move(request: Request, body: MoveBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.move(svc, acc, body.ids, body.folder, actor=_actor(user))


class DeleteBody(IdsBody):
    permanent: bool = False


@router.post("/delete")
def webmail_delete(request: Request, body: DeleteBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.delete(svc, acc, body.ids, permanent=body.permanent, actor=_actor(user))


class JunkBody(IdsBody):
    spam: bool = True


@router.post("/junk")
def webmail_junk(request: Request, body: JunkBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.junk(svc, acc, body.ids, spam=body.spam, actor=_actor(user))


# ---------------------------------------------------------------------------
#  Папки
# ---------------------------------------------------------------------------
class FolderCreateBody(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=200)
    parent: str = Field(default="", max_length=1000)


@router.post("/folders/create")
def webmail_folder_create(request: Request, body: FolderCreateBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.create_folder(svc, acc, body.name, body.parent, actor=_actor(user))


class FolderRenameBody(BaseModel):
    account_id: int
    folder: str = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=200)
    #: перенести папку: в какую (''  — на верхний уровень; None — не переносить)
    parent: Optional[str] = Field(default=None, max_length=1000)


@router.post("/folders/rename")
def webmail_folder_rename(request: Request, body: FolderRenameBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.rename_folder(svc, acc, body.folder, body.name, actor=_actor(user), new_parent=body.parent)


class FolderBody(BaseModel):
    account_id: int
    folder: str = Field(min_length=1, max_length=1000)


@router.post("/folders/read_all")
def webmail_folder_read_all(request: Request, body: FolderBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.mark_all_read(svc, acc, body.folder)


@router.post("/folders/empty")
def webmail_folder_empty(request: Request, body: FolderBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.empty_folder(svc, acc, body.folder, actor=_actor(user))


class FolderDeleteBody(BaseModel):
    account_id: int
    folder: str = Field(min_length=1, max_length=1000)


@router.post("/folders/delete")
def webmail_folder_delete(request: Request, body: FolderDeleteBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wm.delete_folder(svc, acc, body.folder, actor=_actor(user))


# ---------------------------------------------------------------------------
#  Письмо: цепочка, заголовки
# ---------------------------------------------------------------------------
def _message_row(svc, acc, pk: int):
    row = svc.db.get_message(int(pk))
    if row is None or int(row["account_id"]) != int(acc.id):
        raise HTTPException(404, "Письмо не найдено")
    return row


@router.get("/thread")
def webmail_thread(request: Request, account_id: int, id: int, user: dict = Depends(_user)):  # noqa: A002
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    _message_row(svc, acc, id)
    return {"items": wm.thread(svc, int(acc.id), int(id))}


@router.get("/headers")
def webmail_headers(request: Request, account_id: int, id: int, user: dict = Depends(_user)):  # noqa: A002
    """Заголовки письма как есть — для «Показать заголовки» (до 64 КБ)."""
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    row = _message_row(svc, acc, id)
    with svc.store.open_message(int(acc.id), row["stored_path"]) as fh:
        head = fh.read(256 * 1024)
    import re
    m = re.search(rb"\r?\n\r?\n", head)
    text = (head[:m.start()] if m else head)[:65536].decode("utf-8", "replace")
    return {"headers": text, "folder": row["folder"], "uid": row["uid"], "gone": bool(row["gone"])}


@router.get("/contacts")
def webmail_contacts(request: Request, account_id: int, q: str = "", user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return {"items": wm.contacts(svc, acc, q[:100])}


# ---------------------------------------------------------------------------
#  Отправитель и сервер отправки
# ---------------------------------------------------------------------------
@router.get("/identity")
def webmail_identity(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    ident = wm.get_identity(svc, acc)
    ident["can_edit_smtp"] = user.get("role") == "admin"
    ident["send_problem"] = wm.send_problem(svc, acc)
    if _is_mailbox(user):
        for key in ("smtp_username",):
            ident[key] = ""
    return ident


class IdentityBody(BaseModel):
    account_id: int
    display_name: Optional[str] = Field(default=None, max_length=200)
    signature: Optional[str] = Field(default=None, max_length=300_000)
    reply_to: Optional[str] = Field(default=None, max_length=300)
    address: Optional[str] = Field(default=None, max_length=300)
    smtp_host: Optional[str] = Field(default=None, max_length=255)
    smtp_port: Optional[int] = None
    smtp_security: Optional[str] = Field(default=None, max_length=20)
    smtp_auth: Optional[str] = Field(default=None, max_length=20)
    smtp_username: Optional[str] = Field(default=None, max_length=300)
    smtp_password: Optional[str] = Field(default=None, max_length=1000)
    save_sent: Optional[str] = Field(default=None, max_length=20)
    aliases: Optional[str] = Field(default=None, max_length=3000)


@router.put("/identity")
def webmail_identity_save(request: Request, body: IdentityBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    admin = user.get("role") == "admin"
    if not admin:
        _require_act(svc, user, acc)
    data = {k: v for k, v in body.model_dump().items() if k != "account_id" and v is not None}
    smtp_keys = {"address", "smtp_host", "smtp_port", "smtp_security", "smtp_auth", "smtp_username", "smtp_password",
                 "save_sent", "aliases"}
    if not admin and smtp_keys & set(data):
        raise HTTPException(403, "Сервер отправки и адрес отправителя настраивает администратор.")
    res = wm.save_identity(svc, acc, data, admin=admin)
    if admin and smtp_keys & set(data):
        svc.db.add_audit(_actor(user), "webmail_identity", f"ящик «{acc.name}»: настройки отправки")
    return res


# ---------------------------------------------------------------------------
#  Черновики и отправка
# ---------------------------------------------------------------------------
def _draft_account(svc, user: dict, draft_id: int):
    row = wm.get_draft_row(svc, draft_id, _owner(svc, user))
    acc = _account(svc, user, int(row["account_id"]))
    return row, acc


@router.get("/drafts")
def webmail_drafts(request: Request, account_id: Optional[int] = None, user: dict = Depends(_user)):
    svc = _svc(request)
    ids = None
    if account_id is not None:
        _account(svc, user, account_id)
        ids = [int(account_id)]
    elif _is_mailbox(user):
        ids = auth_mod.mailbox_ids(user)
    return {"items": wm.list_drafts(svc, _owner(svc, user), ids)}


class DraftCreateBody(BaseModel):
    account_id: int
    mode: str = Field(default="new", pattern="^(new|reply|reply_all|forward|forward_attach|edit)$")
    ref_id: Optional[int] = None
    #: «Переслать как вложение» нескольких писем
    ref_ids: List[int] = Field(default_factory=list, max_length=50)
    to_addrs: str = Field(default="", max_length=5000)
    cc_addrs: str = Field(default="", max_length=5000)
    subject: str = Field(default="", max_length=998)
    #: текст нового письма (из шаблона, ссылки mailto:) — до подписи
    body: str = Field(default="", max_length=2_000_000)


@router.post("/drafts")
def webmail_draft_create(request: Request, body: DraftCreateBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    fields = wm.compose_fields(svc, acc, body.mode, body.ref_id, body.ref_ids)
    if body.mode == "new":
        fields.update({"to_addrs": body.to_addrs, "cc_addrs": body.cc_addrs, "subject": body.subject,
                       "body": body.body})
    sig = wm.get_identity(svc, acc).get("signature") or ""
    if sig and body.mode != "edit":
        lead = (fields.get("body") or "") if body.mode == "new" else ""
        rest = "" if body.mode == "new" else (fields.get("body") or "")
        fields["body"] = (lead or "<p><br></p>") + '<div class="ma-signature">-- <br>' + sig + "</div>" + rest
    return wm.create_draft(svc, acc, _owner(svc, user), fields)


@router.get("/drafts/{draft_id}")
def webmail_draft_get(request: Request, draft_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    row, _acc = _draft_account(svc, user, draft_id)
    return wm._draft_dict(row)  # noqa: SLF001


class DraftUpdateBody(BaseModel):
    account_id: Optional[int] = None
    to_addrs: Optional[str] = Field(default=None, max_length=5000)
    cc_addrs: Optional[str] = Field(default=None, max_length=5000)
    bcc_addrs: Optional[str] = Field(default=None, max_length=5000)
    subject: Optional[str] = Field(default=None, max_length=998)
    body: Optional[str] = Field(default=None, max_length=wm.MAX_BODY_CHARS)
    from_addr: Optional[str] = Field(default=None, max_length=300)
    priority: Optional[bool] = None
    receipt: Optional[bool] = None


@router.put("/drafts/{draft_id}")
def webmail_draft_update(request: Request, draft_id: int, body: DraftUpdateBody, user: dict = Depends(_user)):
    svc = _svc(request)
    _row, _acc = _draft_account(svc, user, draft_id)
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if data.get("account_id"):
        new_acc = _account(svc, user, int(data["account_id"]))
        _require_act(svc, user, new_acc)
    return wm.update_draft(svc, draft_id, _owner(svc, user), data)


@router.delete("/drafts/{draft_id}")
def webmail_draft_delete(request: Request, draft_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    _draft_account(svc, user, draft_id)
    wm.delete_draft(svc, draft_id, _owner(svc, user))
    return {"ok": True}


@router.post("/drafts/{draft_id}/attachments")
def webmail_draft_attach(request: Request, draft_id: int, file: UploadFile = File(...), user: dict = Depends(_user)):
    svc = _svc(request)
    _row, acc = _draft_account(svc, user, draft_id)
    _require_act(svc, user, acc)
    try:
        return wm.add_attachment(svc, draft_id, _owner(svc, user), file.filename or "file",
                                 file.content_type or "", file.file)
    finally:
        try:
            file.file.close()
        except Exception:  # noqa: BLE001
            pass


@router.delete("/drafts/{draft_id}/attachments/{n}")
def webmail_draft_detach(request: Request, draft_id: int, n: int, user: dict = Depends(_user)):
    svc = _svc(request)
    _draft_account(svc, user, draft_id)
    wm.remove_attachment(svc, draft_id, _owner(svc, user), n)
    return {"ok": True}


@router.post("/drafts/{draft_id}/send")
def webmail_draft_send(request: Request, draft_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    _row, acc = _draft_account(svc, user, draft_id)
    _require_act(svc, user, acc)
    return wm.send(svc, acc, draft_id, _owner(svc, user), actor=_actor(user))


class ScheduleBody(BaseModel):
    send_at: str = Field(min_length=10, max_length=40)


@router.post("/drafts/{draft_id}/schedule")
def webmail_draft_schedule(request: Request, draft_id: int, body: ScheduleBody, user: dict = Depends(_user)):
    """Отправить позже: письмо уйдёт в назначенное время, даже если клиент закрыт."""
    svc = _svc(request)
    _row, acc = _draft_account(svc, user, draft_id)
    _require_act(svc, user, acc)
    return wt.schedule_send(svc, acc, draft_id, _owner(svc, user), body.send_at, actor=_actor(user),
                            role=str(user.get("role") or ""))


@router.post("/drafts/{draft_id}/unschedule")
def webmail_draft_unschedule(request: Request, draft_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    _draft_account(svc, user, draft_id)
    return wt.cancel_schedule(svc, draft_id, _owner(svc, user))


# ---------------------------------------------------------------------------
#  Метки
# ---------------------------------------------------------------------------
@router.get("/tags")
def webmail_tags(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return {"items": wt.list_tags(svc, int(acc.id))}


class TagBody(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=40)
    color: str = Field(default="#2563eb", max_length=7)


@router.post("/tags")
def webmail_tag_create(request: Request, body: TagBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_tag(svc, int(acc.id), body.model_dump())


@router.put("/tags/{tag_id}")
def webmail_tag_update(request: Request, tag_id: int, body: TagBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_tag(svc, int(acc.id), body.model_dump(), tag_id)


@router.delete("/tags/{tag_id}")
def webmail_tag_delete(request: Request, tag_id: int, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    _require_act(svc, user, acc)
    wt.delete_tag(svc, int(acc.id), tag_id)
    return {"ok": True}


class TagMessagesBody(IdsBody):
    keyword: str = Field(min_length=1, max_length=60)
    on: bool = True


@router.post("/tag")
def webmail_tag_messages(request: Request, body: TagMessagesBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.tag_messages(svc, acc, body.ids, body.keyword, body.on)


# ---------------------------------------------------------------------------
#  Шаблоны писем (свои у каждого пользователя)
# ---------------------------------------------------------------------------
@router.get("/templates")
def webmail_templates(request: Request, user: dict = Depends(_user)):
    svc = _svc(request)
    return {"items": wt.list_templates(svc, _owner(svc, user))}


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=2_000_000)


@router.post("/templates")
def webmail_template_create(request: Request, body: TemplateBody, user: dict = Depends(_user)):
    svc = _svc(request)
    return wt.save_template(svc, _owner(svc, user), body.model_dump())


@router.put("/templates/{template_id}")
def webmail_template_update(request: Request, template_id: int, body: TemplateBody, user: dict = Depends(_user)):
    svc = _svc(request)
    return wt.save_template(svc, _owner(svc, user), body.model_dump(), template_id)


@router.delete("/templates/{template_id}")
def webmail_template_delete(request: Request, template_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    wt.delete_template(svc, _owner(svc, user), template_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Адресная книга ящика
# ---------------------------------------------------------------------------
@router.get("/book")
def webmail_book(request: Request, account_id: int, q: str = "", group: str = "", user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return {"items": wt.list_book(svc, int(acc.id), q[:100], group[:60]), "groups": wt.book_groups(svc, int(acc.id))}


class ContactBody(BaseModel):
    account_id: int
    name: str = Field(default="", max_length=200)
    email: str = Field(min_length=3, max_length=300)
    phone: str = Field(default="", max_length=100)
    company: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)
    groups: str = Field(default="", max_length=1000)


@router.post("/book")
def webmail_book_add(request: Request, body: ContactBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_contact(svc, int(acc.id), body.model_dump())


@router.put("/book/{contact_id}")
def webmail_book_update(request: Request, contact_id: int, body: ContactBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_contact(svc, int(acc.id), body.model_dump(), contact_id)


@router.post("/book/delete")
def webmail_book_delete(request: Request, body: IdsBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return {"deleted": wt.delete_contacts(svc, int(acc.id), body.ids)}


@router.post("/book/import")
def webmail_book_import(request: Request, account_id: int = Form(...), file: UploadFile = File(...),
                        user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    _require_act(svc, user, acc)
    try:
        data = file.file.read(5 * 1024 * 1024 + 1)
    finally:
        file.file.close()
    return wt.import_book(svc, int(acc.id), file.filename or "", data)


@router.get("/book/export")
def webmail_book_export(request: Request, account_id: int, fmt: str = "vcf", user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    data, ctype, name = wt.export_book(svc, int(acc.id), "csv" if fmt == "csv" else "vcf")
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
#  Правила сортировки, заблокированные отправители, автоответ
# ---------------------------------------------------------------------------
@router.get("/rules")
def webmail_rules(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    items = wt.list_rules(svc, int(acc.id))
    return {"items": [r for r in items if r["kind"] != "block"], "blocked": [r for r in items if r["kind"] == "block"],
            "status": wt.automation_status(svc, int(acc.id))}


class RuleBody(BaseModel):
    account_id: int
    name: str = Field(default="", max_length=100)
    enabled: bool = True
    match: str = Field(default="all", pattern="^(all|any)$")
    conditions: List[dict] = Field(default_factory=list, max_length=wt.MAX_CONDITIONS)
    actions: List[dict] = Field(default_factory=list, max_length=wt.MAX_ACTIONS)
    stop: bool = False


@router.post("/rules")
def webmail_rule_create(request: Request, body: RuleBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_rule(svc, acc, body.model_dump(), actor=_actor(user))


@router.put("/rules/{rule_id}")
def webmail_rule_update(request: Request, rule_id: int, body: RuleBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.save_rule(svc, acc, body.model_dump(), rule_id, actor=_actor(user))


@router.delete("/rules/{rule_id}")
def webmail_rule_delete(request: Request, rule_id: int, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    _require_act(svc, user, acc)
    wt.delete_rule(svc, acc, rule_id, actor=_actor(user))
    return {"ok": True}


class RuleOrderBody(BaseModel):
    account_id: int
    ids: List[int] = Field(max_length=wt.MAX_RULES)


@router.post("/rules/order")
def webmail_rules_order(request: Request, body: RuleOrderBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    wt.reorder_rules(svc, int(acc.id), body.ids)
    return {"ok": True}


class RuleApplyBody(BaseModel):
    account_id: int
    folder: str = Field(min_length=1, max_length=1000)
    rule_ids: Optional[List[int]] = Field(default=None, max_length=wt.MAX_RULES)


@router.post("/rules/apply")
def webmail_rules_apply(request: Request, body: RuleApplyBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.apply_rules_to_folder(svc, acc, body.folder, rule_ids=body.rule_ids)


class BlockBody(BaseModel):
    account_id: int
    address: str = Field(min_length=3, max_length=300)
    ids: List[int] = Field(default_factory=list, max_length=wm.MAX_ACTION_IDS)


@router.post("/block")
def webmail_block(request: Request, body: BlockBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.block_sender(svc, acc, body.address, ids=body.ids, actor=_actor(user))


@router.get("/autoreply")
def webmail_autoreply(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return wt.get_autoreply(svc, int(acc.id)) | {"interval_min": int(svc.rt("webmail", "rules_interval_min") or 0)}


class AutoreplyBody(BaseModel):
    account_id: int
    enabled: bool = False
    date_from: str = Field(default="", max_length=10)
    date_to: str = Field(default="", max_length=10)
    subject: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=300_000)
    days: int = Field(default=4, ge=1, le=30)


@router.put("/autoreply")
def webmail_autoreply_save(request: Request, body: AutoreplyBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    if body.enabled:
        problem = wm.send_problem(svc, acc)
        if problem:
            raise HTTPException(400, f"Автоответ не сможет отправлять письма: {problem}")
    return wt.save_autoreply(svc, acc, body.model_dump(), actor=_actor(user))


# ---------------------------------------------------------------------------
#  «Напомнить позже»
# ---------------------------------------------------------------------------
class SnoozeBody(IdsBody):
    until: str = Field(min_length=10, max_length=40)


@router.post("/snooze")
def webmail_snooze(request: Request, body: SnoozeBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.snooze(svc, acc, body.ids, body.until, actor=_actor(user))


@router.get("/snoozed")
def webmail_snoozed(request: Request, account_id: int, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    return {"items": wt.list_snoozed(svc, int(acc.id))}


@router.post("/unsnooze")
def webmail_unsnooze(request: Request, body: IdsBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.unsnooze(svc, acc, body.ids)


# ---------------------------------------------------------------------------
#  Архивы .zip
# ---------------------------------------------------------------------------
def _file_response(fh, filename: str, ctype: str = "application/zip"):
    from .api import _attachment_disposition
    size = fh.seek(0, 2)
    fh.seek(0)

    def chunks():
        try:
            while True:
                part = fh.read(1024 * 1024)
                if not part:
                    break
                yield part
        finally:
            fh.close()
    return StreamingResponse(chunks(), media_type=ctype,
                             headers={"Content-Disposition": _attachment_disposition(filename),
                                      "Content-Length": str(size)})


@router.post("/zip")
def webmail_zip(request: Request, body: IdsBody, user: dict = Depends(_user)):
    """Выбранные письма — файлами .eml в одном архиве .zip."""
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    fh = wt.zip_messages(svc, acc, body.ids)
    if user.get("role") == "admin":
        # администратор выгружает чужую переписку — как поиск и выгрузка ящика, это в аудит
        svc.db.add_audit(_actor(user), "webmail_zip", f"ящик «{acc.name}»: писем {len(body.ids)} архивом .zip")
    return _file_response(fh, f"Письма ({len(body.ids)}).zip")


@router.get("/attachments_zip")
def webmail_attachments_zip(request: Request, account_id: int, id: int, user: dict = Depends(_user)):  # noqa: A002
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    fh, name = wt.zip_attachments(svc, acc, _message_row(svc, acc, id))
    return _file_response(fh, name)


# ---------------------------------------------------------------------------
#  Сведения о письме: важность, отписка, уведомление о прочтении, приглашение
# ---------------------------------------------------------------------------
@router.get("/extras")
def webmail_extras(request: Request, account_id: int, id: int, user: dict = Depends(_user)):  # noqa: A002
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    row = _message_row(svc, acc, id)
    return wt.message_extras(svc, acc, row, can_act=not _act_problem(svc, user, acc))


class ReceiptBody(BaseModel):
    account_id: int
    id: int
    send: bool = True


@router.post("/receipt")
def webmail_receipt(request: Request, body: ReceiptBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.send_receipt(svc, acc, _message_row(svc, acc, body.id), send=body.send, actor=_actor(user))


class InviteBody(BaseModel):
    account_id: int
    id: int
    partstat: str = Field(pattern="^(ACCEPTED|TENTATIVE|DECLINED)$")
    comment: str = Field(default="", max_length=1000)


@router.post("/invite")
def webmail_invite(request: Request, body: InviteBody, user: dict = Depends(_user)):
    svc = _svc(request)
    acc = _account(svc, user, body.account_id)
    _require_act(svc, user, acc)
    return wt.invite_reply(svc, acc, _message_row(svc, acc, body.id), body.partstat, comment=body.comment,
                           actor=_actor(user))


@router.get("/invite.ics")
def webmail_invite_ics(request: Request, account_id: int, id: int, user: dict = Depends(_user)):  # noqa: A002
    svc = _svc(request)
    acc = _account(svc, user, account_id)
    data = wt.invite_ics(svc, acc, _message_row(svc, acc, id))
    return Response(data, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="invite.ics"'})
