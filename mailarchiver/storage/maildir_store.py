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
При включённом шифровании к имени добавляется ``.enc`` (см. storage/crypto.py):
сначала сжатие, потом шифрование. Читаются письма любого вида вперемешку —
включение или выключение сжатия/шифрования на старые письма не влияет.
"""
from __future__ import annotations

import gzip
import io
import os
import shutil
import socket
import time
import zlib
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


def _strip_suffixes(name: str) -> str:
    """Имя файла без служебных суффиксов .enc/.gz."""
    if name.endswith(".enc"):
        name = name[:-4]
    if name.endswith(".gz"):
        name = name[:-3]
    return name


class _ClosingGzip(gzip.GzipFile):
    """GzipFile поверх потока расшифровки, закрывающий и сам поток.

    Обычный GzipFile(fileobj=…) закрывает только себя: файл письма оставался
    бы открытым до сборки мусора.
    """

    def __init__(self, inner) -> None:
        super().__init__(fileobj=inner, mode="rb")
        self._inner = inner

    def close(self) -> None:
        try:
            super().close()
        finally:
            try:
                self._inner.close()
            except Exception:  # noqa: BLE001
                pass


class MaildirStore:
    def __init__(self, mail_root: str, *, compress: bool = False, fsync: bool = True,
                 min_free_mb: int = 500, verify_after_write: bool = True,
                 cipher=None, encrypt: bool = False) -> None:
        self.mail_root = mail_root
        self.compress = compress
        self.fsync = fsync
        self.min_free_bytes = int(min_free_mb) * 1024 * 1024
        self.verify_after_write = verify_after_write
        #: ключ для чтения .enc (нужен, даже если новые письма не шифруются)
        self.cipher = cipher
        #: шифровать ли НОВЫЕ письма
        self.encrypt = bool(encrypt and cipher is not None)
        #: шифрование включено в настройках, но ключ недоступен: писать письма
        #: открытым текстом в этом случае НЕЛЬЗЯ (см. store_message)
        self.encryption_blocked = ""
        self._seq = 0
        #: каталоги папок, уже созданные в этом процессе (не дёргать chmod на каждое письмо)
        self._ready_dirs: set = set()
        ensure_dir(mail_root, 0o700)

    def _need_cipher(self):
        if self.cipher is None:
            raise StorageError(
                "Письмо зашифровано, а ключ шифрования не загружен.",
                hint="Укажите файл ключа в storage.encryption_key_file (или верните его на место) "
                     "и перезапустите службу.")
        return self.cipher

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
        if path in self._ready_dirs and os.path.isdir(os.path.join(path, "tmp")):
            return path
        try:
            for sub in ("cur", "new", "tmp"):
                ensure_dir(os.path.join(path, sub), 0o700)
        except OSError as exc:
            raise StorageError(f"Не удалось создать каталог папки «{folder_name}» на диске: {exc}",
                               hint="Проверьте права на каталог с почтой и свободное место.",
                               cause=exc) from exc
        if len(self._ready_dirs) > 10000:
            self._ready_dirs.clear()
        self._ready_dirs.add(path)
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
        if self.encryption_blocked:
            # Шифрование включено, а ключа нет: молча писать открытым текстом
            # нельзя — такие письма так и остались бы открытыми навсегда.
            raise StorageError(
                "Шифрование копии включено, но ключ недоступен — письма не сохраняются, "
                "чтобы не лечь на диск открытым текстом. " + self.encryption_blocked,
                hint="Верните файл ключа (storage.encryption_key_file) или выключите шифрование "
                     "в «Настройки → Хранилище».")
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
        encrypt = self.encrypt and self.cipher is not None
        if encrypt:
            base += ".enc"

        tmp_path = os.path.join(folder_path, "tmp", base)
        cur_path = os.path.join(folder_path, "cur", base)

        payload = gzip.compress(raw) if self.compress else raw
        try:
            with open(tmp_path, "wb") as fh:
                if encrypt:
                    # порциями прямо в файл: без копии всего шифртекста в памяти
                    self.cipher.encrypt_stream(io.BytesIO(payload), fh, len(payload), orig_size=size)
                else:
                    fh.write(payload)
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            del payload
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

    def _stream_digest(self, path: str, chunk: int = 1024 * 1024) -> Tuple[str, int]:
        """SHA-256 и размер содержимого файла письма (распаковка и расшифровка — потоком)."""
        import hashlib
        digest = hashlib.sha256()
        size = 0
        fh = self._open_raw_file(path)
        try:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        return digest.hexdigest(), size

    def _verify(self, path: str, expected_size: int, expected_hash: str) -> None:
        try:
            digest, size = self._stream_digest(path)
        except (OSError, EOFError, zlib.error) as exc:
            self._safe_unlink(path)
            raise StorageError(f"Проверка после записи не удалась (файл не читается): {exc}", cause=exc) from exc
        except StorageError:
            self._safe_unlink(path)
            raise
        if size != expected_size or digest != expected_hash:
            self._safe_unlink(path)
            raise StorageError(
                "Проверка целостности после записи не пройдена (размер/хеш не совпали).",
                hint="Возможны проблемы с диском. Проверьте носитель (smartctl) и файловую систему.",
            )

    # -- чтение --------------------------------------------------------------
    def _read_raw_file(self, path: str) -> bytes:
        """Прочитать письмо целиком (расшифровка и распаковка — потоком с диска,
        без промежуточной копии всего файла в памяти)."""
        fh = self._open_raw_file(path)
        try:
            return fh.read()
        except (EOFError, zlib.error) as exc:
            raise StorageError(f"Файл копии письма повреждён: {exc}",
                               hint="Запустите проверку целостности копии ящика.", cause=exc) from exc
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    def message_path(self, account_id: int, relpath: str) -> str:
        """Абсолютный путь к файлу письма с проверкой, что он внутри каталога ящика.

        Путь берётся из индекса БД; проверка — страховка от записи вида
        ``../../etc/passwd``, если индекс когда-нибудь окажется испорчен.
        Проверка ЛЕКСИЧЕСКАЯ: симлинки внутри ящика не разворачиваем —
        папка, вынесенная ссылкой на другой диск, законна (раньше такие письма
        объявлялись «недопустимым путём», проверка целостности считала их
        повреждёнными, а экспорт молча пропускал).
        """
        rel = relpath or ""
        bad = (not rel or "\x00" in rel or os.path.isabs(rel)
               or any(part == ".." for part in rel.replace("\\", "/").split("/")))
        norm = os.path.normpath(rel) if not bad else ""
        if bad or norm in (".", "") or norm.startswith(".." + os.sep) or norm == "..":
            raise StorageError(f"Недопустимый путь к копии письма: {relpath}",
                               hint="Индекс БД повреждён — запустите проверку целостности.")
        return os.path.join(os.path.abspath(self.account_dir(account_id)), norm)

    def read_message(self, account_id: int, relpath: str) -> bytes:
        path = self.message_path(account_id, relpath)
        if not os.path.exists(path):
            raise StorageError(f"Файл копии не найден: {relpath}", hint="Индекс БД и файлы рассинхронизированы — запустите проверку целостности.")
        try:
            return self._read_raw_file(path)
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать копию письма: {exc}", cause=exc) from exc

    def open_message(self, account_id: int, relpath: str):
        """Открыть копию письма как двоичный поток (распаковка — на лету).

        В отличие от :meth:`read_message` не читает файл целиком: нужно для
        просмотра и скачивания крупных писем без многократного расхода памяти.
        """
        path = self.message_path(account_id, relpath)
        if not os.path.exists(path):
            raise StorageError(f"Файл копии не найден: {relpath}",
                               hint="Индекс БД и файлы рассинхронизированы — запустите проверку целостности.")
        try:
            return self._open_raw_file(path)
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать копию письма: {exc}", cause=exc) from exc

    def hash_message(self, account_id: int, relpath: str, chunk: int = 1024 * 1024) -> Tuple[str, int]:
        """SHA-256 и размер письма, прочитанного ПОТОКОМ (для проверки целостности).

        read_message() держал бы в памяти всё письмо, а при расшифровке — и
        несколько его копий; на письме в сотни мегабайт это гигабайты.
        """
        import hashlib
        digest = hashlib.sha256()
        size = 0
        fh = self.open_message(account_id, relpath)
        try:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать копию письма: {exc}", cause=exc) from exc
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        return digest.hexdigest(), size

    def message_size(self, account_id: int, relpath: str) -> int:
        """Размер письма в байтах (после распаковки)."""
        path = self.message_path(account_id, relpath)
        try:
            return self._plain_size(path)
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать копию письма: {exc}", cause=exc) from exc

    def _open_raw_file(self, path: str):
        name = path
        if name.endswith(".enc"):
            cipher = self._need_cipher()
            fh = cipher.open_reader(open(path, "rb"))
            name = name[:-4]
            if name.endswith(".gz"):
                return _ClosingGzip(fh)
            return fh
        if name.endswith(".gz"):
            return gzip.open(path, "rb")
        return open(path, "rb")

    @staticmethod
    def _plain_size(path: str) -> int:
        if path.endswith(".enc"):
            # размер письма записан в заголовке зашифрованного файла
            from .crypto import read_header
            with open(path, "rb") as fh:
                return int(read_header(fh)["orig_size"])
        if path.endswith(".gz"):
            # Исходный размер хранится в последних 4 байтах gzip (по модулю 2**32 —
            # для отдельного письма этого с запасом хватает).
            with open(path, "rb") as fh:
                fh.seek(-4, os.SEEK_END)
                return int.from_bytes(fh.read(4), "little")
        return os.path.getsize(path)

    # -- перешифровка --------------------------------------------------------
    def convert_message(self, account_id: int, relpath: str, *, encrypt: bool) -> Optional[str]:
        """Зашифровать (или расшифровать) одно письмо на месте.

        Новый файл пишется рядом (tmp → cur, fsync) ПОТОКОМ — письмо в сотни
        мегабайт не требует гигабайта памяти, — сверяется с исходным по
        содержимому (потоковый SHA-256), и только потом старый удаляется.
        Возвращает новый относительный путь или None, если письмо уже в нужном
        виде. Вызывающий обязан сохранить новый путь в индексе ДО удаления
        старого файла — поэтому удаление вынесено в :meth:`drop_converted`.
        """
        old_path = self.message_path(account_id, relpath)
        # Сначала — существует ли файл вообще. Раньше «уже в нужном виде»
        # решалось по одному суффиксу пути, и для отсутствующего файла задание
        # затем удаляло его «двойника» — единственную копию письма.
        if not os.path.exists(old_path):
            raise StorageError(f"Файл копии не найден: {relpath}")
        is_enc = old_path.endswith(".enc")
        if is_enc == encrypt:
            return None
        cipher = self._need_cipher()
        new_path = old_path[:-4] if is_enc else old_path + ".enc"
        folder_dir = os.path.dirname(os.path.dirname(new_path))
        tmp_path = os.path.join(folder_dir, "tmp", os.path.basename(new_path))
        try:
            ensure_dir(os.path.dirname(tmp_path), 0o700)
            with open(tmp_path, "wb") as out:
                if is_enc:
                    src = cipher.open_reader(open(old_path, "rb"))
                    try:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    finally:
                        src.close()
                else:
                    stored_size = os.path.getsize(old_path)
                    orig_size = self._plain_size(old_path)
                    with open(old_path, "rb") as src:
                        cipher.encrypt_stream(src, out, stored_size, orig_size=orig_size)
                out.flush()
                if self.fsync:
                    os.fsync(out.fileno())
            os.replace(tmp_path, new_path)
        except OSError as exc:
            self._safe_unlink(tmp_path)
            raise StorageError(f"Не удалось записать письмо: {exc}", cause=exc) from exc
        except BaseException:
            self._safe_unlink(tmp_path)
            raise
        # сверяем: новое содержимое должно читаться и совпадать со старым
        try:
            if self._stream_digest(new_path) != self._stream_digest(old_path):
                raise StorageError("Проверка после перешифровки не пройдена.")
        except BaseException:
            if new_path != old_path:
                self._safe_unlink(new_path)
            raise
        try:
            st = os.stat(old_path)
            os.utime(new_path, (st.st_atime, st.st_mtime))
        except OSError:
            pass
        return os.path.relpath(new_path, self.account_dir(account_id))

    def drop_converted(self, account_id: int, old_relpath: str) -> None:
        """Удалить прежний файл после успешной перешифровки и записи в индекс."""
        self._safe_unlink(self.message_path(account_id, old_relpath))

    def flags_from_relpath(self, relpath: str) -> List[str]:
        name = _strip_suffixes(os.path.basename(relpath))
        if ":2," in name:
            return maildir_flags_to_imap(name.split(":2,", 1)[1])
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

    def delete_message(self, account_id: int, relpath: str) -> bool:
        """Удалить файл письма. True — файла больше нет (удалён или его и не было).

        False — удалить не удалось (раздел только для чтения, права): тогда и
        запись индекса удалять НЕЛЬЗЯ, иначе файл навсегда остаётся на диске
        невидимым для приложения, а отчёт пишет «освобождено».
        """
        try:
            path = self.message_path(account_id, relpath)
        except StorageError:
            return False
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

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

    def delete_account_files(self, account_id: int) -> Tuple[int, int]:
        """Удалить ВСЕ файлы писем ящика (копия «с нуля»).

        Возвращает ``(удалено файлов, освобождено байт)``. Каталог самого ящика
        создаётся заново пустым, чтобы последующая копия писала в привычное
        место. Вызывается только по явной команде администратора: письма,
        которых уже нет на сервере, после этого не восстановить.
        """
        acc_dir = self.account_dir(account_id)
        count, total = self.account_disk_usage(account_id)
        if os.path.isdir(acc_dir):
            shutil.rmtree(acc_dir, ignore_errors=True)
        ensure_dir(acc_dir, 0o700)
        return count, total

    def quarantine_account_files(self, account_id: int) -> Tuple[str, int, int]:
        """Убрать файлы ящика в карантин вместо удаления (копия «с нуля»).

        Каталог переименовывается в ``<каталог>_old_<дата-время>``: если прогон
        оборвётся на середине, письма ещё на диске и их можно вернуть руками.
        Мгновенная операция — в отличие от удаления сотен тысяч файлов.

        :returns: ``(путь карантина, файлов, байт)``; путь пуст, если копировать
            было нечего.
        """
        acc_dir = self.account_dir(account_id)
        count, total = self.account_disk_usage(account_id)
        quarantine = ""
        if os.path.isdir(acc_dir) and count:
            # Имя подбираем свободное: два пересоздания в пределах одной секунды
            # давали одинаковое имя, и переименование падало с «Directory not
            # empty», а существующий пустой каталог молча заменялся.
            stamp = time.strftime("%Y%m%d_%H%M%S")
            candidate = f"{acc_dir}_old_{stamp}"
            suffix = 0
            while os.path.exists(candidate):
                suffix += 1
                candidate = f"{acc_dir}_old_{stamp}_{suffix}"
            try:
                os.rename(acc_dir, candidate)
            except OSError as exc:
                raise StorageError(f"Не удалось убрать прежнюю копию ящика в карантин: {exc}",
                                   hint="Проверьте права на каталог с почтой и свободное место.") from exc
            quarantine = candidate
            try:
                ensure_dir(acc_dir, 0o700)
            except Exception as exc:  # noqa: BLE001
                # Каталог уже переименован: если новый создать не удалось —
                # возвращаем всё как было, иначе индекс указывал бы в пустоту.
                try:
                    os.rename(candidate, acc_dir)
                except OSError:
                    pass
                raise StorageError(
                    f"Не удалось создать каталог ящика после переноса в карантин: {exc}",
                    hint="Проверьте права на каталог с почтой. Прежняя копия возвращена на место."
                ) from exc
        else:
            ensure_dir(acc_dir, 0o700)
        return quarantine, count, total

    def list_quarantines(self, account_id: int) -> List[Tuple[str, int, int]]:
        """Карантинные копии ящика: ``(путь, файлов, байт)``, свежие первыми.

        Нужны интерфейсу: после пересоздания копии на диске остаётся полная
        прежняя версия, и без такого списка она лежала бы там вечно незаметно.
        """
        acc_dir = self.account_dir(account_id)
        prefix = os.path.basename(acc_dir) + "_old_"
        parent = os.path.dirname(acc_dir)
        out: List[Tuple[str, int, int]] = []
        try:
            names = sorted(os.listdir(parent), reverse=True)
        except OSError:
            return out
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(parent, name)
            count = total = 0
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                        count += 1
                    except OSError:
                        pass
            out.append((path, count, total))
        return out

    def drop_quarantine(self, path: str) -> None:
        """Удалить карантинную копию (только внутри каталога с почтой)."""
        target = os.path.realpath(path)
        root = os.path.realpath(self.mail_root)
        if not target.startswith(root + os.sep) or "_old_" not in os.path.basename(target):
            raise StorageError("Этот каталог не является карантинной копией ящика.",
                               hint="Удалять можно только каталоги вида account_N_old_ДАТА "
                                    "внутри каталога с почтой.")
        shutil.rmtree(target, ignore_errors=True)

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
