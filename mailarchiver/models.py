"""
Модели предметной области: перечисления статусов, типов заданий, режимов
подключения и т.п. Используются строковые константы для удобной сериализации
в JSON и хранения в SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


class JobType:
    BACKUP = "backup"          # резервное копирование ящика по IMAP
    RESTORE = "restore"        # восстановление копии обратно на IMAP
    EXPORT = "export"          # экспорт локальной копии (mbox/eml/msg/pst)
    IMPORT_PST = "import_pst"  # импорт .pst в локальную копию/на сервер
    TEST = "test"              # проверка подключения к ящику
    RETENTION = "retention"    # очистка по политике хранения
    VERIFY = "verify"          # проверка целостности локальной копии
    ANALYZE = "analyze"        # глубокий анализ содержимого писем (аналитика)
    SYNC_EMPLOYEES = "sync_employees"  # синхронизация справочника сотрудников с файлом
    STORAGE_CONVERT = "storage_convert"  # зашифровать/расшифровать уже сохранённые письма
    CHECK_LOGINS = "check_logins"        # проверить пароли (вход) сразу многих ящиков
    REPLICATE = "replicate"              # копия архива вне сервера (папка, rsync, S3)
    DB_SNAPSHOT = "db_snapshot"          # снимок базы данных
    SEARCH_INDEX = "search_index"        # индексация писем для поиска
    DEDUP_REPORT = "dedup_report"        # отчёт: сколько места займут одинаковые вложения, если хранить их один раз

    ALL = [BACKUP, RESTORE, EXPORT, IMPORT_PST, TEST, RETENTION, VERIFY, ANALYZE, SYNC_EMPLOYEES,
           STORAGE_CONVERT, CHECK_LOGINS, REPLICATE, DB_SNAPSHOT, SEARCH_INDEX, DEDUP_REPORT]
    LABELS = {
        BACKUP: "Резервное копирование",
        RESTORE: "Восстановление",
        EXPORT: "Экспорт",
        IMPORT_PST: "Импорт PST",
        TEST: "Проверка подключения",
        RETENTION: "Очистка (ретеншн)",
        VERIFY: "Проверка целостности",
        ANALYZE: "Глубокий анализ писем",
        SYNC_EMPLOYEES: "Синхронизация сотрудников",
        STORAGE_CONVERT: "Шифрование копии",
        CHECK_LOGINS: "Проверка паролей",
        REPLICATE: "Копия вне сервера",
        DB_SNAPSHOT: "Снимок базы",
        SEARCH_INDEX: "Индексация поиска",
        DEDUP_REPORT: "Отчёт об одинаковых вложениях",
    }


class JobStatus:
    QUEUED = "queued"          # ждёт в очереди
    RUNNING = "running"        # выполняется
    SUCCESS = "success"        # успешно завершено
    FAILED = "failed"          # завершено с ошибкой
    CANCELLED = "cancelled"    # отменено пользователем
    PARTIAL = "partial"        # завершено, но с частичными ошибками

    ACTIVE = [QUEUED, RUNNING]
    TERMINAL = [SUCCESS, FAILED, CANCELLED, PARTIAL]
    LABELS = {
        QUEUED: "В очереди",
        RUNNING: "Выполняется",
        SUCCESS: "Успешно",
        FAILED: "Ошибка",
        CANCELLED: "Отменено",
        PARTIAL: "Частично",
    }


class AuthType:
    PASSWORD = "password"      # логин/пароль (LOGIN/PLAIN)
    OAUTH2 = "oauth2"          # XOAUTH2 (Gmail, Microsoft 365)
    MASTER = "master"          # вход учётной записью администратора почты (пароль ящика не нужен)

    ALL = [PASSWORD, OAUTH2, MASTER]


def account_has_credentials(acc) -> bool:
    """Есть ли у ящика чем войти на сервер (пароль, токен OAuth2 или вход администратора)."""
    if acc.auth_type == AuthType.OAUTH2:
        return bool(acc.oauth_refresh_token)
    if acc.auth_type == AuthType.MASTER:
        return True
    return bool(acc.password)


class Security:
    SSL = "ssl"                # неявный TLS (обычно порт 993)
    STARTTLS = "starttls"      # явный TLS (обычно порт 143)
    PLAIN = "plain"            # без шифрования (НЕ рекомендуется)

    ALL = [SSL, STARTTLS, PLAIN]
    LABELS = {
        SSL: "SSL/TLS (порт 993)",
        STARTTLS: "STARTTLS (порт 143)",
        PLAIN: "Без шифрования",
    }


class ExportFormat:
    PST = "pst"
    MBOX = "mbox"
    EML = "eml"
    MSG = "msg"

    ALL = [PST, MBOX, EML, MSG]


class ScheduleKind:
    CRON = "cron"
    INTERVAL = "interval"
    MANUAL = "manual"


@dataclass
class Account:
    """Представление почтового ящика (для передачи между слоями)."""

    id: Optional[int] = None
    name: str = ""
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""            # расшифрованный (в БД хранится зашифрованным)
    auth_type: str = AuthType.PASSWORD
    security: str = Security.SSL
    enabled: bool = True
    folder_include: List[str] = field(default_factory=list)
    folder_exclude: List[str] = field(default_factory=list)
    # OAuth2
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_refresh_token: str = ""
    oauth_token_url: str = ""
    notes: str = ""
    retention_days: int = -1   # -1 = глобальная настройка; 0 = хранить всё; N = N дней
    #: True, если сохранённый секрет не расшифровывается текущим secret.key
    #: (ключ потерян или БД восстановлена из бэкапа без него). Ящик при этом
    #: остаётся видимым и редактируемым — нужно лишь ввести пароль заново.
    secret_broken: bool = False
    #: итог последней попытки входа: ok | auth_error | conn_error | no_password | secret_broken
    login_status: str = ""
    login_checked_at: str = ""
    login_error: str = ""
    #: даты резервных копий: первой и последней удачной (ISO, UTC)
    first_backup_at: str = ""
    last_backup_at: str = ""
    last_backup_status: str = ""
    #: удержание архива: до этой даты (ГГГГ-ММ-ДД) письма ящика не удаляются
    #: очисткой по сроку хранения; 9999-12-31 — бессрочно
    hold_until: str = ""
    #: почему удерживается: dismissed (сотрудник уволен) | manual (решение администратора)
    hold_reason: str = ""
    #: когда сотрудник уволен (ISO) — для ящиков уволенных сотрудников
    dismissed_at: str = ""
    #: копирование выключено автоматически при увольнении (вернётся при повторном приёме)
    auto_disabled: bool = False

    def on_hold(self, today: Optional[str] = None) -> bool:
        """Удерживается ли архив ящика (очистка по сроку не действует)."""
        if not self.hold_until:
            return False
        from datetime import date
        return self.hold_until >= (today or date.today().isoformat())

    def redacted(self) -> dict:
        """Словарь без секретов (для отдачи в API/логи)."""
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth_type": self.auth_type,
            "security": self.security,
            "enabled": self.enabled,
            "folder_include": self.folder_include,
            "folder_exclude": self.folder_exclude,
            "has_password": account_has_credentials(self),
            # Не секреты: без них форма ящика OAuth2 отправляла пустые значения,
            # и любое сохранение молча ломало копирование Gmail/M365.
            "oauth_client_id": self.oauth_client_id,
            "oauth_token_url": self.oauth_token_url,
            "has_oauth_secret": bool(self.oauth_client_secret),
            "notes": self.notes,
            "retention_days": self.retention_days,
            "secret_broken": self.secret_broken,
            "login_status": self.login_status,
            "login_checked_at": self.login_checked_at,
            "login_error": self.login_error,
            "first_backup_at": self.first_backup_at,
            "last_backup_at": self.last_backup_at,
            "last_backup_status": self.last_backup_status,
            "hold_until": self.hold_until,
            "hold_reason": self.hold_reason,
            "on_hold": self.on_hold(),
            "hold_expired": bool(self.hold_until) and not self.on_hold(),
            "dismissed_at": self.dismissed_at,
            "auto_disabled": self.auto_disabled,
        }
