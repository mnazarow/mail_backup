"""Общее для маршрутов API: проверки доступа, ограничения сотрудника и оператора, схемы запросов.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..errors import ValidationError
from ..accountops import credential_problem
from ..models import AuthType, JobType, ScheduleKind, Security
from .. import roles
from . import auth as auth_mod
from ..logging_setup import get_logger

log = get_logger("api")

# Максимальный размер загружаемого .pst. Отдельной настройки для него нет,
# поэтому держим лимит константой: без него один запрос мог бы забить диск.
# Файл больше предела импортируется из каталога импорта на сервере (см. import_pst).
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
SECRET_SETTINGS = {"notifications.smtp_password", "notifications.telegram_bot_token", "ldap.bind_password",
                   "notifications.webhook_secret", "employees.source_url_password",
                   "replica.s3_secret_key", "monitoring.metrics_token", "mailadmin.password",
                   "axigen.admin_password", "axigen.ftp_password",
                   "journal.auth_password", "oidc.client_secret"}
#: Секрет, который уходит на адрес из настроек: при смене адреса его нужно ввести заново
#: (иначе администратор архива мог бы перенаправить пароль администратора почты на свой сервер).
SECRET_BOUND_TO = {"axigen.admin_password": "axigen.host", "axigen.ftp_password": "axigen.host",
                   "mailadmin.password": "mailadmin.host",
                   "notifications.telegram_bot_token": "notifications.telegram_api_url",
                   "notifications.webhook_secret": "notifications.webhook_url",
                   "ldap.bind_password": "ldap.url", "oidc.client_secret": "oidc.issuer"}


def _require_axigen_password(svc, new_values: Dict[str, Any]) -> None:
    """Ослабили или перенаправили соединение с Axigen — пароль вводится заново (см. passwords_to_reenter)."""
    from ..axigen.common import passwords_to_reenter
    what, need = passwords_to_reenter(lambda key: svc.rt("axigen", key), new_values)
    if need:
        raise ValidationError(
            f"Меняется защита соединения с Axigen ({', '.join(what)}) — введите {' и '.join(need)} заново в том же "
            f"сохранении.",
            hint="Сохранённый пароль не отправляется по соединению, которое ослабили или перенаправили, без "
                 "подтверждения. Ничего не сохранено.")


def svc_dep(request: Request):
    return request.app.state.services


def _own_ids(user: dict) -> List[int]:
    """Ящики пользователя-ящика: тот, паролем которого он вошёл, и остальные ящики его карточки сотрудника."""
    return auth_mod.mailbox_ids(user)


def _ensure_account_access(user: dict, account_id: int) -> None:
    """Пользователь-ящик (role=mailbox) имеет доступ только к своим ящикам."""
    if user.get("role") == "mailbox" and account_id not in _own_ids(user):
        raise HTTPException(403, "Доступ разрешён только к своим почтовым ящикам")


def _full_ids(user: dict) -> List[int]:
    """Ящики сотрудника, с которыми можно всё: свои и открытые с полным доступом (1.22.0)."""
    return auth_mod.mailbox_full_ids(user)


def _ensure_full_access(user: dict, account_id: int) -> None:
    """Выгрузка, копирование, восстановление — не для ящика, открытого сотруднику только для чтения."""
    _ensure_account_access(user, account_id)
    if user.get("role") == "mailbox" and auth_mod.mailbox_rights(user, account_id) == "read":
        raise HTTPException(403, "Этот ящик открыт вам только для чтения: выгружать, копировать и восстанавливать "
                                 "его письма нельзя.")


def _ensure_mailbox_server_action(svc, user: dict, account_id: int) -> None:
    """Сотрудник проверяет, копирует и восстанавливает только включённые ящики.

    Выключенный администратором ящик (часто — из-за неверного пароля) сотруднику, вошедшему другим своим
    ящиком, доступен лишь для чтения архива: повторные неудачные входы на почтовый сервер привели бы к
    блокировке адреса архива (fail2ban) — и встало бы копирование всех ящиков. Сам вошедший ящик выключенным
    не бывает: сеанс выключенного ящика не действует.
    """
    if user.get("role") != "mailbox":
        return
    _ensure_full_access(user, account_id)
    acc = svc.db.get_account(int(account_id))
    if acc is not None and not acc.enabled:
        raise HTTPException(403, f"Ящик «{acc.name}» выключен администратором: письма из архива можно читать и "
                                 f"выгружать, а проверять, копировать и восстанавливать — нет.")


def _foreign_target(user: dict, row) -> bool:
    """Задание возвращает письма в ящик, который этому сотруднику не принадлежит (так администратор
    передаёт переписку уволенного коллеге): сотруднику такое задание не показывается вовсе."""
    if row is None or row["type"] != JobType.RESTORE:
        return False
    target = int(roles.job_params(row).get("target_account_id") or 0)
    return bool(target) and target not in _own_ids(user)


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
    if _foreign_target(user, row):
        raise HTTPException(403, "Задание выполнено администратором для другого ящика")


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


#: Только что разрешённые сотруднику задания (ящик, вид) → (время, последний номер задания в базе): задание
#: появляется в базе чуть позже проверки, и залп одновременных запросов иначе проходил её весь —
#: ограничение «одно задание» обходилось. Пока разрешённое задание не появилось в базе (и не дольше
#: нескольких секунд), второе такое же не разрешается; появилось — дальше решает обычная проверка.
_MAILBOX_RESERVE_S = 5.0
_MAILBOX_GUARD_LOCK = threading.Lock()


def _mailbox_flood_guard(svc, user: dict, job_type: str, account_id) -> None:
    """Ограничения для входа по ящику: без них 1500 запросов «скопировать»
    за 3 секунды давали 1500 заданий, а «выгрузить → отменить → повторить» —
    десятки одновременных выгрузок и забитый диск (остановится копирование всех)."""
    if user.get("role") != "mailbox":
        return
    import time as _time
    key = (int(account_id or 0), str(job_type))
    with _MAILBOX_GUARD_LOCK:
        # отметки — у экземпляра службы (своя база), а не у модуля
        reserved: Dict[Tuple[int, str], Tuple[float, int]] = svc.__dict__.setdefault("_mailbox_reserved", {})
        now = _time.monotonic()
        last = int(svc.db.scalar("SELECT COALESCE(MAX(id), 0) FROM jobs") or 0)
        held = reserved.get(key)
        if held is not None and now - held[0] < _MAILBOX_RESERVE_S and last <= held[1]:
            raise ValidationError("Такое задание по вашему ящику уже выполняется или стоит в очереди.",
                                  hint="Дождитесь его завершения.")
        for stale in [k for k, (at, _) in reserved.items() if now - at >= _MAILBOX_RESERVE_S]:
            reserved.pop(stale, None)
        _mailbox_flood_checks(svc, user, job_type, account_id)
        reserved[key] = (now, last)


#: Сотрудник: «Проверить» и проверка папок идут на почтовый сервер (иногда — входом администратора почты),
#: и без предела их можно было запускать без конца. Не больше стольких за окно на ящик и по одной за раз.
MAILBOX_PROBES_PER_WINDOW = 6
MAILBOX_PROBE_WINDOW_S = 600


class _mailbox_probe_guard:
    """``with _mailbox_probe_guard(svc, user, account_id): …`` — предел проверок ящика для сотрудника."""

    def __init__(self, svc, user: dict, account_id: int) -> None:
        self.key = int(account_id) if user.get("role") == "mailbox" else None
        # учёт — у экземпляра службы (своя база), а не у модуля
        self.probes: Dict[int, List[float]] = svc.__dict__.setdefault("_mailbox_probes", {})
        self.busy: set = svc.__dict__.setdefault("_mailbox_probing", set())

    def __enter__(self):
        if self.key is None:
            return self
        import time as _time
        with _MAILBOX_GUARD_LOCK:
            now = _time.monotonic()
            recent = [t for t in self.probes.get(self.key, []) if now - t < MAILBOX_PROBE_WINDOW_S]
            if self.key in self.busy:
                raise ValidationError("Проверка этого ящика уже идёт.", hint="Дождитесь её результата.")
            if len(recent) >= MAILBOX_PROBES_PER_WINDOW:
                wait = int(MAILBOX_PROBE_WINDOW_S - (now - recent[0])) // 60 + 1
                raise ValidationError(f"Ящик проверяли {len(recent)} раз за последние "
                                      f"{MAILBOX_PROBE_WINDOW_S // 60} минут.",
                                      hint=f"Повторите примерно через {wait} мин. Частые входы на почтовый "
                                           f"сервер могут заблокировать адрес архива.")
            recent.append(now)
            self.probes[self.key] = recent
            self.busy.add(self.key)
        return self

    def __exit__(self, *exc) -> None:
        if self.key is not None:
            with _MAILBOX_GUARD_LOCK:
                self.busy.discard(self.key)


def _mailbox_flood_checks(svc, user: dict, job_type: str, account_id) -> None:
    for job in svc.db.active_jobs():
        # задание администратора, возвращающее письма в чужой ящик, сотруднику не видно — и не мешает ему
        if job["type"] == job_type and job["account_id"] == account_id and not _foreign_target(user, job):
            raise ValidationError("Такое задание по вашему ящику уже выполняется или стоит в очереди.",
                                  hint="Дождитесь его завершения.")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    logins = _own_logins(user)
    recent = int(svc.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE created_by IN ({','.join('?' * len(logins))}) "
                               "AND created_at>=?", tuple(logins) + (since,)) or 0)
    if recent >= MAILBOX_JOBS_PER_HOUR:
        raise ValidationError("Слишком много заданий за последний час.",
                              hint="Попробуйте позже или обратитесь к администратору.")
    if job_type == JobType.EXPORT:
        kept = int(svc.db.scalar(f"SELECT COUNT(*) FROM exports WHERE account_id=? AND created_by IN "
                                 f"({','.join('?' * len(logins))}) AND status IN ('success','partial')",
                                 (account_id,) + tuple(logins)) or 0)
        if kept >= MAILBOX_MAX_EXPORTS:
            raise ValidationError(f"У вас уже {kept} готовых выгрузки.",
                                  hint="Скачайте и удалите старые выгрузки в разделе «Экспорт», "
                                       "затем запустите новую.")


def _operator_job_guard(svc, user: dict, job_type: str, account_id, *, need_enabled: bool = False,
                        rebuild: str = "") -> None:
    """Проверки для оператора перед запуском (или повтором) задания по ящику.

    Кнопки в интерфейсе у выключенных ящиков и так неактивны, но сервер не
    должен на это полагаться: выключенный ящик часто выключен из-за неверного
    пароля, и повторные неудачные входы на почтовый сервер могут привести к
    блокировке адреса архива (fail2ban) — тогда встанет копирование всех
    ящиков. Задание, которое уже стоит в очереди, второй раз не ставится.
    """
    if not roles.is_operator(user) or account_id is None:
        return
    acc = svc.db.get_account(int(account_id))
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    if need_enabled:
        if not acc.enabled:
            raise ValidationError(f"Копирование ящика «{acc.name}» выключено.",
                                  hint="Включает ящик администратор.")
        problem = credential_problem(acc)
        if problem:
            raise ValidationError(f"Ящик «{acc.name}» не скопировать: {problem}.",
                                  hint="Пароль ящика задаёт администратор.")
    for job in svc.db.active_jobs():
        if job["type"] != job_type or job["account_id"] != acc.id:
            continue
        if job_type == JobType.BACKUP and str(roles.job_params(job).get("rebuild") or "") != (rebuild or ""):
            continue          # «Докачать потерянные» после обычной копии — не повтор
        raise ValidationError(f"Такое задание по ящику «{acc.name}» уже выполняется или стоит в очереди "
                              f"(№{job['id']}).", hint="Дождитесь его окончания в «Очереди и заданиях».")


def _own_logins(user: dict) -> List[str]:
    """Логины, под которыми входит этот сотрудник (по одному на каждый его ящик)."""
    return [x for x in (user.get("logins") or [user.get("username") or ""]) if x] or [""]


def _is_own_job(user: dict, row) -> bool:
    """Задание запущено самим пользователем-ящиком (а не администратором или планировщиком) —
    под любым из логинов его ящиков: войти он мог паролем любого из них."""
    return row is not None and (row["created_by"] or "") in _own_logins(user)


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
    #: подключение к почтовому серверу (способ входа «через подключение»)
    server_id: Optional[int] = None


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
    #: auto | imap | ftp — для ящиков, которые копируются через FTP Backup Axigen
    via: str = "auto"


#: Сколько писем можно вернуть одним заданием «Восстановить выбранные».
MAX_RESTORE_MESSAGES = 5000


class RestoreMessagesBody(BaseModel):
    """Восстановить отдельные письма из архива ящика (1.15.0)."""
    message_ids: List[int] = Field(..., min_length=1, max_length=MAX_RESTORE_MESSAGES)
    #: в какой ящик: None — в тот же; другой ящик — только администратору
    target_account_id: Optional[int] = None
    #: original — в исходные папки писем; single — в одну папку; prefixed — «Префикс/исходная папка»
    target_mode: str = "original"
    target_folder: str = Field("Восстановлено", max_length=500)
    target_prefix: str = Field("Восстановлено", max_length=500)
    check_duplicates: bool = True


class ScheduleBody(BaseModel):
    #: ящик; для «копирования всех ящиков по очереди» (job_type=backup_all) — не нужен
    account_id: Optional[int] = None
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
    #: без роли — оператор: права администратора выдаются только явно
    role: str = Field("operator", max_length=16)


class UserRoleBody(BaseModel):
    role: str = Field(max_length=16)


class PasswordBody(BaseModel):
    password: str = Field(max_length=PASSWORD_MAX)
