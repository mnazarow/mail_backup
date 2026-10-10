"""
Сборка письма RFC 822 (.eml) из письма, прочитанного из .pst.

В .pst письмо хранится не текстом, а набором свойств MAPI: тема, отправитель,
таблица получателей, тело (текст, HTML, сжатый RTF) и вложения. Здесь из них
собирается обычное письмо MIME:

* заголовки — исходные, если Outlook их сохранил (PidTagTransportMessageHeaders
  есть у полученных писем): так остаются Received, Message-ID и подписи DKIM;
  заголовки о строении тела (Content-…, MIME-Version) отбрасываются, потому что
  тело собирается заново. У отправленных писем и черновиков исходных
  заголовков нет — они составляются из свойств;
* тело — текст и HTML в ``multipart/alternative``; HTML берётся из PidTagHtml
  или достаётся из RTF; картинки, на которые ссылается HTML (``cid:``),
  кладутся рядом в ``multipart/related``;
* вложения — в ``multipart/mixed``; вложенные письма собираются так же и
  прикладываются как ``message/rfc822``.

Сборка детерминирована: одно и то же письмо одного и того же файла даёт те же
байты (границы частей выводятся из номера письма). Поэтому повторный импорт
того же файла в локальную копию узнаёт уже сохранённые письма по SHA-256.

Письмо собирается в памяти (как и везде в службе, где письмо — ``bytes``),
но вложения кодируются кусками, а общий размер письма вместе со всеми письмами,
вложенными в него, ограничен :data:`MAX_EML_BYTES`.

То, чего в письме не оказалось, делится на два вида. *Замечания* (``notes``) —
пропущенное по устройству формата: объект OLE, вложение-ссылка без самого
файла. *Потери* (``lost``) — то, что должно было перенестись, но не прочиталось:
повреждённое вложение, неразобранное тело. Потери делают итог импорта «частичным».
"""
from __future__ import annotations

import binascii
import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from email.utils import format_datetime, getaddresses
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote

import struct

from . import rtf as rtfmod
from .reader import (PT_BINARY, PT_STRING8, PT_UNICODE, Attachment, Message, PstReadError,
                     codepage_codec, filetime_to_epoch)

#: Ошибки чтения повреждённого места файла (кроме «письмо слишком велико»).
READ_ERRORS = (PstReadError, struct.error, IndexError, KeyError, ValueError, OverflowError)

#: Письмо больше этого не собирается (в base64 вложения занимают на треть больше, чем в файле).
MAX_EML_BYTES = 256 * 1024 * 1024
#: Вложенность писем «письмо во вложении письма…».
MAX_DEPTH = 8
#: Сколько вложенных писем (на всех уровнях) собирается в одном письме.
MAX_EMBEDDED = 500
#: Адресный заголовок длиннее этого по адресам не разбирается (кодируется целиком).
MAX_ADDRESS_HEADER = 256 * 1024
#: Текст и HTML письма длиннее этого обрезаются при чтении (значение читается в память).
MAX_BODY_BYTES = 48 * 1024 * 1024
#: Исходные заголовки длиннее этого не разбираются.
MAX_HEADERS_BYTES = 2 * 1024 * 1024

MSGFLAG_READ = 0x01
MSGFLAG_UNSENT = 0x08

#: Классы элементов Outlook, которые письмами не являются (с любым продолжением после точки).
NON_MAIL_CLASSES = ("ipm.appointment", "ipm.contact", "ipm.distlist", "ipm.task", "ipm.stickynote",
                    "ipm.activity", "ipm.configuration", "ipm.microsoft", "ipm.rule", "ipm.ole.class",
                    "ipm.infopathform", "ipm.conflict", "ipm.extendedrule", "ipm.filterdesc",
                    "ipm.aggregated")

_ADDRESS_HEADERS = frozenset(("from", "to", "cc", "bcc", "sender", "reply-to", "resent-from", "resent-to",
                              "resent-cc", "resent-bcc", "resent-sender", "disposition-notification-to",
                              "return-receipt-to", "x-original-to", "delivered-to", "errors-to"))
