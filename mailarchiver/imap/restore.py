"""
Движок восстановления писем из локальной копии обратно на IMAP-сервер.

Источник писем — локальный индекс (БД) + файлы Maildir. Для каждого письма:
  * определяется целевая папка (та же, что была; либо с префиксом; либо одна
    общая папка — по выбору пользователя);
  * при необходимости папка создаётся на сервере;
  * по желанию проверяется дубликат (поиск по Message-ID), чтобы не заливать
    письмо повторно;
  * письмо добавляется командой APPEND с сохранением флагов и даты получения.

Поддерживается «сухой прогон» (dry-run) — подсчёт без реальной заливки.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from ..errors import JobCancelled, MailArchiverError
from ..logging_setup import get_logger
from ..models import Account
from ..storage import MaildirStore
from .client import ConnectOptions, ImapConnection

log = get_logger("restore")

# Системные флаги, которые сервер назначает сам и НЕ принимает в APPEND.
_NON_SETTABLE_FLAGS = {"\\Recent", "\\*"}
# Разрешённые к установке системные флаги IMAP.
_SETTABLE_SYSTEM_FLAGS = {"\\Seen", "\\Answered", "\\Flagged", "\\Draft", "\\Deleted"}


def sanitize_flags_for_append(flags) -> List[str]:
    """
    Оставить только флаги, которые сервер примет в команде APPEND:
    разрешённые системные флаги (\\Seen и т.п.) и пользовательские ключевые
    слова (без обратного слэша). Флаги вроде \\Recent отбрасываются.
    """
    out: List[str] = []
    for fl in flags or ():
        fl = fl.strip()
        if not fl:
            continue
        if fl in _NON_SETTABLE_FLAGS:
            continue
        if fl.startswith("\\") and fl not in _SETTABLE_SYSTEM_FLAGS:
            continue  # неизвестный/системный read-only флаг — пропускаем
        out.append(fl)
    return out


ProgressCB = Callable[[int, int, str, int, float], None]
CancelCB = Callable[[], bool]
EventCB = Callable[[str, str], None]


@dataclass
class RestoreResult:
    restored: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    error_details: List[str] = field(default_factory=list)
    cancelled: bool = False
    dry_run: bool = False

    @property
    def status_label(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.errors and self.restored:
            return "partial"
        if self.errors and not self.restored:
            return "failed"
        return "success"


class RestoreEngine:
    def __init__(self, db, store: MaildirStore, options: ConnectOptions) -> None:
        self.db = db
        self.store = store
        self.options = options

    def run(self, account: Account, *, folders: Optional[List[str]] = None,
            target_mode: str = "original", target_folder: str = "",
            target_prefix: str = "", check_duplicates: bool = True,
            dry_run: bool = False, limit: int = 0,
            progress_cb: Optional[ProgressCB] = None, cancel_cb: Optional[CancelCB] = None,
            event_cb: Optional[EventCB] = None) -> RestoreResult:
        result = RestoreResult(dry_run=dry_run)
        started = time.time()

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[restore %s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Восстановление отменено пользователем.")

        # Собираем письма из индекса
        messages = self._collect_messages(account.id, folders, limit)
        result.total = len(messages)
        emit("INFO", f"К восстановлению отобрано писем: {result.total}"
                     + (" (сухой прогон)" if dry_run else ""))
        if progress_cb:
            progress_cb(0, result.total, "Подготовка", 0, 0.0)

        if not messages:
            return result

        with ImapConnection(account, self.options) as conn:
            folder_list = conn.list_folders()
            delimiter = conn.delimiter
            ensured: set = set()
            created_dup_index: dict = {}

            done = 0
            bytes_done = 0
            for row in messages:
                check_cancel()
                src_folder = row["folder"]
                target = self._resolve_target(src_folder, delimiter, target_mode, target_folder, target_prefix)

                try:
                    raw = self.store.read_message(account.id, row["stored_path"])
                except MailArchiverError as exc:
                    result.errors += 1
                    result.error_details.append(f"{row['stored_path']}: {exc.message}")
                    emit("ERROR", f"Файл копии недоступен: {exc.message}")
                    continue

                if dry_run:
                    result.restored += 1
                    done += 1
                    if progress_cb and done % 20 == 0:
                        progress_cb(done, result.total, f"Проверка {done}/{result.total}", bytes_done, 0.0)
                    continue

                if target not in ensured:
                    try:
                        conn.ensure_folder(target)
                    except MailArchiverError as exc:
                        result.errors += 1
                        result.error_details.append(f"Папка «{target}»: {exc.message}")
                        emit("ERROR", f"Не удалось создать папку «{target}»: {exc.message}")
                        continue
                    ensured.add(target)

                if check_duplicates and row["message_id"]:
                    if self._is_duplicate(conn, target, row["message_id"], created_dup_index):
                        result.skipped += 1
                        done += 1
                        continue

                flags = sanitize_flags_for_append((row["flags"] or "").split(","))
                msg_time = self._iso_to_dt(row["internaldate"])
                try:
                    conn.append(target, raw, flags=flags, msg_time=msg_time)
                    result.restored += 1
                    bytes_done += row["size"] or len(raw)
                    created_dup_index.setdefault(target, set()).add(row["message_id"])
                except MailArchiverError as exc:
                    result.errors += 1
                    result.error_details.append(f"UID {row['uid']} -> «{target}»: {exc.message}")
                    emit("ERROR", f"Ошибка заливки письма: {exc.message}")
                done += 1
                if progress_cb and (done % 10 == 0 or done == result.total):
                    elapsed = max(0.001, time.time() - started)
                    progress_cb(done, result.total, f"Восстановлено {result.restored}/{result.total}",
                                bytes_done, bytes_done / elapsed)

        emit("INFO", f"Восстановление завершено: залито {result.restored}, пропущено {result.skipped}, ошибок {result.errors}.")
        if progress_cb:
            progress_cb(result.total, result.total, "Готово", bytes_done, 0.0)
        return result

    # -- helpers -------------------------------------------------------------
    def _collect_messages(self, account_id: int, folders: Optional[List[str]], limit: int) -> List:
        rows = []
        if folders:
            for fld in folders:
                rows.extend(self.db.list_messages(account_id, folder=fld, limit=1_000_000))
        else:
            rows = self.db.list_messages(account_id, limit=1_000_000)
        if limit and limit > 0:
            rows = rows[:limit]
        return rows

    @staticmethod
    def _resolve_target(src_folder: str, delimiter: str, mode: str, target_folder: str, prefix: str) -> str:
        if mode == "single" and target_folder:
            return target_folder
        if mode == "prefixed" and prefix:
            return f"{prefix}{delimiter}{src_folder}"
        return src_folder

    def _is_duplicate(self, conn: ImapConnection, folder: str, message_id: str, cache: dict) -> bool:
        if message_id in cache.get(folder, set()):
            return True
        try:
            conn.select(folder, readonly=True)
            found = conn.search_header_messageid(message_id)
            return bool(found)
        except MailArchiverError:
            return False

    @staticmethod
    def _iso_to_dt(iso: str) -> Optional[datetime]:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
