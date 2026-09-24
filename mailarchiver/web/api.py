"""
REST API веб-интерфейса. Все ответы — JSON. Ошибки предметной области
(:class:`MailArchiverError`) перехватываются глобально (см. app.py) и отдаются
с понятным пользователю сообщением и подсказкой.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..employees import (
    ACCOUNT_PLACEHOLDERS, TEMPLATE_CSV, TEMPLATE_FILENAME, apply_passwords,
    ensure_account_for_employee, fetch_employee_source, looks_like_email, normalize_email,
    parse_employee_file, parse_password_file, preview_account_template, redact_url,
    sync_employees,
)
from ..errors import MailArchiverError, ValidationError
from ..imap.client import diagnose_folders, probe_account
from ..models import Account, AuthType, JobStatus, JobType, ScheduleKind, Security
from ..util import human_size, safe_filename
from ..version import __version__
from . import auth as auth_mod
from .i18n import all_help
from ..export import list_engines
from ..logging_setup import get_logger

log = get_logger("api")
router = APIRouter(prefix="/api")

# Максимальный размер загружаемого .pst. Отдельной настройки для него нет,
# поэтому держим лимит константой: без него один запрос мог бы забить диск.
MAX_PST_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 ГБ

# Максимальный размер загружаемого списка сотрудников. Выгрузка кадровой
# системы — это таблица на несколько тысяч строк, 20 МБ хватает с запасом;
# ограничение защищает от загрузки чего-то постороннего.
MAX_EMPLOYEE_UPLOAD_BYTES = 20 * 1024 * 1024    # 20 МБ

# Допустимые статусы сотрудника (колонка employees.status).
EMPLOYEE_STATUSES = ("active", "archived")

# Минимальный интервал расписания (совпадает с проверкой в планировщике):
# более частое расписание планировщик не примет и оно молча не сработает.
MIN_SCHEDULE_INTERVAL_S = 60

# Параметры-секреты: не отдаются через GET /settings и не затираются пустым
# значением при сохранении (пустое поле = «оставить как есть»).
SECRET_SETTINGS = {"notifications.smtp_password", "employees.source_url_password",
                   "replica.s3_secret_key", "monitoring.metrics_token", "mailadmin.password"}


def svc_dep(request: Request):
    return request.app.state.services


def _ensure_account_access(user: dict, account_id: int) -> None:
    """Пользователь-ящик (role=mailbox) имеет доступ только к своему ящику."""
    if user.get("role") == "mailbox" and user.get("account_id") != account_id:
        raise HTTPException(403, "Доступ разрешён только к своему почтовому ящику")


def _ensure_job_access(user: dict, row) -> None:
    """Доступ к заданию для пользователя-ящика.

    Общесистемные задания (``account_id IS NULL`` — например глобальный анализ
    писем по всем ящикам) содержат данные по всем ящикам и к тому же тяжело
    нагружают сервер, поэтому доступны только администратору.
    """
    if user.get("role") != "mailbox":
        return
    if row["account_id"] is None:
        raise HTTPException(403, "Общесистемные задания доступны только администратору")
    _ensure_account_access(user, row["account_id"])


def _require_not_mailbox(user: dict, what: str = "") -> None:
    if user.get("role") == "mailbox":
        raise HTTPException(403, f"{what or 'Действие'} недоступно для входа по ящику — обратитесь к администратору")


#: Сколько заданий сотрудник (вход по ящику) может запустить за час.
MAILBOX_JOBS_PER_HOUR = 20
#: Сколько готовых выгрузок может держать сотрудник (каждая — копия ящика на диске сервера).
MAILBOX_MAX_EXPORTS = 3


#: Предел числовых идентификаторов в путях: больше — SQLite бросал OverflowError (500).
_MAX_ID = 2 ** 63 - 1


def _bounded_id(value: int) -> int:
    if value < 0 or value > _MAX_ID:
        raise HTTPException(404, "Не найдено")
    return value


def _mailbox_flood_guard(svc, user: dict, job_type: str, account_id) -> None:
    """Ограничения для входа по ящику: без них 1500 запросов «скопировать»
    за 3 секунды давали 1500 заданий, а «выгрузить → отменить → повторить» —
    десятки одновременных выгрузок и забитый диск (остановится копирование всех)."""
    if user.get("role") != "mailbox":
        return
    for job in svc.db.active_jobs():
        if job["type"] == job_type and job["account_id"] == account_id:
            raise ValidationError("Такое задание по вашему ящику уже выполняется или стоит в очереди.",
                                  hint="Дождитесь его завершения.")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = int(svc.db.scalar("SELECT COUNT(*) FROM jobs WHERE created_by=? AND created_at>=?",
                               (user.get("username") or "", since)) or 0)
    if recent >= MAILBOX_JOBS_PER_HOUR:
        raise ValidationError("Слишком много заданий за последний час.",
                              hint="Попробуйте позже или обратитесь к администратору.")
    if job_type == JobType.EXPORT:
        kept = int(svc.db.scalar("SELECT COUNT(*) FROM exports WHERE account_id=? AND created_by=? "
                                 "AND status IN ('success','partial')",
                                 (account_id, user.get("username") or "")) or 0)
        if kept >= MAILBOX_MAX_EXPORTS:
            raise ValidationError(f"У вас уже {kept} готовых выгрузки.",
                                  hint="Скачайте и удалите старые выгрузки в разделе «Экспорт», "
                                       "затем запустите новую.")


def _is_own_job(user: dict, row) -> bool:
    """Задание запущено самим пользователем-ящиком (а не администратором или планировщиком)."""
    return row is not None and (row["created_by"] or "") == (user.get("username") or "")


# Политика для роли «ящик» (сотрудник вошёл по своему email и паролю).
# Архив ведётся для организации, поэтому сотрудник может читать СВОЮ почту,
# выгружать её и восстанавливать в свой ящик, но не может ничего, что
# уменьшает или искажает архив: менять срок хранения (задание очистки удалило
# бы старые письма), заводить и удалять расписания (так выключается
# копирование), отменять чужие задания (копирование по расписанию), удалять
# чужие выгрузки, загружать .pst (сторонний разборщик на присланном файле).


# =====================================================================
#  Схемы запросов
# =====================================================================
#: Длина логина и пароля в запросах. Без ограничения аноним мог прислать логин
#: в несколько мегабайт — он целиком ложился в журнал попыток и аудит.
USERNAME_MAX = 320
PASSWORD_MAX = 1024


class LoginBody(BaseModel):
    username: str = Field(max_length=USERNAME_MAX)
    password: str = Field(max_length=PASSWORD_MAX)


class SetupBody(BaseModel):
    username: str = Field(max_length=USERNAME_MAX)
    password: str = Field(max_length=PASSWORD_MAX)


class BackupBody(BaseModel):
    """Параметры запуска копирования.

    ``rebuild``: пусто — обычная (инкрементная) копия; ``missing`` — сверить
    индекс с файлами на диске и докачать потерянные письма; ``full`` — стереть
    локальную копию ящика и скачать всё заново.
    """

    rebuild: str = ""


class ExcludeFoldersBody(BaseModel):
    """Папки, которые больше не нужно пытаться копировать."""

    folders: List[str] = []


class AccountBody(BaseModel):
    name: str
    host: str
    port: int = 993
    username: str
    password: str = ""
    auth_type: str = AuthType.PASSWORD
    security: str = Security.SSL
    enabled: bool = True
    # None — «не передано»: при правке ящика остаётся прежнее значение. Раньше
    # не переданное поле молча обнулялось (срок хранения становился «как в
    # общих настройках», белый список папок и заметки пропадали).
    folder_include: Optional[List[str]] = None
    folder_exclude: Optional[List[str]] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: str = ""
    oauth_refresh_token: str = ""
    oauth_token_url: Optional[str] = None
    notes: Optional[str] = None
    retention_days: Optional[int] = None


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
    cron_expr: str = Field("", max_length=200)
    interval_seconds: int = 0
    enabled: bool = True
    # None — «не передано»: при правке расписания прежние параметры сохраняются
    options: Optional[dict] = None


class SettingsBody(BaseModel):
    values: dict  # {"section.key": value}


class EmployeeBody(BaseModel):
    full_name: str
    email: str = ""
    position: str = ""
    department: str = ""
    phone: str = ""
    external_id: str = ""
    status: str = "active"
    notes: str = ""


class EmployeeCreateBody(EmployeeBody):
    #: завести сотруднику почтовый ящик (создаётся ВЫКЛЮЧЕННЫМ, без пароля)
    create_account: bool = False


class UserBody(BaseModel):
    username: str = Field(max_length=USERNAME_MAX)
    password: str = Field(max_length=PASSWORD_MAX)
    role: str = "admin"


class PasswordBody(BaseModel):
    password: str = Field(max_length=PASSWORD_MAX)


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
    result = auth_mod.do_login(svc, request, response, body.username, body.password)
    if result.get("otp_required"):
        # пароль верен, нужен второй шаг — сессии ещё нет
        return {"ok": False, "otp_required": True, "challenge": result["challenge"]}
    return {"ok": True, "user": result}


class OtpLoginBody(BaseModel):
    challenge: str = Field(max_length=4096)
    code: str = Field(max_length=128)


@router.post("/login/otp")
def login_otp(request: Request, response: Response, body: OtpLoginBody):
    svc = svc_dep(request)
    user = auth_mod.do_login_otp(svc, request, response, body.challenge, body.code)
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
    svc = svc_dep(request)
    out = dict(user)
    out["version"] = __version__
    out["require_2fa"] = bool(svc.rt("security", "require_2fa"))
    out["must_enroll_2fa"] = auth_mod.two_factor_required(svc, user)
    return out


# ---------------------------------------------------------------------
#  Двухфакторный вход текущего пользователя
# ---------------------------------------------------------------------
class OtpCodeBody(BaseModel):
    code: str = Field("", max_length=128)


class OtpDisableBody(BaseModel):
    password: str = Field("", max_length=PASSWORD_MAX)
    code: str = Field("", max_length=128)


def _own_web_user(request: Request) -> dict:
    """Текущий пользователь веб-интерфейса (не вход по ящику)."""
    user = auth_mod.require_user(request)
    if user.get("role") == "mailbox" or not user.get("id"):
        raise HTTPException(403, "Двухфакторный вход настраивается для пользователей веб-интерфейса; "
                                 "при входе по ящику защита — пароль самого ящика.")
    return user


def _check_user_code(svc, request: Request, user: dict, code: str, allow_recovery: bool = True) -> str:
    """Проверить код 2FA пользователя: 'totp' | 'recovery'; иначе ValidationError.

    Неверные коды учитываются так же, как при входе (лимит на учётную запись):
    иначе с чужой открытой сессией можно было бы перебрать код здесь и выпустить
    себе новые резервные коды.
    """
    state = svc.db.totp_state(user["id"]) or {}
    if not state.get("enabled"):
        raise ValidationError("Двухфакторный вход не включён.")
    ip = auth_mod.client_ip(request)
    how, attempt_id = auth_mod.check_second_factor(svc, user["id"], user["username"], ip, code,
                                                   allow_recovery=allow_recovery, state=state)
    if how is None:
        svc.db.add_audit(user["username"], "2fa_code_failed", ip)
        raise ValidationError("Неверный код подтверждения.",
                              hint="Введите 6 цифр из приложения-аутентификатора"
                                   + (" или резервный код." if allow_recovery else "."))
    svc.db.delete_login_attempt(attempt_id)
    return how


def _new_recovery(svc, user_id: int):
    from .. import totp as totp_mod
    from ..security import hash_password
    codes = totp_mod.new_recovery_codes()
    svc.db.set_totp_recovery(user_id, [hash_password(totp_mod.normalize_recovery(c)) for c in codes])
    return codes


@router.get("/me/2fa")
def my_2fa_status(request: Request):
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    return {"enabled": bool(state.get("enabled")), "pending": bool(state.get("pending")),
            "recovery_left": len(state.get("recovery") or []),
            "required": bool(svc.rt("security", "require_2fa"))}


@router.post("/me/2fa/setup")
def my_2fa_setup(request: Request):
    """Начать включение: новый секрет, ссылка otpauth и QR-код (пока не активен)."""
    from .. import qrcode, totp as totp_mod
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    if state.get("enabled"):
        raise ValidationError("Двухфакторный вход уже включён.",
                              hint="Чтобы привязать другой телефон, сначала отключите его.")
    secret = totp_mod.new_secret()
    svc.db.set_totp_pending(user["id"], secret)
    uri = totp_mod.provisioning_uri(secret, user["username"])
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "uri": uri, "qr_svg": qrcode.to_svg(uri, scale=5)}


@router.post("/me/2fa/enable")
def my_2fa_enable(request: Request, body: OtpCodeBody):
    """Подтвердить включение кодом из приложения; выдать резервные коды (один раз)."""
    from .. import totp as totp_mod
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    if state.get("enabled"):
        raise ValidationError("Двухфакторный вход уже включён.")
    pending = state.get("pending")
    if not pending:
        raise ValidationError("Сначала получите QR-код для приложения.")
    step = totp_mod.verify(pending, body.code)
    if step is None:
        raise ValidationError("Код не подошёл.",
                              hint="Проверьте, что время на телефоне и на сервере установлено точно, "
                                   "и введите код, который показывает приложение сейчас.")
    svc.db.enable_totp(user["id"], pending, [], step)
    codes = _new_recovery(svc, user["id"])
    svc.db.add_audit(user["username"], "2fa_enabled", auth_mod.client_ip(request))
    return {"ok": True, "recovery_codes": codes}


@router.post("/me/2fa/disable")
def my_2fa_disable(request: Request, body: OtpDisableBody):
    from ..security import verify_password
    svc = svc_dep(request)
    user = _own_web_user(request)
    if svc.rt("security", "require_2fa"):
        raise ValidationError("Двухфакторный вход обязателен по настройкам безопасности — отключить его нельзя.",
                              hint="Привязать другой телефон поможет администратор: «Пользователи» → «Сбросить 2FA».")
    # Пароль здесь проверяется с тем же учётом неудач, что и при входе: иначе
    # с чужой открытой сессией его можно было бы подбирать без ограничений.
    ip = auth_mod.client_ip(request)
    auth_mod.guard_password_check(svc, user["username"], ip)
    row = svc.db.get_user_by_id(user["id"])
    if row is None or not verify_password(body.password or "", row["password_hash"]):
        svc.db.record_login_attempt(user["username"], False, ip)
        svc.db.add_audit(user["username"], "2fa_disable_bad_password", ip)
        raise ValidationError("Неверный пароль.")
    _check_user_code(svc, request, user, body.code)
    svc.db.disable_totp(user["id"])
    svc.db.add_audit(user["username"], "2fa_disabled", auth_mod.client_ip(request))
    return {"ok": True}


@router.post("/me/2fa/recovery")
def my_2fa_new_recovery(request: Request, body: OtpCodeBody):
    """Выпустить новые резервные коды (старые перестают действовать)."""
    svc = svc_dep(request)
    user = _own_web_user(request)
    _check_user_code(svc, request, user, body.code, allow_recovery=False)
    codes = _new_recovery(svc, user["id"])
    svc.db.add_audit(user["username"], "2fa_recovery_regenerated", auth_mod.client_ip(request))
    return {"ok": True, "recovery_codes": codes}


# =====================================================================
#  Дашборд / состояние
# =====================================================================
@router.get("/state")
def state(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    accounts = svc.db.list_accounts()
    if user.get("role") == "mailbox":
        accounts = [a for a in accounts if a.id == user.get("account_id")]
    # Сводку по всем ящикам берём двумя групповыми запросами: по запросу на
    # ящик дашборд заваливал базу на каждом обновлении (см. /state в ws).
    totals_by_acc = svc.db.message_totals_by_account()
    last_by_acc = svc.db.last_runs_by_account()
    acc_out = []
    for a in accounts:
        stat = totals_by_acc.get(a.id) or {"messages": 0, "bytes": 0}
        cnt, by = stat["messages"], stat["bytes"]
        last = last_by_acc.get(a.id)
        acc_out.append({
            **_account_view(a, user),
            "messages": cnt, "bytes": by, "bytes_h": human_size(by),
            "last_run": ({"status": last["status"], "finished_at": last["finished_at"],
                          "messages_new": last["messages_new"]} if last else None),
        })
    counts = svc.db.count_jobs_by_status()
    active = [serialize_job(j, svc.db.account_names(), user) for j in svc.db.active_jobs()]
    if user.get("role") == "mailbox":
        aid = user.get("account_id")
        active = [j for j in active if j["account_id"] == aid]
        # счётчики очереди — общесистемные: показываем только свои задания
        counts = {"running": sum(1 for j in active if j["status"] == "running"),
                  "queued": sum(1 for j in active if j["status"] == "queued")}
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
        # шифрование включено, а ключа нет — копирование писем остановлено
        # (в тексте причины бывает путь к файлу ключа — только администратору)
        "encryption_blocked": (str(getattr(svc.store, "encryption_blocked", "") or "")
                               if user.get("role") == "admin" else ""),
        "replica_alert": _replica_alert(svc) if user.get("role") == "admin" else "",
    }


#: Через сколько часов без удачного прогона копия вне сервера считается отставшей.
REPLICA_ALERT_HOURS = 48


def _replica_alert(svc) -> str:
    """Текст плашки на дашборде, если копия вне сервера включена и не в порядке."""
    if not bool(svc.rt("replica", "enabled")):
        return ""
    try:
        last = json.loads(svc.db.get_meta("replica_last_run") or "null")
    except ValueError:
        last = None
    if not last:
        return ""
    last_ok = svc.db.get_meta("replica_last_ok") or ""
    stale = True
    if last_ok:
        try:
            stale = datetime.now(timezone.utc) - datetime.fromisoformat(last_ok) > timedelta(hours=REPLICA_ALERT_HOURS)
        except ValueError:
            stale = True
    if last.get("status") == "failed":
        return f"Последний прогон завершился ошибкой: {str(last.get('message') or '')[:400]}"
    if stale:
        return f"Удачного прогона не было больше {REPLICA_ALERT_HOURS} ч."
    if last.get("deletions_blocked"):
        return (f"Удаление в копии приостановлено: прогон собирался удалить {last['deletions_blocked']} файлов. "
                f"Если это ожидаемо — разрешите удаление в настройках копии.")
    return ""


@router.get("/live")
def live(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Лёгкий снимок для живого обновления, когда WebSocket недоступен.

    Раньше запасной опрос каждые 3,5 с запрашивал весь /state (сводку по всем
    ящикам — сотни миллисекунд на большом архиве), а использовал из неё только
    активные задания и счётчики.
    """
    from .ws import collect_live
    return collect_live(svc_dep(request), user)


