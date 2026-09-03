"""
Движок резервного копирования почтового ящика по IMAP.

Алгоритм (инкрементальный):
  1. Подключиться к ящику, получить список папок, отфильтровать по правилам.
  2. Фаза планирования: для каждой папки определить, какие письма новые.
     Признак новизны — пара (UIDVALIDITY, UID). Если UIDVALIDITY у папки
     изменился, весь прежний индекс для неё считается недействительным и папка
     перекачивается заново (так устроен протокол IMAP).
  3. Фаза загрузки: скачать новые письма батчами, сохранить в Maildir, занести
     в индекс БД, обновлять прогресс.
  4. Обновить состояние папок и статистику.

Ошибки на уровне отдельной папки не прерывают весь бэкап — они логируются и
учитываются в счётчике ошибок, остальные папки продолжают копироваться.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..errors import JobCancelled, MailArchiverError
from ..logging_setup import get_logger
from ..models import Account
from ..storage import MaildirStore
from .client import ConnectOptions, ImapConnection

log = get_logger("backup")

ProgressCB = Callable[[int, int, str, int, float], None]
CancelCB = Callable[[], bool]
EventCB = Callable[[str, str], None]  # (level, message)


@dataclass
class BackupResult:
    messages_new: int = 0
    bytes_new: int = 0
    messages_total: int = 0
    folders_processed: int = 0
    folders_total: int = 0
    errors: int = 0
    error_details: List[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def status_label(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.errors and self.messages_new:
            return "partial"
        if self.errors:
            return "failed"
        return "success"


def _folder_matches(name: str, patterns: List[str], delimiter: str) -> bool:
    for p in patterns or ():
        if not p:
            continue
        if name == p or name.lower() == p.lower():
            return True
        if name.startswith(p + delimiter) or name.lower().startswith((p + delimiter).lower()):
            return True
    return False


class BackupEngine:
    def __init__(self, db, store: MaildirStore, options: ConnectOptions,
                 *, skip_larger_than_mb: int = 0, download_flags: bool = True,
                 global_exclude: Optional[List[str]] = None, global_include: Optional[List[str]] = None) -> None:
        self.db = db
        self.store = store
        self.options = options
        self.skip_larger_than = int(skip_larger_than_mb) * 1024 * 1024
        self.download_flags = download_flags
        self.global_exclude = global_exclude or []
        self.global_include = global_include or []

    def _select_folders(self, conn: ImapConnection, account: Account) -> List:
        folders = [f for f in conn.list_folders() if f.selectable]
        include = list(account.folder_include or []) + list(self.global_include or [])
        exclude = list(account.folder_exclude or []) + list(self.global_exclude or [])
        result = []
        for f in folders:
            if include and not _folder_matches(f.name, include, f.delimiter):
                continue
            if exclude and _folder_matches(f.name, exclude, f.delimiter):
                continue
            result.append(f)
        return result

    def run(self, account: Account, *, progress_cb: Optional[ProgressCB] = None,
            cancel_cb: Optional[CancelCB] = None, event_cb: Optional[EventCB] = None) -> BackupResult:
        result = BackupResult()
        started = time.time()

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Бэкап отменён пользователем.")

        emit("INFO", f"Подключение к ящику «{account.name}» ({account.host})…")
        with ImapConnection(account, self.options) as conn:
            folders = self._select_folders(conn, account)
            result.folders_total = len(folders)
            emit("INFO", f"Найдено папок к копированию: {len(folders)}")

            # --- Фаза планирования ---
            plan: List[Dict] = []
            total_new = 0
            for f in folders:
                check_cancel()
                try:
                    info = conn.select(f.name, readonly=True)
                except MailArchiverError as exc:
                    result.errors += 1
                    result.error_details.append(f"Папка «{f.name}»: {exc.message}")
                    emit("WARNING", f"Пропуск папки «{f.name}»: {exc.message}")
                    continue
                uidvalidity = info["uidvalidity"]
                state = self.db.get_folder_state(account.id, f.name)
                reindex = state is not None and state["uidvalidity"] != uidvalidity
                if reindex:
                    emit("WARNING", f"UIDVALIDITY папки «{f.name}» изменился — полная перезагрузка папки.")
                    # Удаляем устаревшие записи и файлы этой папки, чтобы не копить дубли.
                    for relpath in self.db.folder_stored_paths(account.id, f.name):
                        try:
                            self.store.delete_message(account.id, relpath)
                        except Exception:  # noqa: BLE001
                            pass
                    self.db.purge_folder_index(account.id, f.name)
                existing = set() if (state is None or reindex) else self.db.existing_uids(account.id, f.name, uidvalidity)
                all_uids = conn.search_all_uids()
                new_uids = [u for u in all_uids if u not in existing]
                new_uids.sort()
                if new_uids:
                    plan.append({"folder": f, "uidvalidity": uidvalidity, "uids": new_uids, "server_count": info["exists"]})
                    total_new += len(new_uids)
                # обновим общее число писем в папке (даже если новых нет)
                self.db.upsert_folder_state(account.id, f.name, uidvalidity,
                                            max(all_uids) if all_uids else 0, info["exists"])

            result.messages_total = sum(len(p["uids"]) for p in plan)
            emit("INFO", f"Новых писем к загрузке: {total_new}")
            if progress_cb:
                progress_cb(0, total_new, "Планирование завершено", 0, 0.0)

            # --- Фаза загрузки ---
            done = 0
            bytes_done = 0
            for p in plan:
                check_cancel()
                f = p["folder"]
                emit("INFO", f"Папка «{f.name}»: загрузка {len(p['uids'])} писем…")
                try:
                    conn.select(f.name, readonly=True)
                    max_uid = 0
                    for msg in conn.fetch_messages(p["uids"]):
                        check_cancel()
                        uid = msg["uid"]
                        raw = msg["raw"]
                        if self.skip_larger_than and len(raw) > self.skip_larger_than:
                            emit("WARNING", f"Письмо UID {uid} пропущено (больше лимита размера).")
                            continue
                        flags = msg["flags"] if self.download_flags else []
                        try:
                            relpath, digest, size = self.store.store_message(
                                account.id, f.name, f.delimiter, uid, raw,
                                flags=flags, internaldate=msg["internaldate"],
                            )
                        except MailArchiverError as exc:
                            result.errors += 1
                            result.error_details.append(f"UID {uid} в «{f.name}»: {exc.message}")
                            emit("ERROR", f"Ошибка сохранения UID {uid}: {exc.message}")
                            continue
                        subject, from_addr, has_attach = self._extract_meta(raw)
                        self.db.add_message_index(
                            account.id, f.name, p["uidvalidity"], uid,
                            self._extract_message_id(raw), size,
                            self._epoch_to_iso(msg["internaldate"]),
                            ",".join(flags), relpath, digest,
                            subject=subject, from_addr=from_addr, has_attach=has_attach,
                        )
                        result.messages_new += 1
                        result.bytes_new += size
                        bytes_done += size
                        done += 1
                        max_uid = max(max_uid, uid)
                        if progress_cb and (done % 10 == 0 or done == total_new):
                            elapsed = max(0.001, time.time() - started)
                            progress_cb(done, total_new, f"«{f.name}»: {done}/{total_new}", bytes_done, bytes_done / elapsed)
                    if max_uid:
                        st = self.db.get_folder_state(account.id, f.name)
                        cur_max = st["last_uid"] if st else 0
                        self.db.upsert_folder_state(account.id, f.name, p["uidvalidity"],
                                                    max(cur_max, max_uid), p["server_count"])
                    result.folders_processed += 1
                except JobCancelled:
                    raise
                except MailArchiverError as exc:
                    result.errors += 1
                    result.error_details.append(f"Папка «{f.name}»: {exc.message}")
                    emit("ERROR", f"Ошибка в папке «{f.name}»: {exc.message}")

            if progress_cb:
                elapsed = max(0.001, time.time() - started)
                progress_cb(done, total_new, "Готово", bytes_done, bytes_done / elapsed)

        emit("INFO", f"Бэкап завершён: новых писем {result.messages_new}, ошибок {result.errors}.")
        return result

    @staticmethod
    def _extract_message_id(raw: bytes) -> str:
        # Быстрый поиск заголовка Message-ID без полного парсинга письма
        try:
            head = raw[:8192].decode("latin-1", "ignore")
        except Exception:  # noqa: BLE001
            return ""
        for line in head.splitlines():
            if line.lower().startswith("message-id:"):
                return line.split(":", 1)[1].strip()[:250]
        return ""

    @staticmethod
    def _extract_meta(raw: bytes):
        """Извлечь тему, отправителя и признак вложений (для списка писем)."""
        subject, from_addr, has_attach = "", "", 0
        try:
            from email import policy
            from email.parser import BytesHeaderParser
            from email.utils import parseaddr
            # policy.default корректно декодирует как RFC 2047, так и «сырые» UTF-8 заголовки
            hdrs = BytesHeaderParser(policy=policy.default).parsebytes(raw)
            subject = str(hdrs.get("Subject", "") or "")
            from_raw = str(hdrs.get("From", "") or "")
            name, addr = parseaddr(from_raw)
            from_addr = from_raw or addr
            ctype = (str(hdrs.get("Content-Type", "")) or "").lower()
            low = raw[:40000].lower()
            if "multipart/mixed" in ctype or b"content-disposition: attachment" in low or b"filename=" in low:
                has_attach = 1
        except Exception:  # noqa: BLE001
            pass
        return subject, from_addr, has_attach

    @staticmethod
    def _epoch_to_iso(epoch: Optional[float]) -> str:
        if not epoch:
            return ""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
