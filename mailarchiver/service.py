"""
Центральный объект приложения :class:`Services` — связывает конфигурацию, БД,
хранилище, очередь заданий, планировщик и уведомления. Используется веб-слоем,
CLI и фоновыми воркерами.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from .config import Config
from .database import Database
from .errors import ValidationError
from .imap.client import ConnectOptions
from .logging_setup import get_logger
from .models import Account
from .notify import Notifier
from .queue.manager import QueueManager
from .scheduler.scheduler import SchedulerService
from .security import SecretBox
from .storage import MaildirStore
from .export.base import MailItem

log = get_logger("service")


class Services:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.secret_box = SecretBox(cfg.secret_key())
        self.db = Database(
            cfg.db_path, self.secret_box,
            busy_timeout_ms=int(cfg.get("database", "busy_timeout_ms", 10000)),
            wal=bool(cfg.get("database", "wal", True)),
        )
        self.store = MaildirStore(
            cfg.mail_root,
            compress=bool(cfg.storage.get("compress", False)),
            fsync=bool(cfg.storage.get("fsync", True)),
            min_free_mb=int(cfg.storage.get("min_free_space_mb", 500)),
            verify_after_write=bool(cfg.storage.get("verify_after_write", True)),
        )
        self.notifier = Notifier(self)
        self.queue = QueueManager(self)
        self.scheduler = SchedulerService(self)
        self._started = False

    # -- жизненный цикл ------------------------------------------------------
    def setup(self) -> None:
        self.cfg.ensure_dirs()
        self.db.init_schema()

    def start(self) -> None:
        if self._started:
            return
        orphans = self.db.reset_orphan_jobs()
        stale_runs = self.db.reset_orphan_runs()
        if stale_runs:
            log.info("Закрыто незавершённых записей прогонов: %d.", stale_runs)
        if orphans:
            log.warning("Обнаружено прерванных заданий при старте: %s (возвращены в очередь/помечены)", orphans)
        self.queue.start()
        self.scheduler.start()
        self._started = True
        log.info("Сервис запущен.")

    def stop(self) -> None:
        if not self._started:
            return
        self.scheduler.stop()
        self.queue.stop()
        self._started = False
        log.info("Сервис остановлен.")

    # -- настройки (БД переопределяет конфиг) --------------------------------
    def rt(self, section: str, key: str):
        """Значение настройки: переопределение из БД или значение из конфига."""
        override = self.db.get_setting(f"{section}.{key}", None)
        if override is not None:
            return override
        return self.cfg.get(section, key)

    def set_rt(self, section: str, key: str, value) -> None:
        self.db.set_setting(f"{section}.{key}", value)

    # -- параметры подключения ----------------------------------------------
    def connect_options(self) -> ConnectOptions:
        return ConnectOptions(
            connect_timeout_s=int(self.rt("backup", "connect_timeout_s")),
            socket_timeout_s=int(self.rt("backup", "socket_timeout_s")),
            verify_ssl=bool(self.rt("security", "imap_ssl_verify")),
            fetch_batch_size=int(self.rt("backup", "fetch_batch_size")),
        )

    # -- источник писем для экспорта/восстановления -------------------------
    def iter_mail_items(self, account_id: int, folders: Optional[List[str]] = None,
                        date_from: Optional[str] = None, date_to: Optional[str] = None,
                        limit: int = 0) -> Iterator[MailItem]:
        # Индекс читаем ПОСТРАНИЧНО. Раньше здесь был limit=1 000 000: весь
        # индекс ящика материализовался в список до первого yield (на 200 000
        # писем — сотни мегабайт сверх расхода самого движка экспорта), а ящик
        # крупнее миллиона писем молча обрезался бы без единой ошибки.
        page = 5000

        def _rows() -> Iterator:
            targets = list(folders) if folders else [None]
            for fld in targets:
                offset = 0
                while True:
                    chunk = self.db.list_messages(account_id, folder=fld, limit=page, offset=offset)
                    if not chunk:
                        break
                    for item in chunk:
                        yield item
                    if len(chunk) < page:
                        break
                    offset += len(chunk)

        count = 0
        for row in _rows():
            idate = row["internaldate"] or ""
            # Сравниваем только календарные даты: internaldate хранится полной
            # меткой времени («2026-09-15T12:00:00+00:00»), а границы задаются
            # днём («2026-09-15») — иначе терялся бы весь последний день.
            if date_from and idate and idate[:10] < date_from[:10]:
                continue
            if date_to and idate and idate[:10] > date_to[:10]:
                continue
            try:
                raw = self.store.read_message(account_id, row["stored_path"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Пропуск письма при экспорте (%s): %s", row["stored_path"], exc)
                continue
            epoch = None
            if idate:
                try:
                    epoch = datetime.fromisoformat(idate).timestamp()
                except (ValueError, TypeError):
                    epoch = None
            yield MailItem(folder=row["folder"], raw=raw,
                           flags=[f for f in (row["flags"] or "").split(",") if f],
                           internaldate=epoch, message_id=row["message_id"] or "", size=row["size"] or len(raw))
            count += 1
            if limit and count >= limit:
                break

    def count_mail_items(self, account_id: int, folders: Optional[List[str]] = None) -> int:
        if not folders:
            return self.db.count_messages(account_id)
        total = 0
        for fld in folders:
            # COUNT(*) в БД вместо выборки всех строк ради len()
            total += self.db.count_folder_messages(account_id, fld)
        return total

    # -- удобные фабрики -----------------------------------------------------
    def require_account(self, account_id: int) -> Account:
        acc = self.db.get_account(account_id)
        if acc is None:
            raise ValidationError(f"Ящик с id={account_id} не найден.")
        return acc

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