#: Поля карточки ящика, которые сотруднику (вход по ящику) не показываем:
#: заметки администратора и технические настройки OAuth2.
_ADMIN_ONLY_ACCOUNT_FIELDS = ("notes", "oauth_client_id", "oauth_token_url", "has_oauth_secret")


def _account_view(acc: Account, user: dict) -> dict:
    data = acc.redacted()
    if user.get("role") == "mailbox":
        for key in _ADMIN_ONLY_ACCOUNT_FIELDS:
            data.pop(key, None)
    return data


def _disk_free(path: str) -> int:
    try:
        from ..util import disk_free_bytes
        return disk_free_bytes(path)
    except Exception:  # noqa: BLE001
        return 0


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
    return Account(
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
        accounts = [a for a in accounts if a.id == user.get("account_id")]
    totals = svc.db.message_totals_by_account()
    last_runs = svc.db.last_runs_by_account()
    out = []
    for a in accounts:
        item = _account_view(a, user)
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
                 user: dict = Depends(auth_mod.require_admin)):
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
    # Каждый секрет обновляется независимо: пустое поле НЕ затирает сохранённый секрет.
    svc.db.update_account(
        acc,
        update_password=bool(body.password),
        update_oauth_secret=bool(body.oauth_client_secret),
        update_oauth_token=bool(body.oauth_refresh_token),
    )
    _after_account_retention_change(svc, before, acc.retention_days)
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
        raise ValidationError(f"Архив ящика удерживается до {acc.hold_until} — удалить его нельзя.",
                              hint="Если удержание больше не нужно, сначала снимите его (меню ящика → «Удержание архива»).")
    if any(j["account_id"] == account_id for j in svc.db.active_jobs()):
        raise ValidationError("По ящику выполняется задание — дождитесь его окончания или отмените.")
    files, size = svc.store.delete_account_files(account_id)
    try:
        import shutil
        shutil.rmtree(svc.store.account_dir(account_id), ignore_errors=True)
    except OSError:
        pass
    svc.db.delete_account(account_id)
    svc.db.add_audit(user["username"], "account_purge", f"{acc.name}: удалено файлов {files} ({human_size(size)})")
    return {"ok": True, "files": files, "bytes": size}


