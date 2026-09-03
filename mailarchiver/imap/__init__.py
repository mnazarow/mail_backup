"""Работа с IMAP: подключение, бэкап, восстановление."""
from .client import ImapConnection, probe_account
from .backup import BackupEngine, BackupResult
from .restore import RestoreEngine, RestoreResult

__all__ = [
    "ImapConnection",
    "probe_account",
    "BackupEngine",
    "BackupResult",
    "RestoreEngine",
    "RestoreResult",
]
