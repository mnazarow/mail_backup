"""API: настройки и справка, шифрование копии, копия вне сервера, журнал безопасности, поиск,
вход через администратора почты.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..errors import MailArchiverError, ValidationError
from ..accountops import longer_retention
from ..models import Account, AuthType, JobType, Security
from . import auth as auth_mod
from .i18n import all_help

from .api_common import (
    SECRET_BOUND_TO, SECRET_SETTINGS, SettingsBody, _bounded_id, _own_ids, _require_axigen_password, log, svc_dep,
)
from .api_jobs import _validate_cron

router = APIRouter(prefix="/api")

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
    from ..config import FILE_ONLY_SETTINGS
    result = {}
    for sec in SETTINGS_SECTIONS:
        section = sec["section"]
        result[section] = {}
        for key in sec["keys"]:
            if f"{section}.{key}" == "server.worker_process":
                value = svc.worker is not None                # как работает служба (учтена и переменная окружения)
            elif f"{section}.{key}" in FILE_ONLY_SETTINGS:
                value = svc.cfg.get(section, key, None)      # действующее значение — из файла
            else:
                value = svc.rt(section, key)
            # Секреты наружу не отдаём даже администратору: в поле показывается
            # пустое значение с подсказкой «без изменений», а сохранённый пароль
            # остаётся в БД (см. update_settings — пустая строка его не затирает).
            if f"{section}.{key}" in SECRET_SETTINGS and value:
                value = ""
            result[section][key] = value
    # Параметры, которые задаются только в config.yaml, показываем «для чтения»:
    # раньше их можно было «сохранить» в интерфейсе, но служба их не читала.
    readonly = sorted(f"{sec['section']}.{key}" for sec in SETTINGS_SECTIONS for key in sec["keys"]
                      if f"{sec['section']}.{key}" in FILE_ONLY_SETTINGS)
    return {"values": result, "sections": SETTINGS_SECTIONS, "help": all_help(), "readonly": readonly,
            "secrets_set": {k: bool(svc.rt(*k.split(".", 1))) for k in SECRET_SETTINGS}}


@router.put("/settings")
def update_settings(request: Request, body: SettingsBody, user: dict = Depends(auth_mod.require_admin)):
    """Сохранить настройки: сначала проверить ВСЁ, потом записать одной транзакцией.

    Раньше значения писались по одному по мере проверки: ошибка в третьем
    параметре оставляла первые два записанными, но не применёнными (и без
    записи в аудите). Типы приводились мягко — ``bool("false")`` включал
    шифрование, NaN ломал раздел «Настройки», неизвестный часовой пояс молча
    становился UTC.
    """
    svc = svc_dep(request)
    from ..config import FILE_ONLY_SETTINGS, UNSUPPORTED_SETTINGS, coerce_setting
    if not isinstance(body.values, dict):
        raise ValidationError("Ожидается набор параметров.")
    if len(body.values) > 500:
        raise ValidationError("Слишком много параметров в одном запросе.")
    new_values: Dict[str, Any] = {}
    for full_key, value in body.values.items():
        full_key = str(full_key)
        if "." not in full_key:
            raise ValidationError(f"Неизвестный параметр «{full_key}».")
        if full_key in FILE_ONLY_SETTINGS:
            raise ValidationError(f"Параметр «{full_key}» задаётся только в config.yaml.",
                                  hint="Измените его в файле конфигурации и перезапустите службу.")
        if full_key in UNSUPPORTED_SETTINGS:
            raise ValidationError(f"Параметр «{full_key}» больше не поддерживается и ни на что не влияет.")
        # Пустое значение секрета означает «оставить как есть» — иначе простое
        # сохранение формы затирало бы сохранённый пароль.
        if full_key in SECRET_SETTINGS and (value is None or value == ""):
            continue
        try:
            coerced = coerce_setting(full_key, value)
        except ValueError as exc:
            raise ValidationError(str(exc), hint="Исправьте значение — ничего не сохранено.")
        # Расписания проверяем сразу: иначе кривое выражение обнаружилось бы
        # только в логах планировщика, а задание молча не запускалось бы.
        if full_key in ("employees.cron", "employees.account_schedule_cron", "retention.cron",
                        "replica.cron", "notifications.summary_cron", "axigen.sync_cron", "drill.cron"):
            _validate_cron(str(coerced or ""))
        if coerced != svc.rt(*full_key.split(".", 1)) or full_key in SECRET_SETTINGS:
            new_values[full_key] = coerced
    if not new_values:
        return {"ok": True, "changed": []}
    for secret, bound in SECRET_BOUND_TO.items():
        if bound in new_values and secret not in new_values and svc.rt(*secret.split(".", 1)):
            old_host = str(svc.rt(*bound.split(".", 1)) or "").strip().lower()
            if old_host and str(new_values[bound] or "").strip().lower() != old_host:
                raise ValidationError(
                    f"Адрес сервера меняется ({old_host} → {new_values[bound] or 'пусто'}) — введите пароль заново "
                    f"в том же сохранении.",
                    hint="Сохранённый пароль не отправляется на новый адрес без подтверждения. Ничего не сохранено.")
    _require_axigen_password(svc, new_values)

    snapshot = svc.db.raw_settings(new_values.keys())
    before_retention = _retention_policy(svc)
    svc.db.set_settings_many(new_values)
    changed = sorted(new_values)
    storage_changed = [k for k in changed if k.startswith("storage.")]
    enc_state: Dict[str, Any] = {}
    try:
        # Ключ шифрования можно создать только здесь — по явному действию
        # администратора (и при запуске службы), но не при просмотре состояния.
        enc_state = svc.apply_runtime_settings(generate_key=bool(storage_changed),
                                               strict=bool(storage_changed))
    except MailArchiverError as exc:
        # Откатываем ВСЁ сохранённое этим запросом, а не только путь к ключу:
        # раньше «storage.encrypt=true» оставался, и следующее применение
        # настроек молча создавало ключ в каталоге данных.
        svc.db.restore_settings(snapshot)
        svc.apply_runtime_settings()
        raise ValidationError(exc.message, hint=(exc.hint or "") + " Настройки не изменены.")
    try:
        svc.scheduler.reload()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось применить расписания после сохранения настроек")
    _after_retention_change(svc, before_retention)
    if any(k.startswith("journal.") for k in changed):
        from ..journal import ensure as journal_ensure
        journal_ensure(svc)
    shown = [k + ("=***" if k in SECRET_SETTINGS else "") for k in changed]
    svc.db.add_audit(user["username"], "settings_update", ", ".join(shown[:30]))
    result = {"ok": True, "changed": changed}
    if enc_state.get("generated"):
        result["notice"] = (f"Создан ключ шифрования: {enc_state['key_path']}. Сохраните его копию "
                            f"ОТДЕЛЬНО от каталога данных — без него зашифрованные письма не прочитать.")
    return result


def _retention_policy(svc) -> Dict[str, Any]:
    return {"enabled": bool(svc.rt("retention", "enabled")),
            "days": int(svc.rt("retention", "keep_days") or 0)}


#: Новый срок хранения длиннее прежнего (0 — «хранить всё» — длиннее любого).
_longer_retention = longer_retention


def _after_retention_change(svc, before: Dict[str, Any]) -> None:
    """Общий срок хранения увеличили — письма, вычищенные по старому сроку и ещё
    лежащие на сервере, снова станут доступны для копирования."""
    after = _retention_policy(svc)
    old_days = before["days"] if before["enabled"] else 0
    new_days = after["days"] if after["enabled"] else 0
    if not _longer_retention(old_days, new_days):
        return
    for acc in svc.db.list_accounts():
        if acc.retention_days is None or acc.retention_days < 0:
            svc.db.clear_retired(acc.id)


# =====================================================================
#  Шифрование локальной копии
# =====================================================================
class StorageConvertBody(BaseModel):
    mode: str = "encrypt"


@router.get("/storage/encryption")
def storage_encryption(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return svc.encryption_status()


@router.post("/storage/convert")
def storage_convert(request: Request, body: StorageConvertBody, user: dict = Depends(auth_mod.require_admin)):
    """Зашифровать (или расшифровать) уже сохранённые письма — по заданию на ящик."""
    svc = svc_dep(request)
    mode = (body.mode or "").strip().lower()
    if mode not in ("encrypt", "decrypt"):
        raise ValidationError("Режим должен быть encrypt или decrypt.")
    if svc.store.cipher is None:
        raise ValidationError("Ключ шифрования не загружен.",
                              hint="Сначала включите «Шифровать письма» в настройках хранилища.")
    if mode == "encrypt" and not svc.store.encrypt:
        raise ValidationError("Шифрование выключено в настройках — новые письма будут открытыми.",
                              hint="Включите «Шифровать письма», затем зашифруйте существующие.")
    if mode == "decrypt" and svc.store.encrypt:
        raise ValidationError("Шифрование включено — новые письма всё равно будут шифроваться.",
                              hint="Сначала выключите «Шифровать письма», затем расшифруйте существующие.")
    busy = {j["account_id"] for j in svc.db.active_jobs() if j["type"] == JobType.STORAGE_CONVERT}
    jobs = []
    for acc in svc.db.list_accounts():
        if acc.id in busy:
            continue
        jobs.append(svc.queue.enqueue(JobType.STORAGE_CONVERT, acc.id, {"mode": mode},
                                      created_by=user["username"]))
    svc.db.add_audit(user["username"], f"storage_{mode}", f"jobs={len(jobs)}")
    return {"ok": True, "jobs": jobs}


# =====================================================================
#  Копия вне сервера и снимки базы
# =====================================================================
class ReplicaRunBody(BaseModel):
    allow_mass_delete: bool = False
    verify: bool = False


class ReplicaPrepareBody(BaseModel):
    force: bool = False


def _active_job(svc, job_type: str):
    for job in svc.db.active_jobs():
        if job["type"] == job_type:
            return job
    return None


@router.get("/replica")
def replica_status(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..replica import status as replica_status_fn
    svc = svc_dep(request)
    data = replica_status_fn(svc)
    job = _active_job(svc, JobType.REPLICATE)
    data["active_job"] = job["id"] if job else None
    snap = _active_job(svc, JobType.DB_SNAPSHOT)
    data["active_snapshot_job"] = snap["id"] if snap else None
    data["next_run"] = svc.scheduler.next_run_of("replica_sync")
    return data


@router.post("/replica/check")
def replica_check(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..replica import check_target
    return check_target(svc_dep(request))


@router.post("/replica/prepare")
def replica_prepare(request: Request, body: ReplicaPrepareBody, user: dict = Depends(auth_mod.require_admin)):
    from ..replica import prepare_target
    svc = svc_dep(request)
    if _active_job(svc, JobType.REPLICATE):
        raise ValidationError("Сейчас идёт копирование — дождитесь его окончания.")
    result = prepare_target(svc, force=bool(body.force))
    if result.get("ok"):
        svc.db.add_audit(user["username"], "replica_prepare", f"{result['action']}: {result['target']}")
    return result


@router.post("/replica/run")
def replica_run(request: Request, body: ReplicaRunBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    job = _active_job(svc, JobType.REPLICATE)
    if job:
        return {"ok": True, "job_id": job["id"], "already": True}
    params = {"by": user["username"]}
    if body.allow_mass_delete:
        params["allow_mass_delete"] = True
    if body.verify:
        params["verify"] = True
    job_id = svc.queue.enqueue(JobType.REPLICATE, None, params, priority=4, created_by=user["username"])
    svc.db.add_audit(user["username"], "replica_start",
                     ("разрешено массовое удаление; " if body.allow_mass_delete else "")
                     + ("полная сверка" if body.verify else "обычный прогон"))
    return {"ok": True, "job_id": job_id}


@router.post("/replica/snapshot")
def replica_snapshot(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    job = _active_job(svc, JobType.DB_SNAPSHOT)
    if job:
        return {"ok": True, "job_id": job["id"], "already": True}
    job_id = svc.queue.enqueue(JobType.DB_SNAPSHOT, None, {}, priority=4, created_by=user["username"])
    svc.db.add_audit(user["username"], "db_snapshot", "")
    return {"ok": True, "job_id": job_id}


@router.post("/replica/ssh-key")
def replica_ssh_key(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..replica import generate_ssh_key
    svc = svc_dep(request)
    key = generate_ssh_key(svc)
    svc.db.add_audit(user["username"], "replica_ssh_key", "")
    return {"ok": True, "public_key": key}


# =====================================================================
#  Журнал безопасности
# =====================================================================
class UnblockBody(BaseModel):
    kind: str = Field("", max_length=20)
    username: str = Field("", max_length=320)
    ip: str = Field("", max_length=64)


@router.get("/security")
def security_journal(request: Request, user: dict = Depends(auth_mod.require_admin)):
    return auth_mod.security_overview(svc_dep(request))


@router.post("/security/unblock")
def security_unblock(request: Request, body: UnblockBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    removed = auth_mod.security_unblock(svc, body.kind, body.username.strip(), body.ip.strip())
    svc.db.add_audit(user["username"], "security_unblock",
                     f"{body.kind}: {body.username or ''} {body.ip or ''}".strip() + f" (записей {removed})")
    return {"ok": True, "removed": removed}


# =====================================================================
#  Поиск по письмам
# =====================================================================
@router.get("/search")
def search_messages(request: Request, q: str = "", account_id: Optional[int] = None, scope: str = "account",
                    folder: str = "", date_from: str = "", date_to: str = "", attach: bool = False,
                    order: str = "date", offset: int = 0, limit: int = 50,
                    user: dict = Depends(auth_mod.require_user)):
    """Поиск писем: сотрудник — только в своём ящике; администратор — в ящике или во всех.

    Каждый поиск администратора записывается в аудит (кто, что и где искал):
    поиск по всем ящикам — это доступ к чужой переписке.
    """
    from ..search import search
    svc = svc_dep(request)
    query = (q or "").strip()
    if len(query) > 500:
        raise ValidationError("Слишком длинный запрос (больше 500 символов).")
    if user.get("role") == "mailbox":
        # сотрудник ищет в одном своём ящике или во всех своих (scope=all), но не в чужих
        own = _own_ids(user)
        if scope == "all":
            ids = own
        else:
            ids = [account_id if account_id in own else own[0]]
            scope = "account"
    elif scope == "all":
        ids = None
    else:
        if account_id is None:
            raise ValidationError("Не указан ящик для поиска.")
        _bounded_id(account_id)
        ids = [account_id]
    result = search(svc, query, account_ids=ids, folder=folder.strip(), date_from=date_from, date_to=date_to,
                    with_attachments=bool(attach), order="rank" if order == "rank" else "date",
                    offset=_bounded_id(max(0, offset)), limit=limit)
    if user.get("role") != "mailbox" and query:
        where = "все ящики" if ids is None else f"ящик #{ids[0]}"
        svc.db.add_audit(user["username"], "search", f"«{query[:200]}» — {where}"
                         + (f", папка «{folder[:100]}»" if folder else "")
                         + (f", найдено {len(result['results'])}{'+' if result['more'] else ''}" if not offset else ""))
    return result


@router.get("/search/status")
def search_status(request: Request, user: dict = Depends(auth_mod.require_user)):
    from ..search import status as search_status_fn
    svc = svc_dep(request)
    data = search_status_fn(svc)
    if user.get("role") == "mailbox":
        # сотруднику — без общих счётчиков архива
        return {"available": data["available"], "bodies": data["bodies"], "enabled": data["enabled"],
                "pending": 0, "indexed": 0, "total": 0}
    job = next((j for j in svc.db.active_jobs() if j["type"] == JobType.SEARCH_INDEX), None)
    data["active_job"] = job["id"] if job else None
    job = next((j for j in svc.db.active_jobs() if j["type"] == JobType.SEARCH_ATTACH), None)
    data["attach_job"] = job["id"] if job else None
    return data


@router.post("/search/attachments")
def search_attachments(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Дочитать текст вложений уже проиндексированных писем — без перестройки индекса (1.21.0)."""
    from ..search import attach_backfill_pending, fts_available
    svc = svc_dep(request)
    if not fts_available(svc.db):
        raise ValidationError("Полнотекстовый поиск недоступен: SQLite собран без FTS5.")
    job = next((j for j in svc.db.active_jobs() if j["type"] == JobType.SEARCH_ATTACH), None)
    if job is not None:
        return {"ok": True, "job_id": job["id"], "already": True}
    job_id = svc.queue.enqueue(JobType.SEARCH_ATTACH, None, {}, priority=8, created_by=user["username"])
    svc.db.add_audit(user["username"], "search_attachments", f"писем с вложениями: {attach_backfill_pending(svc.db)}")
    return {"ok": True, "job_id": job_id}


