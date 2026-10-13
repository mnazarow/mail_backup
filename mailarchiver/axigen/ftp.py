"""
Клиент службы FTP Backup & Restore сервера Axigen.

Служба отдаёт по FTP структуру хранилища: домены, в них учётные записи, в
учётной записи — папки ящика каталогами, письма — файлами (или одним пакетом
``messages.tar`` на папку, если в настройках службы включено «Use the TAR
format to pack messages»). Вход — логином и паролем администратора Axigen,
поэтому пароли сотрудников не нужны.

Устройство дерева в документации Axigen описано только в общих словах, поэтому
клиент ничего не предполагает жёстко:

* список каталога берётся командой ``MLSD``, без неё — ``LIST`` (unix и
  DOS-вид), без неё — ``NLST``;
* имена приходят байтами: UTF-8 или изменённый UTF-7 (как в IMAP: «M&APY-m»),
  смотря по настройке службы «Folder name encoding»; клиент передаёт имя
  серверу ровно в том виде, в каком получил, а для людей раскодирует;
* каталог ящика ищется по нескольким известным раскладкам, а если не нашёлся —
  ошибка перечисляет, что лежит в каталоге домена (и можно задать свой
  шаблон пути в настройках).

Шифрование: ``explicit`` — команда AUTH TLS на обычном порту, ``implicit`` —
TLS с первого байта (отдельный SSL-слушатель). Канал данных шифруется тоже
(PROT P) и продолжает сессию TLS канала управления — так требуют многие
серверы FTPS.
"""
from __future__ import annotations

import base64
import ftplib
import io
import posixpath
import re
import socket
import ssl
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from ..logging_setup import get_logger
from .common import (AxigenAuthError, AxigenConnectionError, AxigenError, AxigenSettings, TlsPolicy,
                     cert_fingerprint, normalize_fingerprint, pretty_fingerprint, AxigenCertError, require_encryption,
                     split_email)

log = get_logger("axigen.ftp")

TAR_NAME = "messages.tar"
#: Предел одного файла (письма), который читается в память, даже если лимит
#: размера копирования не задан: больше — письмо пропускается с предупреждением.
MAX_FILE_BYTES = 512 * 1024 * 1024
#: Каталоги внутри каталога домена, где могут лежать учётные записи.
ACCOUNT_CONTAINERS = ("accounts", "users", "mailboxes", "objects", "Accounts", "Users")


