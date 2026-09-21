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

    ALL = [BACKUP, RESTORE, EXPORT, IMPORT_PST, TEST, RETENTION, VERIFY, ANALYZE, SYNC_EMPLOYEES]
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

    ALL = [PASSWORD, OAUTH2]


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
            "has_password": bool(self.password) or bool(self.oauth_refresh_token),
            "notes": self.notes,
            "retention_days": self.retention_days,
        }
