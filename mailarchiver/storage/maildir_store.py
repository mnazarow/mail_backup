"""
Локальное хранилище копий писем в формате Maildir.

Раскладка на диске::

    <data_dir>/mailboxes/
        account_<id>/
            <Папка1>/
                cur/  new/  tmp/          <- стандартный Maildir
            <Папка1>/<Подпапка>/
                cur/  new/  tmp/
            ...

Каждое письмо — отдельный файл (это надёжно: повреждение одного файла не
задевает остальные, легко просматривать, копировать, восстанавливать).
Имя файла в Maildir несёт флаги письма, а UID и хеш встроены в «уникальную»
часть имени, чтобы восстановление и дедупликация не зависели только от БД.

Формат имени файла (в подпапке ``cur``)::

    <internaldate_epoch>.M<uid>Q<seq>.<sha8>.mailarchiver:2,<flags>

При включённом сжатии к имени добавляется ``.gz`` и содержимое пишется gzip.
"""
from __future__ import annotations

import gzip
import os
import socket
import time
from typing import Iterator, List, Optional, Tuple

from ..errors import DiskSpaceError, StorageError
from ..util import ensure_dir, disk_free_bytes, sha256_hex, sanitize_folder_component

_HOSTNAME = socket.gethostname().replace("/", "_").replace(":", "_") or "host"

# Соответствие IMAP-флагов буквам Maildir
_IMAP_TO_MAILDIR = {
    "\\Seen": "S",
    "\\Answered": "R",
    "\\Flagged": "F",
    "\\Draft": "D",
    "\\Deleted": "T",
}
_MAILDIR_TO_IMAP = {v: k for k, v in _IMAP_TO_MAILDIR.items()}


def imap_flags_to_maildir(flags) -> str:
    """Список IMAP-флагов -> строка букв Maildir (в алфавитном порядке)."""
    letters = set()
    for fl in flags or ():
        if isinstance(fl, bytes):
            fl = fl.decode("ascii", "ignore")
        letters.add(_IMAP_TO_MAILDIR.get(fl, ""))
    letters.discard("")
    return "".join(sorted(letters))


def maildir_flags_to_imap(letters: str) -> List[str]:
    return [_MAILDIR_TO_IMAP[ch] for ch in (letters or "") if ch in _MAILDIR_TO_IMAP]