# ---------------------------------------------------------------------------
#  Имена: изменённый UTF-7 (RFC 3501) и UTF-8
# ---------------------------------------------------------------------------
def utf7_decode(text: str) -> Optional[str]:
    """Раскодировать изменённый UTF-7 IMAP. None — строка не в этой кодировке."""
    if "&" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "&":
            if not (0x20 <= ord(ch) <= 0x7E):
                return None
            out.append(ch)
            i += 1
            continue
        end = text.find("-", i + 1)
        if end < 0:
            return None
        chunk = text[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            if not re.fullmatch(r"[A-Za-z0-9+,]+", chunk):
                return None
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                raw = base64.b64decode(b64, validate=True)
                if len(raw) % 2:
                    return None
                out.append(raw.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                return None
        i = end + 1
    return "".join(out)


def utf7_encode(text: str) -> str:
    """Текст → изменённый UTF-7 IMAP (как служба FTP Backup пишет имена папок по умолчанию)."""
    out: List[str] = []
    pending: List[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()
    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def _bad_name(text: str) -> bool:
    """Имя, которое нельзя считать именем папки: «.», «..», «/», нулевой и управляющие знаки."""
    import unicodedata
    return text in (".", "..") or "/" in text or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text)


def decode_name(wire: str, mode: str = "auto") -> str:
    """Имя с сервера (строка latin-1 — байты как есть) → текст для людей и архива.

    Раскодированное UTF-7 имя, которое стало «..», содержит «/» или управляющие
    знаки (``&AC4ALg-`` → «..»), не принимается — остаётся имя как на сервере.
    """
    raw = wire.encode("latin-1", "replace")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", "replace")
    if mode == "utf8":
        return text
    decoded = utf7_decode(text)
    if decoded is None or (decoded != text and _bad_name(decoded)):
        return text
    if mode == "utf7" or decoded != text:
        return decoded
    return text


def encode_name(text: str) -> str:
    """Имя из настроек (UTF-8) → строка для отправки серверу (latin-1 по байтам)."""
    return text.encode("utf-8").decode("latin-1")


def guess_encoding(entries: Sequence["Entry"], mode: str = "auto") -> str:
    """В какой кодировке сервер пишет имена: utf7 или utf8 (по уже существующим именам).

    Нужна, чтобы создать на сервере новую папку с именем в той же кодировке.
    Не по чему судить (все имена латиницей) — UTF-7: так Axigen пишет по умолчанию.
    """
    if mode in ("utf7", "utf8"):
        return mode
    for entry in entries:
        if any(ord(ch) > 0x7F for ch in entry.wire):
            return "utf8"
    for entry in entries:
        if "&" in entry.wire and entry.name != entry.wire:
            return "utf7"
    return "utf7"


def wire_name(text: str, encoding: str) -> str:
    """Имя для команды серверу в его кодировке (строка latin-1 — байты как есть)."""
    return utf7_encode(text) if encoding == "utf7" else encode_name(text)


# ---------------------------------------------------------------------------
#  Разбор списков каталога
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    wire: str                    # имя, как прислал сервер (для команд)
    name: str                    # имя для людей
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[float] = None

    @property
    def mtime_iso(self) -> str:
        if self.mtime is None:
            return ""
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).isoformat()


_UNIX = re.compile(
    r"^(?P<type>[\-dlbcps])[rwxsStTl\-]{9}\S*\s+\d+\s+\S+(?:\s+\S+)?\s+(?P<size>\d+)\s+"
    r"(?P<date>[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4}))\s(?P<name>.+)$")
# DOS-вид: у каталога после «<DIR>» — выравнивающие пробелы, у файла после
# размера — ровно один пробел, дальше имя целиком (и с пробелами в начале).
_DOS = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}\s*[AaPp][Mm]?)\s+"
    r"(?:(?P<size><DIR>)\s+|(?P<bytes>\d+) )(?P<name>.+)$")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                         "nov", "dec"), 1)}


def _unix_time(text: str, now: Optional[datetime] = None) -> Optional[float]:
    parts = text.split()
    try:
        month = _MONTHS[parts[0].lower()[:3]]
        day = int(parts[1])
        now = now or datetime.now(timezone.utc)
        if ":" in parts[2]:
            hour, minute = (int(x) for x in parts[2].split(":"))
            year = now.year
            stamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            if stamp > now.replace(microsecond=0) and (stamp - now).days > 1:
                stamp = stamp.replace(year=year - 1)
        else:
            stamp = datetime(int(parts[2]), month, day, tzinfo=timezone.utc)
        return stamp.timestamp()
    except (KeyError, ValueError, IndexError):
        return None


def parse_list_line(line: str, mode: str = "auto") -> Optional[Entry]:
    """Строка ответа LIST (unix или DOS) → :class:`Entry` (None — не разобрать)."""
    line = line.rstrip("\r\n")
    if not line.strip() or line.lower().startswith("total "):
        return None
    m = _UNIX.match(line)
    if m:
        kind = m.group("type")
        name = m.group("name")
        if kind == "l" and " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            return None
        return Entry(wire=name, name=decode_name(name, mode), is_dir=kind == "d",
                     size=None if kind == "d" else int(m.group("size")), mtime=_unix_time(m.group("date")))
    m = _DOS.match(line)
    if m:
        name = m.group("name")
        if name in (".", ".."):
            return None
        is_dir = m.group("size") is not None
        stamp = None
        for fmt in ("%m-%d-%y %I:%M%p", "%m-%d-%Y %I:%M%p", "%m-%d-%y %H:%M", "%m-%d-%Y %H:%M"):
            try:
                stamp = datetime.strptime(f"{m.group('date')} {m.group('time').replace(' ', '').upper()}",
                                          fmt).replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue
        return Entry(wire=name, name=decode_name(name, mode), is_dir=is_dir,
                     size=None if is_dir else int(m.group("bytes")), mtime=stamp)
    return None


