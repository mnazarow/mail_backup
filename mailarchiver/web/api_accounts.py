"""API: почтовые ящики, копирование, выгрузка и восстановление, просмотр писем, доступ других сотрудников.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..employees import apply_passwords, parse_password_file
from ..errors import ValidationError
from ..accountops import after_account_retention_change, after_hold_set, credential_problem, hold_label
from ..imap.client import diagnose_folders, probe_account
from ..models import Account, AuthType, JobStatus, JobType, Security
from ..queue.manager import LOCAL_JOB_TYPES
from .. import roles
from ..util import human_size, safe_filename
from . import auth as auth_mod

from .api_common import (
    AccountBody, BackupBody, ExcludeFoldersBody, ExportBody, MAX_EMPLOYEE_UPLOAD_BYTES, MAX_PST_UPLOAD_BYTES,
    RestoreBody, RestoreMessagesBody, RetentionBody, _bounded_id, _ensure_account_access, _ensure_full_access,
    _ensure_mailbox_server_action, _mailbox_flood_guard, _mailbox_probe_guard, _operator_job_guard, _own_ids,
    _require_not_mailbox, log, svc_dep,
)
from .api_state import _account_view
from .api_jobs import backup_all_options, validate_restore_options

router = APIRouter(prefix="/api")

# =====================================================================
#  Аккаунты
# =====================================================================
def _account_from_body(body: AccountBody, account_id: Optional[int] = None,
                       before: Optional[Account] = None) -> Account:
    """Ящик из тела запроса; поля, которых нет в запросе, берутся у ``before``."""
    def keep(value, old, default):
        if value is not None:
            return value
        return old if before is not None else default

    oauth_client_id = keep(body.oauth_client_id, before.oauth_client_id if before else "", "")
    oauth_token_url = keep(body.oauth_token_url, before.oauth_token_url if before else "", "")
    if before is not None and body.auth_type == AuthType.OAUTH2:
        # пустое поле (старая версия интерфейса не знала этих значений) не
        # затирает сохранённое — как и с секретами
        oauth_client_id = oauth_client_id or before.oauth_client_id
        oauth_token_url = oauth_token_url or before.oauth_token_url
    acc = Account(
        id=account_id, name=body.name.strip(), host=body.host.strip(), port=int(body.port),
        username=body.username.strip(), password=body.password, auth_type=body.auth_type,
        security=body.security, enabled=body.enabled,
        folder_include=[str(f).strip() for f in keep(body.folder_include, before.folder_include if before else [], [])
                        if str(f).strip()],
        folder_exclude=[str(f).strip() for f in keep(body.folder_exclude, before.folder_exclude if before else [], [])
                        if str(f).strip()],
        oauth_client_id=(oauth_client_id or "").strip(), oauth_client_secret=body.oauth_client_secret,
        oauth_refresh_token=body.oauth_refresh_token, oauth_token_url=(oauth_token_url or "").strip(),
        notes=keep(body.notes, before.notes if before else "", ""),
        retention_days=keep(body.retention_days, before.retention_days if before else -1, -1),
    )
    if acc.auth_type == AuthType.SERVER:
        acc.server_id = keep(body.server_id, getattr(before, "server_id", None) if before else None, None)
    return acc


def _validate_server_auth(svc, acc: Account) -> None:
    """Ящик «через подключение к почтовому серверу»: подключение есть, умеет входить без паролей, сервер — его."""
    if acc.auth_type != AuthType.SERVER:
        return
    from ..mailservers import service as mss
    from ..mailservers import store as msstore
    from ..mailservers.base import Cap
    conn = msstore.get_server(svc.db, acc.server_id) if acc.server_id else None
    if conn is None:
        raise ValidationError("Выберите подключение к почтовому серверу.",
                              hint="Подключения настраиваются в разделе «Почтовые серверы».")
    cls = mss.driver_class(conn.type)
    if not cls.can(Cap.IMAP_ADMIN):
        raise ValidationError(f"{cls.label}: копирование без паролей сотрудников не поддерживается — выберите вход "
                              f"по паролю.")
    drv = mss.driver(svc, conn)
    # токен приложения (или пароль администратора) уходит только на сервер этого подключения и только шифрованно
    problem = mss.imap_target_problem(drv, acc.host, acc.security)
    if problem:
        host = (drv.imap_server()[0] or "").strip().lower()
        raise ValidationError(problem[:1].upper() + problem[1:] + ".",
                              hint=f"Укажите сервер {host} с SSL/TLS или STARTTLS — или другой способ входа."
                              if host else "Задайте IMAP-сервер в подключении (раздел «Почтовые серверы»).")


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
    if acc.auth_type == AuthType.AXIGEN_FTP and "@" not in acc.username:
        raise ValidationError("Для копирования через FTP Backup Axigen логин — полный адрес почты.",
                              hint="Каталог ящика на FTP Backup ищется по адресу, например ivanov@company.ru.")
    if not 1 <= int(acc.port or 0) <= 65535:
        raise ValidationError("Порт IMAP — число от 1 до 65535.", hint="Обычно 993 (SSL) или 143 (STARTTLS).")
    if acc.retention_days is not None and not -1 <= int(acc.retention_days) <= 36500:
        raise ValidationError("Срок хранения — от 0 до 36500 дней (0 — хранить всё, −1 — как в общих настройках).")
    if len(acc.name) > 200 or len(acc.host) > 255 or len(acc.username) > 320 or len(acc.notes or "") > 5000:
        raise ValidationError("Слишком длинное значение в карточке ящика.",
                              hint="Название — до 200 символов, адрес сервера — до 255, логин — до 320, заметки — до 5000.")
    if len(acc.folder_include) > 1000 or len(acc.folder_exclude) > 1000:
        raise ValidationError("Слишком длинный список папок (больше 1000).")


@router.get("/accounts")
def list_accounts(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Список ящиков с состоянием копии: сколько писем, даты первой и последней
    удачной копии, итог последнего прогона и последней проверки входа."""
    svc = svc_dep(request)
    accounts = svc.db.list_accounts()
    if user.get("role") == "mailbox":
        own = set(_own_ids(user))
        accounts = [a for a in accounts if a.id in own]
    totals = svc.db.message_totals_by_account()
    last_runs = svc.db.last_runs_by_account()
    quarantines = svc.store.quarantine_counts() if user.get("role") != "mailbox" else {}
    owners = {aid: r["full_name"] for aid, r in svc.db.employees_by_account().items()} \
        if user.get("role") != "mailbox" else {}
    out = []
    for a in accounts:
        item = _account_view(a, user)
        item["employee_name"] = owners.get(a.id, "")
        item["quarantines"] = int(quarantines.get(a.id, 0))
        t = totals.get(a.id) or {}
        item["messages"] = int(t.get("messages", 0))
        item["bytes"] = int(t.get("bytes", 0))
        item["bytes_h"] = human_size(item["bytes"])
        run = last_runs.get(a.id)
        item["last_run"] = ({"type": run["type"], "status": run["status"],
                             "started_at": run["started_at"], "finished_at": run["finished_at"],
                             "messages_new": run["messages_new"], "errors": run["errors"]}
                            if run is not None else None)
        out.append(item)
    return out


