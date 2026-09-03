"""
REST API веб-интерфейса. Все ответы — JSON. Ошибки предметной области
(:class:`MailArchiverError`) перехватываются глобально (см. app.py) и отдаются
с понятным пользователю сообщением и подсказкой.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..errors import ValidationError
from ..imap.client import probe_account
from ..models import Account, AuthType, JobStatus, JobType, ScheduleKind, Security
from ..util import human_size
from ..version import __version__
from . import auth as auth_mod
from .i18n import all_help
from ..export import list_engines

router = APIRouter(prefix="/api")


def svc_dep(request: Request):
    return request.app.state.services


def _ensure_account_access(user: dict, account_id: int) -> None:
    """Пользователь-ящик (role=mailbox) имеет доступ только к своему ящику."""
    if user.get("role") == "mailbox" and user.get("account_id") != account_id:
        raise HTTPException(403, "Доступ разрешён только к своему почтовому ящику")


def _require_not_mailbox(user: dict) -> None:
    if user.get("role") == "mailbox":
        raise HTTPException(403, "Действие недоступно для входа по ящику")


# =====================================================================
#  Схемы запросов
# =====================================================================
class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


class AccountBody(BaseModel):
    name: str
    host: str
    port: int = 993
    username: str
    password: str = ""
    auth_type: str = AuthType.PASSWORD
    security: str = Security.SSL
    enabled: bool = True
    folder_include: List[str] = []
    folder_exclude: List[str] = []
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_refresh_token: str = ""
    oauth_token_url: str = ""
    notes: str = ""
    retention_days: int = -1


class RetentionBody(BaseModel):
    days: int = -1        # -1 наследовать глобальную; 0 хранить всё; 3 или 7 — пресеты; N — своё
    run_now: bool = True


class ExportBody(BaseModel):
    engine: str = "auto"
    format: str = "pst"
    folders: Optional[List[str]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    pst_format: Optional[str] = None
    outlook_target: Optional[str] = None
    aspose_license_path: Optional[str] = None
    limit: int = 0


class RestoreBody(BaseModel):
    folders: Optional[List[str]] = None
    target_mode: str = "prefixed"
    target_folder: str = ""
    target_prefix: str = "Восстановлено"
    check_duplicates: bool = True
    dry_run: bool = False
    limit: int = 0


class ScheduleBody(BaseModel):
    account_id: int
    kind: str = ScheduleKind.CRON
    job_type: str = JobType.BACKUP
    cron_expr: str = ""
    interval_seconds: int = 0
    enabled: bool = True
    options: dict = {}


class SettingsBody(BaseModel):
    values: dict  # {"section.key": value}


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "admin"


class PasswordBody(BaseModel):
    password: str


# =====================================================================
#  Аутентификация
# =====================================================================
@router.get("/needs-setup")
def needs_setup(request: Request):
    svc = svc_dep(request)
    return {"needs_setup": svc.db.count_users() == 0, "auth_enabled": bool(svc.rt("security", "auth_enabled"))}


@router.post("/setup")
def setup(request: Request, body: SetupBody):
    svc = svc_dep(request)
    user = auth_mod.create_first_admin(svc, body.username, body.password)
    return {"ok": True, "user": user}


@router.post("/login")
def login(request: Request, response: Response, body: LoginBody):
    svc = svc_dep(request)
    user = auth_mod.do_login(svc, request, response, body.username, body.password)
    return {"ok": True, "user": user}


@router.post("/logout")
def logout(request: Request, response: Response):
    svc = svc_dep(request)
    auth_mod.destroy_session(svc, request, response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = auth_mod.current_user(request)
    if user is None:
        raise HTTPException(401, "Не авторизован")
    return user


# =====================================================================
#  Дашборд / состояние
# =====================================================================
@router.get("/state")
def state(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    accounts = svc.db.list_accounts()
    if user.get("role") == "mailbox":
        accounts = [a for a in accounts if a.id == user.get("account_id")]
    acc_out = []
    for a in accounts:
        cnt = svc.db.count_messages(a.id)
        by = svc.db.sum_message_bytes(a.id)
        runs = svc.db.list_runs(a.id, limit=1)
        last = runs[0] if runs else None
        acc_out.append({
            **a.redacted(),
            "messages": cnt, "bytes": by, "bytes_h": human_size(by),
            "last_run": ({"status": last["status"], "finished_at": last["finished_at"],
                          "messages_new": last["messages_new"]} if last else None),
        })
    counts = svc.db.count_jobs_by_status()
    active = [serialize_job(j) for j in svc.db.active_jobs()]
    if user.get("role") == "mailbox":
        aid = user.get("account_id")
        active = [j for j in active if j["account_id"] == aid]
        total_msgs = svc.db.count_messages(aid)
        total_bytes = svc.db.sum_message_bytes(aid)
    else:
        total_msgs = svc.db.count_messages()
        total_bytes = svc.db.sum_message_bytes()
    disk_free = _disk_free(svc.cfg.mail_root)
    return {
        "version": __version__,
        "accounts": acc_out,
        "job_counts": counts,
        "active_jobs": active,
        "totals": {"accounts": len(accounts), "messages": total_msgs,
                   "bytes": total_bytes, "bytes_h": human_size(total_bytes)},
        "disk": {"free": disk_free, "free_h": human_size(disk_free)},
        "scheduler_running": svc.scheduler.running(),
        "engines": list_engines(),
        "workers": svc.queue.max_workers(),
    }


def _disk_free(path: str) -> int:
    try:
        from ..util import disk_free_bytes
        return disk_free_bytes(path)
    except Exception:  # noqa: BLE001
        return 0


# =====================================================================
#  Аккаунты
# =====================================================================
def _account_from_body(body: AccountBody, account_id: Optional[int] = None) -> Account:
    return Account(
        id=account_id, name=body.name.strip(), host=body.host.strip(), port=int(body.port),
        username=body.username.strip(), password=body.password, auth_type=body.auth_type,
        security=body.security, enabled=body.enabled,
        folder_include=body.folder_include or [], folder_exclude=body.folder_exclude or [],
        oauth_client_id=body.oauth_client_id, oauth_client_secret=body.oauth_client_secret,
        oauth_refresh_token=body.oauth_refresh_token, oauth_token_url=body.oauth_token_url,
        notes=body.notes, retention_days=body.retention_days,
    )


def _validate_account(acc: Account) -> None:
    if not acc.name:
        raise ValidationError("Укажите название ящика.")
    if not acc.host:
        raise ValidationError("Укажите адрес IMAP-сервера.")
    if not acc.username:
        raise ValidationError("Укажите логин.")
    if acc.security not in Security.ALL:
        raise ValidationError("Некорректный режим шифрования.")
    if acc.auth_type not in AuthType.ALL:
        raise ValidationError("Некорректный способ входа.")


@router.get("/accounts")
def list_accounts(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    accounts = svc.db.list_accounts()
    if user.get("role") == "mailbox":
        accounts = [a for a in accounts if a.id == user.get("account_id")]
    return [a.redacted() for a in accounts]


@router.post("/accounts")
def create_account(request: Request, body: AccountBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    acc = _account_from_body(body)
    _validate_account(acc)
    acc.id = svc.db.create_account(acc)
    svc.db.add_audit(user["username"], "account_create", acc.name)
    return {"ok": True, "id": acc.id}


@router.get("/accounts/{account_id}")
def get_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    data = acc.redacted()
    data["folders"] = [{"folder": r["folder"], "count": r["cnt"], "bytes": r["bytes"],
                        "bytes_h": human_size(r["bytes"])} for r in svc.db.folders_summary(account_id)]
    return data


@router.put("/accounts/{account_id}")
def update_account(request: Request, account_id: int, body: AccountBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    if svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    acc = _account_from_body(body, account_id)
    _validate_account(acc)
    # Каждый секрет обновляется независимо: пустое поле НЕ затирает сохранённый секрет.
    svc.db.update_account(
        acc,
        update_password=bool(body.password),
        update_oauth_secret=bool(body.oauth_client_secret),
        update_oauth_token=bool(body.oauth_refresh_token),
    )
    svc.db.add_audit(user["username"], "account_update", acc.name)
    return {"ok": True}


@router.delete("/accounts/{account_id}")
def delete_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    svc.db.delete_account(account_id)
    svc.db.add_audit(user["username"], "account_delete", acc.name)
    return {"ok": True}


@router.post("/accounts/{account_id}/test")
def test_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    result = probe_account(acc, svc.connect_options())
    return result


@router.post("/accounts/{account_id}/backup")
def start_backup(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    max_attempts = int(svc.rt("backup", "retry_attempts") or 1)
    jid = svc.queue.enqueue(JobType.BACKUP, account_id, {}, max_attempts=max_attempts, created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/export")
def start_export(request: Request, account_id: int, body: ExportBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    jid = svc.queue.enqueue(JobType.EXPORT, account_id, body.model_dump(), created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/restore")
def start_restore(request: Request, account_id: int, body: RestoreBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    jid = svc.queue.enqueue(JobType.RESTORE, account_id, body.model_dump(), created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/verify")
def start_verify(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    jid = svc.queue.enqueue(JobType.VERIFY, account_id, {}, created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/retention")
def set_retention(request: Request, account_id: int, body: RetentionBody, user: dict = Depends(auth_mod.require_user)):
    """Задать политику хранения локальной копии ящика (пресеты: 3 дня, 1 неделя, всё)."""
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    svc.db.set_account_retention(account_id, int(body.days))
    result = {"ok": True, "days": int(body.days)}
    if body.days and body.days > 0:
        # автоматическая ежедневная очистка: заводим расписание, если его ещё нет
        existing = [s for s in svc.db.list_schedules(account_id) if s["job_type"] == JobType.RETENTION]
        if not existing:
            svc.db.create_schedule(account_id, ScheduleKind.CRON, JobType.RETENTION,
                                   cron_expr="30 3 * * *", enabled=True)
            svc.scheduler.reload()
            result["schedule_created"] = True
        if body.run_now:
            result["job_id"] = svc.queue.enqueue(JobType.RETENTION, account_id, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "set_retention", f"account={account_id} days={body.days}")
    return result


@router.post("/accounts/{account_id}/import-pst")
def import_pst(request: Request, account_id: int, file: UploadFile = File(...),
               target_prefix: str = Form("Импорт PST"), user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    if not (file.filename or "").lower().endswith(".pst"):
        raise ValidationError("Ожидается файл .pst", hint="Выберите файл с расширением .pst")
    dest = os.path.join(svc.cfg.tmp_dir, f"import_{account_id}_{os.getpid()}_{file.filename}")
    with open(dest, "wb") as fh:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    jid = svc.queue.enqueue(JobType.IMPORT_PST, account_id,
                            {"pst_path": dest, "target": "imap", "target_prefix": target_prefix},
                            created_by=user["username"])
    return {"ok": True, "job_id": jid}


# =====================================================================
#  Просмотр писем (почтовый клиент)
# =====================================================================
@router.get("/accounts/{account_id}/mailfolders")
def mail_folders(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    out = [{"folder": r["folder"], "count": r["cnt"], "bytes": r["bytes"], "bytes_h": human_size(r["bytes"])}
           for r in svc.db.folders_summary(account_id)]
    return {"folders": out, "total": svc.db.count_messages(account_id)}


@router.get("/accounts/{account_id}/messages")
def list_mailbox_messages(request: Request, account_id: int, folder: Optional[str] = None,
                          limit: int = 50, offset: int = 0, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    from ..imap.backup import BackupEngine
    limit = max(1, min(200, limit))
    rows = svc.db.list_messages(account_id, folder=folder, limit=limit, offset=offset)
    out = []
    for r in rows:
        subject, from_addr, has_attach = r["subject"], r["from_addr"], r["has_attach"]
        if subject is None:  # ленивое дозаполнение для копий, снятых до этой версии
            try:
                raw = svc.store.read_message(account_id, r["stored_path"])
                subject, from_addr, has_attach = BackupEngine._extract_meta(raw)
                svc.db.set_message_headers(r["id"], subject, from_addr, has_attach)
            except Exception:  # noqa: BLE001
                subject, from_addr, has_attach = "", "", 0
        flags = [f for f in (r["flags"] or "").split(",") if f]
        out.append({
            "id": r["id"], "folder": r["folder"], "subject": subject or "(без темы)",
            "from": from_addr or "", "date": r["internaldate"], "size": r["size"],
            "size_h": human_size(r["size"] or 0), "has_attach": bool(has_attach),
            "seen": "\\Seen" in flags, "flagged": "\\Flagged" in flags, "answered": "\\Answered" in flags,
        })
    return {"messages": out, "total": svc.db.count_folder_messages(account_id, folder),
            "folder": folder, "limit": limit, "offset": offset}


@router.get("/accounts/{account_id}/messages/{pk}")
def read_mailbox_message(request: Request, account_id: int, pk: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    from ..mailview import parse_message
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    raw = svc.store.read_message(account_id, row["stored_path"])
    parsed = parse_message(raw)
    parsed["id"] = pk
    parsed["folder"] = row["folder"]
    parsed["flags"] = [f for f in (row["flags"] or "").split(",") if f]
    parsed["size_h"] = human_size(row["size"] or len(raw))
    return parsed


@router.get("/accounts/{account_id}/messages/{pk}/attachment/{idx}")
def download_attachment(request: Request, account_id: int, pk: int, idx: int,
                        user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    from ..mailview import get_attachment
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    raw = svc.store.read_message(account_id, row["stored_path"])
    att = get_attachment(raw, idx)
    if att is None:
        raise HTTPException(404, "Вложение не найдено")
    filename, ctype, data = att
    from urllib.parse import quote
    disp = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=ctype or "application/octet-stream",
                    headers={"Content-Disposition": disp})


@router.get("/accounts/{account_id}/messages/{pk}/raw")
def download_message_raw(request: Request, account_id: int, pk: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    raw = svc.store.read_message(account_id, row["stored_path"])
    return Response(content=raw, media_type="message/rfc822",
                    headers={"Content-Disposition": f'attachment; filename="message_{pk}.eml"'})


# =====================================================================
#  Задания
# =====================================================================
def serialize_job(row) -> dict:
    return {
        "id": row["id"], "type": row["type"], "type_label": JobType.LABELS.get(row["type"], row["type"]),
        "account_id": row["account_id"], "status": row["status"],
        "status_label": JobStatus.LABELS.get(row["status"], row["status"]),
        "priority": row["priority"], "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "progress_current": row["progress_current"],
        "progress_total": row["progress_total"], "progress_message": row["progress_message"],
        "bytes_done": row["bytes_done"], "bytes_done_h": human_size(row["bytes_done"] or 0),
        "speed": row["speed"], "speed_h": human_size(row["speed"] or 0) + "/с",
        "error": row["error"], "attempts": row["attempts"], "max_attempts": row["max_attempts"],
        "created_by": row["created_by"],
        "percent": (round(100 * row["progress_current"] / row["progress_total"])
                    if row["progress_total"] else (100 if row["status"] in JobStatus.TERMINAL else 0)),
    }


@router.get("/jobs")
def list_jobs(request: Request, status: Optional[str] = None, type: Optional[str] = None,
              account_id: Optional[int] = None, limit: int = 100,
              user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    if user.get("role") == "mailbox":
        account_id = user.get("account_id")
    rows = svc.db.list_jobs(status=status, job_type=type, account_id=account_id, limit=limit)
    return [serialize_job(r) for r in rows]


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    if row["account_id"] is not None:
        _ensure_account_access(user, row["account_id"])
    data = serialize_job(row)
    try:
        data["result"] = json.loads(row["result"] or "{}")
    except json.JSONDecodeError:
        data["result"] = {}
    data["params"] = json.loads(row["params"] or "{}")
    return data


@router.get("/jobs/{job_id}/events")
def job_events(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    if user.get("role") == "mailbox":
        j = svc.db.get_job(job_id)
        if j and j["account_id"] is not None:
            _ensure_account_access(user, j["account_id"])
    rows = svc.db.list_job_events(job_id, limit=500)
    return [{"ts": r["ts"], "level": r["level"], "message": r["message"]} for r in rows][::-1]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    if row["account_id"] is not None:
        _ensure_account_access(user, row["account_id"])
    svc.queue.cancel(job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    if row["account_id"] is not None:
        _ensure_account_access(user, row["account_id"])
    if row["status"] not in JobStatus.TERMINAL:
        raise ValidationError("Повторить можно только завершённое задание.")
    svc.db.requeue_job(job_id)
    svc.queue._wake.set()
    return {"ok": True}


# =====================================================================
#  Экспорты
# =====================================================================
@router.get("/exports")
def list_exports(request: Request, account_id: Optional[int] = None, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    if user.get("role") == "mailbox":
        account_id = user.get("account_id")
    rows = svc.db.list_exports(account_id, limit=200)
    out = []
    for r in rows:
        out.append({"id": r["id"], "account_id": r["account_id"], "engine": r["engine"], "format": r["format"],
                    "path": r["path"], "filename": os.path.basename(r["path"] or ""), "size": r["size"],
                    "size_h": human_size(r["size"] or 0), "status": r["status"], "error": r["error"],
                    "created_at": r["created_at"],
                    "exists": bool(r["path"] and os.path.exists(r["path"]))})
    return out


@router.get("/exports/{export_id}/download")
def download_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    if r is None or not r["path"] or not os.path.exists(r["path"]):
        raise HTTPException(404, "Файл экспорта не найден")
    _ensure_account_access(user, r["account_id"])
    return FileResponse(r["path"], filename=os.path.basename(r["path"]), media_type="application/octet-stream")


@router.delete("/exports/{export_id}")
def delete_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    if r is None:
        raise HTTPException(404, "Экспорт не найден")
    _ensure_account_access(user, r["account_id"])
    if r["path"] and os.path.exists(r["path"]):
        try:
            os.unlink(r["path"])
        except OSError:
            pass
    svc.db.delete_export(export_id)
    return {"ok": True}


@router.get("/export/engines")
def export_engines(request: Request, user: dict = Depends(auth_mod.require_user)):
    return list_engines()


# =====================================================================
#  Расписания
# =====================================================================
@router.get("/schedules")
def list_schedules(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    mailbox_aid = user.get("account_id") if user.get("role") == "mailbox" else None
    out = []
    for r in svc.db.list_schedules(account_id=mailbox_aid):
        out.append({"id": r["id"], "account_id": r["account_id"], "kind": r["kind"], "job_type": r["job_type"],
                    "cron_expr": r["cron_expr"], "interval_seconds": r["interval_seconds"],
                    "enabled": bool(r["enabled"]), "last_run": r["last_run"],
                    "next_run": svc.scheduler.next_run_for(r["id"]) or r["next_run"],
                    "options": json.loads(r["options"] or "{}")})
    return out


@router.post("/schedules")
def create_schedule(request: Request, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    if user.get("role") == "mailbox":
        _ensure_account_access(user, body.account_id)
    svc.require_account(body.account_id)
    sid = svc.db.create_schedule(body.account_id, body.kind, body.job_type, body.cron_expr,
                                 body.interval_seconds, body.enabled, body.options)
    svc.scheduler.reload()
    return {"ok": True, "id": sid}


@router.put("/schedules/{schedule_id}")
def update_schedule(request: Request, schedule_id: int, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    existing = svc.db.get_schedule(schedule_id)
    if existing is None:
        raise HTTPException(404, "Расписание не найдено")
    if user.get("role") == "mailbox":
        _ensure_account_access(user, existing["account_id"])
        _ensure_account_access(user, body.account_id)
    svc.db.update_schedule(schedule_id, kind=body.kind, job_type=body.job_type, cron_expr=body.cron_expr,
                           interval_seconds=body.interval_seconds, enabled=body.enabled, options=body.options)
    svc.scheduler.reload()
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(request: Request, schedule_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    existing = svc.db.get_schedule(schedule_id)
    if existing and user.get("role") == "mailbox":
        _ensure_account_access(user, existing["account_id"])
    svc.db.delete_schedule(schedule_id)
    svc.scheduler.reload()
    return {"ok": True}


# =====================================================================
#  Настройки и справка
# =====================================================================
@router.get("/help")
def get_help(user: dict = Depends(auth_mod.require_user)):
    return all_help()


@router.get("/settings")
def get_settings(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from .i18n import SETTINGS_SECTIONS
    result = {}
    for sec in SETTINGS_SECTIONS:
        section = sec["section"]
        result[section] = {}
        for key in sec["keys"]:
            result[section][key] = svc.rt(section, key)
    return {"values": result, "sections": SETTINGS_SECTIONS, "help": all_help()}


@router.put("/settings")
def update_settings(request: Request, body: SettingsBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..config import DEFAULTS
    changed = []
    for full_key, value in body.values.items():
        if "." not in full_key:
            continue
        section, key = full_key.split(".", 1)
        # Валидация типа по значению по умолчанию (защита от «битых» настроек,
        # которые могли бы, например, остановить очередь).
        default = DEFAULTS.get(section, {}).get(key)
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            try:
                value = int(value)
            except (ValueError, TypeError):
                raise ValidationError(f"Параметр «{full_key}» должен быть целым числом.")
        elif isinstance(default, float):
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise ValidationError(f"Параметр «{full_key}» должен быть числом.")
        svc.db.set_setting(full_key, value)
        changed.append(full_key)
    svc.db.add_audit(user["username"], "settings_update", ", ".join(changed[:20]))
    # применить то, что можно на лету
    svc.scheduler.reload()
    return {"ok": True, "changed": changed}


# =====================================================================
#  Логи, статистика, аудит
# =====================================================================
@router.get("/logs")
def get_logs(limit: int = 300, level: Optional[str] = None, user: dict = Depends(auth_mod.require_user)):
    from ..logging_setup import memory_handler
    return memory_handler.tail(limit=limit, level=level)


@router.get("/stats")
def get_stats(request: Request, days: int = 30, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    series = svc.db.daily_series(days=days)
    return {
        "series": [{"day": r["day"], "messages": r["messages"], "bytes": r["bytes"],
                    "jobs": r["jobs"], "errors": r["errors"]} for r in series][::-1],
        "totals": {"messages": svc.db.count_messages(), "bytes": svc.db.sum_message_bytes()},
    }


@router.get("/audit")
def get_audit(request: Request, limit: int = 200, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return [{"ts": r["ts"], "user": r["user"], "action": r["action"], "detail": r["detail"]}
            for r in svc.db.list_audit(limit)]


# =====================================================================
#  Пользователи (только администратор)
# =====================================================================
@router.get("/users")
def list_users(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return [{"id": r["id"], "username": r["username"], "role": r["role"], "created_at": r["created_at"],
             "last_login": r["last_login"], "disabled": bool(r["disabled"])} for r in svc.db.list_users()]


@router.post("/users")
def create_user(request: Request, body: UserBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    if svc.db.get_user_by_name(body.username):
        raise ValidationError("Пользователь с таким именем уже существует.")
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    uid = svc.db.create_user(body.username.strip(), hash_password(body.password), body.role)
    svc.db.add_audit(user["username"], "user_create", body.username)
    return {"ok": True, "id": uid}


@router.put("/users/{user_id}/password")
def set_user_password(request: Request, user_id: int, body: PasswordBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    svc.db.set_user_password(user_id, hash_password(body.password))
    return {"ok": True}


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int, disabled: bool = True, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    svc.db.set_user_disabled(user_id, disabled)
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    if user_id == user["id"]:
        raise ValidationError("Нельзя удалить самого себя.")
    if svc.db.count_users() <= 1:
        raise ValidationError("Нельзя удалить последнего пользователя.")
    svc.db.delete_user(user_id)
    svc.db.add_audit(user["username"], "user_delete", str(user_id))
    return {"ok": True}