#: Заголовки, которые описывают строение тела: тело собирается заново, и они стали бы неправдой.
_DROPPED_HEADERS = frozenset(("mime-version", "lines"))
_HEADER_NAME = re.compile(r"^[!-9;-~]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
_SPECIALS = re.compile(r'[()<>\[\]:;@\\,."]')
_MIME_TOKEN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_META_CHARSET = re.compile(rb"""<meta[^>]{0,500}?charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_BAD_FILENAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

#: Кодек Python → имя кодировки в MIME.
_MIME_CHARSETS = {"utf-8": "utf-8", "ascii": "us-ascii", "koi8-r": "koi8-r", "koi8-u": "koi8-u",
                  "cp866": "ibm866", "cp932": "shift_jis", "shift_jis": "shift_jis", "gbk": "gbk",
                  "gb18030": "gb18030", "big5": "big5", "euc_jp": "euc-jp", "euc_kr": "euc-kr",
                  "iso2022_jp": "iso-2022-jp", "iso2022_kr": "iso-2022-kr", "cp949": "ks_c_5601-1987",
                  "cp874": "windows-874", "mac-roman": "macintosh", "mac-cyrillic": "x-mac-cyrillic",
                  "hz": "hz-gb-2312"}


class EmlError(PstReadError):
    """Письмо не удалось собрать (слишком большое, слишком глубокая вложенность…)."""


class BuiltMessage:
    """Собранное письмо и то, что о нём надо знать при заливке."""
    __slots__ = ("raw", "flags", "internaldate", "message_id", "subject", "notes", "lost")

    def __init__(self, raw: bytes, flags: List[str], internaldate: Optional[float], message_id: str,
                 subject: str, notes: List[str], lost: List[str]) -> None:
        self.raw = raw
        self.flags = flags
        self.internaldate = internaldate
        self.message_id = message_id
        self.subject = subject
        self.notes = notes          # пропущено по устройству формата (OLE, вложение-ссылка)
        self.lost = lost            # не прочитано из-за повреждения


def is_mail_class(message_class: str) -> bool:
    """Письмо ли это (а не контакт, встреча, задача, заметка или служебный элемент)."""
    value = (message_class or "").strip().lower()
    return not any(value == name or value.startswith(name + ".") for name in NON_MAIL_CLASSES)


# ---------------------------------------------------------------------------
#  Заголовки
# ---------------------------------------------------------------------------
def _one_line(value) -> str:
    return _SPACES.sub(" ", _CONTROL.sub("", str(value or ""))).strip()


def encode_words(text: str) -> str:
    """Текст заголовка: ASCII — как есть, иначе слова RFC 2047 (UTF-8, base64) не длиннее 75 знаков."""
    if text.isascii():
        return text
    words = []
    chunk = bytearray()
    for char in text:
        data = char.encode("utf-8", "replace")
        if len(chunk) + len(data) > 45:
            words.append("=?utf-8?B?" + binascii.b2a_base64(bytes(chunk), newline=False).decode("ascii") + "?=")
            chunk = bytearray()
        chunk += data
    if chunk:
        words.append("=?utf-8?B?" + binascii.b2a_base64(bytes(chunk), newline=False).decode("ascii") + "?=")
    return " ".join(words)


def _phrase(name: str) -> str:
    """Отображаемое имя в адресном заголовке."""
    name = _one_line(name)
    if not name:
        return ""
    if not name.isascii():
        return encode_words(name)
    if _SPECIALS.search(name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


def _clean_address(address: str) -> str:
    return re.sub(r"[\s<>()\[\],;\"\\]", "", _CONTROL.sub("", str(address or "")))


_ADDRESS = re.compile(r"^[^@:]+@[^@:]+$")


def _smtp_address(address: str) -> str:
    """Адрес SMTP из свойства письма или пустая строка, если это не адрес."""
    address = _clean_address(address)
    return address if _ADDRESS.match(address) else ""


def format_address(name: str, address: str) -> str:
    address = _clean_address(address)
    phrase = _phrase(name if _one_line(name) != address else "")
    if not address:
        return phrase
    return f"{phrase} <{address}>" if phrase else address


def placeholder_address(legacy_dn: str, name: str) -> str:
    """Адрес-заглушка для адресата без адреса SMTP (внутренний адрес Exchange).

    Домен ``.invalid`` зарезервирован (RFC 2606): такой адрес заведомо не
    настоящий, но заголовок остаётся правильным, а имя адресата — на месте.
    """
    tail = ""
    match = re.search(r"/cn=([^/]+)$", legacy_dn or "", re.I)
    if match:
        tail = re.sub(r"[^A-Za-z0-9._-]", "", match.group(1))
    if not tail:
        tail = hashlib.sha1((legacy_dn or name or "").encode("utf-8", "replace")).hexdigest()[:12]
    return f"{tail[:60].lower()}@exchange.invalid"


_FOLD_TOKEN = re.compile(r" +|[^ ]+")
#: Длиннее этого «слово» заголовка режется принудительно (строка письма — не больше 998 знаков).
MAX_HEADER_WORD = 900


def fold(name: str, value: str) -> bytes:
    """Строка заголовка с переносами: не длиннее 78 знаков там, где есть пробел.

    Перенос ставится перед пробелами, а сами пробелы остаются как были (в имени
    файла два пробела подряд не должны стать одним).
    """
    lines: List[str] = []
    current = name + ":"
    gap = " "
    started = False
    for token in _FOLD_TOKEN.findall(value.strip(" ")):
        if token[0] == " ":
            gap = token
            continue
        while token:
            word, token = token[:MAX_HEADER_WORD], token[MAX_HEADER_WORD:]
            if started and len(current) + len(gap) + len(word) > 78:
                lines.append(current)
                current = (gap or " ") + word
            else:
                current += gap + word
            started = True
            gap = " " if token else ""
    lines.append(current)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8", "replace")


def parse_header_block(text: str) -> List[Tuple[str, str]]:
    """Блок заголовков → [(имя, значение со своими переносами)]. Мусор до первого заголовка пропускается."""
    fields: List[Tuple[str, List[str]]] = []
    for line in re.split(r"\r\n|\n|\r", text or ""):
        if not line.strip():
            if fields:
                break                                   # пустая строка — конец заголовков
            continue
        if line[0] in " \t":
            if fields:
                fields[-1][1].append(line)
            continue
        name, sep, value = line.partition(":")
        if not sep or not _HEADER_NAME.match(name.strip()):
            continue                                    # «Microsoft Mail Internet Headers Version 2.0» и т.п.
        fields.append((name.strip(), [value]))
    return [(name, "\r\n".join(parts)) for name, parts in fields]


def _header_bytes(name: str, value: str) -> bytes:
    """Одно поле заголовка в байтах: ASCII с исходными переносами или перекодированное."""
    raw = _CONTROL.sub("", value)
    physical = raw.split("\r\n")
    if raw.isascii() and all(len(line) <= 990 for line in physical):
        first = physical[0]
        if first and first[0] not in " \t":
            first = " " + first
        body = "\r\n".join([first] + [line for line in physical[1:] if line.strip()])
        return (name + ":" + body.rstrip() + "\r\n").encode("ascii", "replace")
    flat = _one_line(raw)
    if name.lower() in _ADDRESS_HEADERS and len(flat) <= MAX_ADDRESS_HEADER:
        try:
            pairs = [(n, a) for n, a in getaddresses([flat]) if n or a]
        except Exception:  # noqa: BLE001 — кривой заголовок не должен ронять письмо
            pairs = []
        if pairs:
            return fold(name, ", ".join(format_address(n, a) for n, a in pairs))
    return fold(name, encode_words(flat))


def _date_value(epoch: float) -> str:
    return format_datetime(datetime.fromtimestamp(epoch, tz=timezone.utc))


# ---------------------------------------------------------------------------
#  Кодирование тел
# ---------------------------------------------------------------------------
def _base64_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Данные кусками → строки base64 по 76 знаков (без перевода строки в конце)."""
    carry = b""
    first = True
    for chunk in chunks:
        data = carry + chunk if carry else chunk
        usable = len(data) - len(data) % 57
        carry = data[usable:]
        if usable:
            encoded = binascii.b2a_base64(data[:usable], newline=False)
            lines = b"\r\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
            yield lines if first else b"\r\n" + lines
            first = False
    if carry:
        tail = binascii.b2a_base64(carry, newline=False)
        yield tail if first else b"\r\n" + tail


def _text_body(data: bytes) -> Tuple[str, bytes]:
    """Текст (UTF-8, переводы строк CRLF) → (Content-Transfer-Encoding, закодированное тело)."""
    plain = len(data) - len(data.translate(None, _ASCII_BYTES))
    if plain * 6 >= len(data) * 5:                      # почти всё — ASCII: читаемый quoted-printable
        encoded = binascii.b2a_qp(data, quotetabs=False, istext=True, header=False)
        encoded = re.sub(rb"\r?\n", b"\r\n", encoded)
        return "quoted-printable", encoded.rstrip(b"\r\n")
    return "base64", b"".join(_base64_lines([data]))


_ASCII_BYTES = bytes(range(128))


def _crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def mime_charset(codec: Optional[str]) -> Optional[str]:
    if not codec:
        return None
    name = codec.lower().replace("_", "-")
    if name in _MIME_CHARSETS:
        return _MIME_CHARSETS[name]
    if codec.lower() in _MIME_CHARSETS:
        return _MIME_CHARSETS[codec.lower()]
    match = re.match(r"^cp(125\d)$", name)
    if match:
        return "windows-" + match.group(1)
    match = re.match(r"^iso8859-(\d+)$", name)
    if match:
        return "iso-8859-" + match.group(1)
    return name


def _param(name: str, value: str) -> str:
    """Параметр заголовка ``name="value"`` для значения из ASCII."""
    return f'{name}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rfc2231(name: str, value: str) -> List[str]:
    """Параметр с не-ASCII значением по RFC 2231, при длинном значении — частями.

    Части режутся по границам знаков: каждая должна раскодироваться сама по себе
    (часть программ переводит их в текст по одной).
    """
    pieces = [quote(char.encode("utf-8", "replace"), safe="") for char in value]
    if sum(map(len, pieces)) <= 60:
        return [f"{name}*=utf-8''{''.join(pieces)}"]
    parts: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > 60:
            parts.append(current)
            current = ""
        current += piece
    if current:
        parts.append(current)
    return [f"{name}*{i}*=" + ("utf-8''" if i == 0 else "") + part for i, part in enumerate(parts)]


def clean_filename(name: str, fallback: str) -> str:
    # сначала отбрасываем путь (имя файла приходит из чужого файла), потом чистим знаки
    name = _CONTROL.sub("", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_FILENAME.sub("_", name).strip(" .")
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and 0 < len(ext) <= 10:
            name = stem[:199 - len(ext)].rstrip(" .") + "." + ext      # расширение сохраняем
        else:
            name = name[:200]
    return name or fallback


def _crlf_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Байты кусками с одиночными «\\n», заменёнными на CRLF (CRLF остаётся как есть)."""
    after_cr = False
    for chunk in chunks:
        if not chunk:
            continue
        head = b""
        if after_cr and chunk[:1] == b"\n":
            head, chunk = b"\n", chunk[1:]
        after_cr = (chunk or head).endswith(b"\r")
        yield head + re.sub(rb"(?<!\r)\n", b"\r\n", chunk)


# ---------------------------------------------------------------------------
#  Дерево MIME
# ---------------------------------------------------------------------------
class _Leaf:
    def __init__(self, headers: List[bytes], body: Callable[[], Iterable[bytes]], *,
                 title: str = "", optional: bool = False, counted: bool = False) -> None:
        self.headers = headers
        self.body = body
        self.title = title          # как назвать часть в сообщении о потере
        self.optional = optional    # вложение: не прочиталось — письмо собирается без него
        self.counted = counted      # тело уже учтено в общем пределе (вложенное письмо)


class _Multi:
    optional = False

    def __init__(self, subtype: str, children: list, boundary: str, extra: str = "") -> None:
        self.subtype = subtype
        self.children = children
        self.boundary = boundary
        self.extra = extra


class _Budget:
    """Предел размера — общий для письма и всех писем, вложенных в него."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise EmlError(f"письмо больше {self.limit // (1024 * 1024)} МБ — не собрано")


class _Sink:
    """Копилка байт письма."""

    def __init__(self, budget: _Budget) -> None:
        self.parts: List[bytes] = []
        self.size = 0
        self.budget = budget

    def __call__(self, data: bytes, counted: bool = False) -> None:
        if not counted:
            self.budget.take(len(data))
        self.size += len(data)
        self.parts.append(data)

    def mark(self) -> Tuple[int, int, int]:
        return len(self.parts), self.size, self.budget.used

    def rollback(self, mark: Tuple[int, int, int]) -> None:
        """Забыть всё, что записано после отметки (часть не дочиталась)."""
        del self.parts[mark[0]:]
        self.size = mark[1]
        self.budget.used = mark[2]

    def value(self) -> bytes:
        data = b"".join(self.parts)
        self.parts = []
        return data


# ---------------------------------------------------------------------------
#  Сборка
# ---------------------------------------------------------------------------
def _row_text(row: Dict[int, object], pid: int) -> str:
    for ptype in (PT_UNICODE, PT_STRING8):
        value = row.get((pid << 16) | ptype)
        if isinstance(value, str) and value:
            return value
    return ""


class _Builder:
    def __init__(self, message: Message, shared: Optional[dict] = None, limit: int = MAX_EML_BYTES) -> None:
        self.msg = message
        self.pc = message.pc
        self.notes: List[str] = []
        self.lost: List[str] = []
        #: общее для письма и всех писем внутри него: предел размера, счётчики границ и вложенных писем
        self.shared = shared if shared is not None else {"budget": _Budget(limit), "boundaries": 0, "embedded": 0}
        self._seed: Optional[str] = None

    # -- мелочи ---------------------------------------------------------------
    def _boundary(self) -> str:
        """Граница частей. Выводится из самого письма (ключ поиска или Message-ID), а не
        из случайных чисел: одно и то же письмо всегда собирается в одни и те же байты —
        и из того же файла, и из другой выгрузки того же ящика. С содержимым частей
        граница не совпадёт: тела кодируются в base64 и quoted-printable, где «=_» не
        бывает, а номер у каждой границы свой, в том числе во вложенных письмах."""
        if self._seed is None:
            key = self.pc.get(0x300B, None)
            if not isinstance(key, (bytes, bytearray)) or not key:
                key = (self.pc.text(0x1035) or f"nid:{self.msg.nid}").encode("utf-8", "replace")
            self._seed = hashlib.sha1(bytes(key)).hexdigest()[:16]
        self.shared["boundaries"] += 1
        return f"=_MailArchiver_{self._seed}_{self.shared['boundaries']}"

    def _address(self, name: str, addrtype: str, email: str, smtp: str) -> Tuple[str, str]:
        name = _one_line(name).strip("'")
        addrtype = (addrtype or "").strip().upper()
        if _smtp_address(smtp):
            return name, _smtp_address(smtp)
        if addrtype != "EX" and _smtp_address(email):
            return name, _smtp_address(email)
        if " " not in name and _smtp_address(name):
            return "", _smtp_address(name)
        if email or name:
            return name, placeholder_address(_clean_address(email), name)
        return "", ""

    def _sender(self) -> Tuple[Tuple[str, str], Tuple[str, str]]:
        pc = self.pc
        representing = self._address(pc.text(0x0042), pc.text(0x0064), pc.text(0x0065), pc.text(0x5D02))
        sender = self._address(pc.text(0x0C1A), pc.text(0x0C1E), pc.text(0x0C1F), pc.text(0x5D01))
        return representing, sender

    def subject(self) -> str:
        subject = self.pc.text(0x0037)
        if subject.startswith("\x01") and len(subject) >= 2:
            subject = subject[2:]                       # служебная отметка длины префикса «RE: »
        return _one_line(subject)

    def flags(self) -> List[str]:
        pc = self.pc
        out: List[str] = []
        message_flags = pc.number(0x0E07, 0)
        if message_flags & MSGFLAG_READ:
            out.append("\\Seen")
        if message_flags & MSGFLAG_UNSENT:
            out.append("\\Draft")
        verb = pc.number(0x1081, 0)
        icon = pc.number(0x1080, 0)
        if verb in (102, 103) or icon == 0x105:
            out.append("\\Answered")
        elif verb == 104 or icon == 0x106:
            out.append("$Forwarded")
        if pc.number(0x1090, 0) == 2:
            out.append("\\Flagged")
        return out

    def internaldate(self) -> Optional[float]:
        for pid in (0x0E06, 0x0039, 0x3007):
            value = self.pc.get(pid, None)
            if isinstance(value, int) and not isinstance(value, bool):
                epoch = filetime_to_epoch(value)
                if epoch:
                    return epoch
        return None

    # -- заголовки ------------------------------------------------------------
    def _recipient_lists(self) -> Dict[int, List[str]]:
        lists: Dict[int, List[str]] = {1: [], 2: [], 3: []}
        try:
            rows = self.msg.recipients()
        except READ_ERRORS as exc:
            self.lost.append(f"получатели не прочитаны ({exc})")
            return lists
        for row in rows:
            kind = row.get(0x0C150003)
            kind = (kind & 0x0F) if isinstance(kind, int) else 1
            if kind not in lists:
                continue
            name, address = self._address(_row_text(row, 0x3001) or _row_text(row, 0x5FF6),
                                          _row_text(row, 0x3002), _row_text(row, 0x3003),
                                          _row_text(row, 0x39FE))
            formatted = format_address(name, address)
            if formatted:
                lists[kind].append(formatted)
        return lists

    def message_id(self, names: Dict[str, str]) -> str:
        existing = _one_line(names.get("message-id") or self.pc.text(0x1035))
        if existing:
            # один идентификатор в угловых скобках, что бы ни было записано в свойстве
            match = re.search(r"<[^<>\s]+>", existing)
            if match:
                return match.group(0)
            bare = re.sub(r"[\s<>]", "", existing)
            if bare:
                return f"<{bare}>"
        # Письму без Message-ID (черновик, отправленное) даём свой, всегда один
        # и тот же: по нему повторная заливка на сервер узнаёт уже залитое.
        key = self.pc.get(0x300B, None)
        if not isinstance(key, (bytes, bytearray)) or not key:
            key = "|".join((str(self.msg.nid), self.subject(), str(self.pc.get(0x0039, "")),
                            self.pc.text(0x0C1F), str(self.pc.number(0x0E08, 0)))).encode("utf-8", "replace")
        return f"<{hashlib.sha1(bytes(key)).hexdigest()}@pst.mailarchiver.invalid>"

    def headers(self) -> Tuple[List[bytes], Dict[str, str]]:
        """Заголовки письма без описания тела. → (строки, {имя в нижнем регистре: значение})."""
        pc = self.pc
        transport = pc.text(0x007D, limit=MAX_HEADERS_BYTES) if 0x007D in pc else ""
        lines: List[bytes] = []
        names: Dict[str, str] = {}
        content_type = ""
        for name, value in parse_header_block(transport):
            low = name.lower()
            flat = _one_line(value)
            if low == "content-type":
                content_type = flat
            # пустое поле («Subject:» без темы) не пишем: иначе оно заслонило бы
            # значение из свойств письма
            if low in _DROPPED_HEADERS or low.startswith("content-") or not flat:
                continue
            names.setdefault(low, flat)
            lines.append(_header_bytes(name, value))
        from_transport = bool(lines)

        def add(name: str, value: str) -> None:
            if value and name.lower() not in names:
                names[name.lower()] = value
                lines.append(fold(name, value))

        representing, sender = self._sender()
        author = representing if representing[1] else sender
        add("From", format_address(*author))
        if not from_transport and sender[1] and representing[1] and sender[1].lower() != representing[1].lower():
            add("Sender", format_address(*sender))
        if not any(key in names for key in ("to", "cc", "bcc")):
            lists = self._recipient_lists()
            add("To", ", ".join(lists[1]))
            add("Cc", ", ".join(lists[2]))
            add("Bcc", ", ".join(lists[3]))
        add("Subject", encode_words(self.subject()))
        submit = pc.get(0x0039, None)
        epoch = filetime_to_epoch(submit) if isinstance(submit, int) and not isinstance(submit, bool) else None
        epoch = epoch or self.internaldate()
        if epoch:
            add("Date", _date_value(epoch))
        message_id = self.message_id(names)
        add("Message-ID", message_id)
        names["message-id"] = message_id
        if not from_transport:
            add("In-Reply-To", _one_line(pc.text(0x1042)))
            add("References", _one_line(pc.text(0x1039)))
            importance = pc.number(0x0017, 1)
            if importance == 2:
                add("Importance", "high")
                add("X-Priority", "1")
            elif importance == 0:
                add("Importance", "low")
                add("X-Priority", "5")
            sensitivity = {1: "Personal", 2: "Private", 3: "Company-Confidential"}.get(pc.number(0x0036, 0))
            if sensitivity:
                add("Sensitivity", sensitivity)
            topic = _one_line(pc.text(0x0070))
            if topic:
                add("Thread-Topic", encode_words(topic))
            index = pc.get(0x0071, None)
            if isinstance(index, (bytes, bytearray)) and 0 < len(index) <= 500:
                add("Thread-Index", binascii.b2a_base64(bytes(index), newline=False).decode("ascii"))
        names["content-type"] = content_type
        return lines, names

    # -- тело -----------------------------------------------------------------
    def _bodies(self) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
        """→ (текст, HTML в байтах, кодировка HTML для MIME)."""
        pc = self.pc
        text: Optional[str] = None
        if 0x1000 in pc:
            text = pc.text(0x1000, limit=MAX_BODY_BYTES)
        html: Optional[bytes] = None
        charset: Optional[str] = None
        kind = pc.type_of(0x1013)
        if kind in (PT_BINARY, PT_STRING8):
            # байты как есть: и двоичное свойство, и строка старого файла ANSI — HTML в той
            # кодировке, в какой письмо пришло; перекодирование только испортило бы его
            value = pc.raw(0x1013, limit=MAX_BODY_BYTES)
            if value.strip(b"\x00"):
                html = value.rstrip(b"\x00")
                charset = self._html_charset(html, pc.get(0x3FDE, None))
        elif kind == PT_UNICODE:
            value = pc.text(0x1013, limit=MAX_BODY_BYTES)
            if value:
                html, charset = self._html_from_text(value), "utf-8"
        if html is None and pc.type_of(0x1009) == PT_BINARY:
            try:
                what, content = rtfmod.body_from_compressed(pc.raw(0x1009, limit=MAX_BODY_BYTES))
            except (rtfmod.RtfError,) + READ_ERRORS as exc:
                self.lost.append(f"тело в RTF не разобрано ({exc})")
            else:
                if what == "html" and content.strip():
                    html, charset = self._html_from_text(content), "utf-8"
                elif what == "text" and content.strip() and not (text or "").strip():
                    text = content
        if text is not None and not text.strip() and html is not None:
            text = None
        return text, html, charset

    @staticmethod
    def _html_from_text(value: str) -> bytes:
        """HTML-строка → байты UTF-8; объявление кодировки внутри HTML исправляется под неё."""
        # объявление кодировки стоит в начале документа — дальше первых килобайт его не ищем
        head = _META_CHARSET_TEXT.sub(r"\g<1>utf-8", value[:65536])
        return _crlf(head + value[65536:]).encode("utf-8", "replace")

    def _html_charset(self, html: bytes, codepage) -> str:
        codec = codepage_codec(codepage)
        if codec and not codec.startswith("utf-16"):
            return mime_charset(codec) or "utf-8"
        match = _META_CHARSET.search(html[:4096])
        if match:
            import codecs
            try:
                return mime_charset(codecs.lookup(match.group(1).decode("ascii", "replace")).name) or "utf-8"
            except LookupError:
                pass
        try:
            html.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return mime_charset(self.msg.codec) or "windows-1252"

    @staticmethod
    def _text_leaf(subtype: str, data: bytes, charset: str) -> _Leaf:
        if subtype == "plain":
            encoding, body = _text_body(data)
        else:
            encoding, body = "base64", b"".join(_base64_lines([data]))
        headers = [fold("Content-Type", f'text/{subtype}; charset="{charset}"'),
                   fold("Content-Transfer-Encoding", encoding)]
        return _Leaf(headers, lambda: (body,))

    # -- вложения -------------------------------------------------------------
    def _attachment_leaf(self, att: Attachment, index: int, inline: bool) -> Optional[_Leaf]:
        name = att.long_filename or att.filename or att.display_name
        if att.is_embedded_message:
            if self.msg.depth + 1 > MAX_DEPTH:
                self.lost.append(f"вложенное письмо глубже {MAX_DEPTH} уровней не перенесено")
                return None
            self.shared["embedded"] += 1
            if self.shared["embedded"] > MAX_EMBEDDED:
                raise EmlError(f"в письме больше {MAX_EMBEDDED} вложенных писем — не собрано")
            inner = _Builder(att.embedded(), self.shared)
            raw = inner.build().raw
            self.notes.extend(inner.notes)
            self.lost.extend(inner.lost)
            title = clean_filename(name or inner.subject(), f"письмо-{index}")
            if not title.lower().endswith(".eml"):
                title = clean_filename(title + ".eml", f"письмо-{index}.eml")
            headers = self._file_headers("message/rfc822", title, False, "")
            if not raw.isascii():
                headers.append(fold("Content-Transfer-Encoding", "8bit"))
            return _Leaf(headers, lambda: (raw,), title=title, counted=True)
        if att.method == 6:
            self.notes.append(f"вложение-объект OLE «{name or index}» не перенесено")
            return None
        if not att.has_data:
            if att.method in (2, 3, 4, 7):
                self.notes.append(f"вложение-ссылка «{name or index}» (файла в письме нет) пропущено")
            else:
                self.notes.append(f"вложение «{name or index}» без данных пропущено")
            return None
        title = clean_filename(name, f"вложение-{index}")
        ctype = att.mime_type.strip().lower()
        if not _MIME_TOKEN.match(ctype) or ctype.startswith(("multipart/", "message/")):
            ctype = mimetypes.guess_type(title)[0] or "application/octet-stream"
        if ctype.startswith(("multipart/", "message/")):
            # файл .eml или .mht, лежащий во вложении байтами, — это файл, а не часть письма:
            # в base64 типы message/* и multipart/* передавать нельзя
            ctype = "application/octet-stream"
        headers = self._file_headers(ctype, title, inline, att.content_id)
        headers.append(fold("Content-Transfer-Encoding", "base64"))
        return _Leaf(headers, lambda: _base64_lines(att.chunks()), title=title, optional=True)

    @staticmethod
    def _file_headers(ctype: str, title: str, inline: bool, content_id: str) -> List[bytes]:
        disposition = "inline" if inline else "attachment"
        if title.isascii():
            headers = [fold("Content-Type", f"{ctype}; {_param('name', title)}"),
                       fold("Content-Disposition", f"{disposition}; {_param('filename', title)}")]
        else:
            # имя в Content-Type — словами RFC 2047 (так его читает Outlook), в
            # Content-Disposition — по RFC 2231 (так положено по стандарту)
            headers = [fold("Content-Type", f'{ctype}; name="{encode_words(title)}"'),
                       fold("Content-Disposition", disposition + "; " + "; ".join(_rfc2231("filename", title)))]
        cid = _clean_address(content_id)
        if cid and cid.isascii():
            headers.append(fold("Content-ID", f"<{cid}>"))
        return headers

    # -- всё вместе -----------------------------------------------------------
    def build(self) -> BuiltMessage:
        if self.msg.depth > MAX_DEPTH:
            raise EmlError(f"вложенность писем больше {MAX_DEPTH}")
        lines, names = self.headers()
        sink = _Sink(self.shared["budget"])
        for line in lines:
            sink(line)
        try:
            attachments = self.msg.attachments()
        except READ_ERRORS as exc:
            self.lost.append(f"вложения не прочитаны ({exc})")
            attachments = []
        self.lost.extend(self.msg.attachment_errors)
        if not self._smime(attachments, names, sink):
            sink(fold("MIME-Version", "1.0"))
            self._emit(self._tree(attachments), sink)
            sink(b"\r\n")
        return BuiltMessage(sink.value(), self.flags(), self.internaldate(), names.get("message-id", ""),
                            self.subject(), self.notes, self.lost)

    def _emit(self, node, sink: _Sink) -> None:
        if isinstance(node, _Multi):
            sink(fold("Content-Type", f'multipart/{node.subtype}; boundary="{node.boundary}"{node.extra}'))
            sink(b"\r\n")
            mark = node.boundary.encode("ascii")
            for child in node.children:
                before = sink.mark()
                sink(b"--" + mark + b"\r\n")
                try:
                    self._emit(child, sink)
                except EmlError:
                    raise
                except READ_ERRORS as exc:
                    if not child.optional:
                        raise
                    # вложение оборвалось на середине: убираем его часть целиком —
                    # письмо остаётся правильным, а о потере сказано в итоге
                    sink.rollback(before)
                    self.lost.append(f"вложение «{child.title}» не прочитано ({exc})")
                    continue
                sink(b"\r\n")
            sink(b"--" + mark + b"--\r\n")
            return
        for header in node.headers:
            sink(header)
        sink(b"\r\n")
        for chunk in node.body():
            sink(chunk, node.counted)

    def _smime(self, attachments: List[Attachment], names: Dict[str, str], sink: _Sink) -> bool:
        """Подписанное или зашифрованное письмо: Outlook хранит его тело целиком одним вложением."""
        if not self.msg.message_class.lower().startswith("ipm.note.smime") or len(attachments) != 1:
            return False
        att = attachments[0]
        ctype = att.mime_type.strip().lower()
        if not att.has_data:
            return False
        if ctype.startswith("multipart/signed"):
            # вложение — готовая часть MIME: свои заголовки (Content-Type с границей) и тело
            chunks = _crlf_chunks(att.chunks())
            head = next(chunks, b"").lstrip()
            block = head.split(b"\r\n\r\n", 1)[0]
            if not re.search(rb"(?im)^content-type[ \t]*:", block):
                return False
            if not re.search(rb"(?im)^mime-version[ \t]*:", block):
                sink(fold("MIME-Version", "1.0"))
            sink(head)
            for chunk in chunks:
                sink(chunk)
            sink(b"\r\n")
            return True
        if ctype in ("application/pkcs7-mime", "application/x-pkcs7-mime"):
            original = names.get("content-type", "")
            if not original.lower().startswith(("application/pkcs7-mime", "application/x-pkcs7-mime")):
                original = f'{ctype}; name="smime.p7m"'
            sink(fold("MIME-Version", "1.0"))
            sink(fold("Content-Type", original))
            sink(fold("Content-Transfer-Encoding", "base64"))
            sink(fold("Content-Disposition", 'attachment; filename="smime.p7m"'))
            sink(b"\r\n")
            for chunk in _base64_lines(att.chunks()):
                sink(chunk)
            sink(b"\r\n")
            return True
        return False

    def _tree(self, attachments: List[Attachment]):
        text, html, charset = self._bodies()
        lowered = html.lower() if html is not None else b""
        inline: List[_Leaf] = []
        files: List[_Leaf] = []
        for index, att in enumerate(attachments, start=1):
            cid = _clean_address(att.content_id)
            # ссылка на картинку — «cid:имя» целиком, а не начало более длинного имени
            is_inline = bool(cid) and bool(lowered) and re.search(
                rb"cid:" + re.escape(cid.lower().encode("utf-8", "replace")) + rb"(?![a-z0-9@._+\-])", lowered) is not None
            try:
                leaf = self._attachment_leaf(att, index, is_inline)
            except EmlError:
                raise
            except READ_ERRORS as exc:
                self.lost.append(f"вложение {index} не прочитано ({exc})")
                continue
            if leaf is not None:
                (inline if is_inline else files).append(leaf)
        parts: List[_Leaf] = []
        if text is not None or html is None:
            parts.append(self._text_leaf("plain", _crlf(text or "").encode("utf-8", "replace"), "utf-8"))
        if html is not None:
            parts.append(self._text_leaf("html", html, charset or "utf-8"))
        body = parts[0] if len(parts) == 1 else _Multi("alternative", parts, self._boundary())
        if inline:
            body = _Multi("related", [body] + inline, self._boundary(),
                          '; type="multipart/alternative"' if isinstance(body, _Multi) else '; type="text/html"')
        if files:
            return _Multi("mixed", [body] + files, self._boundary())
        return body


_META_CHARSET_TEXT = re.compile(r"""(<meta[^>]{0,500}?charset\s*=\s*["']?\s*)[A-Za-z0-9_\-:.]+""", re.I)


def build_eml(message: Message, *, limit: int = MAX_EML_BYTES) -> BuiltMessage:
    """Собрать письмо RFC 822 из письма .pst."""
    return _Builder(message, limit=limit).build()
