"""
Копирование ящика через службу FTP Backup Axigen — без пароля ящика.

Ход прогона:

1. Войти в FTP Backup учётной записью администратора Axigen и найти каталог
   ящика (путь запоминается и в следующий раз проверяется первым).
2. Обойти папки ящика (вложенные каталоги — вложенные папки). Пропускаются
   папки из «Не копировать папки (FTP)» настроек Axigen (календарь, контакты…),
   а также правила «Копировать только» / «Исключить папки» ящика и общих
   настроек — те же, что у копирования по IMAP.
3. По каждой папке сравнить список файлов с тем, что уже взято
   (таблица ``axigen_ftp_files``): новым считается файл, которого не было, или
   файл того же имени другого размера. Файлы, которых на сервере больше нет,
   забываются — письма в архиве остаются (архив не удаляет то, что удалили на
   сервере).
4. Скачать новые файлы. Не похожий на письмо файл (служебный файл сервера)
   запоминается, чтобы не качать его каждый раз, и в архив не попадает.
   Письмо, которое уже есть в этой папке архива (то же содержимое — например,
   скопированное раньше по IMAP), второй раз не записывается: переход ящика с
   IMAP на FTP Backup не удваивает архив.
5. Записать письмо в архив тем же путём, что и копирование по IMAP (Maildir,
   индекс, поиск, аналитика), под особым UIDVALIDITY
   (:data:`~mailarchiver.axigen.store.FTP_UIDVALIDITY`).

Если служба Axigen упаковывает письма папки в ``messages.tar`` («Use the TAR
format to pack messages»), пакет читается потоком; пока он не изменился (тот
же размер и время), папка не скачивается. Изменился — скачивается целиком,
но записываются только новые письма. Для ежедневного копирования эту
настройку службы лучше выключить.

Отметок «прочитано» и «важное» FTP Backup не отдаёт — у писем, взятых этим
способом, их нет. Дата письма — время доставки на сервер (верхний заголовок
Received), без него — заголовок Date, без него — время файла.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..errors import DiskSpaceError, JobCancelled, MailArchiverError
from ..imap.backup import BackupEngine, BackupResult, MAX_MESSAGE_EVENTS, MAX_SKIPPED_FOLDERS_IN_SUMMARY
from ..imap.client import folder_matches
from ..logging_setup import get_logger
from ..models import Account
from ..util import sha256_hex
from . import store as axstore
from .common import AxigenConnectionError, AxigenError, AxigenSettings
from .ftp import TAR_NAME, AxigenFTP, Entry, FileTooLarge, FtpPathError, join, walk_folders

log = get_logger("axigen.backup")

FTP_UIDVALIDITY = axstore.FTP_UIDVALIDITY
DELIMITER = "/"
INDEX_BATCH = 200
#: Сколько раз подряд можно переподключаться, не скачав ни одного файла.
MAX_STALLED_RECONNECTS = 3

_HEADER_START = re.compile(rb"^(?:From |[!-9;-~]+:)")
_KNOWN_HEADERS = (b"\nfrom:", b"\ndate:", b"\nsubject:", b"\nmessage-id:", b"\nreceived:", b"\nreturn-path:",
                  b"\nto:", b"\nmime-version:", b"\ncontent-type:", b"\ndelivered-to:", b"\nx-")


def looks_like_message(raw: bytes) -> bool:
    """Похоже ли содержимое файла на письмо (RFC 5322): заголовки в начале."""
    head = raw[:65536]
    if not head:
        return False
    first = head.split(b"\n", 1)[0]
    if first.upper().startswith(b"BEGIN:V"):       # vCard / iCalendar
        return False
    if not _HEADER_START.match(first):
        return False
    end = re.search(rb"\r?\n\r?\n", head)
    block = b"\n" + (head[:end.start()] if end else head).lower()
    return any(name in block for name in _KNOWN_HEADERS)


def _headers(raw: bytes) -> List[Tuple[str, str]]:
    """Заголовки письма (имя в нижнем регистре, значение со склейкой свёрнутых строк)."""
    head = raw[:256 * 1024]
    end = re.search(rb"\r?\n\r?\n", head)
    text = (head[:end.start()] if end else head).decode("latin-1", "replace").replace("\r\n", "\n")
    out: List[Tuple[str, str]] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            name, value = out[-1]
            out[-1] = (name, value + " " + line.strip())
        elif ":" in line:
            name, _, value = line.partition(":")
            out.append((name.strip().lower(), value.strip()))
    return out


def _to_epoch(text: str) -> Optional[float]:
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    stamp = dt.timestamp()
    # дата из будущего или до 1990 года — мусор отправителя, не время доставки
    if stamp < 631152000 or stamp > time.time() + 3 * 86400:
        return None
    return stamp


def message_time(raw: bytes) -> Optional[float]:
    """Время доставки: верхний Received (последний сервер — наш), затем Date."""
    headers = _headers(raw)
    for name, value in headers:
        if name == "received" and ";" in value:
            stamp = _to_epoch(value.rsplit(";", 1)[1])
            if stamp:
                return stamp
            break
    for name, value in headers:
        if name == "date":
            return _to_epoch(value)
    return None


class FtpBackupEngine(BackupEngine):
    """Копирование ящика через FTP Backup Axigen (тот же итог, что у копирования по IMAP)."""

    def __init__(self, db, store, settings: AxigenSettings, *, skip_larger_than_mb: int = 0,
                 global_exclude: Optional[List[str]] = None, global_include: Optional[List[str]] = None,
                 unreadable_grace_runs: int = 3, retention_days: int = 0,
                 client_factory: Optional[Callable[[AxigenSettings], AxigenFTP]] = None) -> None:
        super().__init__(db, store, None, skip_larger_than_mb=skip_larger_than_mb, download_flags=False,
                         global_exclude=global_exclude, global_include=global_include,
                         unreadable_grace_runs=unreadable_grace_runs, retention_days=retention_days)
        self.st = settings
        self.client_factory = client_factory or AxigenFTP.open

    # ------------------------------------------------------------------
    def run(self, account: Account, *, progress_cb=None, cancel_cb=None, event_cb=None) -> BackupResult:
        result = BackupResult()
        started = time.time()
        self._explained_unreadable = False
        try:
            self._problem_folders = {row["folder"] for row in self.db.list_folder_problems(account.id)}
        except Exception:  # noqa: BLE001
            self._problem_folders = None
        events = [0]

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def emit_msg(level: str, msg: str) -> None:
            events[0] += 1
            if events[0] <= MAX_MESSAGE_EVENTS:
                emit(level, msg)
            elif events[0] == MAX_MESSAGE_EVENTS + 1:
                emit("WARNING", f"Сообщений об отдельных письмах больше {MAX_MESSAGE_EVENTS} — дальше они пишутся "
                                f"только в журнал службы; итог — в конце задания.")
            else:
                log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s", account.name, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Бэкап отменён пользователем.")

        run = _Run(self, account, result, emit, emit_msg, check_cancel, progress_cb, started)
        emit("INFO", f"Подключение к службе FTP Backup Axigen ({self.st.host}:{self.st.ftp_port}) для ящика "
                     f"«{account.name}» — без пароля ящика…")
        run.open()
        try:
            run.execute()
        finally:
            run.flush()
            run.close()

        summary = (f"Бэкап через FTP Backup завершён: новых писем {result.messages_new}, "
                   f"пропущено по размеру {result.messages_skipped}, ошибок {result.errors}.")
        if result.messages_recognized:
            summary += (f" Уже были в архиве (скопированы раньше, в том числе по IMAP): {result.messages_recognized} "
                        f"— второй раз не записаны.")
        if result.other_files:
            summary += f" Файлов, не похожих на письма (служебные файлы сервера): {result.other_files}."
        if result.messages_vanished:
            summary += f" Удалено на сервере во время копирования: {result.messages_vanished} (это не ошибка)."
        if result.reconnects:
            summary += f" Переподключений после обрыва связи: {result.reconnects}."
        if result.known_unreadable_folders:
            shown = ", ".join(result.known_unreadable_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            summary += (f" Известные нечитаемые папки ({len(result.known_unreadable_folders)}): {shown} — "
                        f"сервер не открывает их давно, ошибкой не считаем.")
        if result.skipped_folders:
            shown = ", ".join(result.skipped_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            rest = len(result.skipped_folders) - MAX_SKIPPED_FOLDERS_IN_SUMMARY
            summary += (f" КОПИЯ НЕПОЛНАЯ: не удалось прочитать папки ({len(result.skipped_folders)}): {shown}"
                        f"{f' и ещё {rest}' if rest > 0 else ''}.")
        emit("WARNING" if (result.skipped_folders or result.messages_failed) else "INFO", summary)
        return result


class _Run:
    """Состояние одного прогона (чтобы не тащить десяток параметров через все методы)."""

    def __init__(self, engine: FtpBackupEngine, account: Account, result: BackupResult, emit, emit_msg,
                 check_cancel, progress_cb, started: float) -> None:
        self.e = engine
        self.db = engine.db
        self.store = engine.store
        self.st = engine.st
        self.acc = account
        self.result = result
        self.emit = emit
        self.emit_msg = emit_msg
        self.check_cancel = check_cancel
        self.progress_cb = progress_cb
        self.started = started
        self.client: Optional[AxigenFTP] = None
        self.index_rows: List[tuple] = []
        self.file_rows: List[tuple] = []
        self.next_uid: Dict[str, int] = {}
        self.done = 0
        self.total = 0
        self.bytes = 0
        self.stalled = 0
        days = engine.retention_days
        self.cutoff = (time.time() - (days + 1) * 86400) if days > 0 else None
        self.limit = engine.skip_larger_than
        try:
            self.archive_names = {name.lower(): name for name in self.db.account_folders(account.id)}
        except Exception:  # noqa: BLE001
            self.archive_names = {}

    # -- соединение ----------------------------------------------------------
    def open(self) -> None:
        self.client = self.e.client_factory(self.st)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def reconnect(self, exc: BaseException) -> None:
        self.result.reconnects += 1
        self.stalled += 1
        if self.stalled > MAX_STALLED_RECONNECTS:
            raise AxigenConnectionError(
                f"Связь с FTP Backup рвётся снова и снова ({exc}) — копирование отложено, задание повторится.")
        self.emit("WARNING", f"Связь с FTP Backup оборвалась ({getattr(exc, 'message', exc)}) — переподключаемся.")
        self.close()
        time.sleep(min(10, 2 * self.stalled))
        self.open()

    # -- папки ---------------------------------------------------------------
    def archive_folder(self, name: str) -> str:
        """Имя папки в архиве: как уже записано (регистр — как у IMAP), «Inbox» → «INBOX»."""
        hit = self.archive_names.get(name.lower())
        if hit:
            return hit
        if name.lower() == "inbox":
            return "INBOX"
        if "/" in name:
            head, _, tail = name.partition("/")
            if head.lower() == "inbox":
                return "INBOX/" + tail
        return name

    def excluded(self, name: str) -> bool:
        own = list(self.st.ftp_exclude_folders or [])
        if folder_matches(name, own, DELIMITER):
            return True
        exclude = list(self.acc.folder_exclude or []) + list(self.e.global_exclude or [])
        return bool(exclude) and folder_matches(self.archive_folder(name), exclude, DELIMITER)

    def included(self, name: str) -> bool:
        include = list(self.acc.folder_include or []) + list(self.e.global_include or [])
        return not include or folder_matches(self.archive_folder(name), include, DELIMITER)

    # -- прогон --------------------------------------------------------------
    def execute(self) -> None:
        hint = axstore.ftp_path(self.db, self.acc.id)
        root = self._call(lambda: self.client.find_account_dir(self.acc.username, hint))
        if root != hint:
            axstore.set_ftp_path(self.db, self.acc.id, root)
            self.emit("INFO", f"Каталог ящика на FTP Backup: {self.client.shown(root)}.")

        folders: List[Tuple[str, str, List[Entry]]] = []

        def on_error(name: str, exc: BaseException) -> None:
            text = getattr(exc, "message", str(exc))
            self.e._register_loss(self.acc, self.archive_folder(name), f"не читается на FTP Backup: {text}",
                                  f"Папка не читается на FTP Backup: {text}", self.result, self.emit,
                                  error_text=text, delimiter=DELIMITER)

        listing = self._call(lambda: list(walk_folders(self.client, root, excluded=self.excluded,
                                                       on_error=on_error)))
        for name, path, files in listing:
            if self.included(name):
                folders.append((name, path, files))
        self.result.folders_total = len(folders)
        self.emit("INFO", f"Найдено папок к копированию: {len(folders)}")

        plan = []
        for name, path, files in folders:
            self.check_cancel()
            item = self.plan_folder(name, path, files)
            if item is not None:
                plan.append(item)
                self.total += len(item["new"])
        self.result.messages_total = self.total
        self.emit("INFO", f"Новых файлов к загрузке: {self.total}")
        self.report("Планирование завершено")

        for item in plan:
            self.check_cancel()
            folder = item["folder"]
            try:
                if item["tar"] is not None:
                    self.load_tar(item)
                else:
                    if item["new"]:
                        self.emit("INFO", f"Папка «{folder}»: загрузка {len(item['new'])} файлов…")
                    self.load_files(item)
                self.result.folders_processed += 1
            except (JobCancelled, DiskSpaceError, AxigenConnectionError):
                raise
            except MailArchiverError as exc:
                self.flush()
                self.e._folder_load_failed(folder, exc.message, self.result, self.emit)
            except Exception as exc:  # noqa: BLE001
                self.flush()
                log.exception("[%s] Сбой при загрузке папки «%s» через FTP Backup", self.acc.name, folder)
                self.e._folder_load_failed(folder, f"{type(exc).__name__}: {exc}", self.result, self.emit)
        self.flush()
        self.report("Готово")
        if self.result.other_files and not self.result.messages_new and not self.result.messages_recognized \
                and not self._has_mail():
            self.result.add_error("Ни один файл ящика на FTP Backup не похож на письмо")
            self.emit("ERROR", "Ни один файл в папках ящика на FTP Backup не похож на письмо — в архив ничего не "
                               "записано. Проверьте, тот ли каталог найден (он назван в начале журнала), и "
                               "сообщите разработчикам, как выглядят файлы писем на вашем сервере.")

    def _has_mail(self) -> bool:
        return bool(self.db.scalar("SELECT 1 FROM axigen_ftp_files WHERE account_id=? AND kind='mail' LIMIT 1",
                                   (self.acc.id,)))

    def _call(self, fn):
        """Действие с сервером; обрыв связи — переподключиться и повторить."""
        while True:
            try:
                value = fn()
                self.stalled = 0
                return value
            except AxigenConnectionError as exc:
                self.reconnect(exc)

    def plan_folder(self, name: str, path: str, files: List[Entry]) -> Optional[Dict]:
        folder = self.archive_folder(name)
        self.result.folders_read += 1
        self.e._forget_unreadable(self.acc, folder)
        known = axstore.ftp_files(self.db, self.acc.id, folder)
        present = axstore.ftp_present_keys(self.db, self.acc.id, folder)
        tar = next((f for f in files if f.name.lower() == TAR_NAME), None)
        item = {"folder": folder, "name": name, "path": path, "known": known, "present": present,
                "tar": tar, "new": []}
        if tar is not None:
            row = known.get(tar.wire)
            if row is not None and row["kind"] == "tar" and int(row["size"]) == (tar.size if tar.size is not None
                                                                                 else -1) \
                    and (row["mtime"] or "") == tar.mtime_iso:
                return None                      # пакет не менялся
            return item
        listed = {f.wire for f in files}
        gone = [n for n, row in known.items() if n not in listed]
        if gone:
            self.forget(folder, known, gone)
        item["new"] = [f for f in files if not self.is_done(known.get(f.wire), f.size, present)]
        return item

    @staticmethod
    def is_done(row, size: Optional[int], present: Set[Tuple[int, int]]) -> bool:
        if row is None:
            return False
        if size is not None and int(row["size"]) != int(size):
            return False
        if row["kind"] != "mail":
            return True
        return (int(row["uidvalidity"]), int(row["uid"])) in present

    def forget(self, folder: str, known: Dict, names: List[str]) -> None:
        retired = [int(known[n]["uid"]) for n in names
                   if int(known[n]["uidvalidity"]) == FTP_UIDVALIDITY and known[n]["kind"] == "mail"]
        axstore.ftp_forget(self.db, self.acc.id, folder, names)
        if retired:
            # письмо в индексе остаётся; забываем только «вычищенные по сроку» номера
            in_index = {int(r["uid"]) for r in self.db.query(
                "SELECT uid FROM messages WHERE account_id=? AND folder=? AND uidvalidity=?",
                (self.acc.id, folder, FTP_UIDVALIDITY))}
            axstore.ftp_forget_retired(self.db, self.acc.id, folder, [u for u in retired if u not in in_index])

    # -- загрузка ------------------------------------------------------------
    def load_files(self, item: Dict) -> None:
        folder = item["folder"]
        for entry in item["new"]:
            self.check_cancel()
            if self.limit and entry.size is not None and entry.size > self.limit:
                self.skip_size(folder, entry.name, entry.size)
                continue
            if self.cutoff is not None and entry.mtime is not None and entry.mtime < self.cutoff - 86400:
                self.retire(folder, entry.wire, entry.size, entry.mtime_iso)
                continue
            raw = self.fetch(item, entry)
            if raw is None:
                continue
            self.take(item, entry.wire, entry.name, raw, entry.size, entry.mtime, entry.mtime_iso)
        self.flush()

    def fetch(self, item: Dict, entry: Entry) -> Optional[bytes]:
        attempts = 0
        while True:
            try:
                raw = self.client.retr(join(item["path"], entry.wire), self.limit)
                self.stalled = 0
                return raw
            except FileTooLarge as big:
                self.skip_size(item["folder"], entry.name, big.size)
                return None
            except FtpPathError as exc:
                if exc.reply[:3] == "550" and not self.still_listed(item, entry):
                    # файла уже нет: письмо удалили или переместили, пока шло копирование
                    self.result.messages_vanished += 1
                    self.done += 1
                    return None
                if exc.reply[:3] == "550":
                    # файл в папке есть, а сервер его не отдаёт — это потеря, а не «удалено»
                    self.result.messages_failed += 1
                    self.result.add_error(f"«{item['folder']}», файл {entry.name}: сервер не отдал файл "
                                          f"({exc.reply[:120]})")
                    self.emit_msg("ERROR", f"Сервер не отдал файл {entry.name} из «{item['folder']}», хотя он "
                                           f"есть в папке ({exc.reply[:120]}).")
                    self.step()
                    return None
                raise
            except AxigenConnectionError as exc:
                attempts += 1
                self.flush()
                self.reconnect(exc)
                if attempts > 2:
                    raise

    def still_listed(self, item: Dict, entry: Entry) -> bool:
        """Есть ли файл в папке сейчас (после отказа сервера его отдать)."""
        try:
            return any(e.wire == entry.wire for e in self.client.listdir(item["path"]))
        except AxigenError:
            return False

    def take(self, item: Dict, name: str, label: str, raw: bytes, size: Optional[int], mtime: Optional[float],
             mtime_iso: str) -> None:
        """Записать один файл (письмо или служебный)."""
        folder = item["folder"]
        real_size = len(raw)
        if not looks_like_message(raw):
            self.result.other_files += 1
            self.file_rows.append((self.acc.id, folder, name, real_size, mtime_iso, 0, 0, "other"))
            self.step()
            return
        when = message_time(raw) or mtime
        if self.cutoff is not None and when is not None and when < self.cutoff:
            self.retire(folder, name, real_size, mtime_iso)
            return
        digest = sha256_hex(raw)
        twin = self.find_twin(folder, raw, digest)
        if twin is not None:
            self.result.messages_recognized += 1
            self.file_rows.append((self.acc.id, folder, name, real_size, mtime_iso, twin[0], twin[1], "mail"))
            self.step()
            return
        uid = self.allocate(folder)
        try:
            rel, sha, stored = self.store.store_message(self.acc.id, folder, DELIMITER, uid, raw, flags=[],
                                                        internaldate=when)
        except DiskSpaceError:
            raise
        except Exception as exc:  # noqa: BLE001
            text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
            self.result.add_error(f"«{folder}», файл {label}: {text}")
            self.emit_msg("ERROR", f"Ошибка сохранения файла {label} из «{folder}»: {text}")
            self.step()
            return
        subject, from_addr, has_attach = BackupEngine._extract_meta(raw)
        message_id = BackupEngine._extract_message_id(raw)
        self.index_rows.append((self.acc.id, folder, FTP_UIDVALIDITY, uid, message_id, stored,
                                BackupEngine._epoch_to_iso(when) if when else "", "", rel, sha,
                                subject[:500], from_addr[:300], 1 if has_attach else 0))
        self.file_rows.append((self.acc.id, folder, name, real_size, mtime_iso, FTP_UIDVALIDITY, uid, "mail"))
        item["present"].add((FTP_UIDVALIDITY, uid))
        self.result.messages_new += 1
        self.result.bytes_new += stored
        self.bytes += stored
        self.step()
        if len(self.index_rows) >= INDEX_BATCH:
            self.flush()

    def find_twin(self, folder: str, raw: bytes, digest: str) -> Optional[Tuple[int, int]]:
        """Письмо архива с тем же содержимым в этой папке (или с тем же Message-ID, размером и датой)."""
        row = self.db.query_one("SELECT uidvalidity, uid FROM messages WHERE account_id=? AND folder=? AND sha256=? "
                                "LIMIT 1", (self.acc.id, folder, digest))
        if row is not None:
            return int(row["uidvalidity"]), int(row["uid"])
        for pending in self.index_rows:
            if pending[1] == folder and pending[9] == digest:
                return int(pending[2]), int(pending[3])
        message_id = BackupEngine._extract_message_id(raw)
        if not message_id:
            return None
        rows = self.db.query("SELECT uidvalidity, uid, stored_path FROM messages WHERE account_id=? AND folder=? "
                             "AND message_id=? AND size=? LIMIT 3", (self.acc.id, folder, message_id, len(raw)))
        if len(rows) != 1:
            return None
        date = " ".join(next((v for n, v in _headers(raw) if n == "date"), "").split())[:120]
        archived = self.e._archived_date(self.acc, rows[0]["stored_path"])
        if date and archived == date:
            return int(rows[0]["uidvalidity"]), int(rows[0]["uid"])
        return None

    def allocate(self, folder: str) -> int:
        if folder not in self.next_uid:
            self.next_uid[folder] = axstore.ftp_max_uid(self.db, self.acc.id, folder)
        self.next_uid[folder] += 1
        return self.next_uid[folder]

    def retire(self, folder: str, name: str, size: Optional[int], mtime_iso: str) -> None:
        uid = self.allocate(folder)
        self.db.retire_uids(self.acc.id, folder, FTP_UIDVALIDITY, [uid])
        self.file_rows.append((self.acc.id, folder, name, -1 if size is None else int(size), mtime_iso,
                               FTP_UIDVALIDITY, uid, "mail"))
        self.result.messages_outside_retention += 1
        self.step()

    def skip_size(self, folder: str, label: str, size: int) -> None:
        self.result.messages_skipped += 1
        self.emit_msg("WARNING", f"Файл {label} в «{folder}» пропущен (больше лимита размера: {size} Б).")
        self.step()

    def load_tar(self, item: Dict) -> None:
        folder, tar = item["folder"], item["tar"]
        known = item["known"]
        self.emit("INFO", f"Папка «{folder}»: письма упакованы сервером в {TAR_NAME} — читаем пакет целиком "
                          f"({tar.size if tar.size is not None else '?'} Б).")
        seen: Set[str] = set()
        path = join(item["path"], tar.wire)
        attempts = 0
        while True:
            try:
                for member, data, size, mtime in self.client.iter_tar(path, self.limit):
                    self.check_cancel()
                    name = f"{TAR_NAME}/{member}"
                    seen.add(name)
                    if self.is_done(known.get(name), size, item["present"]):
                        continue
                    if data is None:
                        self.skip_size(folder, member, size)
                        continue
                    iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else ""
                    self.take(item, name, member, data, size, mtime, iso)
                break
            except AxigenConnectionError as exc:
                attempts += 1
                self.flush()
                if attempts > 2:
                    raise
                self.reconnect(exc)
                seen.clear()
        self.flush()
        gone = [n for n in known if n.startswith(TAR_NAME + "/") and n not in seen]
        if gone:
            self.forget(folder, known, gone)
        axstore.ftp_save(self.db, [(self.acc.id, folder, tar.wire, -1 if tar.size is None else tar.size,
                                    tar.mtime_iso, 0, 0, "tar")])

    # -- учёт ----------------------------------------------------------------
    def step(self) -> None:
        self.done += 1
        if self.done % 10 == 0 or self.done == self.total:
            self.report(f"{self.done}/{self.total}")

    def report(self, label: str) -> None:
        if self.progress_cb:
            elapsed = max(0.001, time.time() - self.started)
            self.progress_cb(min(self.done, self.total) if self.total else self.done, self.total, label,
                             self.bytes, self.bytes / elapsed)

    def flush(self) -> None:
        """Сначала индекс писем, потом список файлов: обрыв между ними даст лишь повторную
        загрузку файла (и он узнается по содержимому), но не «взятый» файл без письма."""
        if self.index_rows:
            rows, self.index_rows = self.index_rows, []
            inserted = self.db.add_message_index_batch(rows)
            if inserted != len(rows):
                raise MailArchiverError(f"В индекс записано {inserted} писем из {len(rows)}: номера писем в архиве "
                                        f"ящика уже заняты.",
                                        hint="Запустите у ящика «Проверить целостность копии» и повторите.")
        if self.file_rows:
            rows, self.file_rows = self.file_rows, []
            axstore.ftp_save(self.db, rows)


def probe(svc, account: Account, settings: Optional[AxigenSettings] = None) -> Dict:
    """Проверить доступ к ящику через FTP Backup (для кнопки «Проверить подключение»).

    Возвращает то же, что проверка IMAP: {ok, error, hint, folders:[{name, files}], kind, path}.
    """
    from .common import load_settings
    st = settings or load_settings(svc)
    out = {"ok": False, "error": None, "hint": None, "folders": [], "capabilities": [], "duplicate_folders": [],
           "kind": "", "via": "axigen_ftp", "path": ""}
    try:
        with AxigenFTP.open(st) as client:
            hint = axstore.ftp_path(svc.db, account.id) if account.id else ""
            root = client.find_account_dir(account.username, hint)
            out["path"] = client.shown(root)
            if account.id and root != hint:
                axstore.set_ftp_path(svc.db, account.id, root)
            excluded = list(st.ftp_exclude_folders or [])
            for name, _path, files in walk_folders(client, root, excluded=lambda n: folder_matches(n, excluded, "/"),
                                                   on_error=lambda n, e: out["folders"].append(
                                                       {"name": n, "error": getattr(e, "message", str(e))})):
                out["folders"].append({"name": name, "files": len(files), "delimiter": "/", "selectable": True,
                                       "flags": [], "special": "", "duplicate": False,
                                       "tar": any(f.name.lower() == TAR_NAME for f in files)})
            out["ok"] = True
    except AxigenError as exc:
        out["error"] = exc.message
        out["hint"] = exc.hint
        # пароль ящика тут ни при чём (входит администратор Axigen) — не «неверный пароль ящика»
        out["kind"] = "conn_error"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["kind"] = "conn_error"
    return out


def _archive_folder_name(svc, account: Account, server_name: str) -> str:
    """Папка архива для папки сервера: то же имя без учёта регистра («Inbox» → «INBOX»)."""
    candidates = list(svc.db.account_folders(account.id)) + axstore.ftp_folders(svc.db, account.id)
    low = server_name.lower()
    for name in candidates:
        if name.lower() == low:
            return name
    return "INBOX" if low == "inbox" else server_name


def folder_gaps(svc, account: Account, folder: str, files: List[Entry]) -> Tuple[List[str], int, str, int]:
    """Чего из файлов папки сервера нет в архиве.

    Возвращает (имена писем, которых нет в архиве; файлов-не-писем; причина «нельзя
    проверить»; сколько из недостающих копирование ещё не видело — пришли после копии).

    Письмо считается в архиве, только если его файл взят копированием через
    FTP Backup (с тем же размером) и письмо лежит в индексе — не «вычищено по
    сроку» и не пропущено по размеру. Никаких поблажек по времени файла: письмо,
    перемещённое в папку только что, может быть старым.
    """
    if any(f.name.lower() == TAR_NAME for f in files):
        return [], 0, "письма папки упакованы сервером в messages.tar — полноту копии по файлам проверить нельзя", 0
    known = axstore.ftp_files(svc.db, account.id, folder)
    in_index = {(int(r["uidvalidity"]), int(r["uid"])) for r in svc.db.query(
        "SELECT uidvalidity, uid FROM messages WHERE account_id=? AND folder=?", (account.id, folder))}
    missing: List[str] = []
    other = unseen = 0
    for entry in files:
        row = known.get(entry.wire)
        if row is None or (entry.size is not None and int(row["size"]) != int(entry.size)):
            missing.append(entry.name)
            unseen += 1
        elif row["kind"] != "mail":
            other += 1
        elif (int(row["uidvalidity"]), int(row["uid"])) not in in_index:
            missing.append(entry.name)
    return missing, other, "", unseen


def _folder_path(client: AxigenFTP, root: str, server_name: str) -> Optional[str]:
    path = root
    for part in server_name.split("/"):
        entries = client.listdir(path)
        hit = next((e for e in entries if e.is_dir and e.name.lower() == part.lower()), None)
        if hit is None:
            return None
        path = join(path, hit.wire)
    return path


def verify_folder(svc, st: AxigenSettings, account: Account, server_name: str) -> Tuple[bool, str, bool]:
    """Все ли письма папки сервера уже в архиве — перед её очисткой на сервере.

    Возвращает (можно ли очищать, почему нет, нужна ли свежая копия — в папке есть
    файлы, которых копирование ещё не видело).
    """
    folder = _archive_folder_name(svc, account, server_name)
    with AxigenFTP.open(st) as client:
        root = client.find_account_dir(account.username, axstore.ftp_path(svc.db, account.id))
        path = _folder_path(client, root, server_name)
        if path is None:
            return False, "папки нет на FTP Backup", False
        files = [e for e in client.listdir(path) if not e.is_dir]
    missing, other, problem, unseen = folder_gaps(svc, account, folder, files)
    if problem:
        return False, problem, False
    if other:
        return False, f"в папке есть файлы, не похожие на письма ({other}) — их нет в архиве", False
    if missing:
        return False, (f"в архиве нет {len(missing)} из {len(files)} писем папки (пришли после копии, пропущены "
                       f"по размеру или по сроку хранения), например {missing[0]}"), bool(unseen)
    return True, "", False


def mailbox_coverage(svc, st: AxigenSettings, account: Account) -> Dict:
    """Все ли письма ящика на сервере есть в архиве (перед удалением учётной записи).

    Проверяются все папки, кроме «Не копировать папки (FTP)» (календарь, контакты —
    не почта), в том числе исключённые из копирования правилами ящика: их письма
    при удалении учётной записи пропали бы. Возвращает {ok, folders: [{name,
    files, missing, other, problem}], missing, problems}.
    """
    excluded = list(st.ftp_exclude_folders or [])
    report: List[Dict] = []
    with AxigenFTP.open(st) as client:
        root = client.find_account_dir(account.username, axstore.ftp_path(svc.db, account.id))

        def on_error(name: str, exc: BaseException) -> None:
            report.append({"name": name, "files": None, "missing": 0, "other": 0, "unseen": 0,
                           "problem": f"не читается на FTP Backup: {getattr(exc, 'message', exc)}"})

        for name, _path, files in walk_folders(client, root, excluded=lambda n: folder_matches(n, excluded, "/"),
                                               on_error=on_error):
            folder = _archive_folder_name(svc, account, name)
            missing, other, problem, unseen = folder_gaps(svc, account, folder, files)
            report.append({"name": name, "files": len(files), "missing": len(missing), "other": other,
                           "unseen": unseen, "problem": problem, "sample": missing[:3]})
    total_missing = sum(r["missing"] + r["other"] for r in report)
    problems = [r for r in report if r["problem"]]
    only_unseen = bool(total_missing) and not problems and all(
        r["missing"] == r["unseen"] and not r["other"] for r in report)
    return {"ok": not total_missing and not problems, "folders": report, "missing": total_missing,
            "problems": len(problems), "only_unseen": only_unseen}


def describe_gaps(cov: Dict, limit: int = 8) -> str:
    parts = []
    for r in cov["folders"]:
        if r["problem"]:
            parts.append(f"«{r['name']}»: {r['problem']}")
        elif r["missing"] or r["other"]:
            parts.append(f"«{r['name']}»: нет в архиве {r['missing'] + r['other']} из {r['files']}")
    shown = "; ".join(parts[:limit])
    if len(parts) > limit:
        shown += f" и ещё папок: {len(parts) - limit}"
    return shown
