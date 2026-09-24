"""
Шифрование файлов писем в локальной копии (AES-256-GCM, потоковый формат).

Формат файла (``*.enc``), версия 2::

    MAENC2\\0\\0          8 байт   сигнатура и версия формата
    key_id              8 байт   отпечаток главного ключа («не тот ключ» видно сразу)
    salt               16 байт   случайная соль файла: из неё и главного ключа (HKDF-SHA256)
                                 получается СОБСТВЕННЫЙ ключ этого файла
    nonce_prefix        7 байт   случайный префикс nonce
    chunk_size          4 байта  размер открытой порции (big-endian)
    plain_size          8 байт   сколько байт зашифровано (после gzip, если он есть)
    orig_size           8 байт   размер самого письма (до сжатия)
    порции: AES-GCM(ключ файла, nonce = prefix || номер(4) || признак_последней(1), aad = заголовок)

Письмо режется на порции по 64 КБ, у каждой свой тег подлинности. Поэтому
зашифрованное письмо можно читать ПОТОКОМ (просмотр и скачивание крупных
писем не загружают файл в память), а подмена, перестановка или обрезка порций
обнаруживаются: номер порции и признак последней входят в nonce, заголовок —
в проверяемые данные каждой порции (схема STREAM, Hoang et al.).

Почему ключ на каждый файл. В версии 1 все файлы шифровались одним ключом, а
уникальность nonce держалась на 56 случайных битах префикса: на миллионах писем
вероятность совпадения переставала быть пренебрежимой (порог NIST — 2⁻³² —
пройден уже после ~6 тысяч файлов), а совпадение nonce в GCM раскрывает текст
и позволяет подделывать порции. С отдельным ключом на файл (как в Tink
AES-GCM-HKDF) повтор nonce между файлами ничего не значит. Файлы версии 1
по-прежнему читаются.

Главный ключ — 32 случайных байта в отдельном файле. Без него зашифрованные
письма не прочитать ничем, поэтому ключ нужно хранить в резервной копии
ОТДЕЛЬНО от каталога данных — иначе шифрование защищает лишь от утечки одной
папки писем.
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
import io
import os
import secrets
import struct
from typing import BinaryIO, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..errors import StorageError

MAGIC_V1 = b"MAENC1\x00\x00"
MAGIC = b"MAENC2\x00\x00"
_HEADER_V1 = struct.Struct(">8s8s7sIQQ")        # 43 байта
_HEADER = struct.Struct(">8s8s16s7sIQQ")        # 59 байт
HEADER_SIZE = _HEADER.size
_MAGIC_LEN = 8
TAG_SIZE = 16
CHUNK_SIZE = 64 * 1024
KEY_SIZE = 32
SALT_SIZE = 16
SUFFIX = ".enc"
_HKDF_INFO = b"mailarchiver-file-key-v2"


def key_id_of(key: bytes) -> bytes:
    return hmac.new(key, b"mailarchiver-storage-key-v1", hashlib.sha256).digest()[:8]


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def generate_key_file(path: str) -> bytes:
    """Создать новый файл ключа (0600). Существующий файл не перезаписывается."""
    key = secrets.token_bytes(KEY_SIZE)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise StorageError(f"Файл ключа уже существует: {path}",
                           hint="Существующий ключ перезаписывать нельзя — им зашифрованы письма.")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    # запись о новом файле в каталоге тоже должна пережить сбой питания:
    # иначе после перезагрузки ключа могло не оказаться, а письма — уже им зашифрованы
    _fsync_dir(parent)
    return key


def load_key_file(path: str) -> bytes:
    """Прочитать ключ: 64 шестнадцатеричных символа (BOM, пробелы и переводы строк допустимы)."""
    with open(path, "rb") as fh:
        data = fh.read(4096)
    text = data.decode("utf-8-sig", "replace")
    text = "".join(text.split())          # пробелы, \r\n от Windows-редакторов
    try:
        key = binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != KEY_SIZE:
        raise StorageError(f"Файл ключа шифрования повреждён: {path}",
                           hint="Ожидается 64 шестнадцатеричных символа (32 байта). Верните файл из резервной копии.")
    return key


def key_file_warnings(path: str) -> list:
    """Замечания к правам файла ключа (для интерфейса и журнала).

    Ключ, читаемый всеми, — это ключ, который может унести любой пользователь
    сервера. Чтение группой допустимо: так служба читает ключ, принадлежащий
    root (chown root:mailarchiver, chmod 640).
    """
    out = []
    try:
        st = os.stat(path)
    except OSError:
        return out
    mode = st.st_mode & 0o777
    if mode & 0o007:
        out.append(f"Файл ключа {path} доступен ВСЕМ пользователям сервера ({oct(mode)}). "
                   f"Выполните: chmod 640 {path}")
    elif mode & 0o020:
        out.append(f"Файл ключа {path} может изменять группа ({oct(mode)}). Выполните: chmod 640 {path}")
    return out


class StorageCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_SIZE:
            raise ValueError("Ключ шифрования должен быть 32 байта")
        self._master = bytes(key)
        self._aead_v1 = AESGCM(key)
        self.key_id = key_id_of(key)

    @property
    def key_id_hex(self) -> str:
        return self.key_id.hex()

    def _file_aead(self, salt: bytes) -> AESGCM:
        file_key = HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=salt,
                        info=_HKDF_INFO).derive(self._master)
        return AESGCM(file_key)

    @staticmethod
    def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
        return prefix + struct.pack(">I", index) + (b"\x01" if last else b"\x00")

    # -- шифрование --------------------------------------------------------
    def encrypt_stream(self, src: BinaryIO, dst: BinaryIO, plain_size: int,
                       orig_size: Optional[int] = None) -> int:
        """Зашифровать ровно ``plain_size`` байт из ``src`` в ``dst`` порциями.

        В памяти одновременно только одна порция — письмо в сотни мегабайт
        шифруется без многократного расхода памяти. Возвращает число
        записанных байт.
        """
        plain_size = int(plain_size)
        count = max(1, -(-plain_size // CHUNK_SIZE))
        if count > 0xFFFFFFFF:
            raise StorageError("Письмо слишком велико для шифрования.")
        salt = secrets.token_bytes(SALT_SIZE)
        prefix = secrets.token_bytes(7)
        header = _HEADER.pack(MAGIC, self.key_id, salt, prefix, CHUNK_SIZE, plain_size,
                              plain_size if orig_size is None else int(orig_size))
        aead = self._file_aead(salt)
        dst.write(header)
        written = len(header)
        left = plain_size
        for i in range(count):
            want = min(CHUNK_SIZE, left)
            chunk = _read_exact(src, want)
            if len(chunk) != want:
                raise StorageError("Исходные данные письма изменились во время шифрования.")
            block = aead.encrypt(self._nonce(prefix, i, i == count - 1), chunk, header)
            dst.write(block)
            written += len(block)
            left -= want
        if src.read(1):
            raise StorageError("Исходные данные письма изменились во время шифрования.")
        return written

    def encrypt(self, plain: bytes, orig_size: Optional[int] = None) -> bytes:
        out = io.BytesIO()
        self.encrypt_stream(io.BytesIO(plain), out, len(plain), orig_size)
        return out.getvalue()

    # -- расшифровка -------------------------------------------------------
    def decrypt(self, blob: bytes) -> bytes:
        reader = self.open_reader(io.BytesIO(blob))
        try:
            return reader.read()
        finally:
            reader.close()

    def open_reader(self, fh) -> io.BufferedReader:
        """Поток открытых данных поверх зашифрованного файла."""
        return io.BufferedReader(_DecryptingRaw(self, fh), buffer_size=CHUNK_SIZE)


def _read_exact(src: BinaryIO, size: int) -> bytes:
    parts = []
    left = size
    while left > 0:
        block = src.read(left)
        if not block:
            break
        parts.append(block)
        left -= len(block)
    return b"".join(parts)


def read_header(fh) -> dict:
    magic = fh.read(_MAGIC_LEN)
    if magic == MAGIC:
        rest = fh.read(_HEADER.size - _MAGIC_LEN)
        raw = magic + rest
        if len(raw) != _HEADER.size:
            raise StorageError("Зашифрованный файл письма повреждён: неполный заголовок.")
        _m, key_id, salt, prefix, chunk_size, plain_size, orig_size = _HEADER.unpack(raw)
        version = 2
    elif magic == MAGIC_V1:
        rest = fh.read(_HEADER_V1.size - _MAGIC_LEN)
        raw = magic + rest
        if len(raw) != _HEADER_V1.size:
            raise StorageError("Зашифрованный файл письма повреждён: неполный заголовок.")
        _m, key_id, prefix, chunk_size, plain_size, orig_size = _HEADER_V1.unpack(raw)
        salt = b""
        version = 1
    elif len(magic) < _MAGIC_LEN:
        raise StorageError("Зашифрованный файл письма повреждён: неполный заголовок.")
    else:
        raise StorageError("Файл не является зашифрованным письмом MailArchiver (неверная сигнатура).")
    if not (1024 <= chunk_size <= 16 * 1024 * 1024):
        raise StorageError("Зашифрованный файл письма повреждён: неверный размер порции.")
    return {"raw": raw, "version": version, "key_id": key_id, "salt": salt, "prefix": prefix,
            "chunk_size": chunk_size, "plain_size": plain_size, "orig_size": orig_size}


class _DecryptingRaw(io.RawIOBase):
    def __init__(self, cipher: StorageCipher, fh) -> None:
        super().__init__()
        self._fh = fh
        try:
            hdr = read_header(fh)
            if hdr["key_id"] != cipher.key_id:
                raise StorageError(
                    "Ключ шифрования не подходит к этому письму.",
                    hint="Письмо зашифровано другим ключом. Укажите в storage.encryption_key_file "
                         "тот файл ключа, которым шифровался архив.")
        except BaseException:
            fh.close()
            raise
        self._aead = cipher._file_aead(hdr["salt"]) if hdr["version"] == 2 else cipher._aead_v1
        self._hdr = hdr
        self._count = max(1, -(-hdr["plain_size"] // hdr["chunk_size"]))
        self._index = 0
        self._buf = b""
        self._pos = 0

    def readable(self) -> bool:
        return True

    def _next_chunk(self) -> bool:
        if self._index >= self._count:
            return False
        last = self._index == self._count - 1
        size = self._hdr["chunk_size"]
        want = (self._hdr["plain_size"] - size * self._index if last else size) + TAG_SIZE
        blob = _read_exact(self._fh, want)
        if len(blob) != want:
            raise StorageError("Зашифрованный файл письма обрезан или повреждён.",
                               hint="Запустите проверку целостности копии ящика.")
        try:
            self._buf = self._aead.decrypt(
                StorageCipher._nonce(self._hdr["prefix"], self._index, last), blob, self._hdr["raw"])
        except InvalidTag:
            raise StorageError("Зашифрованный файл письма повреждён (проверка подлинности не пройдена).",
                               hint="Файл изменён или испорчен на диске. Запустите проверку целостности.")
        self._pos = 0
        self._index += 1
        if last and self._fh.read(1):
            raise StorageError("Зашифрованный файл письма повреждён: лишние данные в конце.")
        return True

    def readinto(self, b) -> int:
        if self._pos >= len(self._buf):
            if not self._next_chunk():
                return 0
        n = min(len(b), len(self._buf) - self._pos)
        b[:n] = self._buf[self._pos:self._pos + n]
        self._pos += n
        return n

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()