class MaildirStore:
    def __init__(self, mail_root: str, *, compress: bool = False, fsync: bool = True,
                 min_free_mb: int = 500, verify_after_write: bool = True) -> None:
        self.mail_root = mail_root
        self.compress = compress
        self.fsync = fsync
        self.min_free_bytes = int(min_free_mb) * 1024 * 1024
        self.verify_after_write = verify_after_write
        self._seq = 0
        ensure_dir(mail_root, 0o700)

    # -- пути ----------------------------------------------------------------
    def account_dir(self, account_id: int) -> str:
        return os.path.join(self.mail_root, f"account_{account_id}")

    def folder_relpath(self, folder_name: str, delimiter: str) -> str:
        """IMAP-имя папки -> относительный путь на диске (безопасный)."""
        delimiter = delimiter or "/"
        parts = folder_name.split(delimiter) if delimiter else [folder_name]
        safe = [sanitize_folder_component(p) for p in parts if p != ""]
        return os.path.join(*safe) if safe else "INBOX"

    def folder_dir(self, account_id: int, folder_name: str, delimiter: str = "/") -> str:
        path = os.path.join(self.account_dir(account_id), self.folder_relpath(folder_name, delimiter))
        for sub in ("cur", "new", "tmp"):
            ensure_dir(os.path.join(path, sub), 0o700)
        return path

    # -- запись --------------------------------------------------------------
    def _check_space(self) -> None:
        free = disk_free_bytes(self.mail_root)
        if free < self.min_free_bytes:
            raise DiskSpaceError(
                f"Недостаточно места на диске: свободно {free // (1024*1024)} МБ, "
                f"требуется минимум {self.min_free_bytes // (1024*1024)} МБ.",
                hint="Освободите место или уменьшите storage.min_free_space_mb / включите ретеншн.",
            )

    def store_message(
        self, account_id: int, folder_name: str, delimiter: str, uid: int,
        raw: bytes, flags=(), internaldate: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """
        Сохранить письмо. Возвращает (относительный_путь, sha256, размер_байт).
        Относительный путь — от account_dir (для хранения в БД).
        """
        if not raw:
            raise StorageError("Пустое тело письма — сохранять нечего.")
        self._check_space()

        digest = sha256_hex(raw)
        size = len(raw)
        folder_path = self.folder_dir(account_id, folder_name, delimiter)

        self._seq = (self._seq + 1) % 1_000_000
        epoch = int(internaldate or time.time())
        flag_letters = imap_flags_to_maildir(flags)
        unique = f"{epoch}.M{uid}Q{self._seq}P{os.getpid()}.{digest[:8]}.mailarchiver.{_HOSTNAME}"
        base = f"{unique}:2,{flag_letters}"
        if self.compress:
            base += ".gz"

        tmp_path = os.path.join(folder_path, "tmp", base)
        cur_path = os.path.join(folder_path, "cur", base)

        payload = gzip.compress(raw) if self.compress else raw
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(payload)
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            os.replace(tmp_path, cur_path)
            try:
                os.utime(cur_path, (epoch, epoch))
            except OSError:
                pass
        except OSError as exc:
            self._safe_unlink(tmp_path)
            raise StorageError(f"Не удалось записать письмо на диск: {exc}", cause=exc) from exc

        if self.verify_after_write:
            self._verify(cur_path, size, digest)

        rel = os.path.relpath(cur_path, self.account_dir(account_id))
        return rel, digest, size

    def _verify(self, path: str, expected_size: int, expected_hash: str) -> None:
        try:
            data = self._read_raw_file(path)
        except OSError as exc:
            raise StorageError(f"Проверка после записи не удалась (файл не читается): {exc}", cause=exc) from exc
        if len(data) != expected_size or sha256_hex(data) != expected_hash:
            self._safe_unlink(path)
            raise StorageError(
                "Проверка целостности после записи не пройдена (размер/хеш не совпали).",
                hint="Возможны проблемы с диском. Проверьте носитель (smartctl) и файловую систему.",
            )

    # -- чтение --------------------------------------------------------------
    def _read_raw_file(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            data = fh.read()
        if path.endswith(".gz"):
            return gzip.decompress(data)
        return data

    def read_message(self, account_id: int, relpath: str) -> bytes:
        path = os.path.join(self.account_dir(account_id), relpath)
        if not os.path.exists(path):
            raise StorageError(f"Файл копии не найден: {relpath}", hint="Индекс БД и файлы рассинхронизированы — запустите проверку целостности.")
        try:
            return self._read_raw_file(path)
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать копию письма: {exc}", cause=exc) from exc

    def flags_from_relpath(self, relpath: str) -> List[str]:
        name = os.path.basename(relpath)
        if ":2," in name:
            letters = name.split(":2,", 1)[1]
            if letters.endswith(".gz"):
                letters = letters[:-3]
            return maildir_flags_to_imap(letters)
        return []

    def iter_messages(self, account_id: int) -> Iterator[Tuple[str, str]]:
        """Обойти все файлы писем аккаунта на диске -> (folder_relpath, file_relpath)."""
        acc_dir = self.account_dir(account_id)
        if not os.path.isdir(acc_dir):
            return
        for root, dirs, files in os.walk(acc_dir):
            if os.path.basename(root) != "cur":
                continue
            folder_rel = os.path.relpath(os.path.dirname(root), acc_dir)
            for fn in files:
                file_rel = os.path.relpath(os.path.join(root, fn), acc_dir)
                yield folder_rel, file_rel

    def delete_message(self, account_id: int, relpath: str) -> None:
        self._safe_unlink(os.path.join(self.account_dir(account_id), relpath))

    def account_disk_usage(self, account_id: int) -> Tuple[int, int]:
        """Вернуть (число_файлов, суммарный_размер_на_диске) для аккаунта."""
        acc_dir = self.account_dir(account_id)
        count, total = 0, 0
        for root, _dirs, files in os.walk(acc_dir):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    count += 1
                except OSError:
                    pass
        return count, total

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