@router.post("/search/reindex")
def search_reindex(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Перестроить индекс поиска с нуля (например, после смены настроек индексации)."""
    from ..search import fts_available, reset_index
    svc = svc_dep(request)
    if not fts_available(svc.db):
        raise ValidationError("Полнотекстовый поиск недоступен: SQLite собран без FTS5.")
    for job in svc.db.active_jobs():
        if job["type"] == JobType.SEARCH_INDEX:
            svc.queue.cancel(job["id"])
    reset_index(svc.db)
    job_id = svc.queue.enqueue(JobType.SEARCH_INDEX, None, {}, priority=8, created_by=user["username"])
    svc.db.add_audit(user["username"], "search_reindex", "")
    return {"ok": True, "job_id": job_id}


# =====================================================================
#  Вход в ящики через администратора почты
# =====================================================================
class MailadminCheckBody(BaseModel):
    username: str = Field("", max_length=320)


class MailadminConvertBody(BaseModel):
    to: str = "master"


@router.post("/mailadmin/check")
def mailadmin_check(request: Request, body: MailadminCheckBody, user: dict = Depends(auth_mod.require_admin)):
    """Проверить вход администратора в ящик: указанный или первый ящик этого сервера."""
    from ..imap.client import check_login
    svc = svc_dep(request)
    master = svc.master_credentials()
    if master is None:
        raise ValidationError("Вход через администратора почты выключен.",
                              hint="Включите его и сохраните настройки.")
    if not master["host"] or not master["user"] or not master["password"]:
        raise ValidationError("Заполните сервер, логин и пароль администратора почты и сохраните настройки.")
    username = (body.username or "").strip()
    if not username:
        for acc in svc.db.list_accounts():
            if (acc.host or "").strip().lower() == master["host"].lower():
                username = acc.username
                break
    if not username:
        raise ValidationError("Укажите адрес ящика для проверки.",
                              hint="Ящиков с этим сервером пока нет — впишите любой существующий адрес.")
    trial = Account(name=username, host=master["host"], port=993, username=username, auth_type=AuthType.MASTER,
                    security=Security.SSL)
    for acc in svc.db.list_accounts():
        if acc.username.lower() == username.lower() and (acc.host or "").strip().lower() == master["host"].lower():
            trial.port, trial.security = acc.port, acc.security
            break
    opts = svc.connect_options()
    opts.connect_timeout_s = min(int(opts.connect_timeout_s or 30), 20)
    status, error = check_login(trial, opts)
    svc.db.add_audit(user["username"], "mailadmin_check", f"{username}: {status}")
    return {"ok": status == "ok", "status": status, "error": error, "username": username}


@router.post("/mailadmin/convert")
def mailadmin_convert(request: Request, body: MailadminConvertBody, user: dict = Depends(auth_mod.require_admin)):
    """Перевести ящики сервера администратора на вход через него (или вернуть вход по паролям)."""
    svc = svc_dep(request)
    to = (body.to or "").strip()
    if to not in (AuthType.MASTER, AuthType.PASSWORD):
        raise ValidationError("Неизвестный способ входа.")
    host = str(svc.rt("mailadmin", "host") or "").strip().lower()
    if not host:
        raise ValidationError("Сначала укажите сервер администратора почты и сохраните настройки.")
    if to == AuthType.MASTER and svc.master_credentials() is None:
        raise ValidationError("Вход через администратора почты выключен.")
    source = AuthType.PASSWORD if to == AuthType.MASTER else AuthType.MASTER
    ids = [a.id for a in svc.db.list_accounts()
           if (a.host or "").strip().lower() == host and a.auth_type == source]
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        svc.db.execute(f"UPDATE accounts SET auth_type=?, login_status='', updated_at=? WHERE id IN "
                       f"({','.join('?' * len(part))})", (to, datetime.now(timezone.utc).isoformat(), *part))
    svc.db.add_audit(user["username"], "mailadmin_convert", f"{to}: {len(ids)}")
    return {"ok": True, "changed": len(ids), "to": to}
