"""
ZIP-архив, зашифрованный паролем по стандарту WinZip AES (AES-256, AE-2) — 1.21.0.

Нужен для выгрузок дел: архив с перепиской проверки можно передать по почте или на носителе, и
без пароля его не открыть. Такие архивы открывают 7-Zip, WinRAR, WinZip, PeaZip, Keka (macOS),
``7z x`` в Linux; «Проводник» Windows и встроенная утилита macOS AES-архивы не открывают.

Формат (APPNOTE 6.3.x + «AES Encryption Information» WinZip):

* метод сжатия в заголовке — 99, настоящий (deflate) — в дополнительном поле 0x9901;
* ключи — PBKDF2-HMAC-SHA1 (1000 повторов) от пароля и случайной «соли» 16 байт:
  32 байта ключа AES, 32 байта ключа HMAC и 2 байта проверки пароля;
* данные: соль, проверка пароля, AES-256-CTR (счётчик — little-endian, с 1), 10 байт HMAC-SHA1;
* AE-2: CRC-32 в заголовках — 0 (целостность — по HMAC).

Пишется потоком: каждый файл сжимается и шифруется частями, размеры дописываются в локальный
заголовок после данных (файл архива — обычный, с перемоткой). Поддерживается ZIP64 (архивы и
файлы больше 4 ГБ, больше 65535 файлов).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
import zlib
from typing import BinaryIO, List, Optional, Tuple

SALT_LEN = 16          # AES-256
KEY_LEN = 32
PBKDF2_ROUNDS = 1000
MAC_LEN = 10
CHUNK = 1024 * 1024
Z64 = 0xFFFFFFFF


def _dos_time(ts: Optional[float] = None) -> Tuple[int, int]:
    t = time.localtime(ts if ts is not None else time.time())
    year = max(1980, t.tm_year)
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


class _AesCtrLE:
    """AES-CTR с 128-битным счётчиком little-endian, начиная с 1 (как в WinZip)."""

    def __init__(self, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        self._ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        self._counter = 1
        self._rest = b""

    def _stream(self, n: int) -> bytes:
        out = self._rest
        need = n - len(out)
        if need > 0:
            blocks = (need + 15) // 16
            ctrs = b"".join((self._counter + i).to_bytes(16, "little") for i in range(blocks))
            self._counter += blocks
            out += self._ecb.update(ctrs)
        self._rest = out[n:]
        return out[:n]

    def process(self, data: bytes) -> bytes:
        if not data:
            return b""
        ks = self._stream(len(data))
        return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(len(data), "little")


class _Entry:
    __slots__ = ("name", "offset", "csize", "usize", "dtime", "ddate", "zip64_local")

    def __init__(self, name: bytes, offset: int, dtime: int, ddate: int, zip64_local: bool) -> None:
        self.name, self.offset, self.dtime, self.ddate, self.zip64_local = name, offset, dtime, ddate, zip64_local
        self.csize = self.usize = 0


class AesZipWriter:
    """``with AesZipWriter(path, password) as z: z.write_stream(name, fileobj)``."""

    def __init__(self, path: str, password: str) -> None:
        if not password:
            raise ValueError("Нужен пароль архива.")
        self.fh: BinaryIO = open(path, "wb")
        self.password = password.encode("utf-8")
        self.entries: List[_Entry] = []
        self._closed = False

    # -- запись файлов -----------------------------------------------------
    def _local_header(self, e: _Entry) -> bytes:
        extra = b""
        csize = usize = 0
        if e.zip64_local:
            extra += struct.pack("<HHQQ", 0x0001, 16, e.usize, e.csize)
            csize = usize = Z64
        extra += struct.pack("<HHHHBH", 0x9901, 7, 2, 0x4541, 3, 8)       # AE-2, «AE», AES-256, deflate
        return struct.pack("<IHHHHHIIIHH", 0x04034B50, 51, 0x0801, 99, e.dtime, e.ddate, 0, csize, usize,
                           len(e.name), len(extra)) + e.name + extra

    def write_stream(self, name: str, src: BinaryIO, *, size_hint: int = -1, mtime: Optional[float] = None) -> int:
        """Сжать, зашифровать и записать содержимое ``src``. → размер исходных данных."""
        dtime, ddate = _dos_time(mtime)
        big = size_hint < 0 or size_hint > 0xF0000000
        e = _Entry(name.encode("utf-8"), self.fh.tell(), dtime, ddate, big)
        self.fh.write(self._local_header(e))
        salt = os.urandom(SALT_LEN)
        keys = hashlib.pbkdf2_hmac("sha1", self.password, salt, PBKDF2_ROUNDS, 2 * KEY_LEN + 2)
        enc, mac = _AesCtrLE(keys[:KEY_LEN]), hmac.new(keys[KEY_LEN:2 * KEY_LEN], digestmod=hashlib.sha1)
        self.fh.write(salt + keys[2 * KEY_LEN:])
        comp = zlib.compressobj(6, zlib.DEFLATED, -15)
        written = SALT_LEN + 2
        usize = 0
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            usize += len(chunk)
            data = enc.process(comp.compress(chunk))
            mac.update(data)
            self.fh.write(data)
            written += len(data)
        data = enc.process(comp.flush())
        mac.update(data)
        self.fh.write(data)
        written += len(data)
        self.fh.write(mac.digest()[:MAC_LEN])
        written += MAC_LEN
        e.csize, e.usize = written, usize
        end = self.fh.tell()
        if not e.zip64_local and (e.csize >= Z64 or e.usize >= Z64):
            raise ValueError("Файл больше 4 ГБ без отметки ZIP64")      # не бывает: size_hint с запасом
        self.fh.seek(e.offset)
        self.fh.write(self._local_header(e))                            # те же длины — дописываем размеры
        self.fh.seek(end)
        self.entries.append(e)
        return usize

    def writestr(self, name: str, data: bytes, mtime: Optional[float] = None) -> int:
        import io
        return self.write_stream(name, io.BytesIO(data), size_hint=len(data), mtime=mtime)

    def write_file(self, name: str, path: str) -> int:
        with open(path, "rb") as fh:
            return self.write_stream(name, fh, size_hint=os.path.getsize(path), mtime=os.path.getmtime(path))

    # -- оглавление --------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cd_start = self.fh.tell()
        for e in self.entries:
            z64 = b""
            usize, csize, offset = e.usize, e.csize, e.offset
            if e.usize >= Z64 or e.zip64_local:
                z64 += struct.pack("<Q", e.usize)
                usize = Z64
            if e.csize >= Z64 or e.zip64_local:
                z64 += struct.pack("<Q", e.csize)
                csize = Z64
            if e.offset >= Z64:
                z64 += struct.pack("<Q", e.offset)
                offset = Z64
            extra = (struct.pack("<HH", 0x0001, len(z64)) + z64 if z64 else b"") + \
                struct.pack("<HHHHBH", 0x9901, 7, 2, 0x4541, 3, 8)
            self.fh.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x033F, 51, 0x0801, 99, e.dtime, e.ddate,
                                      0, csize, usize, len(e.name), len(extra), 0, 0, 0, 0o100644 << 16, offset)
                          + e.name + extra)
        cd_end = self.fh.tell()
        count, cd_size = len(self.entries), cd_end - cd_start
        if count >= 0xFFFF or cd_size >= Z64 or cd_start >= Z64:
            self.fh.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 0x033F, 45, 0, 0, count, count, cd_size,
                                      cd_start))
            self.fh.write(struct.pack("<IIQI", 0x07064B50, 0, cd_end, 1))
            self.fh.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, Z64, Z64, 0))
        else:
            self.fh.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, cd_size, cd_start, 0))
        self.fh.close()

    def abort(self) -> None:
        self._closed = True
        try:
            self.fh.close()
        except OSError:
            pass

    def __enter__(self) -> "AesZipWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()
