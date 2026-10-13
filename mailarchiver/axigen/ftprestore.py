"""
Восстановление писем из архива в ящик Axigen через службу FTP Backup — без
пароля ящика.

Отбор писем и правила папок назначения — те же, что у восстановления по IMAP
(:class:`mailarchiver.imap.restore.RestoreEngine`): в исходные папки, в папки
с префиксом или в одну папку; «сухой прогон»; ограничение числа писем.

Отличия от IMAP:

* письмо заливается файлом в каталог папки (``STOR``); недостающая папка
  создаётся (``MKD``) с именем в той же кодировке, что у соседних папок;
* отметки «прочитано», «важное» и дату получения FTP не передаёт — у
  восстановленных так писем их нет;
* дубли проверяются по Message-ID: у файлов папки, которые архив уже брал,
  Message-ID известен из индекса, остальные файлы читаются с сервера (не больше
  :data:`MAX_DUP_DOWNLOADS` на папку — больше честно отмечается «без проверки
  дублей»);
* в конце папка перечитывается: письмо считается восстановленным, только если
  сервер его показывает. Не показывает — ошибка, а не «успешно»: как именно
  Axigen принимает залитые файлы, в документации не описано.

Требует «Разрешить изменения на сервере» в настройках Axigen: файлы заливаются
учётной записью администратора.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..errors import JobCancelled, MailArchiverError
from ..imap.restore import MAX_MESSAGE_EVENTS, RestoreEngine, RestoreResult, _normalize_message_id
from ..logging_setup import get_logger
from ..models import Account
from . import store as axstore
from .common import AxigenConnectionError, AxigenError, AxigenSettings
from .ftp import AxigenFTP, Entry, FileTooLarge, FtpPathError, guess_encoding, join, wire_name

log = get_logger("axigen.restore")

#: Сколько незнакомых архиву файлов папки читать ради проверки дублей.
MAX_DUP_DOWNLOADS = 2000
#: Сколько раз пробовать залить одно письмо, если связь рвётся.
MAX_STOR_ATTEMPTS = 3
_MID = re.compile(rb"^message-id:[ \t]*(.*(?:\r?\n[ \t]+.*)*)", re.I | re.M)


def message_id_of(raw: bytes) -> str:
    head = raw[:256 * 1024]
    end = re.search(rb"\r?\n\r?\n", head)
    block = head[:end.start()] if end else head
    m = _MID.search(block)
    if not m:
        return ""
    return _normalize_message_id(" ".join(m.group(1).decode("latin-1", "replace").split()))


def upload_name(row) -> str:
    """Имя файла для письма архива: одно и то же при повторном восстановлении."""
    sha = re.sub(r"[^0-9a-f]", "", str(row["sha256"] or "").lower())[:12] or "0"
    return f"ma{int(row['uid'])}-{sha}.eml"


@dataclass
class _Folder:
    target: str                       # имя папки назначения («Восстановлено/INBOX»)
    path: str = ""                    # путь на сервере (в виде для команд)
    created: bool = False             # папку создали в этом прогоне
    before: Dict[str, Entry] = field(default_factory=dict)   # файлы до заливки: имя → запись
    ids: Optional[Set[str]] = None    # Message-ID писем папки (None — ещё не собраны)
    dup_off: bool = False             # проверка дублей недоступна
    uploaded: List[str] = field(default_factory=list)         # залитые имена
    failed: bool = False


class FtpRestoreEngine(RestoreEngine):
    """Восстановление через FTP Backup (тот же итог, что у :class:`RestoreEngine`)."""

    def __init__(self, db, store, settings: AxigenSettings, client_factory=None) -> None:
        super().__init__(db, store, None)
        self.st = settings
        self.client_factory = client_factory or AxigenFTP.open

    def run(self, account: Account, *, folders: Optional[List[str]] = None,
            target_mode: str = "prefixed", target_folder: str = "", target_prefix: str = "",
            check_duplicates: bool = True, dry_run: bool = False, limit: int = 0,
            progress_cb=None, cancel_cb=None, event_cb=None) -> RestoreResult:
        result = RestoreResult(dry_run=dry_run)
        started = time.time()
        events = [0]

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[restore %s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def emit_msg(level: str, msg: str) -> None:
            events[0] += 1
            if events[0] <= MAX_MESSAGE_EVENTS:
                emit(level, msg)
            elif events[0] == MAX_MESSAGE_EVENTS + 1:
                emit("WARNING", f"Сообщений об отдельных письмах больше {MAX_MESSAGE_EVENTS} — дальше они пишутся "
                                f"только в журнал службы; итог — в конце задания.")

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Восстановление отменено пользователем.")

        result.total = self._count_messages(account.id, folders, limit)
        emit("INFO", f"Восстановление через FTP Backup Axigen (без пароля ящика). Отобрано писем: {result.total}"
                     + (" (сухой прогон)" if dry_run else "") + ". Отметки «прочитано» и «важное» через FTP не "
                                                                  "передаются.")
        if progress_cb:
            progress_cb(0, result.total, "Подготовка", 0, 0.0)
        if not result.total:
            return result

        run = _Run(self, account, result, emit, emit_msg, check_duplicates, dry_run)
        run.open()
        done = 0
        bytes_done = 0
        try:
            for row in self._iter_messages(account.id, folders, limit):
                check_cancel()
                target = self._resolve_target(row["folder"], "/", target_mode, target_folder, target_prefix)
                state = run.folder(target)
                done += 1
                if state.failed:
                    result.add_error(f"UID {row['uid']} → «{target}»: папка назначения недоступна")
                    continue
                try:
                    raw = self.store.read_message(account.id, row["stored_path"])
                except MailArchiverError as exc:
                    result.add_error(f"{row['stored_path']}: {exc.message}")
                    emit_msg("ERROR", f"Файл копии недоступен: {exc.message}")
                    continue
                outcome = run.restore_one(state, row, raw)
                if outcome == "restored":
                    result.restored += 1
                    bytes_done += len(raw)
                elif outcome == "skipped":
                    result.skipped += 1
                if progress_cb and (done % 20 == 0 or done == result.total):
                    elapsed = max(0.001, time.time() - started)
                    progress_cb(done, result.total, f"{done}/{result.total}", bytes_done, bytes_done / elapsed)
            if not dry_run:
                run.verify()
        finally:
            run.close()
        emit("INFO" if not result.errors else "WARNING",
             f"Восстановление через FTP Backup: залито {result.restored}, пропущено {result.skipped}, "
             f"ошибок {result.errors}.")
        return result


class _Run:
    def __init__(self, engine: FtpRestoreEngine, account: Account, result: RestoreResult, emit, emit_msg,
                 check_duplicates: bool, dry_run: bool) -> None:
        self.e = engine
        self.db = engine.db
        self.acc = account
        self.result = result
        self.emit = emit
        self.emit_msg = emit_msg
        self.check_duplicates = check_duplicates
        self.dry_run = dry_run
        self.client: Optional[AxigenFTP] = None
        self.root = ""
        self.folders: Dict[str, _Folder] = {}
        self.encoding = "utf7"

    # -- соединение ----------------------------------------------------------
    def open(self) -> None:
        self.client = self.e.client_factory(self.e.st)
        hint = axstore.ftp_path(self.db, self.acc.id)
        self.root = self.client.find_account_dir(self.acc.username, hint)
        if self.root != hint:
            axstore.set_ftp_path(self.db, self.acc.id, self.root)
        top = self.client.listdir(self.root)
        self.encoding = guess_encoding(top, self.e.st.ftp_name_encoding)
        self.emit("INFO", f"Каталог ящика на FTP Backup: {self.client.shown(self.root)}.")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _again(self, fn):
        for attempt in range(MAX_STOR_ATTEMPTS):
            try:
                return fn()
            except AxigenConnectionError as exc:
                self.result.reconnects += 1
                if attempt + 1 >= MAX_STOR_ATTEMPTS:
                    raise
                self.emit("WARNING", f"Связь с FTP Backup оборвалась ({exc.message}) — переподключаемся.")
                time.sleep(min(10, 2 * (attempt + 1)))
                self.client.reconnect()

    # -- папки ---------------------------------------------------------------
    def folder(self, target: str) -> _Folder:
        state = self.folders.get(target)
        if state is not None:
            return state
        state = _Folder(target=target)
        self.folders[target] = state
        try:
            self._locate(state)
        except AxigenError as exc:
            state.failed = True
            self.emit("ERROR", f"Папка «{target}» на сервере недоступна: {exc.message}")
        return state

    def _locate(self, state: _Folder) -> None:
        from .ftp import _bad_name
        path = self.root
        parts = [p for p in state.target.split("/") if p]
        bad = next((p for p in parts if _bad_name(p)), None)
        if bad is not None:
            raise AxigenError(f"имя папки «{bad}» нельзя передать службе FTP Backup («.», «..» или управляющие "
                              f"знаки) — восстановите эти письма в другую папку")
        for part in parts:
            entries = self._again(lambda path=path: self.client.listdir(path))
            hit = next((e for e in entries if e.is_dir and e.name.lower() == part.lower()), None)
            if hit is None and part.lower() == "inbox":
                hit = next((e for e in entries if e.is_dir and e.name.lower() in ("inbox", "входящие")), None)
            if hit is not None:
                path = join(path, hit.wire)
                continue
            new_path = join(path, wire_name(part, self.encoding))
            if self.dry_run:
                state.created = True
                state.path = new_path
                return
            try:
                self._again(lambda p=new_path: self.client.mkd(p))
            except FtpPathError as exc:
                raise AxigenError(f"не удалось создать папку «{part}» ({exc.reply})") from exc
            self.emit("INFO", f"Создана папка «{state.target}» на сервере.")
            state.created = True
            path = new_path
        state.path = path
        if not state.created:
            files = self._again(lambda: self.client.listdir(path))
            state.before = {e.wire: e for e in files if not e.is_dir}

    # -- проверка дублей -----------------------------------------------------
    def _folder_ids(self, state: _Folder) -> Set[str]:
        if state.ids is not None:
            return state.ids
        ids: Set[str] = set()
        if not state.before:
            state.ids = ids
            return ids
        archive_folder = self._archive_folder(state.target)
        known = axstore.ftp_files(self.db, self.acc.id, archive_folder)
        by_key = {(int(r["uidvalidity"]), int(r["uid"])): r["message_id"] for r in self.db.query(
            "SELECT uidvalidity, uid, message_id FROM messages WHERE account_id=? AND folder=?",
            (self.acc.id, archive_folder))}
        unknown: List[Entry] = []
        for wire, entry in state.before.items():
            row = known.get(wire)
            if row is not None and row["kind"] == "mail" and (entry.size is None or int(row["size"]) == entry.size):
                mid = by_key.get((int(row["uidvalidity"]), int(row["uid"])))
                if mid:
                    ids.add(_normalize_message_id(mid))
                    continue
            unknown.append(entry)
        if len(unknown) > MAX_DUP_DOWNLOADS:
            state.dup_off = True
            self.emit("WARNING", f"Папка «{state.target}»: незнакомых архиву писем {len(unknown)} — читать их все "
                                 f"ради проверки дублей долго. Письма будут залиты без проверки — возможны повторы.")
        else:
            for entry in unknown:
                try:
                    raw = self._again(lambda e=entry: self.client.retr(join(state.path, e.wire)))
                except FtpPathError:
                    continue
                except FileTooLarge:
                    # письмо слишком большое, чтобы читать его ради Message-ID: его повтор не узнаем
                    self.emit("WARNING", f"Папка «{state.target}»: письмо {entry.name} слишком большое для проверки "
                                         f"дублей — его повтор, если он есть в архиве, будет залит ещё раз.")
                    continue
                mid = message_id_of(raw)
                if mid:
                    ids.add(mid)
            if unknown:
                self.emit("INFO", f"Папка «{state.target}»: для проверки дублей прочитано писем с сервера: "
                                  f"{len(unknown)}.")
        state.ids = ids
        return ids

    def _archive_folder(self, target: str) -> str:
        low = target.lower()
        for name in self.db.account_folders(self.acc.id):
            if name.lower() == low:
                return name
        return "INBOX" if low == "inbox" else target

    # -- заливка -------------------------------------------------------------
    def restore_one(self, state: _Folder, row, raw: bytes) -> str:
        name = upload_name(row)
        mid = message_id_of(raw) or _normalize_message_id(row["message_id"] or "")
        if self.check_duplicates:
            if name in state.before or name in state.uploaded:
                return "skipped"
            if not state.dup_off and mid and mid in self._folder_ids(state):
                return "skipped"
            if state.dup_off:
                self.result.dup_check_unavailable += 1
        if self.dry_run:
            return "restored"
        try:
            self._again(lambda: self.client.stor(join(state.path, name), raw))
        except FtpPathError as exc:
            self.result.add_error(f"UID {row['uid']} → «{state.target}»: сервер не принял файл ({exc.reply})")
            self.emit_msg("ERROR", f"Письмо UID {row['uid']} не залито в «{state.target}»: {exc.reply}")
            return "error"
        state.uploaded.append(name)
        if mid and state.ids is not None:
            state.ids.add(mid)
        return "restored"

    def verify(self) -> None:
        """Перечитать папки: всё ли залитое сервер показывает."""
        for state in self.folders.values():
            if not state.uploaded or state.failed:
                continue
            files = self._again(lambda s=state: self.client.listdir(s.path))
            after = {e.wire for e in files if not e.is_dir}
            missing = [n for n in state.uploaded if n not in after]
            if not missing:
                continue
            grown = len(after - set(state.before)) if not state.created else len(after)
            if grown >= len(state.uploaded):
                # имена сервер поменял, но писем прибавилось ровно столько — залито всё
                self.emit("INFO", f"Папка «{state.target}»: сервер переименовал залитые файлы; писем прибавилось "
                                  f"{grown}.")
                continue
            lost = len(state.uploaded) - grown
            self.result.restored -= lost
            for _ in range(lost):
                self.result.add_error(f"«{state.target}»: сервер принял файл, но письма в папке нет")
            self.emit("ERROR", f"Папка «{state.target}»: залито {len(state.uploaded)} писем, а сервер показывает "
                               f"только {max(0, grown)} новых. Как Axigen принимает залитые через FTP Backup "
                               f"письма на вашем сервере, проверьте в WebMail; при повторе уже показанные письма "
                               f"второй раз не зальются.")


__all__ = ["FtpRestoreEngine", "message_id_of", "upload_name", "MAX_DUP_DOWNLOADS"]