class CheckLoginsBody(BaseModel):
    account_ids: Optional[List[int]] = None
    only_enabled: bool = False


@router.post("/accounts/check-logins")
def check_logins(request: Request, body: Optional[CheckLoginsBody] = None,
                 user: dict = Depends(auth_mod.require_staff)):
    """Проверить пароли (вход) всех ящиков — или перечисленных — фоновым заданием."""
    svc = svc_dep(request)
    body = body or CheckLoginsBody()
    busy = [j for j in svc.db.active_jobs() if j["type"] == JobType.CHECK_LOGINS]
    if busy:
        return {"ok": True, "job_id": busy[0]["id"], "already": True}
    params: Dict[str, Any] = {"only_enabled": bool(body.only_enabled)}
    if body.account_ids:
        if len(body.account_ids) > 10000:
            raise ValidationError("Слишком длинный список ящиков.")
        params["account_ids"] = sorted({int(i) for i in body.account_ids})
    jid = svc.queue.enqueue(JobType.CHECK_LOGINS, None, params, priority=3, created_by=user["username"])
    svc.db.add_audit(user["username"], "check_logins",
                     f"ящиков: {len(params.get('account_ids') or []) or 'все'}")
    return {"ok": True, "job_id": jid}


@router.get("/accounts/{account_id}/runs")
def account_runs(request: Request, account_id: int, limit: int = 100,
                 user: dict = Depends(auth_mod.require_user)):
    """История копирования ящика: даты прогонов и их итоги."""
    svc = svc_dep(request)
    _ensure_account_access(user, _bounded_id(account_id))
    limit = max(1, min(500, int(limit)))
    return [{"id": r["id"], "type": r["type"], "status": r["status"], "started_at": r["started_at"],
             "finished_at": r["finished_at"], "messages_new": r["messages_new"],
             "bytes_new": r["bytes_new"], "bytes_new_h": human_size(r["bytes_new"] or 0),
             "errors": r["errors"], "detail": r["detail"] or ""}
            for r in svc.db.list_runs(account_id, limit=limit)]