def parse_mlsd_time(value: str) -> Optional[float]:
    m = re.match(r"^(\d{14})(?:\.\d+)?$", value or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
#  Соединение
# ---------------------------------------------------------------------------
class _FTPS(ftplib.FTP_TLS):
    """FTPS: явный (AUTH TLS) или неявный TLS, сессия TLS канала данных — общая с управлением."""

    implicit = False
    policy: TlsPolicy = TlsPolicy()
    fingerprint = ""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if not self.implicit:
            return super().connect(host, port, timeout, source_address)
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if self.timeout is not None and not self.timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        raw = socket.create_connection((self.host, self.port), self.timeout, source_address=source_address)
        self.af = raw.family
        self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        self.fingerprint = self.policy.check(self.sock, f"FTP Backup Axigen {self.host}:{self.port}")
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def auth(self):
        resp = super().auth()
        self.fingerprint = self.policy.check(self.sock, f"FTP Backup Axigen {self.host}:{self.port}")
        return resp

    def login(self, user="", passwd="", acct="", secure=True):
        if self.implicit:
            # TLS уже есть — AUTH TLS не нужен
            return ftplib.FTP.login(self, user, passwd, acct)
        return super().login(user, passwd, acct, secure)

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = getattr(self.sock, "session", None)
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
            try:
                # канал данных — тот же сервер, что и канал управления: по нему идут
                # списки папок и письма (сертификат по отпечатку проверяется и тут)
                self.policy.check(conn, f"FTP Backup Axigen {self.host}:{self.port} (канал данных)")
            except BaseException:
                conn.close()
                raise
        return conn, size


class AxigenFTP:
    """Сессия FTP Backup. Использовать как контекстный менеджер."""

    def __init__(self, st: AxigenSettings) -> None:
        self.st = st
        self.mode = st.ftp_name_encoding
        self.ftp: Optional[ftplib.FTP] = None
        self.fingerprint = ""
        self.welcome = ""
        self.features: List[str] = []
        self._mlsd: Optional[bool] = None
        self.commands = 0

    @classmethod
    def open(cls, st: AxigenSettings) -> "AxigenFTP":
        st.require()
        client = cls(st)
        client.connect()
        return client

    def __enter__(self) -> "AxigenFTP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def where(self) -> str:
        return f"FTP Backup Axigen {self.st.host}:{self.st.ftp_port}"

    def connect(self) -> None:
        st = self.st
        timeout = float(st.timeout_s)
        # Через FTP идут и пароль администратора, и сами письма — по сети только с TLS.
        require_encryption(st, st.ftp_tls != "none", "FTP Backup Axigen", setting="Шифрование FTP Backup")
        try:
            if st.ftp_tls == "none":
                ftp = ftplib.FTP(encoding="latin-1", timeout=timeout)
            else:
                ftp = _FTPS(encoding="latin-1", timeout=timeout, context=st.ftp_policy.context())
                ftp.implicit = st.ftp_tls == "implicit"
                ftp.policy = st.ftp_policy
            self.ftp = ftp
            self.welcome = ftp.connect(st.host, int(st.ftp_port), timeout=timeout)
            ftp.login(st.ftp_login, st.ftp_secret)
            if isinstance(ftp, _FTPS):
                ftp.prot_p()
                self.fingerprint = ftp.fingerprint or cert_fingerprint(ftp.sock)
            ftp.set_pasv(True)
            try:
                ftp.voidcmd("TYPE I")
            except ftplib.Error:
                pass
            try:
                feat = ftp.sendcmd("FEAT")
                self.features = [ln.strip().upper() for ln in feat.splitlines()[1:-1]]
            except ftplib.Error:
                self.features = []
        except AxigenCertError:
            self.close()
            raise
        except ssl.SSLCertVerificationError as exc:
            self.close()
            raise AxigenCertError(
                f"{self.where}: сертификат сервера не прошёл проверку ({exc.verify_message or exc}).",
                hint="У сервера во внутренней сети сертификат часто самоподписанный. Нажмите «Проверить "
                     "подключение» в «Настройки → Сервер Axigen», сверьте отпечаток с сервером и нажмите "
                     "«Доверять этому сертификату».", cause=exc) from exc
        except ftplib.error_perm as exc:
            self.close()
            text = str(exc)
            if text[:3] in ("530", "430"):
                raise AxigenAuthError(
                    f"{self.where}: вход не принят ({text}).",
                    hint="Служба FTP Backup пускает администратора Axigen; проверьте логин и пароль "
                         "(отдельные поля для FTP в настройках можно оставить пустыми — тогда берутся данные "
                         "администратора CLI) и что у администратора есть право резервного копирования.") from exc
            if text[:3] in ("534", "504", "500", "502") and st.ftp_tls == "explicit":
                raise AxigenConnectionError(
                    f"{self.where}: сервер не принял AUTH TLS ({text}).",
                    hint="Возможно, на этом порту — SSL-слушатель: выберите шифрование «TLS сразу (implicit)».",
                    cause=exc) from exc
            raise AxigenConnectionError(f"{self.where}: {text}", cause=exc) from exc
        except (socket.timeout, TimeoutError) as exc:
            self.close()
            raise AxigenConnectionError(f"{self.where}: сервер не ответил за {int(timeout)} с.",
                                        hint="Проверьте адрес и порт службы FTP Backup и что сервер архива "
                                             "пускают на этот порт.", cause=exc) from exc
        except ssl.SSLError as exc:
            self.close()
            raise AxigenConnectionError(f"{self.where}: сбой TLS ({exc}).",
                                        hint="Проверьте, какой слушатель на этом порту: обычный (тогда AUTH TLS "
                                             "или без шифрования) или SSL (тогда «TLS сразу»).", cause=exc) from exc
        except (OSError, EOFError, ftplib.Error) as exc:
            self.close()
            raise AxigenConnectionError(
                f"{self.where}: нет соединения ({exc}).",
                hint="Служба FTP Backup по умолчанию слушает только 127.0.0.1. С сервера архива к ней не "
                     "подключиться, пока в Axigen не добавлен слушатель на сетевом адресе (WebAdmin → "
                     "Services → FTP Backup & Restore → Listeners) с разрешением для адреса сервера архива.",
                cause=exc) from exc

    def close(self) -> None:
        ftp, self.ftp = self.ftp, None
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass

    # -- обёртка ошибок --------------------------------------------------------
    def _call(self, what: str, fn, *args, **kw):
        if self.ftp is None:
            raise AxigenConnectionError(f"{self.where}: соединение закрыто.")
        self.commands += 1
        try:
            return fn(*args, **kw)
        except ftplib.error_perm as exc:
            raise FtpPathError(f"{what}: {exc}", reply=str(exc)) from exc
        except ftplib.error_temp as exc:
            raise AxigenConnectionError(f"{self.where}: {what}: временный отказ ({exc}).", cause=exc) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AxigenConnectionError(f"{self.where}: {what}: сервер не ответил за {self.st.timeout_s} с.",
                                        cause=exc) from exc
        except (OSError, EOFError, ftplib.Error, ssl.SSLError) as exc:
            raise AxigenConnectionError(f"{self.where}: {what}: связь оборвалась ({exc}).", cause=exc) from exc

    # -- каталоги ----------------------------------------------------------------
    def listdir(self, path: str) -> List[Entry]:
        """Содержимое каталога ``path`` (путь — в виде для сервера)."""
        if self._mlsd is None and self.features:
            self._mlsd = any(f.startswith("MLST") for f in self.features) or None
        if self._mlsd is not False:
            try:
                entries = self._call(f"MLSD {self.shown(path)}", self._mlsd_list, path)
                self._mlsd = True
                return entries
            except FtpPathError as exc:
                code = exc.reply[:3]
                if code in ("500", "501", "502", "504"):
                    self._mlsd = False            # команды нет — дальше только LIST
                elif self._mlsd is True:
                    raise                         # MLSD работает, значит отказ — по самому пути
                # иначе неясно, нет команды или нет каталога: решит LIST
        lines: List[str] = []
        self._call(f"LIST {self.shown(path)}", self.ftp.retrlines, f"LIST {path}" if path else "LIST", lines.append)
        entries = [e for e in (parse_list_line(ln, self.mode) for ln in lines) if e is not None]
        if lines and not entries:
            # LIST не разобрать — запросим просто имена; каталог это или файл, узнаем по CWD
            names: List[str] = []
            self._call(f"NLST {self.shown(path)}", self.ftp.retrlines, f"NLST {path}" if path else "NLST",
                       names.append)
            entries = []
            for wire in names:
                wire = posixpath.basename(wire.rstrip("/")) or wire
                if wire in (".", ".."):
                    continue
                entries.append(Entry(wire=wire, name=decode_name(wire, self.mode),
                                     is_dir=self.is_dir(join(path, wire))))
        return entries

    def _mlsd_list(self, path: str) -> List[Entry]:
        out = []
        # без OPTS MLST: часть серверов её не знает, а нужные факты и так приходят по умолчанию
        for name, facts in self.ftp.mlsd(path):
            kind = (facts.get("type") or "").lower()
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            size = facts.get("size")
            out.append(Entry(wire=name, name=decode_name(name, self.mode), is_dir=kind == "dir",
                             size=int(size) if size and size.isdigit() else None,
                             mtime=parse_mlsd_time(facts.get("modify", ""))))
        return out

    def is_dir(self, path: str) -> bool:
        if self.ftp is None:
            return False
        try:
            cur = self.ftp.pwd()
            self.ftp.cwd(path)
            self.ftp.cwd(cur)
            return True
        except ftplib.Error:
            return False

    def shown(self, path: str) -> str:
        return "/".join(decode_name(part, self.mode) for part in (path or "/").split("/"))

    # -- файлы -------------------------------------------------------------------
    def retr(self, path: str, limit: int = 0) -> bytes:
        """Скачать файл в память. Больше ``limit`` (или :data:`MAX_FILE_BYTES`) — FileTooLarge."""
        buf = io.BytesIO()
        limit = min(limit, MAX_FILE_BYTES) if limit else MAX_FILE_BYTES

        def take(chunk: bytes) -> None:
            buf.write(chunk)
            if limit and buf.tell() > limit:
                raise FileTooLarge(buf.tell())

        try:
            self._call(f"RETR {self.shown(path)}", self.ftp.retrbinary, f"RETR {path}", take, 256 * 1024)
        except FileTooLarge:
            # Передачу прервали сами. Сколько ответов сервер пришлёт после обрыва,
            # заранее не знать — надёжнее начать сессию заново, чем гадать.
            self.reconnect()
            raise
        return buf.getvalue()

    def stor(self, path: str, data: bytes) -> None:
        """Залить файл (восстановление письма в папку ящика)."""
        self._call(f"STOR {self.shown(path)}", self.ftp.storbinary, f"STOR {path}", io.BytesIO(data), 256 * 1024)

    def mkd(self, path: str) -> None:
        self._call(f"MKD {self.shown(path)}", self.ftp.mkd, path)

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def iter_tar(self, path: str, limit: int = 0) -> Iterator[Tuple[str, Optional[bytes], int, Optional[float]]]:
        """Письма пакета ``messages.tar`` потоком: (имя, байты или None — больше limit, размер, время)."""
        if self.ftp is None:
            raise AxigenConnectionError(f"{self.where}: соединение закрыто.")
        self.commands += 1
        try:
            conn = self.ftp.transfercmd(f"RETR {path}")
        except ftplib.error_perm as exc:
            raise FtpPathError(f"RETR {self.shown(path)}: {exc}", reply=str(exc)) from exc
        except (OSError, EOFError, ftplib.Error, ssl.SSLError) as exc:
            raise AxigenConnectionError(f"{self.where}: RETR {self.shown(path)}: {exc}", cause=exc) from exc
        finished = False
        try:
            stream = conn.makefile("rb")
            try:
                cap = min(limit, MAX_FILE_BYTES) if limit else MAX_FILE_BYTES
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    for member in tar:
                        if not member.isfile():
                            continue
                        name = member.name
                        if member.size > cap:
                            yield name, None, int(member.size), float(member.mtime or 0) or None
                            continue
                        fh = tar.extractfile(member)
                        data = fh.read() if fh is not None else b""
                        yield name, data, int(member.size), float(member.mtime or 0) or None
            except tarfile.TarError as exc:
                raise AxigenError(f"Пакет писем {self.shown(path)} не читается как tar ({exc}).") from exc
            finished = True
        except (OSError, EOFError, ssl.SSLError) as exc:
            raise AxigenConnectionError(f"{self.where}: связь оборвалась при чтении {self.shown(path)} ({exc}).",
                                        cause=exc) from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if finished and self.ftp is not None:
                try:
                    self.ftp.voidresp()
                except (ftplib.Error, OSError, EOFError):
                    finished = False
            if not finished:
                # чтение прервано (ошибка, отмена) — сессию открываем заново
                try:
                    self.reconnect()
                except AxigenError:
                    self.close()

    # -- поиск каталога ящика ----------------------------------------------------
    def find_account_dir(self, email: str, hint: str = "") -> str:
        """Путь к каталогу ящика ``email`` на сервере (в виде для команд).

        ``hint`` — путь, найденный в прошлый раз; берётся, только если он и сейчас
        ведёт в каталог именно этой учётной записи (логин ящика могли сменить).
        """
        from .directory import valid_account_name, valid_domain
        local, domain = split_email(email)
        if not domain:
            raise AxigenError(f"«{email}» — не адрес почты: каталог ящика ищется по домену.")
        if not valid_domain(domain) or not valid_account_name(local):
            raise AxigenError(f"«{email}» — не адрес учётной записи Axigen: каталог ящика по нему не ищется.")
        if not self.st.domain_allowed(domain):
            raise AxigenError(f"Домен {domain} не входит в «Домены» настроек Axigen — ящик через FTP Backup не "
                              f"читается.", hint="Добавьте домен в «Настройки → Сервер Axigen → Домены».")
        template = (self.st.ftp_account_path or "").strip()
        path = ""
        if template:
            try:
                formatted = template.format(domain=domain, local=local, email=email.lower(), name=local)
            except (KeyError, IndexError, ValueError) as exc:
                raise AxigenError(f"В шаблоне «Путь к ящику на FTP» («{template}») непонятная подстановка ({exc}).",
                                  hint="Можно: {domain}, {local}, {email}.") from exc
            parts = [p for p in formatted.strip("/").split("/") if p]
            if any(p in (".", "..") for p in parts):
                raise AxigenError(f"Шаблон «Путь к ящику на FTP» для {email} дал путь с «..» — не используется.")
            path = "/" + "/".join(encode_name(p) for p in parts)
        if hint and self._hint_fits(hint, path, local, email, domain):
            try:
                self.listdir(hint)
                return hint
            except FtpPathError:
                pass
        if template:
            try:
                self.listdir(path)
            except FtpPathError as exc:
                raise AxigenError(
                    f"{self.where}: по шаблону «{template}» каталог ящика {email} не найден ({path}: {exc.reply}).",
                    hint="Проверьте «Путь к ящику на FTP» в настройках Axigen или очистите его — тогда каталог "
                         "ищется сам.") from exc
            return path
        domain_dir = self._find_domain_dir(domain)
        entries = self.listdir(domain_dir)
        found = self._pick(entries, (local, email))
        if found is not None:
            return join(domain_dir, found.wire)
        for box in entries:
            if box.is_dir and box.name.lower() in {c.lower() for c in ACCOUNT_CONTAINERS}:
                inner = self.listdir(join(domain_dir, box.wire))
                found = self._pick(inner, (local, email))
                if found is not None:
                    return join(join(domain_dir, box.wire), found.wire)
        sample = ", ".join(e.name + ("/" if e.is_dir else "") for e in entries[:30]) or "пусто"
        raise AxigenError(
            f"{self.where}: в каталоге домена {self.shown(domain_dir)} нет каталога ящика «{local}». "
            f"Там лежит: {sample}.",
            hint="Если ящик на сервере называется иначе, чем логин в архиве, исправьте логин. Если каталоги "
                 "ящиков лежат глубже — задайте «Путь к ящику на FTP», например domains/{domain}/{local}.")

    def _hint_fits(self, hint: str, template_path: str, local: str, email: str, domain: str) -> bool:
        """Ведёт ли запомненный путь в каталог этой учётной записи этого домена."""
        if template_path:
            return hint == template_path
        parts = [decode_name(p, self.mode).lower() for p in hint.strip("/").split("/") if p]
        return (len(parts) >= 2 and parts[-1] in (local.lower(), email.lower())
                and domain.lower() in parts[:-1] and ".." not in parts)

    def _find_domain_dir(self, domain: str) -> str:
        root = self.listdir("/")
        found = self._pick(root, (domain,))
        if found is not None:
            return join("/", found.wire)
        for top in root:
            if top.is_dir and top.name.lower() == "domains":
                inner = self.listdir(join("/", top.wire))
                found = self._pick(inner, (domain,))
                if found is not None:
                    return join(join("/", top.wire), found.wire)
                sample = ", ".join(e.name for e in inner[:30]) or "пусто"
                raise AxigenError(f"{self.where}: домена {domain} нет в каталоге /{top.name} (там: {sample}).",
                                  hint="Проверьте домен в логине ящика и что администратор FTP видит этот домен.")
        sample = ", ".join(e.name + ("/" if e.is_dir else "") for e in root[:30]) or "пусто"
        raise AxigenError(f"{self.where}: не найден каталог домена {domain} (в корне: {sample}).",
                          hint="Проверьте домен в логине ящика или задайте «Путь к ящику на FTP» в настройках.")

    @staticmethod
    def _pick(entries: Sequence[Entry], names: Sequence[str]) -> Optional[Entry]:
        wanted = [n.lower() for n in names if n]
        for want in wanted:
            for entry in entries:
                if entry.is_dir and entry.name.lower() == want:
                    return entry
        return None


class FtpPathError(AxigenError):
    """Сервер отказал по конкретному пути (нет каталога, нет прав) — не обрыв связи."""

    code = "axigen_ftp_path"

    def __init__(self, message: str, *, reply: str = "", **kw) -> None:
        super().__init__(message, **kw)
        self.reply = reply


class FileTooLarge(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


def join(base: str, name: str) -> str:
    base = base or "/"
    return (base.rstrip("/") + "/" + name) if base != "/" else "/" + name


def walk_folders(client: AxigenFTP, root: str, *, excluded: Callable[[str], bool],
                 on_error: Callable[[str, BaseException], None], max_depth: int = 30
                 ) -> Iterator[Tuple[str, str, List[Entry]]]:
    """Папки ящика: (имя папки «a/b», путь на сервере, файлы папки).

    Вложенная папка — вложенный каталог. Исключённая папка (``excluded``)
    пропускается вместе с вложенными. Папка, которую сервер не дал прочитать,
    передаётся в ``on_error`` и не прерывает обход остальных.
    """
    top = client.listdir(root)
    stack: List[Tuple[str, str, int]] = []
    for entry in sorted((e for e in top if e.is_dir), key=lambda e: e.name.lower(), reverse=True):
        stack.append((entry.name, join(root, entry.wire), 1))
    while stack:
        name, path, depth = stack.pop()
        if excluded(name):
            continue
        try:
            entries = client.listdir(path)
        except FtpPathError as exc:
            on_error(name, exc)
            continue
        files = [e for e in entries if not e.is_dir]
        yield name, path, files
        if depth >= max_depth:
            continue
        subdirs = sorted((e for e in entries if e.is_dir), key=lambda e: e.name.lower(), reverse=True)
        for sub in subdirs:
            if sub.name in (".", "..") or sub.wire in (".", ".."):
                continue
            stack.append((f"{name}/{sub.name}", join(path, sub.wire), depth + 1))


def describe_tls(client: AxigenFTP) -> str:
    if client.st.ftp_tls == "none":
        return "без шифрования"
    fp = pretty_fingerprint(client.fingerprint)
    return ("TLS сразу (implicit)" if client.st.ftp_tls == "implicit" else "AUTH TLS (explicit)") + \
           (f", сертификат {fp}" if fp else "")


__all__ = ["AxigenFTP", "Entry", "FtpPathError", "FileTooLarge", "TAR_NAME", "decode_name", "encode_name",
           "guess_encoding", "join", "parse_list_line", "utf7_decode", "utf7_encode", "walk_folders", "wire_name",
           "normalize_fingerprint"]
