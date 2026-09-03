"""
Иерархия исключений MailArchiver.

Все ошибки приложения наследуются от :class:`MailArchiverError`, что позволяет
единообразно ловить и показывать их пользователю. У каждой ошибки есть:
  * ``message``   — понятное человеку описание (на русском);
  * ``code``      — короткий машинный код (для API и логов);
  * ``hint``      — подсказка, как исправить (показывается в интерфейсе);
  * ``retryable`` — можно ли повторить операцию автоматически.
"""
from __future__ import annotations

from typing import Optional


class MailArchiverError(Exception):
    """Базовое исключение приложения."""

    code: str = "error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        hint: Optional[str] = None,
        retryable: Optional[bool] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.hint = hint
        if retryable is not None:
            self.retryable = retryable
        self.cause = cause

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:  # pragma: no cover - тривиально
        base = self.message
        if self.hint:
            base += f" (подсказка: {self.hint})"
        return base


# --- Конфигурация и запуск -------------------------------------------------
class ConfigError(MailArchiverError):
    code = "config_error"


class DependencyError(MailArchiverError):
    """Отсутствует необязательная зависимость (например, Aspose.Email)."""

    code = "dependency_missing"


# --- Аутентификация и доступ ----------------------------------------------
class AuthError(MailArchiverError):
    code = "auth_error"


class PermissionError_(MailArchiverError):
    code = "permission_denied"


# --- IMAP ------------------------------------------------------------------
class ImapError(MailArchiverError):
    code = "imap_error"


class ImapAuthError(ImapError):
    code = "imap_auth_error"


class ImapConnectionError(ImapError):
    code = "imap_connection_error"
    retryable = True


class ImapTimeoutError(ImapError):
    code = "imap_timeout"
    retryable = True


class ImapProtocolError(ImapError):
    code = "imap_protocol_error"
    retryable = True


# --- Хранилище -------------------------------------------------------------
class StorageError(MailArchiverError):
    code = "storage_error"


class DiskSpaceError(StorageError):
    code = "disk_space"


# --- Экспорт ---------------------------------------------------------------
class ExportError(MailArchiverError):
    code = "export_error"


class PstEngineError(ExportError):
    code = "pst_engine_error"


# --- Восстановление --------------------------------------------------------
class RestoreError(MailArchiverError):
    code = "restore_error"


# --- Очередь и задания -----------------------------------------------------
class JobError(MailArchiverError):
    code = "job_error"


class JobCancelled(MailArchiverError):
    """Задание отменено пользователем — не считается сбоем."""

    code = "job_cancelled"


class ValidationError(MailArchiverError):
    code = "validation_error"