@router.post("/accounts")
def create_account(request: Request, body: AccountBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    acc = _account_from_body(body)
    _validate_account(acc)
    _validate_server_auth(svc, acc)
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
    data = _account_view(acc, user)
    data["folders"] = [{"folder": r["folder"], "count": r["cnt"], "bytes": r["bytes"],
                        "bytes_h": human_size(r["bytes"])} for r in svc.db.folders_summary(account_id)]
    if user.get("role") in roles.STAFF_ROLES:
        # сколько писем копирование пропускает по сроку хранения (окно «Хранение копий»)
        data["retired"] = svc.db.count_retired(account_id)
    return data


@router.put("/accounts/{account_id}")
def update_account(request: Request, account_id: int, body: AccountBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    if svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    before = svc.db.get_account(account_id)
    acc = _account_from_body(body, account_id, before)
    _validate_account(acc)
    _validate_server_auth(svc, acc)
    # Каждый секрет обновляется независимо: пустое поле НЕ затирает сохранённый секрет.
    svc.db.update_account(
        acc,
        update_password=bool(body.password),
        update_oauth_secret=bool(body.oauth_client_secret),
        update_oauth_token=bool(body.oauth_refresh_token),
    )
    _after_account_retention_change(svc, before, acc.retention_days)
    if before is not None and (before.username.lower() != acc.username.lower() or before.auth_type != acc.auth_type):
        # каталог ящика на FTP Backup искался по прежнему логину — ищется заново
        try:
            from ..axigen import store as axstore
            axstore.forget_ftp_path(svc.db, account_id)
        except Exception:  # noqa: BLE001
            log.debug("Не удалось забыть путь ящика на FTP Backup", exc_info=True)
    closed = 0
    if body.password or (before is not None and before.username.lower() != acc.username.lower()):
        # Новый пароль (или другой логин) ящика: сеансы сотрудника, вошедшего
        # по старому паролю, завершаем — как при смене пароля пользователя.
        closed = svc.db.delete_mailbox_sessions(account_id)
    svc.db.add_audit(user["username"], "account_update", acc.name + (f" (завершено сеансов: {closed})" if closed else ""))
    return {"ok": True, "sessions_closed": closed}


@router.post("/accounts/{account_id}/logout-sessions")
def logout_mailbox_sessions(request: Request, account_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Завершить сеансы сотрудника, вошедшего в интерфейс по паролю этого ящика."""
    svc = svc_dep(request)
    acc = svc.require_account(_bounded_id(account_id))
    closed = svc.db.delete_mailbox_sessions(account_id)
    svc.db.add_audit(user["username"], "account_logout_sessions", f"{acc.name}: {closed}")
    return {"ok": True, "closed": closed}


@router.post("/accounts/{account_id}/exclude-folders")
def exclude_account_folders(request: Request, account_id: int, body: ExcludeFoldersBody,
                            user: dict = Depends(auth_mod.require_user)):
    """Добавить папки в «Пропускать папки» этого ящика.

    Нужен для папок, которые почтовый сервер не даёт открыть и починить нельзя:
    без исключения каждый прогон бесконечно помечался бы «копия неполная», и на
    этом фоне настоящая пропажа писем осталась бы незамеченной. Правим только
    список исключений — остальные поля ящика (и его пароль) не трогаем.
    """
    svc = svc_dep(request)
    _require_not_mailbox(user)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    wanted = [str(f).strip() for f in (body.folders or []) if str(f).strip()]
    if not wanted:
        raise ValidationError("Не указано ни одной папки.",
                              hint="Передайте список имён папок в поле «folders».")
    existing = list(acc.folder_exclude or [])
    added = [f for f in wanted if f not in existing]
    acc.folder_exclude = existing + added
    svc.db.update_account(acc, update_password=False, update_oauth_secret=False,
                          update_oauth_token=False)
    svc.db.add_audit(user["username"], "account_exclude_folders",
                     f"{acc.name}: {', '.join(added) or 'без изменений'}"[:500])
    return {"ok": True, "added": added, "folder_exclude": acc.folder_exclude}


@router.delete("/accounts/{account_id}")
def delete_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    if acc.on_hold():
        # Без ящика и индекса архив недоступен ни в просмотре, ни в поиске, ни для
        # выгрузки — удержание (увольнение, проверка) защищает и от этого.
        raise ValidationError(f"Архив ящика удерживается ({hold_label(acc.hold_until)}) — ящик не удаляется.",
                              hint="Если удержание больше не нужно, сначала снимите его (меню ящика → «Удержание архива»).")
    if any(j["account_id"] == account_id and j["status"] == JobStatus.RUNNING for j in svc.db.active_jobs()) or \
            account_id in svc.queue.held_accounts():
        # Копирование, которое пишет в индекс удалённого ящика, падало бы на
        # каждом письме, а файлы оставались бы сиротами.
        raise ValidationError("По ящику выполняется задание — дождитесь его окончания или отмените.",
                              hint="Ящик может копироваться в проходе «все ящики по очереди».")
    svc.db.delete_account(account_id)
    svc.db.add_audit(user["username"], "account_delete", acc.name)
    return {"ok": True}


class HoldBody(BaseModel):
    #: ГГГГ-ММ-ДД, «forever» (бессрочно) или пусто (снять удержание)
    until: str = Field("", max_length=20)


@router.post("/accounts/{account_id}/hold")
def set_hold(request: Request, account_id: int, body: HoldBody, user: dict = Depends(auth_mod.require_admin)):
    """Удержание архива: пока оно действует, письма ящика не удаляются по сроку хранения."""
    from ..employees import HOLD_FOREVER
    from ..util import parse_day
    svc = svc_dep(request)
    acc = svc.require_account(account_id)
    if acc.hold_reason == "case":
        from ..cases import hold_cases_text
        raise ValidationError(f"Архив ящика удерживается {hold_cases_text(svc, account_id) or 'делом'} — "
                              f"удержание снимается закрытием дела.",
                              hint="Раздел «Дела»: закройте дело или уберите из него ящик.")
    raw = (body.until or "").strip().lower()
    if raw in ("forever", "бессрочно"):
        until = HOLD_FOREVER
    elif raw:
        day = parse_day(raw)
        if day.isoformat() < datetime.now(timezone.utc).date().isoformat():
            raise ValidationError("Дата окончания удержания уже прошла.")
        until = day.isoformat()
    else:
        until = ""
    svc.db.set_account_hold(account_id, until, "manual" if until else "")
    if until:
        after_hold_set(svc, account_id)
    svc.db.add_audit(user["username"], "account_hold",
                     f"{acc.name}: " + ("снято" if not until else ("бессрочно" if until == HOLD_FOREVER else f"до {until}")))
    return {"ok": True, "hold_until": until}


class PurgeBody(BaseModel):
    confirm_name: str = Field("", max_length=200)


@router.post("/accounts/{account_id}/purge")
def purge_account(request: Request, account_id: int, body: PurgeBody, user: dict = Depends(auth_mod.require_admin)):
    """Удалить ящик ВМЕСТЕ с архивом писем на диске (например, когда истекло
    удержание архива уволенного сотрудника). Необратимо."""
    svc = svc_dep(request)
    acc = svc.require_account(account_id)
    if (body.confirm_name or "").strip() != acc.name.strip():
        raise ValidationError("Для подтверждения введите название ящика точно.")
    if acc.on_hold():
        raise ValidationError(f"Архив ящика удерживается ({hold_label(acc.hold_until)}) — удалить его нельзя.",
                              hint="Если удержание больше не нужно, сначала снимите его (меню ящика → «Удержание архива»).")
    if any(j["account_id"] == account_id for j in svc.db.active_jobs()) or \
            account_id in svc.queue.held_accounts():
        raise ValidationError("По ящику выполняется задание — дождитесь его окончания или отмените.",
                              hint="Ящик может копироваться в проходе «все ящики по очереди».")
    if not svc.queue.acquire_account(account_id, -account_id):
        raise ValidationError("По ящику выполняется задание — дождитесь его окончания или отмените.")
    try:
        files, size = svc.store.delete_account_files(account_id)
        try:
            import shutil
            shutil.rmtree(svc.store.account_dir(account_id), ignore_errors=True)
        except OSError:
            pass
        svc.db.delete_account(account_id)
    finally:
        svc.queue.release_account(account_id, -account_id)
    svc.db.add_audit(user["username"], "account_purge", f"{acc.name}: удалено файлов {files} ({human_size(size)})")
    return {"ok": True, "files": files, "bytes": size}


@router.post("/accounts/{account_id}/test")
def test_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    _ensure_mailbox_server_action(svc, user, account_id)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    if acc.auth_type == AuthType.AXIGEN_FTP:
        # ящик Axigen проверяется тем же путём, каким копируется, — через FTP Backup,
        # входом администратора Axigen: это только администратору MailArchiver
        # (ответ перечисляет чужие каталоги, а частые входы могли бы заблокировать
        # учётную запись администратора на сервере)
        if not auth_mod.is_admin(user):
            raise HTTPException(403, "Проверить ящик, который копируется через FTP Backup Axigen, может только "
                                     "администратор.")
        from ..axigen.ftpbackup import probe as axigen_probe
        result = axigen_probe(svc, acc)
    else:
        with _mailbox_probe_guard(svc, user, acc.id):
            result = probe_account(acc, svc.connect_options())
    # итог попытки входа запоминаем у ящика (отбор «с неправильным паролем»)
    svc.db.set_login_status(acc.id, "ok" if result.get("ok") else (result.get("kind") or "conn_error"),
                            "" if result.get("ok") else (result.get("error") or ""))
    return result


@router.post("/accounts/import-passwords")
def import_account_passwords(request: Request, file: UploadFile = File(...),
                             enable: str = Form("false"),
                             user: dict = Depends(auth_mod.require_admin)):
    """Массово проставить пароли ящикам из файла «адрес — пароль».

    Ящики, заведённые сотрудникам автоматически, создаются без пароля: без этой
    загрузки администратору пришлось бы открывать сотни карточек по одной.
    Пароли сохраняются в базе зашифрованными, в журнал и в ответ не попадают —
    наружу уходят только счётчики и адреса, для которых ящик не нашёлся.
    """
    svc = svc_dep(request)
    origin_name = os.path.basename(file.filename or "")
    fname = safe_filename(origin_name, default="passwords.xlsx")
    # uuid, а не PID: два администратора (или один в двух вкладках) с файлами
    # одинакового имени писали бы в один путь — и применился бы чужой файл.
    dest = os.path.join(svc.cfg.tmp_dir, f"passwords_{uuid.uuid4().hex}_{fname}")
    written = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_EMPLOYEE_UPLOAD_BYTES:
                    raise ValidationError(
                        f"Файл слишком большой: допустимо не более {human_size(MAX_EMPLOYEE_UPLOAD_BYTES)}.",
                        hint="В файле нужны всего две колонки — адрес и пароль.")
                fh.write(chunk)
        if not written:
            raise ValidationError("Файл пуст.",
                                  hint="Нужны две колонки: адрес ящика и пароль (XLSX или CSV).")
        rows, problems = parse_password_file(dest, origin_name or fname)
        result = apply_passwords(svc, rows, enable=str(enable).lower() in ("1", "true", "yes", "on"))
    finally:
        # Файл с паролями не должен остаться на диске ни при успехе, ни при ошибке.
        try:
            os.unlink(dest)
        except OSError:
            pass
    svc.db.add_audit(user["username"], "accounts_import_passwords",
                     f"файл={origin_name or fname} обновлено={result['updated']} "
                     f"включено={result['enabled']} не найдено={len(result['not_found'])}")
    return {"ok": True, "updated": result["updated"], "enabled": result["enabled"],
            "not_found": result["not_found"][:200], "not_found_count": len(result["not_found"]),
            "total_rows": result["total_rows"], "problems": problems[:200],
            "problem_count": len(problems)}


@router.get("/accounts/{account_id}/quarantines")
def list_account_quarantines(request: Request, account_id: int,
                             user: dict = Depends(auth_mod.require_admin)):
    """Карантинные копии ящика, оставшиеся после пересоздания «с нуля».

    Каталог со старыми письмами не удаляется сразу намеренно — на случай, если
    пересоздание пошло не так. Но без этого списка он лежал бы на диске вечно и
    незаметно.
    """
    from .. import quarantine as qmod
    svc = svc_dep(request)
    if svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    from ..queue.jobs import effective_retention_days
    acc = svc.db.get_account(account_id)
    days = effective_retention_days(svc, acc)
    items = svc.store.list_quarantines(account_id)
    active = _quarantine_jobs(svc, account_id)
    out = []
    for path, files, size in items:
        check = qmod.stored_check(svc.db, path)
        if check is not None:
            # «Вернуть» доступно, как только копия стала полной, — не дожидаясь нового сравнения
            check = dict(check, new_copy_complete=qmod.new_copy_complete(svc, account_id, path))
        safe, why = qmod.is_safe_to_delete(check, days)
        out.append({"path": path, "name": os.path.basename(path), "files": files, "bytes": size,
                    "bytes_h": human_size(size), "check": check, "check_label": qmod.check_label(check),
                    "safe": safe, "unsafe_reason": why})
    return {"quarantines": out,
            "job": ({"id": active[0]["id"], "type": active[0]["type"], "status": active[0]["status"]}
                    if active else None)}


def _quarantine_jobs(svc, account_id: int) -> List[Any]:
    """Задания, которые сейчас работают с прежними копиями ящика (или ждут очереди):
    сравнение, возврат писем, удаление."""
    out = []
    for job in svc.db.active_jobs():
        if job["account_id"] == account_id and job["type"] in (JobType.QUARANTINE_CHECK, JobType.QUARANTINE_RESCUE):
            out.append(job)
        elif job["type"] == JobType.CLEANUP:
            try:
                items = (json.loads(job["params"] or "{}") or {}).get("items") or []
            except (TypeError, ValueError):
                items = []
            if any(int(it.get("account_id") or 0) == account_id for it in items if isinstance(it, dict)):
                out.append(job)
    return out


class QuarantineBody(BaseModel):
    path: str
    #: удалить, даже если сравнение не подтвердило, что все письма есть в новой копии
    force: bool = False


class QuarantineCheckBody(BaseModel):
    #: пусто — все прежние копии ящика
    path: str = Field("", max_length=4096)
    #: True — не только сравнить, но и вернуть в архив письма, которых нет в новой копии
    rescue: bool = False


@router.post("/accounts/{account_id}/quarantines/check")
def check_account_quarantine(request: Request, account_id: int, body: QuarantineCheckBody,
                             user: dict = Depends(auth_mod.require_admin)):
    """Сравнить прежнюю копию с новой (или вернуть недостающие письма) — фоновым заданием."""
    svc = svc_dep(request)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    known = svc.store.quarantine_paths(account_id)
    if not known:
        raise ValidationError("У ящика нет прежних копий.")
    paths: List[str] = []
    if body.path:
        norm = os.path.normpath(body.path)
        paths = [p for p in known if os.path.normpath(p) == norm]
        if not paths:
            raise ValidationError("Такой прежней копии у этого ящика нет.", hint="Обновите список прежних копий.")
    job_type = JobType.QUARANTINE_RESCUE if body.rescue else JobType.QUARANTINE_CHECK
    for job in svc.db.active_jobs():
        if job["account_id"] == account_id and job["type"] in (JobType.QUARANTINE_CHECK, JobType.QUARANTINE_RESCUE):
            return {"ok": True, "job_id": job["id"], "already": True}
    job_id = svc.queue.enqueue(job_type, account_id, {"paths": paths}, priority=4, created_by=user["username"])
    svc.db.add_audit(user["username"], "quarantine_rescue" if body.rescue else "quarantine_check",
                     f"{acc.name}: {', '.join(os.path.basename(p) for p in (paths or known))}"[:500])
    return {"ok": True, "job_id": job_id}


@router.post("/accounts/{account_id}/quarantines/delete")
def delete_account_quarantine(request: Request, account_id: int, body: QuarantineBody,
                              user: dict = Depends(auth_mod.require_admin)):
    """Удалить карантинную копию — по явной команде администратора."""
    svc = svc_dep(request)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    if acc.on_hold():
        # Прежняя копия может хранить письма, которых уже нет ни на сервере, ни в
        # новой копии, — удержание архива (проверка, спор) защищает и её.
        raise ValidationError("Архив ящика удерживается — прежние копии не удаляются.",
                              hint="Если удержание больше не нужно, снимите его: меню ящика → «Удержание архива».")
    from .. import quarantine as qmod
    if body.path not in set(svc.store.quarantine_paths(account_id)):
        raise ValidationError("Такой карантинной копии у этого ящика нет.",
                              hint="Обновите список карантинных копий.")
    busy = _quarantine_jobs(svc, account_id) + [
        j for j in svc.db.active_jobs() if j["account_id"] == account_id and j["type"] in LOCAL_JOB_TYPES]
    if busy or account_id in svc.queue.held_accounts():
        # Возврат писем читает прежнюю копию прямо сейчас, копия «с нуля» может
        # сделать новую — удалять, пока по ящику идёт работа, нельзя.
        raise ValidationError("По ящику выполняется задание — прежнюю копию удалить сейчас нельзя.",
                              hint="Дождитесь окончания задания (раздел «Задания») и повторите.")
    from ..queue.jobs import effective_retention_days
    safe, why = qmod.is_safe_to_delete(qmod.stored_check(svc.db, body.path), effective_retention_days(svc, acc))
    if not safe and not body.force:
        raise ValidationError(f"Прежнюю копию не удаляем: {why}.",
                              hint="Если письма из неё точно не нужны, удалите с подтверждением потери писем.")
    # Удаление сотен тысяч файлов — минуты: фоновым заданием, как у группового действия.
    item = {"account_id": account_id, "name": acc.name, "what": "quarantine", "paths": [body.path],
            "only_safe": not body.force}
    job_id = svc.queue.enqueue(JobType.CLEANUP, None, {"items": [item]}, priority=4, created_by=user["username"])
    svc.db.add_audit(user["username"], "quarantine_delete_request",
                     (f"{acc.name}: {os.path.basename(body.path)}" + ("" if safe else f" — без проверки ({why})"))[:500])
    return {"ok": True, "job_id": job_id, "forced": bool(body.force and not safe)}


@router.post("/accounts/{account_id}/folders/diagnose")
def diagnose_account_folders(request: Request, account_id: int,
                             user: dict = Depends(auth_mod.require_user)):
    """Проверить каждую папку ящика: открывается ли она и что в ней.

    Отдельная кнопка, а не часть «Проверить подключение»: здесь на каждую
    папку идёт запрос к серверу, и на ящике с сотней папок это заметно дольше.
    Зато сразу видно, из-за каких именно папок копия считается неполной и
    теряются ли при этом письма.
    """
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    _ensure_mailbox_server_action(svc, user, account_id)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    with _mailbox_probe_guard(svc, user, acc.id):
        result = diagnose_folders(acc, svc.connect_options(),
                                  global_include=svc.rt("backup", "folder_include") or [],
                                  global_exclude=svc.rt("backup", "folder_exclude") or [])
    # Дополняем историей отказов: одно дело — папка сломалась сегодня, другое —
    # она не открывается третью неделю подряд.
    grace = int(svc.rt("backup", "unreadable_folder_grace_runs") or 0)
    problems = {row["folder"]: row for row in svc.db.list_folder_problems(account_id)}
    for row in result.get("folders") or []:
        problem = problems.get(row["name"])
        if problem is None:
            continue
        row["fails"] = int(problem["fails"])
        row["since"] = (problem["first_failed"] or "")[:10]
        if row.get("verdict") == "broken" and grace and row["fails"] > grace:
            row["detail"] += (f"; не открывается {row['fails']} прогонов подряд"
                              f"{' с ' + row['since'] if row['since'] else ''} — "
                              f"заданием больше не считается ошибкой")
    result["grace_runs"] = grace
    svc.db.add_audit(user["username"], "account_folders_diagnose",
                     f"{acc.name}: проблемных папок {len(result.get('broken_folders') or [])}")
    return result


@router.post("/accounts/{account_id}/backup")
def start_backup(request: Request, account_id: int, body: Optional[BackupBody] = None,
                 user: dict = Depends(auth_mod.require_user)):
    """Поставить копирование в очередь.

    По умолчанию копия инкрементная. ``rebuild=missing`` возвращает письма,
    файлы которых потерялись на диске; ``rebuild=full`` стирает локальную копию
    ящика и качает всё заново — это необратимо, поэтому доступно только
    администратору и записывается в аудит.
    """
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    _ensure_mailbox_server_action(svc, user, account_id)
    acc = svc.require_account(account_id, need_secret=True)
    rebuild = (body.rebuild if body else "").strip().lower()
    if rebuild not in ("", "missing", "full"):
        raise ValidationError(f"Неизвестный режим пересоздания копии: «{rebuild}».",
                              hint="Допустимо: пусто (обычная копия), missing, full.")
    if rebuild == "full":
        # Стирание локальной копии — не то действие, которое можно доверить
        # владельцу ящика или оператору: письма, удалённые на сервере, после
        # него не вернуть.
        if (user.get("role") or "") != roles.ROLE_ADMIN:
            raise HTTPException(403, "Полное пересоздание копии доступно только администратору")
        if acc.on_hold():
            raise ValidationError(f"Архив ящика удерживается ({hold_label(acc.hold_until)}) — копию «с нуля» не делаем.",
                                  hint="Если удержание больше не нужно, снимите его (меню ящика → "
                                       "«Удержание архива»). Потерянные письма вернёт режим «Докачать потерянные».")
        svc.db.add_audit(user["username"], "backup_rebuild_full_request", acc.name)
    _mailbox_flood_guard(svc, user, JobType.BACKUP, account_id)
    _operator_job_guard(svc, user, JobType.BACKUP, account_id, need_enabled=True, rebuild=rebuild)
    params = {"rebuild": rebuild} if rebuild else {}
    max_attempts = int(svc.rt("backup", "retry_attempts") or 1)
    # Пересоздание не повторяем автоматически: повтор «полной» копии стёр бы
    # уже скачанное во второй раз.
    if rebuild:
        max_attempts = 1
    jid = svc.queue.enqueue(JobType.BACKUP, account_id, params,
                            max_attempts=max_attempts, created_by=user["username"])
    return {"ok": True, "job_id": jid, "rebuild": rebuild}


class BackupAllBody(BaseModel):
    #: True — одним заданием, ящик за ящиком (бережно к почтовому серверу);
    #: False — по заданию на ящик, параллельно (сколько позволяет очередь)
    sequential: bool = False
    pause_seconds: int = 0
    stop_at: str = Field("", max_length=5)


@router.post("/accounts/backup-all")
def start_backup_all(request: Request, body: Optional[BackupAllBody] = None,
                     user: dict = Depends(auth_mod.require_staff)):
    """Поставить в очередь резервное копирование СРАЗУ ВСЕХ включённых ящиков.

    Выключенные ящики пропускаются (они намеренно исключены из копирования).
    Ящики, по которым копирование уже идёт или стоит в очереди, тоже
    пропускаются — чтобы повторное нажатие кнопки не удваивало работу.

    ``sequential`` — одно задание, которое копирует ящики строго по одному
    (как расписание «Копирование всех ящиков по очереди»).
    """
    svc = svc_dep(request)
    body = body or BackupAllBody()
    if body.sequential:
        options = backup_all_options({"pause_seconds": body.pause_seconds, "stop_at": body.stop_at})
        from ..queue.jobs import is_global_pass
        running = next((j for j in svc.db.active_jobs() if is_global_pass(j)), None)
        if running is not None:
            return {"ok": True, "sequential": True, "job_id": running["id"], "already": True}
        enabled = len(svc.db.list_accounts(only_enabled=True))
        jid = svc.queue.enqueue(JobType.BACKUP_ALL, None, options,
                                max_attempts=int(svc.rt("backup", "retry_attempts") or 1),
                                created_by=user["username"])
        svc.db.add_audit(user["username"], "backup_all", f"по очереди: ящиков {enabled}, задание №{jid}")
        return {"ok": True, "sequential": True, "job_id": jid, "total_enabled": enabled}
    max_attempts = int(svc.rt("backup", "retry_attempts") or 1)
    busy = {j["account_id"] for j in svc.db.active_jobs() if j["type"] == JobType.BACKUP}
    started, skipped = [], []
    for acc in svc.db.list_accounts(only_enabled=True):
        if acc.id in busy:
            skipped.append({"account_id": acc.id, "name": acc.name})
            continue
        jid = svc.queue.enqueue(JobType.BACKUP, acc.id, {}, max_attempts=max_attempts,
                                created_by=user["username"])
        started.append({"account_id": acc.id, "name": acc.name, "job_id": jid})
    svc.db.add_audit(user["username"], "backup_all",
                     f"запущено: {len(started)}, пропущено: {len(skipped)}")
    return {"ok": True, "started": started, "skipped": skipped,
            "total_enabled": len(started) + len(skipped)}


@router.post("/accounts/{account_id}/export")
def start_export(request: Request, account_id: int, body: ExportBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_full_access(user, account_id)
    svc.require_account(account_id)
    # Каждый экспорт — полная копия ящика на диске сервера: сотруднику — лимиты.
    _mailbox_flood_guard(svc, user, JobType.EXPORT, account_id)
    params = body.model_dump()
    params.pop("aspose_license_path", None)   # путь к лицензии — только из настроек
    params.pop("outlook_target", None)        # параметр ни на что не влиял
    if params.get("date_from") or params.get("date_to"):
        svc.day_bounds_utc(params.get("date_from"), params.get("date_to"))   # проверка формата дат
    jid = svc.queue.enqueue(JobType.EXPORT, account_id, params, created_by=user["username"])
    svc.db.add_audit(user["username"], "export_start", f"account={account_id} engine={params.get('engine')}")
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/restore")
def start_restore(request: Request, account_id: int, body: RestoreBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    _ensure_mailbox_server_action(svc, user, account_id)
    svc.require_account(account_id, need_secret=True)
    # Заливка в ИСХОДНЫЕ папки живого ящика возможна только явным режимом
    # "original". Раньше очистка поля «Префикс папки» молча приводила к тому
    # же результату: весь архив уходил прямо в рабочий INBOX, и отменить это
    # уже нельзя.
    data = validate_restore_options(body.model_dump())
    acc = svc.require_account(account_id)
    from ..queue.jobs import restore_via_ftp
    if restore_via_ftp(acc, data):
        # Заливка через FTP Backup идёт учётной записью администратора Axigen —
        # только администратору архива и только если изменения на сервере разрешены.
        if user.get("role") != "admin":
            raise ValidationError("Восстановление через FTP Backup Axigen доступно только администратору.",
                                  hint="У ящика нет пароля для восстановления по IMAP — обратитесь к администратору.")
        from ..axigen.actions import require_changes_allowed
        from ..axigen.common import load_settings as _axigen_settings
        require_changes_allowed(_axigen_settings(svc).require())
        data["ftp_confirmed_by"] = user["username"]
    _mailbox_flood_guard(svc, user, JobType.RESTORE, account_id)
    mode = data["target_mode"]
    if mode == "original" and not body.dry_run:
        svc.db.add_audit(user["username"], "restore_original", f"account={account_id}")
    jid = svc.queue.enqueue(JobType.RESTORE, account_id, data, created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/restore-messages")
def restore_messages(request: Request, account_id: int, body: RestoreMessagesBody,
                     user: dict = Depends(auth_mod.require_user)):
    """Вернуть на почтовый сервер отдельные письма из архива ящика ``account_id``.

    По умолчанию — в тот же ящик, в исходные папки писем (письма, которые там уже есть, не заливаются
    повторно). Администратор может вернуть их и в ДРУГОЙ ящик — например, переписку уволенного в ящик
    коллеги; это всегда пишется в аудит. Оператору недоступно: писем он не видит.
    """
    svc = svc_dep(request)
    _bounded_id(account_id)
    _ensure_account_access(user, account_id)
    _ensure_mailbox_server_action(svc, user, account_id)
    source = svc.require_account(account_id)
    ids = sorted({_bounded_id(int(i)) for i in body.message_ids})
    target_id = int(body.target_account_id or 0) or account_id
    if target_id != account_id and user.get("role") != roles.ROLE_ADMIN:
        raise HTTPException(403, "Вернуть письма в другой ящик может только администратор")
    target = svc.require_account(_bounded_id(target_id), need_secret=True)
    problem = credential_problem(target)
    if not problem and target.auth_type == AuthType.AXIGEN_FTP and not target.password:
        problem = "у ящика нет пароля, а отдельные письма восстанавливаются только по IMAP"
    if problem:
        raise ValidationError(f"В ящик «{target.name}» письма не вернуть: {problem}.",
                              hint=("Задайте пароль в карточке ящика (раздел «Почтовые ящики»)."
                                    if user.get("role") == roles.ROLE_ADMIN else
                                    "Обратитесь к администратору архива."))
    data = validate_restore_options({"target_mode": body.target_mode, "target_folder": body.target_folder,
                                     "target_prefix": body.target_prefix, "via": "imap", "limit": 0})
    present = svc.db.message_ids_present(account_id, ids)
    if len(present) != len(ids):
        missing = len(ids) - len(present)
        raise ValidationError(f"Писем нет в архиве ящика «{source.name}»: {missing} из {len(ids)}.",
                              hint="Они могли быть удалены по сроку хранения. Обновите список писем и "
                                   "выберите их заново.")
    _mailbox_flood_guard(svc, user, JobType.RESTORE, account_id)
    params = {"message_ids": ids, "target_mode": data["target_mode"],
              "target_folder": data["target_folder"], "target_prefix": data["target_prefix"],
              "check_duplicates": bool(body.check_duplicates), "via": "imap"}
    if target.id != account_id:
        params["target_account_id"] = target.id
    jid = svc.queue.enqueue(JobType.RESTORE, account_id, params, created_by=user["username"])
    where = "в тот же ящик" if target.id == account_id else f"в ящик «{target.name}» ({target.username})"
    mode = {"original": "в исходные папки", "single": f"в папку «{data['target_folder']}»",
            "prefixed": f"в папки «{data['target_prefix']}/…»"}[data["target_mode"]]
    svc.db.add_audit(user["username"], "restore_messages",
                     f"писем {len(ids)} из архива «{source.name}» {where}, {mode}; задание №{jid}"[:900])
    return {"ok": True, "job_id": jid, "count": len(ids)}


@router.post("/accounts/{account_id}/verify")
def start_verify(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    _mailbox_flood_guard(svc, user, JobType.VERIFY, account_id)
    _operator_job_guard(svc, user, JobType.VERIFY, account_id)
    jid = svc.queue.enqueue(JobType.VERIFY, account_id, {}, created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/retention")
def set_retention(request: Request, account_id: int, body: RetentionBody, user: dict = Depends(auth_mod.require_user)):
    """Задать политику хранения локальной копии ящика (пресеты: 3 дня, 1 неделя, всё)."""
    svc = svc_dep(request)
    # Срок хранения определяет, какие письма задание очистки УДАЛИТ из архива.
    # Сотрудник, выставивший своему ящику «3 дня», стирал бы архив своей почты.
    _require_not_mailbox(user, "Изменение срока хранения")
    _ensure_account_access(user, account_id)
    before = svc.require_account(account_id)
    days = int(body.days)
    if days < -1 or days > 36500:
        raise ValidationError("Срок хранения — от 0 до 36500 дней (0 — хранить всё, −1 — как в общих настройках).")
    svc.db.set_account_retention(account_id, days)
    _after_account_retention_change(svc, before, days)
    # Отдельное расписание очистки больше не заводим: ежедневная очистка по
    # срокам хранения (retention.cron) обходит ВСЕ ящики — и заданные здесь, и в
    # форме ящика, и в шаблоне ящиков сотрудников.
    result = {"ok": True, "days": days, "sweep_cron": str(svc.rt("retention", "cron") or "")}
    if days > 0 and body.run_now:
        result["job_id"] = svc.queue.enqueue(JobType.RETENTION, account_id, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "retention_set", f"account={account_id} days={days}")
    return result


def _after_account_retention_change(svc, before, new_days: int) -> None:
    """Срок хранения ящика стал длиннее — вычищенные письма снова скачиваются
    (см. :func:`mailarchiver.accountops.after_account_retention_change`)."""
    after_account_retention_change(svc, before)


@router.post("/accounts/{account_id}/retired/clear")
def clear_account_retired(request: Request, account_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Забыть письма ящика, отсеянные по сроку хранения.

    Копирование не скачивает письма старше срока хранения и не качает заново
    удалённые очисткой — их номера помнит список «отсеянных». Если даты тогда
    были неверными (часы сервера архива уходили вперёд, у писем на почтовом
    сервере стояли не те даты), список можно забыть: следующее копирование
    заново проверит даты этих писем — моложе срока скачаются, старше снова
    будут пропущены.
    """
    svc = svc_dep(request)
    acc = svc.require_account(_bounded_id(account_id))
    cleared = svc.db.clear_retired(acc.id)
    svc.db.add_audit(user["username"], "account_retired_clear", f"{acc.name}: забыто отсеянных писем {cleared}")
    return {"ok": True, "cleared": cleared}


@router.get("/import-files")
def list_import_files(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Файлы .pst/.ost в каталоге импорта на сервере — для окна «Импорт .pst».

    Так импортируются файлы больше предела загрузки: администратор кладёт файл
    в каталог сам, а здесь выбирает его по имени.
    """
    from ..pstimport import inbox
    svc = svc_dep(request)
    # Права те же, что у самого импорта: оператору точка закрыта списком
    # OPERATOR_ROUTES, сотруднику (вход по ящику) — здесь.
    _require_not_mailbox(user, "Импорт .pst")
    directory = svc.cfg.import_dir
    # Почему каталог недоступен, окну нужно знать точно: ссылке на неподключённый диск
    # не поможет «mkdir», закрытому каталогу — «подключите диск» (problem — вид причины,
    # reason — она же словами, link_target — куда ведёт каталог, если он ссылка).
    kind, reason = inbox.problem(svc.cfg)
    return {"directory": directory,
            "link_target": os.path.realpath(directory) if os.path.islink(directory) else "",
            "files": [] if kind else inbox.list_files(directory),
            "available": not kind, "problem": kind, "reason": reason,
            "user": inbox.service_user(), "max_upload_bytes": MAX_PST_UPLOAD_BYTES}


@router.post("/accounts/{account_id}/import-pst")
def import_pst(request: Request, account_id: int, file: Optional[UploadFile] = File(None),
               server_file: str = Form(""),
               target_prefix: str = Form("Импорт PST"), no_prefix: str = Form(""),
               target: str = Form("imap"), skip_existing: str = Form("1"),
               engine: str = Form("builtin"), ansi_codepage: str = Form("cp1251"),
               user: dict = Depends(auth_mod.require_user)):
    """Импорт .pst в ящик: ``target`` — ``imap`` (на почтовый сервер) или ``local``
    (в локальную копию); ``engine`` — ``builtin`` (встроенный разбор) или ``readpst``.

    Файл — один из двух: загруженный (``file``) или уже лежащий в каталоге импорта
    на сервере (``server_file`` — имя из списка ``GET /api/import-files``). Второй
    способ — для файлов больше предела загрузки; такой файл не копируется и после
    импорта остаётся на месте.

    Папки создаются внутри ``target_prefix``. Без общей папки (под своими именами)
    — только с явным ``no_prefix=1``: пустое поле формы FastAPI заменяет значением
    по умолчанию, и «пустой префикс» одним полем передать нельзя — да и не стоит:
    очищенное по ошибке поле не должно заливать архив прямо в рабочие папки."""
    from ..pstimport import importer, inbox
    svc = svc_dep(request)
    # Разбор присланного файла — тяжёлая работа над чужими данными, а импорт
    # ещё и пишет в ящик: это не то, что стоит открывать каждому сотруднику.
    _require_not_mailbox(user, "Импорт .pst")
    _ensure_account_access(user, account_id)
    acc = svc.require_account(account_id)
    if target not in importer.TARGETS:
        raise ValidationError("Неизвестная цель импорта.",
                              hint="Допустимо: imap (на почтовый сервер) или local (в локальную копию).")
    if engine not in importer.ENGINES:
        raise ValidationError("Неизвестный способ разбора файла.", hint="Допустимо: builtin или readpst.")
    if ansi_codepage not in importer.ANSI_CODECS:
        raise ValidationError("Неизвестная кодировка для файлов ANSI.",
                              hint="Допустимо: " + ", ".join(importer.ANSI_CODECS) + ".")
    off = ("0", "false", "no", "off", "")
    if str(no_prefix).strip().lower() not in off:
        prefix = ""
    else:
        prefix = importer.clean_prefix(target_prefix) or importer.DEFAULT_PREFIX
    skip = str(skip_existing).strip().lower() not in off
    # Поле файла без выбранного файла браузер присылает частью с пустым именем —
    # это «файл не выбран», а не загрузка.
    uploaded = file is not None and bool(file.filename)
    server_name = server_file if isinstance(server_file, str) else ""
    if uploaded and server_name:
        raise ValidationError("Файл указан дважды: и загруженный, и с сервера.",
                              hint="Выберите что-то одно: загрузить файл с компьютера или взять файл из "
                                   "каталога импорта на сервере.")
    if not uploaded and not server_name:
        raise ValidationError("Файл для импорта не выбран.",
                              hint="Выберите файл .pst или .ost на своём компьютере либо файл из каталога "
                                   "импорта на сервере.")
    if server_name:
        kind, reason = inbox.problem(svc.cfg)
        if kind:
            raise ValidationError(f"Взять файл с сервера сейчас нельзя. {reason}.",
                                  hint="Что с этим делать, написано в окне импорта под списком «Файл на "
                                       "сервере». Загрузка файла с компьютера при этом работает.")
        # Имя пришло из браузера: путь по нему строит только inbox.locate — он пускает
        # лишь обычный файл, лежащий прямо в каталоге импорта. Файл не копируется.
        dest, info = inbox.locate(svc.cfg.import_dir, server_name)
        origin_name = os.path.basename(dest)
        # Пути в параметрах задания нет намеренно: прежние версии после импорта удаляли
        # файл по pst_path, и после отката на них «Повторить» стёрло бы файл администратора.
        # Приметы файла — чтобы под тем же именем не импортировался уже другой файл.
        job_file = {"source": "server", "server_file": origin_name, "fingerprint": inbox.fingerprint(info)}
        source = "server"
    else:
        # имя файла приходит от клиента: берём только базовое имя и чистим его,
        # иначе «../../» в имени увело бы запись за пределы каталога временных файлов
        origin_name = os.path.basename(file.filename or "")
        if not origin_name.lower().endswith(inbox.EXTENSIONS):
            raise ValidationError("Ожидается файл .pst или .ost",
                                  hint="Выберите файл данных Outlook с расширением .pst или .ost")
        fname = safe_filename(origin_name, default="import" + origin_name[-4:].lower())
        # uuid в имени: почти все файлы называются «Outlook.pst», и вторая загрузка
        # в тот же ящик затирала первую (первое задание импортировало чужой файл).
        dest = os.path.join(svc.cfg.tmp_dir, f"import_{account_id}_{uuid.uuid4().hex}_{fname}")
        source = "upload"
        written = 0
        try:
            with open(dest, "wb") as fh:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_PST_UPLOAD_BYTES:
                        raise ValidationError(
                            f"Файл слишком большой: через интерфейс загружается не более "
                            f"{human_size(MAX_PST_UPLOAD_BYTES)}.",
                            hint=f"Положите файл в каталог импорта на сервере ({svc.cfg.import_dir}) и выберите "
                                 f"в окне импорта «Взять с сервера» — так импортируется файл любого размера.")
                    fh.write(chunk)
        except BaseException:
            # частично записанный файл не должен оставаться на диске
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise
        job_file = {"source": "upload", "pst_path": dest}
    jid = svc.queue.enqueue(JobType.IMPORT_PST, account_id,
                            dict(job_file, target=target, target_prefix=prefix, skip_existing=skip,
                                 engine=engine, ansi_codepage=ansi_codepage),
                            created_by=user["username"])
    svc.db.add_audit(user["username"], "import_pst",
                     f"{acc.name}: {origin_name} "
                     + ("(из каталога импорта на сервере), " if source == "server" else "(загружен через интерфейс), ")
                     + ("в локальную копию" if target == "local" else "на сервер")
                     + (f", префикс «{prefix}»" if prefix else ", без префикса"))
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



#: Типы, которые браузер мог бы отрисовать сам как страницу. Вложение с таким
#: типом открылось бы в контексте нашего сайта (скрипты из письма получили бы
#: доступ к API от имени пользователя), поэтому они отдаются как
#: application/octet-stream — файл просто скачивается.
_ACTIVE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
                 "application/xml", "text/javascript", "application/javascript",
                 "application/x-javascript", "text/xsl", "multipart/x-mixed-replace"}


def _safe_media_type(ctype: str) -> str:
    ctype = (ctype or "").strip().lower()
    if not ctype or ctype in _ACTIVE_TYPES or "/" not in ctype:
        return "application/octet-stream"
    return ctype


def _attachment_disposition(filename: str) -> str:
    """Content-Disposition с именем в UTF-8 и ASCII-запасным вариантом."""
    from urllib.parse import quote
    name = (filename or "attachment").replace("\r", " ").replace("\n", " ")
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name) or "attachment"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


#: просмотры писем персоналом, уже записанные в аудит: (пользователь, ящик, письмо, что) → время
_VIEWS_SEEN: Dict[tuple, float] = {}
_VIEWS_LOCK = threading.Lock()
VIEW_AUDIT_TTL_S = 3600


def _audit_view(svc, user: dict, account_id: int, pk: int, what: str) -> None:
    """Кто из персонала читал чужие письма (1.21.0): просмотр, вложение, исходный файл — в аудит.

    Сотрудник в своём ящике не записывается; одно и то же действие с письмом — не чаще раза в час.
    """
    if user.get("role") == "mailbox" or not bool(svc.rt("audit", "message_views")
                                                 if svc.rt("audit", "message_views") is not None else True):
        return
    key = (user.get("username") or "", int(account_id), int(pk), what)
    now = time.monotonic()
    with _VIEWS_LOCK:
        if _VIEWS_SEEN.get(key, -1e18) > now - VIEW_AUDIT_TTL_S:
            return
        _VIEWS_SEEN[key] = now
        if len(_VIEWS_SEEN) > 50000:
            for k in [k for k, t in _VIEWS_SEEN.items() if t <= now - VIEW_AUDIT_TTL_S]:
                _VIEWS_SEEN.pop(k, None)
    acc = svc.db.get_account(int(account_id))
    svc.db.add_audit(user.get("username") or "", "message_view",
                     f"ящик «{acc.name if acc else account_id}»: письмо #{int(pk)} — {what}")


@router.get("/accounts/{account_id}/messages/{pk}")
def read_mailbox_message(request: Request, account_id: int, pk: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    from ..mailview import summarize_source
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    stored = row["stored_path"]
    size = svc.store.message_size(account_id, stored)
    parsed = summarize_source(lambda: svc.store.open_message(account_id, stored), size)
    parsed["id"] = pk
    parsed["folder"] = row["folder"]
    parsed["flags"] = [f for f in (row["flags"] or "").split(",") if f]
    parsed["size_h"] = human_size(row["size"] or size)
    parsed["gone"] = bool(row["gone"])          # письма в этой папке на сервере больше нет (1.17.0)
    parsed["date"] = row["internaldate"]
    from ..journal import receipt as _journal_receipt
    parsed["journal"] = _journal_receipt(svc, pk) if user.get("role") != "mailbox" else None   # откуда (1.21.0)
    _audit_view(svc, user, account_id, pk, "просмотр")
    return parsed


@router.get("/accounts/{account_id}/messages/{pk}/attachment/{idx}")
def download_attachment(request: Request, account_id: int, pk: int, idx: int,
                        user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    from ..mailview import attachment_from_source
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    stored = row["stored_path"]
    size = svc.store.message_size(account_id, stored)
    # Вложение отдаётся ПОТОКОМ: у крупного письма (любого размера) память
    # расходуется порциями, а не десятикратным объёмом файла, как при разборе
    # модулем email целиком.
    att = attachment_from_source(lambda: svc.store.open_message(account_id, stored), size, idx)
    if att is None:
        raise HTTPException(404, "Вложение не найдено")
    filename, ctype, chunks, length = att
    _audit_view(svc, user, account_id, pk, f"вложение «{(filename or '')[:120]}»")
    headers = {"Content-Disposition": _attachment_disposition(filename)}
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(chunks, media_type=_safe_media_type(ctype), headers=headers)


# ---- общие и делегированные ящики (1.22.0) --------------------------------
class DelegateBody(BaseModel):
    employee_id: int
    rights: str = Field("read", max_length=8)
    until: str = Field("", max_length=10)
    note: str = Field("", max_length=300)


@router.get("/accounts/{account_id}/delegates")
def account_delegates(request: Request, account_id: int, user: dict = Depends(auth_mod.require_admin)):
    from .. import delegation
    svc = svc_dep(request)
    svc.require_account(account_id)
    return {"items": delegation.list_for_account(svc, account_id)}


@router.post("/accounts/{account_id}/delegates")
def account_delegate_add(request: Request, account_id: int, body: DelegateBody,
                         user: dict = Depends(auth_mod.require_admin)):
    from .. import delegation
    svc = svc_dep(request)
    svc.require_account(account_id)
    return delegation.grant(svc, user["username"], account_id, body.employee_id, body.rights, body.until, body.note)


@router.delete("/accounts/{account_id}/delegates/{employee_id}")
def account_delegate_remove(request: Request, account_id: int, employee_id: int,
                            user: dict = Depends(auth_mod.require_admin)):
    from .. import delegation
    svc = svc_dep(request)
    return delegation.revoke(svc, user["username"], account_id, employee_id)


@router.get("/accounts/{account_id}/messages/{pk}/raw")
def download_message_raw(request: Request, account_id: int, pk: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    from ..mailview import open_source_stream
    row = svc.db.get_message(pk)
    if row is None or row["account_id"] != account_id:
        raise HTTPException(404, "Письмо не найдено")
    stored = row["stored_path"]
    size = svc.store.message_size(account_id, stored)
    _audit_view(svc, user, account_id, pk, "скачан исходный файл .eml")
    # Файл открывается до ответа: без ключа или при пропавшем файле клиент
    # получает понятную ошибку, а не «200 OK» с оборванным телом.
    chunks = open_source_stream(lambda: svc.store.open_message(account_id, stored))
    return StreamingResponse(chunks,
                             media_type="message/rfc822",
                             headers={"Content-Disposition": f'attachment; filename="message_{pk}.eml"',
                                      "Content-Length": str(size)})