@router.post("/accounts/{account_id}/test")
def test_account(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
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
    svc = svc_dep(request)
    if svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    items = svc.store.list_quarantines(account_id)
    return {"quarantines": [{"path": path, "name": os.path.basename(path),
                             "files": files, "bytes": size, "bytes_h": human_size(size)}
                            for path, files, size in items]}


class QuarantineBody(BaseModel):
    path: str


@router.post("/accounts/{account_id}/quarantines/delete")
def delete_account_quarantine(request: Request, account_id: int, body: QuarantineBody,
                              user: dict = Depends(auth_mod.require_admin)):
    """Удалить карантинную копию — по явной команде администратора."""
    svc = svc_dep(request)
    if svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    known = {path for path, _f, _b in svc.store.list_quarantines(account_id)}
    if body.path not in known:
        raise ValidationError("Такой карантинной копии у этого ящика нет.",
                              hint="Обновите список карантинных копий.")
    svc.store.drop_quarantine(body.path)
    svc.db.add_audit(user["username"], "quarantine_delete", body.path[:500])
    return {"ok": True}


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
    acc = svc.db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
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
    acc = svc.require_account(account_id, need_secret=True)
    rebuild = (body.rebuild if body else "").strip().lower()
    if rebuild not in ("", "missing", "full"):
        raise ValidationError(f"Неизвестный режим пересоздания копии: «{rebuild}».",
                              hint="Допустимо: пусто (обычная копия), missing, full.")
    if rebuild == "full":
        # Стирание локальной копии — не то действие, которое можно доверить
        # владельцу ящика: письма, удалённые на сервере, после него не вернуть.
        if (user.get("role") or "") != "admin":
            raise HTTPException(403, "Полное пересоздание копии доступно только администратору")
        svc.db.add_audit(user["username"], "backup_rebuild_full_request", acc.name)
    _mailbox_flood_guard(svc, user, JobType.BACKUP, account_id)
    params = {"rebuild": rebuild} if rebuild else {}
    max_attempts = int(svc.rt("backup", "retry_attempts") or 1)
    # Пересоздание не повторяем автоматически: повтор «полной» копии стёр бы
    # уже скачанное во второй раз.
    if rebuild:
        max_attempts = 1
    jid = svc.queue.enqueue(JobType.BACKUP, account_id, params,
                            max_attempts=max_attempts, created_by=user["username"])
    return {"ok": True, "job_id": jid, "rebuild": rebuild}


@router.post("/accounts/backup-all")
def start_backup_all(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Поставить в очередь резервное копирование СРАЗУ ВСЕХ включённых ящиков.

    Выключенные ящики пропускаются (они намеренно исключены из копирования).
    Ящики, по которым копирование уже идёт или стоит в очереди, тоже
    пропускаются — чтобы повторное нажатие кнопки не удваивало работу.
    """
    svc = svc_dep(request)
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
    _ensure_account_access(user, account_id)
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
    svc.require_account(account_id, need_secret=True)
    # Заливка в ИСХОДНЫЕ папки живого ящика возможна только явным режимом
    # "original". Раньше очистка поля «Префикс папки» молча приводила к тому
    # же результату: весь архив уходил прямо в рабочий INBOX, и отменить это
    # уже нельзя.
    data = validate_restore_options(body.model_dump())
    _mailbox_flood_guard(svc, user, JobType.RESTORE, account_id)
    mode = data["target_mode"]
    if mode == "original" and not body.dry_run:
        svc.db.add_audit(user["username"], "restore_original", f"account={account_id}")
    jid = svc.queue.enqueue(JobType.RESTORE, account_id, data, created_by=user["username"])
    return {"ok": True, "job_id": jid}


@router.post("/accounts/{account_id}/verify")
def start_verify(request: Request, account_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    _mailbox_flood_guard(svc, user, JobType.VERIFY, account_id)
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
    """Срок хранения ящика стал длиннее — письма, вычищенные по старому сроку и
    ещё лежащие на сервере, снова должны скачаться при следующем копировании."""
    from ..queue.jobs import effective_retention_days
    old_effective = effective_retention_days(svc, before)
    after = svc.db.get_account(before.id)
    if after is None:
        return
    if _longer_retention(old_effective, effective_retention_days(svc, after)):
        svc.db.clear_retired(before.id)


@router.post("/accounts/{account_id}/import-pst")
def import_pst(request: Request, account_id: int, file: UploadFile = File(...),
               target_prefix: str = Form("Импорт PST"), user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    # Разбор .pst выполняет сторонняя утилита (readpst) на присланном файле —
    # это не то, что стоит открывать каждому сотруднику.
    _require_not_mailbox(user, "Импорт .pst")
    _ensure_account_access(user, account_id)
    svc.require_account(account_id)
    # имя файла приходит от клиента: берём только базовое имя и чистим его,
    # иначе «../../» в имени увело бы запись за пределы каталога временных файлов
    origin_name = os.path.basename(file.filename or "")
    if not origin_name.lower().endswith(".pst"):
        raise ValidationError("Ожидается файл .pst", hint="Выберите файл с расширением .pst")
    fname = safe_filename(origin_name, default="import.pst")
    # uuid в имени: почти все файлы называются «Outlook.pst», и вторая загрузка
    # в тот же ящик затирала первую (первое задание импортировало чужой файл).
    dest = os.path.join(svc.cfg.tmp_dir, f"import_{account_id}_{uuid.uuid4().hex}_{fname}")
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
                        f"Файл слишком большой: допустимо не более {human_size(MAX_PST_UPLOAD_BYTES)}.",
                        hint="Разделите архив .pst на части и импортируйте их по очереди.")
                fh.write(chunk)
    except BaseException:
        # частично записанный файл не должен оставаться на диске
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
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
    headers = {"Content-Disposition": _attachment_disposition(filename)}
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(chunks, media_type=_safe_media_type(ctype), headers=headers)


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
    # Файл открывается до ответа: без ключа или при пропавшем файле клиент
    # получает понятную ошибку, а не «200 OK» с оборванным телом.
    chunks = open_source_stream(lambda: svc.store.open_message(account_id, stored))
    return StreamingResponse(chunks,
                             media_type="message/rfc822",
                             headers={"Content-Disposition": f'attachment; filename="message_{pk}.eml"',
                                      "Content-Length": str(size)})


# =====================================================================
#  Задания
# =====================================================================
def visible_author(author: str, viewer: Optional[dict]) -> str:
    """Кто запустил задание — как его видит пользователь.

    Сотруднику (вход по ящику) не показываем логины администраторов: только
    «вы», «расписание» или безличное «администратор».
    """
    author = author or ""
    if not viewer or viewer.get("role") != "mailbox":
        return author
    if author in ("", "scheduler", "system") or author == (viewer.get("username") or ""):
        return author
    return "администратор"


def serialize_job(row, names: Optional[Dict[int, str]] = None, viewer: Optional[dict] = None) -> dict:
    return {
        "id": row["id"], "type": row["type"], "type_label": JobType.LABELS.get(row["type"], row["type"]),
        "account_id": row["account_id"],
        # имя ящика — с сервера: после обновления страницы список ящиков в
        # браузере ещё пуст, и колонка «Ящик» показывала «—»
        "account_name": (names or {}).get(row["account_id"], "") if row["account_id"] else "",
        "status": row["status"],
        "status_label": JobStatus.LABELS.get(row["status"], row["status"]),
        "priority": row["priority"], "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "progress_current": row["progress_current"],
        "progress_total": row["progress_total"], "progress_message": row["progress_message"],
        "bytes_done": row["bytes_done"], "bytes_done_h": human_size(row["bytes_done"] or 0),
        "speed": row["speed"], "speed_h": human_size(row["speed"] or 0) + "/с",
        "error": row["error"], "attempts": row["attempts"], "max_attempts": row["max_attempts"],
        "created_by": visible_author(row["created_by"], viewer),
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
    limit = max(1, min(1000, int(limit)))
    rows = svc.db.list_jobs(status=status, job_type=type, account_id=account_id, limit=limit)
    names = svc.db.account_names()
    return [serialize_job(r, names, user) for r in rows]


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    data = serialize_job(row, svc.db.account_names(), user)
    try:
        data["result"] = json.loads(row["result"] or "{}")
    except json.JSONDecodeError:
        data["result"] = {}
    data["params"] = json.loads(row["params"] or "{}")
    return data


@router.get("/jobs/{job_id}/events")
def job_events(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    rows = svc.db.list_job_events(job_id, limit=500)
    return [{"ts": r["ts"], "level": r["level"], "message": r["message"]} for r in rows][::-1]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        # Иначе сотрудник мог бы снимать каждое копирование своего ящика по
        # расписанию сразу после старта — и архив перестал бы пополняться.
        raise HTTPException(403, "Отменить можно только задание, запущенное вами")
    svc.queue.cancel(job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        raise HTTPException(403, "Повторить можно только задание, запущенное вами")
    if row["status"] not in JobStatus.TERMINAL:
        raise ValidationError("Повторить можно только завершённое задание.")
    if user.get("role") == "mailbox":
        # те же ограничения, что и при запуске: иначе «выгрузить → отменить →
        # повторить» давало десятки одновременных выгрузок одного ящика
        _mailbox_flood_guard(svc, user, row["type"], row["account_id"])
    # Ручной повтор: снова все попытки и снятый флаг отмены.
    svc.queue.retry(job_id)
    svc.db.add_audit(user["username"], "job_retry", f"#{job_id} {row['type']}")
    return {"ok": True}


# =====================================================================
#  Экспорты
# =====================================================================
_EXPORT_STATUS = {"pending": "готовится", "running": "готовится", "success": "готово",
                  "partial": "готово частично", "failed": "ошибка", "cancelled": "отменён"}


@router.get("/exports")
def list_exports(request: Request, account_id: Optional[int] = None, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    if user.get("role") == "mailbox":
        account_id = user.get("account_id")
    rows = svc.db.list_exports(account_id, limit=200)
    mailbox = user.get("role") == "mailbox"
    authors = {}
    job_ids = [r["job_id"] for r in rows if r["job_id"]]
    if job_ids:
        marks = ",".join("?" * len(job_ids))
        for j in svc.db.query(f"SELECT id, created_by FROM jobs WHERE id IN ({marks})", tuple(job_ids)):
            authors[j["id"]] = j["created_by"] or ""
    out = []
    names = svc.db.account_names()
    for r in rows:
        author = authors.get(r["job_id"], "") or (r["created_by"] if "created_by" in r.keys() else "") or ""
        item = {"id": r["id"], "account_id": r["account_id"], "account_name": names.get(r["account_id"], ""),
                "engine": r["engine"], "format": r["format"],
                "filename": os.path.basename(r["path"] or ""), "size": r["size"],
                "size_h": human_size(r["size"] or 0), "status": r["status"],
                "status_label": _EXPORT_STATUS.get(r["status"], r["status"]), "error": r["error"],
                "created_at": r["created_at"], "created_by": visible_author(author, user),
                "exists": bool(r["path"] and os.path.exists(r["path"])),
                "can_delete": (not mailbox) or author == (user.get("username") or "")}
        if not mailbox:
            # полный путь на сервере — только администратору
            item["path"] = r["path"]
        out.append(item)
    return out


@router.get("/exports/{export_id}/download")
def download_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    # Проверка доступа — сразу после получения записи: если сначала проверять
    # наличие файла, перебором id можно отличить чужой существующий экспорт.
    if r is None:
        raise HTTPException(404, "Файл экспорта не найден")
    _ensure_account_access(user, r["account_id"])
    if not r["path"] or not os.path.exists(r["path"]):
        raise HTTPException(404, "Файл экспорта не найден")
    return FileResponse(r["path"], filename=os.path.basename(r["path"]), media_type="application/octet-stream")


@router.delete("/exports/{export_id}")
def delete_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    if r is None:
        raise HTTPException(404, "Экспорт не найден")
    _ensure_account_access(user, r["account_id"])
    if user.get("role") == "mailbox":
        job = svc.db.get_job(r["job_id"]) if r["job_id"] else None
        if not _is_own_job(user, job):
            # выгрузку мог сделать администратор (например, для юристов) —
            # сотрудник не должен её удалять
            raise HTTPException(403, "Удалить можно только выгрузку, сделанную вами")
    if r["path"] and os.path.exists(r["path"]):
        try:
            os.unlink(r["path"])
        except OSError:
            pass
    svc.db.delete_export(export_id)
    svc.db.add_audit(user["username"], "export_delete", str(export_id))
    return {"ok": True}


@router.get("/export/engines")
def export_engines(request: Request, user: dict = Depends(auth_mod.require_user)):
    return list_engines()


@router.get("/export/defaults")
def export_defaults(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Значения по умолчанию для диалога экспорта (из раздела «Настройки»).

    Раньше «Движок экспорта по умолчанию», «Формат PST» и «Целевая версия
    Outlook» в настройках сохранялись, но диалог их не учитывал.
    """
    svc = svc_dep(request)
    return {"engine": str(svc.rt("export", "default_engine") or "auto"),
            "pst_format": str(svc.rt("export", "pst_format") or "unicode"),
            "outlook_target": str(svc.rt("export", "outlook_target") or "2016+"),
            "pst_split_size_mb": int(svc.rt("export", "pst_split_size_mb") or 0)}


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


def _validate_cron(expr: str) -> None:
    """Проверить cron-выражение (5 полей и понятный APScheduler синтаксис)."""
    expr = (expr or "").strip()
    hint = "Пример: «0 3 * * *» — каждый день в 03:00."
    if len(expr.split()) != 5:
        raise ValidationError(f"Некорректное cron-выражение: «{expr}» (нужно 5 полей).", hint=hint)
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"Некорректное cron-выражение: «{expr}» ({exc}).", hint=hint) from exc



def validate_restore_options(data: dict) -> dict:
    """Проверить и нормализовать параметры восстановления.

    Общая для ручного запуска и для расписаний: заливка в ИСХОДНЫЕ папки живого
    ящика должна быть только осознанным выбором, а пустой префикс — не молчаливым
    синонимом этого выбора.
    """
    out = dict(data or {})
    mode = str(out.get("target_mode") or "prefixed").strip().lower()
    if mode not in ("original", "single", "prefixed"):
        raise ValidationError(f"Неизвестный режим восстановления «{out.get('target_mode')}».",
                              hint="Допустимо: original, single, prefixed.")
    out["target_mode"] = mode
    out["target_prefix"] = str(out.get("target_prefix") or "").strip()
    out["target_folder"] = str(out.get("target_folder") or "").strip()
    if mode == "prefixed" and not out["target_prefix"]:
        raise ValidationError("Укажите префикс папок — иначе письма попадут прямо в рабочие папки ящика.",
                              hint="Например «Восстановлено». Для заливки в исходные папки выберите режим «в исходные папки».")
    if mode == "single" and not out["target_folder"]:
        raise ValidationError("Укажите папку назначения.", hint="Например «Восстановлено».")
    try:
        limit = int(out.get("limit") or 0)
    except (TypeError, ValueError):
        raise ValidationError("Ограничение числа писем должно быть целым числом.")
    if limit < 0:
        raise ValidationError("Ограничение числа писем не может быть отрицательным.")
    out["limit"] = limit
    return out


#: Что можно запускать по расписанию. Остальное (выгрузка, перешифровка,
#: импорт .pst, пересоздание копии) — только вручную: раньше API принимал любой
#: тип и любые параметры, например backup {"rebuild": "full"} в обход проверок
#: ручного запуска или выгрузку каждую минуту.
SCHEDULE_JOB_TYPES = (JobType.BACKUP, JobType.RETENTION, JobType.VERIFY, JobType.RESTORE)
#: Самый длинный интервал расписания — год.
MAX_SCHEDULE_INTERVAL_S = 366 * 86400


def _validate_schedule(body: ScheduleBody, existing_options: Optional[dict] = None) -> dict:
    """Проверить расписание ДО записи в БД; вернуть итоговые параметры задания.

    Иначе некорректное расписание (кривой cron, слишком маленький интервал)
    принимается, а планировщик молча не может построить триггер — задание
    никогда не срабатывает.
    """
    if body.job_type not in SCHEDULE_JOB_TYPES:
        raise ValidationError(f"По расписанию нельзя запускать задание «{body.job_type}».",
                              hint="Допустимо: копирование (backup), очистка (retention), "
                                   "проверка целостности (verify), восстановление (restore).")
    options = body.options if body.options is not None else dict(existing_options or {})
    if not isinstance(options, dict):
        raise ValidationError("Параметры задания должны быть объектом.")
    if body.job_type == JobType.RESTORE:
        # Без этой проверки расписание «восстановление раз в сутки» уходило в
        # обработчик с умолчанием target_mode="original" и регулярно заливало
        # весь архив прямо в рабочие папки живого ящика.
        options = validate_restore_options(options)
    else:
        # у копирования, очистки и проверки по расписанию параметров нет
        options = {}
    if body.kind == ScheduleKind.CRON:
        _validate_cron(body.cron_expr)
    elif body.kind == ScheduleKind.INTERVAL:
        if int(body.interval_seconds or 0) < MIN_SCHEDULE_INTERVAL_S:
            raise ValidationError(f"Интервал не может быть меньше {MIN_SCHEDULE_INTERVAL_S // 60} мин.",
                                  hint="Укажите интервал от 1 минуты.")
        if int(body.interval_seconds) > MAX_SCHEDULE_INTERVAL_S:
            raise ValidationError("Интервал не может быть больше года.")
    else:
        raise ValidationError(f"Неизвестный тип расписания: {body.kind}",
                              hint="Допустимые значения: «cron» или «interval».")
    return options


@router.post("/schedules")
def create_schedule(request: Request, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    # Расписаниями определяется, копируется ли ящик вообще и что чистится —
    # у сотрудника доступ к ним только на просмотр.
    _require_not_mailbox(user, "Изменение расписаний")
    svc.require_account(body.account_id)
    options = _validate_schedule(body)
    sid = svc.db.create_schedule(body.account_id, body.kind, body.job_type, body.cron_expr.strip(),
                                 body.interval_seconds, body.enabled, options)
    svc.db.add_audit(user["username"], "schedule_create", f"id={sid} account={body.account_id} type={body.job_type}")
    svc.scheduler.reload()
    return {"ok": True, "id": sid}


@router.put("/schedules/{schedule_id}")
def update_schedule(request: Request, schedule_id: int, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user, "Изменение расписаний")
    existing = svc.db.get_schedule(schedule_id)
    if existing is None:
        raise HTTPException(404, "Расписание не найдено")
    svc.require_account(body.account_id)
    try:
        old_options = json.loads(existing["options"] or "{}")
    except (TypeError, ValueError):
        old_options = {}
    options = _validate_schedule(body, old_options if body.job_type == existing["job_type"] else {})
    # account_id тоже сохраняется: раньше смена ящика в форме молча игнорировалась
    svc.db.update_schedule(schedule_id, account_id=body.account_id, kind=body.kind, job_type=body.job_type,
                           cron_expr=body.cron_expr.strip(), interval_seconds=body.interval_seconds,
                           enabled=body.enabled, options=options)
    # Правка пишется в аудит, как создание и удаление: иначе выключить
    # копирование ящика можно было без следа.
    changes = []
    for key, new_val in (("account_id", body.account_id), ("job_type", body.job_type), ("kind", body.kind),
                         ("cron_expr", body.cron_expr.strip()), ("interval_seconds", body.interval_seconds),
                         ("enabled", 1 if body.enabled else 0)):
        if existing[key] != new_val and not (key == "interval_seconds" and body.kind == ScheduleKind.CRON):
            changes.append(f"{key}: {existing[key]} → {new_val}")
    svc.db.add_audit(user["username"], "schedule_update",
                     f"id={schedule_id} account={body.account_id}" + (" " + "; ".join(changes) if changes else ""))
    svc.scheduler.reload()
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(request: Request, schedule_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user, "Изменение расписаний")
    existing = svc.db.get_schedule(schedule_id)
    if existing is None:
        raise HTTPException(404, "Расписание не найдено")
    svc.db.delete_schedule(schedule_id)
    svc.db.add_audit(user["username"], "schedule_delete", f"id={schedule_id} account={existing['account_id']}")
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
    from ..config import FILE_ONLY_SETTINGS
    result = {}
    for sec in SETTINGS_SECTIONS:
        section = sec["section"]
        result[section] = {}
        for key in sec["keys"]:
            if f"{section}.{key}" in FILE_ONLY_SETTINGS:
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
                        "replica.cron", "notifications.summary_cron"):
            _validate_cron(str(coerced or ""))
        if coerced != svc.rt(*full_key.split(".", 1)) or full_key in SECRET_SETTINGS:
            new_values[full_key] = coerced
    if not new_values:
        return {"ok": True, "changed": []}

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


def _longer_retention(old_days: int, new_days: int) -> bool:
    """Новый срок хранения длиннее прежнего (0 — «хранить всё» — длиннее любого)."""
    if old_days <= 0:
        return False
    return new_days <= 0 or new_days > old_days


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
        ids = [int(user.get("account_id"))]
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
    return data


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


# =====================================================================
#  Мониторинг и сводка
# =====================================================================
@router.get("/monitoring/summary")
def monitoring_summary_preview(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..monitoring import weekly_summary
    svc = svc_dep(request)
    subject, body = weekly_summary(svc)
    return {"subject": subject, "body": body, "last_sent": svc.db.get_meta("summary_last_sent") or "",
            "next_run": svc.scheduler.next_run_of("weekly_summary")}


@router.post("/monitoring/summary")
def monitoring_summary_send(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..monitoring import send_weekly_summary
    svc = svc_dep(request)
    if not svc.notifier.enabled():
        raise ValidationError("Уведомления по e-mail выключены.",
                              hint="Включите их и заполните SMTP в разделе «Уведомления».")
    result = send_weekly_summary(svc)
    svc.db.add_audit(user["username"], "summary_send", "ok" if result["ok"] else result.get("error", "")[:300])
    if not result["ok"]:
        raise ValidationError(f"Сводка не отправлена: {result['error']}",
                              hint="Проверьте настройки SMTP в разделе «Уведомления».")
    return result


# =====================================================================
#  Логи, статистика, аудит
# =====================================================================
@router.get("/logs")
def get_logs(limit: int = 300, level: Optional[str] = None, user: dict = Depends(auth_mod.require_admin)):
    """Общий лог сервиса: содержит имена и хосты всех ящиков и ошибки чужих
    заданий, поэтому доступен только администратору."""
    from ..logging_setup import memory_handler
    limit = max(1, min(2000, int(limit)))
    return memory_handler.tail(limit=limit, level=level)


@router.get("/stats")
def get_stats(request: Request, days: int = 30, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    days = max(1, min(365, int(days)))
    if user.get("role") == "mailbox":
        # пользователь-ящик видит статистику только по своему ящику
        aid = user.get("account_id")
        series = svc.db.daily_series(days=days, account_id=aid)
        totals = {"messages": svc.db.count_messages(aid), "bytes": svc.db.sum_message_bytes(aid)}
    else:
        series = svc.db.daily_series(days=days)
        totals = {"messages": svc.db.count_messages(), "bytes": svc.db.sum_message_bytes()}
    return {
        "series": [{"day": r["day"], "messages": r["messages"], "bytes": r["bytes"],
                    "jobs": r["jobs"], "errors": r["errors"]} for r in series][::-1],
        "totals": totals,
    }


@router.get("/audit")
def get_audit(request: Request, limit: int = 200, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return [{"ts": r["ts"], "user": r["user"], "action": r["action"], "detail": r["detail"]}
            for r in svc.db.list_audit(limit)]


# =====================================================================
#  Аналитика (только администратор)
# =====================================================================
@router.get("/analytics/system")
def analytics_system(request: Request, account_id: Optional[int] = None, days: int = 90,
                     user: dict = Depends(auth_mod.require_admin)):
    """Аналитика по всем параметрам системы (хранилище, задания, прогоны и т.д.)."""
    svc = svc_dep(request)
    from ..analytics import system_analytics
    if account_id is not None and svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    days = max(7, min(365, int(days)))
    return system_analytics(svc, account_id=account_id, days=days)


#: Сколько расчётов «Аналитики писем» считается одновременно. Расчёт обходит
#: весь индекс; без ограничения несколько одновременно открытых вкладок
#: занимали весь пул потоков и съедали память.
_ANALYTICS_SLOTS = threading.BoundedSemaphore(2)


@router.get("/analytics/mail")
def analytics_mail(request: Request, account_id: Optional[int] = None, refresh: bool = False,
                   user: dict = Depends(auth_mod.require_admin)):
    """Аналитика по содержанию писем (метаданные индекса: тема, отправитель, дата…)."""
    svc = svc_dep(request)
    from ..analytics import mail_analytics
    if account_id is not None and svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    # Ждать нельзя: эндпоинт синхронный и занимает поток из пула (по умолчанию
    # их 40). Ожидание семафора внутри потока подвешивало ВЕСЬ синхронный REST —
    # несколько вкладок «Аналитика писем» с обновлением клали интерфейс на
    # десятки секунд. Отказываем сразу, как это уже сделано для входа по ящику.
    if not _ANALYTICS_SLOTS.acquire(blocking=False):
        raise ValidationError("Сейчас уже считается аналитика писем. Повторите через минуту.",
                              hint="Расчёт обходит весь индекс писем и выполняется не более чем в два потока. "
                                   "Готовый результат кэшируется — обычно ждать не приходится.")
    try:
        return mail_analytics(svc, account_id=account_id, use_cache=not refresh)
    finally:
        _ANALYTICS_SLOTS.release()


@router.get("/analytics/mail/deep")
def analytics_mail_deep(request: Request, account_id: Optional[int] = None,
                        user: dict = Depends(auth_mod.require_admin)):
    """Кэш результата глубокого анализа писем (типы вложений, домены, язык, слова тела)."""
    svc = svc_dep(request)
    from ..analytics import load_deep
    data = load_deep(svc, account_id)
    running = [serialize_job(j) for j in svc.db.list_jobs(job_type=JobType.ANALYZE, limit=5)
               if j["status"] in JobStatus.ACTIVE
               and (account_id is None or j["account_id"] == account_id)]
    return {"available": data is not None, "data": data, "running": running}


@router.post("/analytics/mail/scan")
def analytics_mail_scan(request: Request, account_id: Optional[int] = None,
                        user: dict = Depends(auth_mod.require_admin)):
    """Запустить (пересчитать) глубокий анализ писем как фоновое задание."""
    svc = svc_dep(request)
    if account_id is not None:
        svc.require_account(account_id)
    # не плодим дубли: если такой анализ уже в очереди/работе — вернём его
    for j in svc.db.list_jobs(job_type=JobType.ANALYZE, limit=20):
        if j["status"] in JobStatus.ACTIVE and j["account_id"] == account_id:
            return {"ok": True, "job_id": j["id"], "already_running": True}
    jid = svc.queue.enqueue(JobType.ANALYZE, account_id, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "analytics_scan", f"account={account_id}")
    return {"ok": True, "job_id": jid}


@router.get("/analytics/dedup")
def analytics_dedup(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Отчёт «Одинаковые вложения» (последний сохранённый) и ход подсчёта."""
    svc = svc_dep(request)
    from .. import dedup
    job = _active_job(svc, JobType.DEDUP_REPORT)
    return {"report": dedup.load_report(svc), "status": dedup.status(svc),
            "running": [serialize_job(job)] if job else []}


class DedupScanBody(BaseModel):
    full: bool = False          # забыть прочитанное и прочитать весь архив заново


@router.post("/analytics/dedup/scan")
def analytics_dedup_scan(request: Request, body: DedupScanBody, user: dict = Depends(auth_mod.require_admin)):
    """Посчитать (или досчитать) отчёт об одинаковых вложениях фоновым заданием."""
    svc = svc_dep(request)
    job = _active_job(svc, JobType.DEDUP_REPORT)
    if job:
        return {"ok": True, "job_id": job["id"], "already_running": True}
    jid = svc.queue.enqueue(JobType.DEDUP_REPORT, None, {"full": bool(body.full)}, priority=8,
                            created_by=user["username"])
    svc.db.add_audit(user["username"], "dedup_report", "подсчёт заново" if body.full else "подсчёт")
    return {"ok": True, "job_id": jid}


# =====================================================================
#  Сотрудники (только администратор)
#
#  ВНИМАНИЕ: маршруты с постоянными путями (/template.csv, /import, /sync)
#  объявлены ДО /employees/{employee_id} — иначе FastAPI сопоставит их с
#  маршрутом по идентификатору и попытается разобрать «template.csv» как int.
# =====================================================================
def _employee_out(row) -> dict:
    """Карточка сотрудника для интерфейса (вместе с состоянием ящика)."""
    account_enabled = row["account_enabled"]
    return {
        "id": row["id"],
        "external_id": row["external_id"] or "",
        "full_name": row["full_name"] or "",
        "email": row["email"] or "",
        "position": row["position"] or "",
        "department": row["department"] or "",
        "phone": row["phone"] or "",
        "status": row["status"] or "active",
        "account_id": row["account_id"],
        "account_name": row["account_name"] or "" if row["account_id"] else "",
        "account_enabled": bool(account_enabled) if account_enabled is not None else False,
        "notes": row["notes"] or "",
        "source": row["source"] or "manual",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_seen_at": row["last_seen_at"],
        "dismissed_at": _row_get(row, "dismissed_at") or "",
        "account_hold_until": (_row_get(row, "account_hold_until") or "") if row["account_id"] else "",
    }


def _row_get(row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _employee_values(body: "EmployeeBody") -> dict:
    """Нормализованные поля карточки из тела запроса."""
    status = (body.status or "active").strip().lower()
    if status not in EMPLOYEE_STATUSES:
        raise ValidationError(f"Неизвестный статус сотрудника: «{body.status}».",
                              hint="Допустимы «active» (работает) и «archived» (в архиве).")
    full_name = (body.full_name or "").strip()
    if not full_name:
        raise ValidationError("Укажите ФИО сотрудника.")
    email = normalize_email(body.email)
    if email and not looks_like_email(email):
        raise ValidationError(f"Некорректный e-mail: «{body.email}».",
                              hint="Адрес должен быть вида ivanov@example.ru.")
    return {
        "full_name": full_name,
        "email": email,
        "position": (body.position or "").strip(),
        "department": (body.department or "").strip(),
        "phone": (body.phone or "").strip(),
        "external_id": (body.external_id or "").strip(),
        "status": status,
        "notes": (body.notes or "").strip(),
    }


@router.get("/employees")
def list_employees(request: Request, query: Optional[str] = None, status: Optional[str] = None,
                   limit: int = 100, offset: int = 0, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    rows = svc.db.list_employees(query=query, status=status, limit=limit, offset=offset)
    return {
        "employees": [_employee_out(r) for r in rows],
        # total — сколько строк подходит под фильтр (для постраничной навигации),
        # counts — общая сводка по всему справочнику (для плашек в шапке).
        "total": svc.db.count_employees(status=status, query=query),
        "counts": svc.db.employee_counts(),
    }


@router.get("/employees/template.csv")
def employees_template(user: dict = Depends(auth_mod.require_admin)):
    """Файл-образец: заголовки и одна строка для примера."""
    # BOM — чтобы Excel открыл файл в UTF-8, а не показал кракозябры.
    data = "﻿".encode("utf-8") + TEMPLATE_CSV.encode("utf-8")
    return Response(
        content=data, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


@router.post("/employees/import")
def import_employees(request: Request, file: UploadFile = File(...),
                     user: dict = Depends(auth_mod.require_admin)):
    """Загрузить файл CSV/XLSX и сразу применить его к справочнику."""
    svc = svc_dep(request)
    # имя файла приходит от клиента: берём только базовое имя и чистим его,
    # иначе «../../» в имени увело бы запись за пределы каталога временных файлов
    origin_name = os.path.basename(file.filename or "")
    fname = safe_filename(origin_name, default="employees.csv")
    dest = os.path.join(svc.cfg.tmp_dir, f"employees_{uuid.uuid4().hex}_{fname}")
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
                        hint="Выгрузите сотрудников по частям или уберите лишние листы и колонки.")
                fh.write(chunk)
        if not written:
            raise ValidationError("Файл пуст.", hint="Выберите выгрузку из кадровой системы (CSV или XLSX).")
        rows, problems = parse_employee_file(dest, origin_name or fname)
        # wait=False: если идёт синхронизация по расписанию — сразу понятный
        # отказ, а не зависший на минуты запрос
        result = sync_employees(svc, rows, create_accounts=bool(svc.rt("employees", "create_accounts")),
                                wait=False)
    finally:
        # временный файл не нужен ни при успехе, ни при ошибке
        try:
            os.unlink(dest)
        except OSError:
            pass
    problems = problems + list(result["problems"])
    svc.db.add_audit(user["username"], "employees_import",
                     f"файл={origin_name or fname} создано={result['created']} обновлено={result['updated']}")
    return {"ok": True, "created": result["created"], "updated": result["updated"],
            "accounts_created": result["accounts_created"], "accounts_linked": result["accounts_linked"],
            "total_rows": result["total_rows"],
            # Список режем: на кривом файле в 20 МБ это сотни тысяч записей —
            # и в памяти, и в JSON-ответе.
            "problems": problems[:200], "problem_count": len(problems),
            "skipped_inactive": result.get("skipped_inactive", 0),
            "duplicate_rows": result.get("duplicate_rows", 0),
            "duplicate_emails": result.get("duplicate_emails", [])[:50],
            "warnings": result.get("warnings", [])}


def _employee_source(svc) -> dict:
    """Текущий источник списка сотрудников в виде, пригодном для интерфейса.

    Пароль наружу не отдаём — только признак того, что он задан.
    """
    stype = str(svc.rt("employees", "source_type") or "file").strip().lower()
    if stype not in ("file", "url"):
        stype = "file"
    path = str(svc.rt("employees", "source_file") or "").strip()
    url = str(svc.rt("employees", "source_url") or "").strip()
    # Адрес отдаём БЕЗ секретов: выгрузку часто открывают ссылкой с токеном,
    # и этот токен не должен оседать в интерфейсе, аудите и журнале.
    safe_url = redact_url(url)
    return {
        "type": stype,
        "path": path,
        "url": safe_url,
        "target": safe_url if stype == "url" else path,
        "configured": bool(url if stype == "url" else path),
        "auth": bool(str(svc.rt("employees", "source_url_user") or "").strip()),
        "verify_ssl": bool(svc.rt("employees", "source_url_verify_ssl")),
        "format": str(svc.rt("employees", "source_url_format") or "auto"),
        "timeout_s": int(svc.rt("employees", "source_url_timeout_s") or 60),
        "sync_enabled": bool(svc.rt("employees", "sync_enabled")),
        "cron": str(svc.rt("employees", "cron") or ""),
    }


def _check_employee_source(svc) -> dict:
    """Проверить источник: он доступен и читается? Справочник не меняем."""
    src = _employee_source(svc)
    if src["type"] == "url":
        if not src["url"]:
            raise ValidationError(
                "Не задан адрес выгрузки сотрудников.",
                hint="Заполните «Адрес выгрузки (URL)» в настройках, раздел «Сотрудники», "
                     "либо переключите источник на «Файл на сервере».")
        # Скачиваем по НАСТОЯЩЕМУ адресу: в src["url"] он уже с вырезанными
        # секретами (token=***), и проверка ходила не туда — сервер отвечал 403,
        # хотя сама синхронизация работала.
        data, name = fetch_employee_source(
            str(svc.rt("employees", "source_url") or "").strip(),
            username=str(svc.rt("employees", "source_url_user") or ""),
            password=str(svc.rt("employees", "source_url_password") or ""),
            verify_ssl=src["verify_ssl"], timeout_s=src["timeout_s"], fmt=src["format"])
        rows, problems = parse_employee_file(data, name)
        return {"ok": True, "source": src, "filename": name, "bytes": len(data),
                "rows": len(rows), "problems": problems[:50], "problem_count": len(problems)}

    if not src["path"]:
        raise ValidationError(
            "Не задан файл со списком сотрудников.",
            hint="Укажите путь к файлу CSV/XLSX в настройках, раздел «Сотрудники» → «Файл-источник». "
                 "Либо загрузите файл кнопкой «Импорт из файла».")
    if not os.path.isfile(src["path"]):
        raise ValidationError(
            f"Файл со списком сотрудников не найден: {src['path']}",
            hint="Проверьте путь и права доступа: файл читает служба mailarchiver.")
    rows, problems = parse_employee_file(src["path"], os.path.basename(src["path"]))
    return {"ok": True, "source": src, "filename": os.path.basename(src["path"]),
            "bytes": os.path.getsize(src["path"]), "rows": len(rows),
            "problems": problems[:50], "problem_count": len(problems)}


@router.get("/employees/source")
def employee_source(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Какой источник списка настроен — для подсказок в разделе «Сотрудники»."""
    return {"source": _employee_source(svc_dep(request))}


@router.post("/employees/source/check")
def employee_source_check(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверить источник, ничего не записывая в справочник.

    Нужна, чтобы администратор убедился в правильности адреса или пути до
    того, как включит синхронизацию по расписанию: иначе первая же ошибка
    всплыла бы ночью, в журнале планировщика.
    """
    result = _check_employee_source(svc_dep(request))
    svc_dep(request).db.add_audit(user["username"], "employees_source_check",
                                  f"{result['source']['type']}: {result['source']['target']}"[:500])
    return result


@router.get("/employees/account-template")
def employee_account_template(request: Request, full_name: str = "Иванов Иван Иванович",
                              email: str = "ivanov@example.ru", position: str = "Менеджер",
                              department: str = "Отдел продаж",
                              user: dict = Depends(auth_mod.require_admin)):
    """Показать, какой ящик получится по шаблону из настроек.

    Нужен, чтобы не проверять шаблон «вживую»: ошибку в подстановке иначе
    видно только после синхронизации, когда ящики уже созданы.
    """
    svc = svc_dep(request)
    row = {"position": position, "department": department, "external_id": "1234"}
    return {"preview": preview_account_template(svc, full_name=full_name, email=email, row=row),
            "placeholders": list(ACCOUNT_PLACEHOLDERS),
            "create_accounts": bool(svc.rt("employees", "create_accounts"))}


@router.post("/employees/sync")
def sync_employees_now(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Поставить в очередь синхронизацию с источником, указанным в настройках.

    Источник — файл на сервере или адрес выгрузки; что именно, решает настройка
    «Источник списка». Наличие источника проверяем здесь же, чтобы ошибка
    показалась сразу в интерфейсе, а не только в журнале задания.
    """
    svc = svc_dep(request)
    src = _employee_source(svc)
    if src["type"] == "url":
        if not src["url"]:
            raise ValidationError(
                "Не задан адрес выгрузки сотрудников.",
                hint="Заполните «Адрес выгрузки (URL)» в настройках, раздел «Сотрудники», "
                     "либо переключите источник на «Файл на сервере».")
    else:
        if not src["path"]:
            raise ValidationError(
                "Не задан файл со списком сотрудников.",
                hint="Укажите путь к файлу CSV/XLSX в настройках, раздел «Сотрудники» → «Файл-источник». "
                     "Либо загрузите файл кнопкой «Импорт из файла».")
        if not os.path.isfile(src["path"]):
            raise ValidationError(
                f"Файл со списком сотрудников не найден: {src['path']}",
                hint="Проверьте путь и права доступа: файл читает служба mailarchiver.")
    # Синхронизация уже идёт или ждёт очереди — вторая копия не нужна (очередь
    # всё равно не запустит их одновременно, но двойной клик ставил две).
    busy = [j for j in svc.db.active_jobs() if j["type"] == JobType.SYNC_EMPLOYEES]
    if busy:
        return {"ok": True, "job_id": busy[0]["id"], "already": True, "source": src}
    jid = svc.queue.enqueue(JobType.SYNC_EMPLOYEES, None, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "employees_sync_start", f"{src['type']}: {src['target']}"[:500])
    return {"ok": True, "job_id": jid, "source": src}


@router.post("/employees")
def create_employee(request: Request, body: EmployeeCreateBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    values = _employee_values(body)
    if body.create_account and not values["email"]:
        # проверяем ДО создания карточки: иначе карточка появлялась, а запрос
        # всё равно завершался ошибкой
        raise ValidationError("Чтобы завести ящик, укажите e-mail сотрудника.")
    employee_id = svc.db.create_employee(source="manual", **values)
    account_id = None
    warning = ""
    if body.create_account:
        # ящик создаётся по тому же шаблону, что и при синхронизации
        from ..employees import account_template
        tpl = account_template(svc)
        account_id, action = ensure_account_for_employee(
            svc, employee_id, values["full_name"], values["email"], row=values, tpl=tpl,
            create=bool(str(tpl.get("host") or "").strip()))
        if action == "no_host":
            warning = ("Карточка создана, но ящик не заведён: в шаблоне ящиков не указан IMAP-сервер "
                       "(кнопка «Шаблон ящиков» в разделе «Сотрудники»).")
        elif action == "created" and tpl.get("schedule_enabled"):
            try:
                svc.scheduler.reload()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось перечитать расписания")
    svc.db.add_audit(user["username"], "employee_create", values["full_name"])
    return {"ok": True, "id": employee_id, "account_id": account_id, "warning": warning}


@router.put("/employees/{employee_id}")
def update_employee(request: Request, employee_id: int, body: EmployeeBody,
                    user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    before = svc.db.get_employee(employee_id)
    if before is None:
        raise HTTPException(404, "Сотрудник не найден")
    values = _employee_values(body)
    status = values.pop("status")
    svc.db.update_employee(employee_id, **values)
    svc.db.add_audit(user["username"], "employee_update", values["full_name"])
    result: Dict[str, Any] = {"ok": True}
    # Увольнение и возврат — со всеми последствиями для ящика (удержание архива,
    # последняя копия и выключение копирования).
    from ..employees import dismiss_employee, rehire_employee
    if status == "archived" and before["status"] != "archived":
        result["dismissed"] = dismiss_employee(svc, employee_id, by=user["username"], reason="вручную")
    elif status == "active" and before["status"] == "archived":
        result["rehired"] = rehire_employee(svc, employee_id, by=user["username"])
    return result


@router.delete("/employees/{employee_id}")
def delete_employee(request: Request, employee_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Удалить карточку сотрудника. Почтовый ящик и локальные копии писем
    остаются на месте — их удаляют отдельно в разделе «Ящики»."""
    svc = svc_dep(request)
    row = svc.db.get_employee(employee_id)
    if row is None:
        raise HTTPException(404, "Сотрудник не найден")
    svc.db.delete_employee(employee_id)
    svc.db.add_audit(user["username"], "employee_delete", row["full_name"] or str(employee_id))
    return {"ok": True}


@router.post("/employees/{employee_id}/link-account")
def link_employee_account(request: Request, employee_id: int, account_id: int = 0,
                          user: dict = Depends(auth_mod.require_admin)):
    """Привязать сотруднику существующий ящик (account_id=0 — отвязать)."""
    svc = svc_dep(request)
    row = svc.db.get_employee(employee_id)
    if row is None:
        raise HTTPException(404, "Сотрудник не найден")
    if account_id:
        if svc.db.get_account(int(account_id)) is None:
            raise HTTPException(404, "Ящик не найден")
        svc.db.set_employee_account(employee_id, int(account_id))
    else:
        # Отвязка НЕ удаляет ящик: он просто перестаёт числиться за сотрудником.
        svc.db.set_employee_account(employee_id, None)
    svc.db.add_audit(user["username"], "employee_link_account",
                     f"employee={employee_id} account={account_id or 'нет'}")
    return {"ok": True}


# =====================================================================
#  Пользователи (только администратор)
# =====================================================================
@router.get("/users")
def list_users(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return [{"id": r["id"], "username": r["username"], "role": r["role"], "created_at": r["created_at"],
             "last_login": r["last_login"], "disabled": bool(r["disabled"]),
             "totp_enabled": bool(r["totp_enabled"])} for r in svc.db.list_users()]


@router.post("/users/{user_id}/2fa/reset")
def reset_user_2fa(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Сбросить 2FA другого пользователя (потерял телефон). Сеансы завершаются."""
    svc = svc_dep(request)
    if user_id == user["id"]:
        raise ValidationError("Свой двухфакторный вход отключается в профиле (нужны пароль и код).")
    target = svc.db.get_user_by_id(user_id)
    if target is None:
        raise ValidationError("Пользователь не найден.")
    svc.db.disable_totp(user_id)
    svc.db.delete_user_sessions(user_id)
    svc.db.add_audit(user["username"], "2fa_reset", target["username"])
    return {"ok": True}


@router.post("/users")
def create_user(request: Request, body: UserBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    role = (body.role or "admin").strip().lower()
    if role != "admin":
        # вход по ящику (role=mailbox) выполняется по email и паролю ящика,
        # отдельная запись пользователя для этого не создаётся и не работает
        raise ValidationError("Здесь можно создать только пользователя с ролью «admin».",
                              hint="Для доступа к своему ящику пользователь входит по email и паролю ящика.")
    # Сначала strip, потом проверки: логин « adm» раньше проходил проверку
    # уникальности и падал в БД с ошибкой 500, а «   » создавал пустое имя.
    username = (body.username or "").strip()
    if not username:
        raise ValidationError("Укажите имя пользователя.")
    if svc.db.get_user_by_name(username):
        raise ValidationError("Пользователь с таким именем уже существует.")
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    try:
        uid = svc.db.create_user(username, hash_password(body.password), role)
    except sqlite3.IntegrityError:
        raise ValidationError("Пользователь с таким именем уже существует.")
    svc.db.add_audit(user["username"], "user_create", username)
    return {"ok": True, "id": uid}


@router.put("/users/{user_id}/password")
def set_user_password(request: Request, user_id: int, response: Response, body: PasswordBody,
                      user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    # set_user_password завершает все сеансы пользователя (иначе старая cookie
    # продолжала бы работать после смены пароля). Если администратор меняет
    # пароль СЕБЕ, тут же выдаём новую сессию — иначе его выбрасывало бы из
    # интерфейса сразу после сохранения.
    svc.db.set_user_password(user_id, hash_password(body.password))
    target = svc.db.get_user_by_id(user_id)
    svc.db.add_audit(user["username"], "user_password", (target["username"] if target else str(user_id)))
    if user_id == user["id"]:
        auth_mod.create_session(svc, response, user_id, request, role=user.get("role", "admin"))
        return {"ok": True, "sessions_closed": True, "self": True}
    return {"ok": True, "sessions_closed": True}


@router.post("/users/{user_id}/logout-all")
def logout_all_sessions(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Завершить все открытые сеансы пользователя."""
    svc = svc_dep(request)
    target = svc.db.get_user_by_id(user_id)
    if not target:
        raise ValidationError("Пользователь не найден.")
    closed = svc.db.delete_user_sessions(user_id)
    svc.db.add_audit(user["username"], "user_logout_all", target["username"])
    return {"ok": True, "closed": closed}


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int, disabled: bool = True, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    if user_id == user["id"] and disabled:
        raise ValidationError("Нельзя отключить самого себя.")
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    svc.db.set_user_disabled(user_id, disabled)
    svc.db.add_audit(user["username"], "user_disable" if disabled else "user_enable", str(user_id))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    if user_id == user["id"]:
        raise ValidationError("Нельзя удалить самого себя.")
    if svc.db.count_users() <= 1:
        raise ValidationError("Нельзя удалить последнего пользователя.")
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    svc.db.delete_user(user_id)
    svc.db.add_audit(user["username"], "user_delete", str(user_id))
    return {"ok": True}
