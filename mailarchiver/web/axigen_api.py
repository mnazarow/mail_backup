"""
REST API раздела «Сервер Axigen» (только администратор).

Логика — в :mod:`mailarchiver.axigen`; здесь приём запросов, права, аудит.
Всё, что меняет сервер Axigen, дополнительно требует «Разрешить изменения на
сервере» в настройках (проверяется в :mod:`mailarchiver.axigen.actions`).
"""
from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..errors import ValidationError
from ..logging_setup import get_logger
from ..models import JobType
from . import auth as auth_mod

log = get_logger("web.axigen")
router = APIRouter(prefix="/api/axigen")


def _svc(request: Request):
    return request.app.state.services


def _settings(svc):
    from ..axigen.common import load_settings
    return load_settings(svc)


# ---------------------------------------------------------------------------
#  Состояние раздела
# ---------------------------------------------------------------------------
@router.get("/status")
def axigen_status(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Настройки (без паролей), последнее известное состояние сервера, проблемы, сверка."""
    from ..axigen import monitor
    from ..axigen import store as axstore
    from ..axigen.common import is_loopback, pretty_fingerprint
    svc = _svc(request)
    st = _settings(svc)
    rows = axstore.list_accounts(svc.db)
    live = [r for r in rows if not r.get("deleted_at")]
    last_sync = svc.db.query_one(
        "SELECT id, status, finished_at, result FROM jobs WHERE type=? AND status IN ('success','partial','failed') "
        "ORDER BY id DESC LIMIT 1", (JobType.AXIGEN_SYNC,))
    running = next((dict(j) for j in svc.db.active_jobs() if j["type"] == JobType.AXIGEN_SYNC), None)
    sync = None
    if last_sync is not None:
        try:
            result = json.loads(last_sync["result"] or "{}")
        except (TypeError, ValueError):
            result = {}
        sync = {"job_id": last_sync["id"], "status": last_sync["status"], "finished_at": last_sync["finished_at"],
                "summary": result.get("summary", "")}
    return {
        "enabled": st.enabled, "configured": st.configured,
        "settings": {"host": st.host, "cli_port": st.cli_port, "cli_tls": st.cli_tls, "ftp_port": st.ftp_port,
                     "ftp_tls": st.ftp_tls, "admin_user": st.admin_user, "imap_host": st.imap_host or st.host,
                     "domains": st.domains, "allow_changes": st.allow_changes, "loopback": is_loopback(st.host),
                     "allow_plaintext": st.allow_plaintext,
                     "cli_cert": pretty_fingerprint(st.cli_cert_sha256), "ftp_cert": pretty_fingerprint(st.ftp_cert_sha256),
                     "sync_enabled": bool(svc.rt("axigen", "sync_enabled")),
                     "sync_cron": svc.rt("axigen", "sync_cron"),
                     "monitor_enabled": bool(svc.rt("axigen", "monitor_enabled"))},
        "status": monitor.cached(svc),
        "problems": monitor.problems(svc, st),
        "counts": {"accounts": len(live), "blocked": sum(1 for r in live if r.get("login_blocked")),
                   "deleted": sum(1 for r in rows if r.get("deleted_at")),
                   "domains": len({r["domain"] for r in live})},
        "last_sync": sync, "sync_running": running["id"] if running else None,
    }


@router.post("/check")
def axigen_check(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверить подключение к CLI и FTP Backup (по сохранённым настройкам)."""
    from ..axigen.service import check_connection
    svc = _svc(request)
    res = check_connection(svc, username=user["username"])
    svc.db.add_audit(user["username"], "axigen_check", "успешно" if res.get("ok") else
                     str(res.get("error") or (res.get("cli") or {}).get("error") or
                         (res.get("ftp") or {}).get("error") or "ошибка")[:300])
    return res


class TrustBody(BaseModel):
    which: str = Field("cli", max_length=8)          # cli | ftp
    fingerprint: str = Field("", max_length=200)
    #: пароль вводится заново: сохранённый не должен уйти серверу с новым сертификатом
    #: без подтверждения того, кто его знает (как при смене адреса сервера)
    password: str = Field("", max_length=200)


@router.post("/trust")
def axigen_trust(request: Request, body: TrustBody, user: dict = Depends(auth_mod.require_admin)):
    """Доверять сертификату сервера по отпечатку (самоподписанный сертификат Axigen).

    Отпечаток читается заново и должен совпасть с тем, что видел администратор:
    иначе он подтвердил бы не тот сертификат, который будет сохранён. Пароль
    этого канала вводится заново и сохраняется вместе с отпечатком: иначе
    администратор архива, не знающий пароля Axigen, мог бы закрепить сертификат
    своего сервера-посредника и получить сохранённый пароль.
    """
    from ..axigen.common import AxigenError, normalize_fingerprint, pretty_fingerprint
    from ..axigen.service import cli_fingerprint, ftp_fingerprint
    svc = _svc(request)
    st = _settings(svc)
    if body.which not in ("cli", "ftp"):
        raise ValidationError("Неизвестная служба: нужен cli или ftp.")
    wanted = normalize_fingerprint(body.fingerprint)
    if not wanted:
        raise ValidationError("Отпечаток — 64 шестнадцатеричных знака SHA-256.")
    if not st.host:
        raise ValidationError("Сначала задайте и сохраните адрес сервера Axigen.")
    if not body.password:
        raise ValidationError("Введите пароль заново — он будет сохранён вместе с отпечатком.",
                              hint="Сохранённый пароль не отправляется серверу с новым сертификатом без подтверждения.")
    if any(ord(c) < 32 or ord(c) == 127 for c in body.password):
        raise ValidationError("В пароле не должно быть управляющих знаков.")
    if body.which == "ftp" and st.ftp_tls == "none":
        raise ValidationError("У FTP Backup не включено шифрование — доверять нечему.",
                              hint="Выберите «TLS сразу» или «AUTH TLS» и сохраните настройки.")
    if body.which == "cli" and st.cli_tls != "tls":
        raise ValidationError("У CLI не включён SSL — доверять нечему.",
                              hint="Выберите «CLI через SSL» и сохраните настройки.")
    try:
        actual = normalize_fingerprint(cli_fingerprint(st) if body.which == "cli" else ftp_fingerprint(st))
    except AxigenError as exc:
        raise ValidationError(exc.message, hint=exc.hint) from exc
    if not actual:
        raise ValidationError("Не удалось прочитать сертификат сервера — проверьте адрес, порт и шифрование.")
    if actual != wanted:
        raise ValidationError(
            f"Сервер сейчас предъявляет другой сертификат ({pretty_fingerprint(actual)}), чем тот, что вы "
            f"подтверждаете ({pretty_fingerprint(wanted)}).",
            hint="Повторите проверку подключения и сверьте отпечаток с сервером ещё раз. Ничего не сохранено.")
    key = "axigen.cli_cert_sha256" if body.which == "cli" else "axigen.ftp_cert_sha256"
    # пароль этого канала: у FTP Backup — свой, если задан отдельный пользователь FTP
    secret = "axigen.ftp_password" if body.which == "ftp" and st.ftp_password else "axigen.admin_password"
    svc.db.set_settings_many({key: pretty_fingerprint(actual), secret: body.password})
    svc.apply_runtime_settings()
    svc.db.add_audit(user["username"], "axigen_trust", f"{body.which}: {pretty_fingerprint(actual)}")
    return {"ok": True, "fingerprint": pretty_fingerprint(actual)}


@router.post("/refresh")
def axigen_refresh(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Прочитать состояние сервера сейчас (версия, лицензия, домены, очередь)."""
    from ..axigen import monitor
    svc = _svc(request)
    status = monitor.refresh(svc, username=user["username"])
    return {"status": status, "problems": monitor.problems(svc, status=status)}


# ---------------------------------------------------------------------------
#  Учётные записи
# ---------------------------------------------------------------------------
@router.get("/accounts")
def axigen_accounts(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Учётные записи последней сверки вместе с ящиками архива."""
    from ..axigen import directory
    svc = _svc(request)
    return directory.overview(svc, _settings(svc))


@router.get("/accounts/export")
def axigen_accounts_export(request: Request, format: str = "xlsx", user: dict = Depends(auth_mod.require_admin)):
    from ..axigen.report import export_accounts
    svc = _svc(request)
    fmt = (format or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        raise ValidationError("Формат — xlsx или csv.")
    data, filename, ctype = export_accounts(svc, fmt)
    svc.db.add_audit(user["username"], "axigen_export", fmt)
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.post("/sync")
def axigen_sync(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Сверить учётные записи сервера с архивом (задание в очереди)."""
    svc = _svc(request)
    _settings(svc).require()
    running = next((j for j in svc.db.active_jobs() if j["type"] == JobType.AXIGEN_SYNC), None)
    if running is not None:
        return {"ok": True, "job_id": running["id"], "already": True}
    job_id = svc.queue.enqueue(JobType.AXIGEN_SYNC, None, {"requested_by": user["username"]}, priority=3,
                               created_by=user["username"])
    svc.db.add_audit(user["username"], "axigen_sync_start", f"задание №{job_id}")
    return {"ok": True, "job_id": job_id}


class EmailBody(BaseModel):
    email: str = Field("", max_length=320)


@router.post("/accounts/reload")
def axigen_account_reload(request: Request, body: EmailBody, user: dict = Depends(auth_mod.require_admin)):
    """Перечитать одну учётную запись с сервера (не дожидаясь общей сверки)."""
    from ..axigen import directory
    from ..axigen import store as axstore
    from ..axigen.actions import parse_email
    from ..axigen.service import logged_session
    svc = _svc(request)
    st = _settings(svc).require()
    email = (body.email or "").strip().lower()
    local, domain = parse_email(email)
    if not st.domain_allowed(domain):
        raise ValidationError(f"Домен {domain} не входит в домены из настроек Axigen.")
    with logged_session(svc, username=user["username"], kind="read", summary=f"Учётная запись {email}",
                        st=st) as cli:
        names = directory.list_account_names(cli, domain)
        if local not in names and local.lower() not in {n.lower() for n in names}:
            row = axstore.get_account(svc.db, email)
            if row is not None and not row.get("deleted_at"):
                axstore.note_deleted(svc.db, email)
            raise ValidationError(f"Учётной записи {email} на сервере нет.")
        name = next((n for n in names if n.lower() == local.lower()), local)
        info = directory.account_details(cli, domain, name)
    axstore.save_snapshot(svc.db, [info], [])
    return {"ok": True, "account": axstore.get_account(svc.db, email)}


@router.get("/mailbox/{account_id}")
def axigen_mailbox(account_id: int, request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Сведения сервера Axigen о ящике архива (по последней сверке) — для карточки ящика."""
    from ..axigen import directory
    from ..axigen import store as axstore
    from ..axigen.jobs import server_row
    svc = _svc(request)
    st = _settings(svc)
    acc = svc.db.get_account(int(account_id))
    if acc is None or not st.enabled:
        return {"row": None, "enabled": st.enabled}
    row = server_row(axstore.list_accounts(svc.db), acc)
    return {"row": row, "enabled": True, "same_server": directory.is_axigen_mailbox(acc, st),
            "ftp_path": axstore.ftp_path(svc.db, acc.id), "ftp": axstore.ftp_stats(svc.db, acc.id)}


class MailboxesBody(BaseModel):
    emails: List[str] = Field(default_factory=list)
    auth: str = Field("", max_length=16)
    enable: bool = True
    preview: bool = True


@router.post("/mailboxes")
def axigen_add_mailboxes(request: Request, body: MailboxesBody, user: dict = Depends(auth_mod.require_admin)):
    """Завести в архиве ящики для учётных записей сервера (на сервере ничего не меняется)."""
    from ..axigen.jobs import add_mailboxes
    from ..models import AuthType
    svc = _svc(request)
    st = _settings(svc)
    if not st.enabled:
        raise ValidationError("Работа с сервером Axigen выключена.", hint="Включите её в «Настройки → Сервер Axigen».")
    auth = body.auth or str(svc.rt("axigen", "new_mailbox_auth") or AuthType.AXIGEN_FTP)
    if auth not in (AuthType.AXIGEN_FTP, AuthType.PASSWORD):
        raise ValidationError("Способ копирования — через FTP Backup или по паролю.")
    if not body.emails:
        raise ValidationError("Не выбрано ни одной учётной записи.")
    if len(body.emails) > 20000:
        raise ValidationError("За раз — не больше 20 000 учётных записей.")
    items = add_mailboxes(svc, body.emails, user=user["username"], auth=auth, enable=body.enable,
                          preview=body.preview)
    created = [i for i in items if i["status"] == "ok"]
    if not body.preview and created:
        svc.db.add_audit(user["username"], "axigen_add_mailboxes", f"заведено ящиков: {len(created)}")
    return {"ok": True, "items": items, "created": 0 if body.preview else len(created),
            "would_create": len(created) if body.preview else 0}


class CreateBody(BaseModel):
    email: str = Field("", max_length=320)
    password: str = Field("", max_length=128)
    generate: bool = False
    first_name: str = Field("", max_length=200)
    last_name: str = Field("", max_length=200)
    add_to_archive: bool = True


@router.post("/accounts/create")
def axigen_create(request: Request, body: CreateBody, user: dict = Depends(auth_mod.require_admin)):
    """Создать учётную запись на сервере Axigen (и, по желанию, ящик в архиве)."""
    from ..axigen import actions
    from ..axigen.jobs import create_account
    svc = _svc(request)
    password = actions.random_password() if body.generate else body.password
    res = create_account(svc, user=user["username"], email=body.email, password=password,
                         first_name=body.first_name, last_name=body.last_name, add_to_archive=body.add_to_archive)
    if body.generate:
        res["password"] = password          # показывается один раз — чтобы передать сотруднику
    return res


class PasswordSetBody(BaseModel):
    email: str = Field("", max_length=320)
    password: str = Field("", max_length=128)
    generate: bool = False


@router.post("/accounts/password")
def axigen_password(request: Request, body: PasswordSetBody, user: dict = Depends(auth_mod.require_admin)):
    """Задать учётной записи пароль на сервере (свой или случайный — он показывается один раз)."""
    from ..axigen import actions
    from ..axigen.jobs import set_account_password
    svc = _svc(request)
    password = actions.random_password() if body.generate else body.password
    res = set_account_password(svc, user=user["username"], email=body.email, password=password)
    if body.generate:
        res["password"] = password
    return res


# ---------------------------------------------------------------------------
#  Консоль, диагностика, очередь, журнал
# ---------------------------------------------------------------------------
class ConsoleBody(BaseModel):
    commands: List[str] = Field(default_factory=list)


@router.post("/console")
def axigen_console(request: Request, body: ConsoleBody, user: dict = Depends(auth_mod.require_admin)):
    """Команды CLI только для чтения (LIST, SHOW…) — одной сессией."""
    from ..axigen.service import run_console
    svc = _svc(request)
    _settings(svc).require()
    res = run_console(svc, username=user["username"], commands=body.commands)
    svc.db.add_audit(user["username"], "axigen_console", "; ".join(body.commands)[:300])
    return res


@router.get("/diagnostics")
def axigen_diagnostics(request: Request, email: str = "", user: dict = Depends(auth_mod.require_admin)):
    """Сырые ответы ключевых команд — как сервер их пишет (для разбора непривычного формата)."""
    from ..axigen.service import diagnostics
    svc = _svc(request)
    _settings(svc).require()
    return diagnostics(svc, username=user["username"], email=(email or "").strip())


@router.get("/queue")
def axigen_queue(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Очередь писем сервера (только чтение)."""
    from ..axigen.service import read_queue
    svc = _svc(request)
    _settings(svc).require()
    return read_queue(svc, username=user["username"])


@router.get("/log")
def axigen_log(request: Request, kind: str = "", before: int = 0, limit: int = 100,
               user: dict = Depends(auth_mod.require_admin)):
    from ..axigen import store as axstore
    svc = _svc(request)
    if kind and kind not in ("people", "read", "write", "console", "check", "monitor", "error"):
        raise ValidationError("Неизвестный вид записей журнала.")
    return {"items": axstore.list_log(svc.db, limit, kind=kind, before=before)}


@router.get("/log/{log_id}")
def axigen_log_item(log_id: int, request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..axigen import store as axstore
    svc = _svc(request)
    item = axstore.get_log(svc.db, log_id)
    if item is None:
        raise ValidationError("Запись журнала не найдена (возможно, уже вытеснена более новыми).")
    return item


__all__ = ["router"]
