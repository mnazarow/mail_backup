"""
MailArchiver — сервис резервного копирования почтовых ящиков по IMAP.

Пакет содержит:
  * imap/      — подключение к IMAP, инкрементальный бэкап и восстановление;
  * storage/   — локальное хранилище копий (Maildir/EML) и ретеншн;
  * export/    — экспорт копий в mbox/eml/msg/pst (движки);
  * queue/     — очередь заданий и пул воркеров;
  * scheduler/ — планировщик заданий по расписанию;
  * stats/     — сбор статистики и метрик;
  * web/       — веб-интерфейс (FastAPI) и REST/WebSocket API.
"""
from .version import __version__, APP_NAME, APP_TITLE

__all__ = ["__version__", "APP_NAME", "APP_TITLE"]
