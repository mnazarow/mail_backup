"""
Встроенный движок экспорта в .pst (Outlook) — без внешних программ.

Пишет файл формата Unicode PST (Outlook 2003 и новее) модулем
:mod:`pstformat`: папки ящика, письма с темой, отправителем, получателями
(«Кому», «Копия», «Скрытая»), датами, флагами «прочитано/отвечено/помечено»,
текстом и HTML-версией письма, заголовками и ВЛОЖЕНИЯМИ. Картинки, встроенные
в HTML (ссылки ``cid:``), скрыты из списка вложений и показываются в теле
письма; вложенные письма (message/rfc822) прикладываются файлами .eml.

Порядок работы:

1. письма сбрасываются во временные файлы (в памяти — только короткие записи);
2. папки IMAP раскладываются в дерево Outlook: INBOX становится «Входящими»,
   корзина — «Удалёнными», «Отправленные» отмечаются как папка отправленных;
3. письма пишутся папка за папкой: MIME разбирается потоково
   (:mod:`mimestream`), вложения любого размера уходят в файл кусками;
4. крупный ящик делится на части до ~1,9 ГБ — каждая часть открывается в
   Outlook сама по себе и повторяет дерево папок.
"""
from __future__ import annotations

import codecs
import math
import email.utils
import errno
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
from array import array
from html import unescape
from typing import Dict, Iterable, List, Optional, Tuple

from ..errors import PstEngineError
from ..logging_setup import get_logger
from ..util import decode_mime_header, disk_free_bytes, human_size
from . import pstformat as pf
from .base import CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB

log = get_logger("export.native")

#: Предел одной части. Формат Unicode допускает больше, но за ~2 ГБ нужны
#: страницы FPMap, которые этот движок не создаёт.
PART_LIMIT_BYTES = 1_900_000_000
#: Меньше этого делить .pst нет смысла (и так проще не наделать тысячи файлов).
MIN_SPLIT_BYTES = 50 * 1024 * 1024
#: Сколько отдельных ошибок перечислять в результате экспорта.
MAX_ERROR_DETAILS = 200
#: Текст и HTML письма длиннее этого сохраняются вложением-файлом.
BODY_CAP = 16 * 1024 * 1024
#: Заголовки письма (PidTagTransportMessageHeaders) — не больше этого.
HEADERS_CAP = 1024 * 1024
#: Сколько байт письма просматривать в поисках конца блока заголовков.
HEADER_SCAN_LIMIT = 16 * 1024 * 1024
#: Частей письма больше этого не разбираем (защита от «бомб» из тысяч частей).
MAX_PARTS = 5000
#: Строки в таблице содержимого папки обрезаются, как у самого Outlook/Exchange.
TABLE_TEXT = 255

INBOX_NAME = "Входящие"
TRASH_NAME = "Удаленные"
_TRASH_NAMES = ("trash", "deleted items", "deleted messages", "deleted", "корзина",
                "удаленные", "удалённые")
_SENT_NAMES = ("sent", "sent items", "sent messages", "sent mail", "отправленные")

# Флаги IMAP в виде битов (держим число, а не список строк на каждое письмо)
F_SEEN, F_ANSWERED, F_FLAGGED, F_DRAFT, F_FORWARDED = 1, 2, 4, 8, 16

MSGFLAG_READ = 0x01
MSGFLAG_UNSENT = 0x08
MSGFLAG_HASATTACH = 0x10

BASE_WARNING = ("Файл .pst создан встроенным движком (формат Unicode, Outlook 2003 и новее): "
                "папки, письма, получатели, текст и HTML, вложения. Перед массовой выгрузкой "
                "откройте пробный файл в своём Outlook.")
ANSI_NOTE = ("Формат ANSI (Outlook 97–2002) встроенный движок не пишет — создан Unicode PST, "
             "его открывает Outlook 2003 и все более новые. ")


class PstWriteError(PstEngineError):
    """Фатальная ошибка записи .pst: продолжать экспорт бессмысленно."""


def part_path(out_path: str, number: int) -> str:
    """Имя N-й части: ящик.pst → ящик_part2.pst."""
    base, ext = os.path.splitext(out_path)
    return f"{base}_part{number}{ext or '.pst'}"


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
#  Текст: заголовки, строки, HTML
# ---------------------------------------------------------------------------
_SURROGATES = re.compile("[\ud800-\udfff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value) -> str:
    """Строка, пригодная для UTF-16: без «сырых» байтов-суррогатов и NUL."""
    if value is None:
        return ""
    text = str(value)
    if _SURROGATES.search(text):
        try:
            raw = text.encode("utf-8", "surrogateescape")
            from ..mailview import decode_text_bytes
            text = decode_text_bytes(raw, None)
        except UnicodeError:
            pass
        text = _SURROGATES.sub("�", text)
    return text.replace("\x00", "")


def _one_line(value) -> str:
    text = _clean_text(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return _CONTROL.sub("", text).strip()


def _decode_hdr(value) -> str:
    """Заголовок письма в читаемом виде (см. util.decode_mime_header)."""
    return decode_mime_header(value)


class _RawHeaders:
    """Заголовки письма из «сырого» блока: один проход, без разбора политикой email.

    Политика email.policy.default разбирает адресные заголовки в дерево
    объектов: на письме с тремя тысячами получателей это съедало сотни
    мегабайт и секунду времени. Здесь значения только склеиваются из
    свёрнутых строк; адреса разбирает email.utils.getaddresses.
    """

    def __init__(self, blob: bytes) -> None:
        self._values: Dict[str, List[bytes]] = {}
        current: Optional[List[bytes]] = None
        for line in blob.replace(b"\r\n", b"\n").split(b"\n"):
            if line[:1] in (b" ", b"\t"):
                if current:
                    current[-1] = current[-1] + b" " + line.strip()
                continue
            name, sep, value = line.partition(b":")
            if not sep or not name.strip() or b" " in name.strip():
                current = None
                continue
            current = self._values.setdefault(name.strip().lower().decode("latin-1"), [])
            current.append(value.strip())

    def raw(self, name: str, *, joined: bool = False) -> str:
        values = self._values.get(name.lower()) or []
        if not values:
            return ""
        chosen = values if joined else values[:1]
        return ", ".join(v.decode("utf-8", "surrogateescape") for v in chosen)

    def text(self, name: str) -> str:
        """Значение для показа: RFC 2047 и 8-битные байты расшифрованы."""
        value = self.raw(name)[:65536]
        if not value:
            return ""
        try:
            return _decode_hdr(value)
        except Exception:  # noqa: BLE001
            return value


def _getaddresses(value: str):
    try:
        return email.utils.getaddresses([value], strict=False)
    except TypeError:                     # Python без параметра strict
        return email.utils.getaddresses([value])


def _addresses(headers: _RawHeaders, name: str) -> List[Tuple[str, str]]:
    """[(имя, адрес)] из адресного заголовка — устойчиво к кривым спискам."""
    raw = headers.raw(name, joined=True)[:4 * 1024 * 1024]
    if not raw:
        return []
    try:
        pairs = _getaddresses(raw)
    except Exception:  # noqa: BLE001
        pairs = []
    out: List[Tuple[str, str]] = []
    for display, spec in pairs:
        if display:
            try:
                display = _decode_hdr(display)
            except Exception:  # noqa: BLE001
                pass
        display, spec = _one_line(display), _one_line(spec)
        if spec in ("<>", "@"):
            spec = ""
        if display or spec:
            out.append((display, spec))
    return out


_RE_BLOCK_START = re.compile(r"<(script|style|head)\b", re.I)
_RE_BLOCK_END = {name: re.compile(r"</" + name + r"\b[^>]*>", re.I) for name in ("script", "style", "head")}
_RE_BODY_START = re.compile(r"<body\b", re.I)
_RE_BREAK = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/h[1-6]|/li)\b[^>]*>")
_RE_TAG = re.compile(r"<[^>]+>")


def _strip_blocks(html_src: str) -> str:
    """Убрать <script>, <style> и <head> вместе с содержимым — за один проход.

    Регулярное выражение с «ленивым» ``.*?`` на каждом незакрытом теге
    просматривало текст до конца: письмо с тысячами незакрытых <style>
    разбиралось минутами. Здесь закрывающий тег ищется один раз от места
    открывающего. Незакрытые <script>/<style> обрезают хвост (как браузер),
    незакрытый <head> заканчивается на <body>.
    """
    out = []
    pos = 0
    while True:
        found = _RE_BLOCK_START.search(html_src, pos)
        if not found:
            out.append(html_src[pos:])
            break
        out.append(html_src[pos:found.start()])
        name = found.group(1).lower()
        close = _RE_BLOCK_END[name].search(html_src, found.end())
        if close is not None:
            pos = close.end()
            continue
        if name == "head":
            body = _RE_BODY_START.search(html_src, found.end())
            if body is not None:
                pos = body.start()
                continue
        break
    return " ".join(out)


def html_to_text(html_src: str) -> str:
    """Грубо превратить HTML в текст (для текстовой версии письма)."""
    text = _strip_blocks(html_src)
    text = _RE_BREAK.sub("\n", text)
    text = _RE_TAG.sub("", text)
    text = unescape(text)
    text = re.sub(r"[ \t\x0b\f\r]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_META_CHARSET_TEXT = re.compile(r"""(<meta[^>]{0,500}?charset\s*=\s*["']?\s*)[A-Za-z0-9_\-:.]+""", re.I)

# Кодировка → кодовая страница Windows (PidTagInternetCodepage).
_CODEPAGES = {
    "utf-8": 65001, "ascii": 20127, "iso8859-1": 28591, "iso8859-2": 28592, "iso8859-3": 28593,
    "iso8859-4": 28594, "iso8859-5": 28595, "iso8859-6": 28596, "iso8859-7": 28597,
    "iso8859-8": 28598, "iso8859-9": 28599, "iso8859-13": 28603, "iso8859-15": 28605,
    "cp1250": 1250, "cp1251": 1251, "cp1252": 1252, "cp1253": 1253, "cp1254": 1254,
    "cp1255": 1255, "cp1256": 1256, "cp1257": 1257, "cp1258": 1258, "koi8-r": 20866,
    "koi8-u": 21866, "cp866": 866, "mac-cyrillic": 10007, "gb2312": 936, "gbk": 936,
    "gb18030": 54936, "big5": 950, "shift_jis": 932, "cp932": 932, "euc_jp": 51932,
    "iso2022_jp": 50220, "euc_kr": 51949, "cp949": 949, "utf-16": 1200, "utf-16-le": 1200,
    "utf-16-be": 1201, "tis-620": 874, "cp874": 874,
}


def _codepage(charset: Optional[str]) -> Optional[int]:
    if not charset:
        return None
    try:
        name = codecs.lookup(charset.strip().strip("\"'")).name
    except (LookupError, ValueError):
        return None
    return _CODEPAGES.get(name)


def _html_body(raw: bytes, charset: Optional[str]) -> Tuple[bytes, int, str]:
    """(байты HTML, кодовая страница, текст) — исходные байты по возможности сохраняются."""
    from ..mailview import decode_text_bytes
    declared = charset
    if not declared:
        found = _META_CHARSET.search(raw[:8192])
        if found:
            declared = found.group(1).decode("ascii", "ignore")
    cp = _codepage(declared)
    if cp and cp not in (1200, 1201):
        try:
            return raw, cp, raw.decode(codecs.lookup(declared).name, "replace")
        except (LookupError, ValueError):
            pass
    text = decode_text_bytes(raw, declared)
    # Байты перекодированы в UTF-8 — объявление кодировки в самом HTML должно
    # сказать то же, иначе показ письма зависел бы от того, чему верит программа.
    text = _META_CHARSET_TEXT.sub(r"\g<1>utf-8", text)
    return text.encode("utf-8"), 65001, text


_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|vs|tr|rv|ha|отв|ответ|пересл|перес)"
                     r"\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+", re.I)


def _topic(subject: str) -> str:
    """Тема без «Re:», «Fwd:», «Отв:» — для группировки писем в беседы."""
    found = _PREFIX.match(subject[:512])
    return (subject[found.end():] if found else subject).strip()


def _epoch_of(value) -> Optional[float]:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        epoch = dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if not 0 < epoch < 4102444800:          # 1970–2100
        return None
    return epoch


def _flag_bits(flags) -> int:
    if isinstance(flags, int):
        return flags
    names = {str(f).strip().lstrip("\\").lower() for f in (flags or [])}
    bits = 0
    if "seen" in names:
        bits |= F_SEEN
    if "answered" in names:
        bits |= F_ANSWERED
    if "flagged" in names:
        bits |= F_FLAGGED
    if "draft" in names:
        bits |= F_DRAFT
    if "$forwarded" in names or "forwarded" in names:
        bits |= F_FORWARDED
    return bits


_EXT = {"text/plain": ".txt", "text/html": ".html", "text/calendar": ".ics", "text/csv": ".csv",
        "message/rfc822": ".eml", "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "application/pdf": ".pdf", "application/zip": ".zip", "application/ms-tnef": ".dat",
        "text/rfc822-headers": ".txt", "application/pkcs7-signature": ".p7s",
        "application/x-pkcs7-signature": ".p7s", "application/pkcs7-mime": ".p7m"}


def _clean_filename(name) -> str:
    text = _one_line(name)
    text = re.sub(r'[\\/:*?"<>|]+', "_", text).strip(" .")
    return text[:200]


# ---------------------------------------------------------------------------
#  Разбор письма
# ---------------------------------------------------------------------------
class _Att:
    __slots__ = ("name", "ctype", "cid", "value", "size", "inline")

    def __init__(self, name: str, ctype: str, cid: str, value, size: int, inline: bool) -> None:
        self.name = name
        self.ctype = ctype
        self.cid = cid
        self.value = value
        self.size = size
        self.inline = inline


class _Sink:
    """Приёмник тела одной части письма: текст/HTML в память, вложение — в файл."""

    def __init__(self, leaf, role: str, pst: pf.PstFile) -> None:
        from .. import mimestream
        self.leaf = leaf
        self.role = role
        self.pst = pst
        self.dec = mimestream.decoder_for(leaf.cte)
        self.buf: Optional[bytearray] = bytearray() if role != "att" else None
        self.stream = pst.stream() if role == "att" else None
        self.head = bytearray()          # начало вложенного письма — для его темы
        self.moved = False               # текст оказался слишком большим — стал вложением

    def _put(self, data: bytes) -> None:
        if self.buf is not None:
            self.buf += data
            if len(self.buf) > BODY_CAP:
                self.stream = self.pst.stream()
                self.stream.write(bytes(self.buf))
                self.buf = None
                self.moved = True
            return
        if self.leaf.ctype == "message/rfc822" and len(self.head) < 65536:
            self.head += data[:65536 - len(self.head)]
        self.stream.write(data)

    def feed(self, chunk: bytes) -> None:
        out = self.dec.feed(chunk)
        if out:
            self._put(out)

    def close(self) -> None:
        tail = self.dec.close()
        if tail:
            self._put(tail)


class _Parsed:
    def __init__(self) -> None:
        self.headers = None
        self.text: Optional[bytes] = None
        self.text_charset: Optional[str] = None
        self.html: Optional[bytes] = None
        self.html_charset: Optional[str] = None
        self.attachments: List[_Att] = []
        self.skipped_parts = 0


def _attachment_from(sink: _Sink, index: int) -> _Att:
    leaf = sink.leaf
    value = sink.stream.finish()
    size = sink.stream.size
    ctype = leaf.ctype or "application/octet-stream"
    name = _clean_filename(leaf.filename or "")
    if not name and ctype == "message/rfc822" and sink.head:
        try:
            subject = _clean_filename(_RawHeaders(_raw_header_block(bytes(sink.head))).text("Subject"))[:80]
        except Exception:  # noqa: BLE001
            subject = ""
        if subject:
            name = subject + ".eml"
    if not name and sink.moved:
        name = "message.html" if ctype == "text/html" else "message.txt"
    if not name:
        ext = _EXT.get(ctype) or mimetypes.guess_extension(ctype) or ""
        name = (f"forwarded_{index}" if ctype == "message/rfc822" else f"attachment_{index}") + ext
    elif ctype == "message/rfc822" and not name.lower().endswith(".eml"):
        name += ".eml"
    cid = ""
    try:
        raw_cid = leaf.headers.get("Content-ID")
        if raw_cid:
            cid = _one_line(str(raw_cid)).strip().strip("<>").strip()
    except Exception:  # noqa: BLE001
        cid = ""
    return _Att(name, ctype, cid, value, size, leaf.disp == "inline")


def parse_message(fh, pst: pf.PstFile) -> _Parsed:
    """Один проход по письму: заголовки, текст, HTML; вложения — сразу в .pst."""
    from .. import mimestream
    from ..mailview import _is_attachment
    parsed = _Parsed()
    events = mimestream.scan(fh)
    sink: Optional[_Sink] = None
    parts = 0
    text_busy = html_busy = False
    try:
        for kind, leaf, payload in events:
            if kind == "top":
                parsed.headers = payload
                continue
            if kind == "leaf":
                parts += 1
                if parts > MAX_PARTS:
                    parsed.skipped_parts += 1
                    continue
                is_att = _is_attachment(leaf.ctype, leaf.disp, leaf.filename)
                ctype = leaf.ctype
                if ctype.startswith("multipart/"):
                    ctype = "text/plain"          # multipart без boundary — модуль email читает как текст
                if not is_att and ctype == "text/plain" and not text_busy:
                    role, text_busy = "text", True
                elif not is_att and ctype == "text/html" and not html_busy:
                    role, html_busy = "html", True
                else:
                    role = "att"
                sink = _Sink(leaf, role, pst)
                leaf.want = True
                continue
            if sink is None or leaf is not sink.leaf:
                continue
            if kind == "data":
                sink.feed(payload)
                continue
            if kind == "end":
                sink.close()
                if sink.buf is not None:
                    body = bytes(sink.buf)
                    if sink.role == "text":
                        if body.strip():
                            parsed.text, parsed.text_charset = body, leaf.charset
                        else:
                            text_busy = False          # пустая часть: текст возьмём из следующей
                    else:
                        if body.strip():
                            parsed.html, parsed.html_charset = body, leaf.charset
                        else:
                            html_busy = False
                else:
                    parsed.attachments.append(_attachment_from(sink, len(parsed.attachments) + 1))
                sink = None
    finally:
        events.close()
    return parsed


def _raw_header_block(head: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        cut = head.find(sep)
        if cut >= 0:
            return head[:cut]
    return head


def _importance(headers: _RawHeaders) -> Tuple[int, int]:
    imp = headers.text("Importance").strip().lower()
    xp = headers.text("X-Priority").strip()
    ms = headers.text("X-MSMail-Priority").strip().lower()
    prio = headers.text("Priority").strip().lower()
    if imp == "high" or xp[:1] in ("1", "2") or ms == "high" or prio == "urgent":
        return 2, 1
    if imp == "low" or xp[:1] in ("4", "5") or ms == "low" or prio == "non-urgent":
        return 0, -1
    return 1, 0


def _sensitivity(headers: _RawHeaders) -> int:
    value = headers.text("Sensitivity").strip().lower()
    return {"personal": 1, "private": 2, "company-confidential": 3}.get(value, 0)


def _address_props(prefix: str, name: str, address: str) -> List[Tuple[int, int, object]]:
    """Свойства отправителя (SenderXxx или SentRepresentingXxx)."""
    ids = {"sender": (0x0C1A, 0x0C1F, 0x0C1E, 0x0C19, 0x0C1D),
           "repr": (0x0042, 0x0065, 0x0064, 0x0041, 0x003B)}[prefix]
    out: List[Tuple[int, int, object]] = [(ids[0], pf.PT_UNICODE, name or address)]
    if address:
        out += [(ids[1], pf.PT_UNICODE, address), (ids[2], pf.PT_UNICODE, "SMTP"),
                (ids[3], pf.PT_BINARY, pf.one_off_entry_id(name, address)),
                (ids[4], pf.PT_BINARY, pf.smtp_search_key(address))]
    return out


def _table_text(value: str) -> str:
    """Строка для таблицы содержимого: не длиннее 255 знаков UTF-16 (как у MAPI)."""
    text = value[:TABLE_TEXT]
    units = len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)
    while units > TABLE_TEXT:
        units -= 2 if ord(text[-1]) > 0xFFFF else 1
        text = text[:-1]
    return text


class _Row:
    """Строка таблицы содержимого папки — компактно (кортеж вместо словаря)."""
    __slots__ = ("nid", "importance", "sensitivity", "subject", "submit", "sender", "topic",
                 "cc", "to", "delivery", "flags", "size")

    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)

    def cells(self) -> Dict[int, object]:
        return {0x00170003: self.importance, 0x001A001F: "IPM.Note", 0x00360003: self.sensitivity,
                0x0037001F: self.subject or None, 0x00390040: self.submit or None,
                0x0042001F: self.sender or None, 0x0070001F: self.topic or None,
                0x0E03001F: self.cc or None, 0x0E04001F: self.to or None,
                0x0E060040: self.delivery or None, 0x0E070003: self.flags,
                0x0E080003: self.size, 0x0E170003: 0}


def write_message(pst: pf.PstFile, fh, folder_nid: int, *, flags: int,
                  internaldate: Optional[float], size: int,
                  now_ft: int = 0) -> Tuple[_Row, List[str]]:
    """Разобрать письмо из файла и записать его в .pst. → (строка таблицы, замечания)."""
    notes: List[str] = []
    head = fh.read(HEADERS_CAP)
    # Блок заголовков бывает огромным (тысячи Received, гигантский X-…): тема
    # и адреса могут стоять после первого мегабайта — дочитываем до пустой строки.
    while len(head) < HEADER_SCAN_LIMIT and b"\r\n\r\n" not in head and b"\n\n" not in head:
        more = fh.read(HEADERS_CAP)
        if not more:
            break
        head += more
    fh.seek(0)
    parsed = parse_message(fh, pst)
    blob = _raw_header_block(head)
    headers = _RawHeaders(blob)

    subject = _one_line(headers.text("Subject")).lstrip("\x01")
    topic = _topic(subject)
    senders = _addresses(headers, "From") or _addresses(headers, "Sender")
    sender_name, sender_addr = senders[0] if senders else ("", "")
    groups = [(1, _addresses(headers, "To")), (2, _addresses(headers, "Cc")),
              (3, _addresses(headers, "Bcc"))]
    submit_epoch = _epoch_of(headers.raw("Date"))
    delivery_epoch = internaldate or submit_epoch
    submit_ft = pf.to_filetime(submit_epoch or delivery_epoch) if (submit_epoch or delivery_epoch) else 0
    delivery_ft = pf.to_filetime(delivery_epoch) if delivery_epoch else 0
    importance, priority = _importance(headers)
    sensitivity = _sensitivity(headers)

    from ..mailview import decode_text_bytes
    text = decode_text_bytes(parsed.text, parsed.text_charset) if parsed.text is not None else None
    html_bytes = html_cp = None
    html_text = ""
    if parsed.html is not None:
        html_bytes, html_cp, html_text = _html_body(parsed.html, parsed.html_charset)
    if text is None and html_text:
        text = html_to_text(html_text)
    text = _clean_text(text) if text else ""

    # Картинки, на которые ссылается HTML (cid:), прячем из списка вложений.
    lowered = html_text.lower() if html_text else ""
    attachments = []
    visible = 0
    for att in parsed.attachments:
        hidden = bool(att.cid) and lowered and ("cid:" + att.cid.lower()) in lowered
        if not hidden:
            visible += 1
        ext = os.path.splitext(att.name)[1]
        props = [(0x3705, pf.PT_I4, 1), (0x3701, pf.PT_BINARY, att.value),
                 (0x0E20, pf.PT_I4, min(att.size, 0x7FFFFFFF)),
                 (0x3704, pf.PT_UNICODE, att.name), (0x3707, pf.PT_UNICODE, att.name),
                 (0x3001, pf.PT_UNICODE, att.name), (0x3703, pf.PT_UNICODE, ext or None),
                 (0x370E, pf.PT_UNICODE, att.ctype), (0x370B, pf.PT_I4, -1),
                 (0x3712, pf.PT_UNICODE, att.cid or None),
                 (0x3714, pf.PT_I4, 4 if hidden else None),
                 (0x7FFE, pf.PT_BOOL, bool(hidden)), (0x7FFA, pf.PT_I4, 0),
                 (0x7FFD, pf.PT_I4, 0), (0x7FFF, pf.PT_BOOL, False)]
        cells = {0x0E200003: min(att.size, 0x7FFFFFFF), 0x3704001F: att.name, 0x37050003: 1,
                 0x370B0003: -1}
        attachments.append((props, cells))
    if parsed.skipped_parts:
        notes.append(f"в письме больше {MAX_PARTS} частей, лишние не выгружены ({parsed.skipped_parts})")

    recipients = []
    displays = {1: [], 2: [], 3: []}
    for rtype, pairs in groups:
        for name, addr in pairs:
            if not addr:
                continue
            displays[rtype].append(name or addr)
            recipients.append({0x0C150003: rtype, 0x3001001F: name or addr, 0x3002001F: "SMTP",
                               0x3003001F: addr, 0x0FFF0102: pf.one_off_entry_id(name, addr),
                               0x300B0102: pf.smtp_search_key(addr), 0x0FFE0003: 6,
                               0x39000003: 0})
    display_to = "; ".join(displays[1])
    display_cc = "; ".join(displays[2])
    display_bcc = "; ".join(displays[3])

    msg_flags = 0
    if flags & F_SEEN:
        msg_flags |= MSGFLAG_READ
    if flags & F_DRAFT:
        msg_flags |= MSGFLAG_UNSENT
    if visible:
        msg_flags |= MSGFLAG_HASATTACH

    headers_text = ""
    if blob:
        headers_text = _clean_text(decode_text_bytes(blob[:HEADERS_CAP], None))
        if not headers_text.endswith("\n"):
            headers_text += "\r\n"

    props: List[Tuple[int, int, object]] = [
        (0x001A, pf.PT_UNICODE, "IPM.Note"),
        (0x0037, pf.PT_UNICODE, subject),
        (0x0070, pf.PT_UNICODE, topic),
        (0x0039, pf.PT_SYSTIME, submit_ft or None),
        (0x0E06, pf.PT_SYSTIME, delivery_ft or None),
        # время создания и изменения обязательны — без дат письма берём момент выгрузки
        (0x3007, pf.PT_SYSTIME, delivery_ft or now_ft or pf.to_filetime(time.time())),
        (0x3008, pf.PT_SYSTIME, delivery_ft or now_ft or pf.to_filetime(time.time())),
        (0x0E07, pf.PT_I4, msg_flags),
        (0x0E08, pf.PT_I4, min(size, 0x7FFFFFFF)),
        (0x0E17, pf.PT_I4, 0),
        (0x0017, pf.PT_I4, importance),
        (0x0026, pf.PT_I4, priority),
        (0x0036, pf.PT_I4, sensitivity),
        (0x0E04, pf.PT_UNICODE, display_to),
        (0x0E03, pf.PT_UNICODE, display_cc),
        (0x0E02, pf.PT_UNICODE, display_bcc),
        (0x0E1B, pf.PT_BOOL, bool(visible)),
        (0x300B, pf.PT_BINARY, uuid.uuid4().bytes),
        (0x007D, pf.PT_UNICODE, headers_text or None),
        (0x1035, pf.PT_UNICODE, _one_line(headers.text("Message-ID")) or None),
        (0x1042, pf.PT_UNICODE, _one_line(headers.text("In-Reply-To")) or None),
        (0x1039, pf.PT_UNICODE, _one_line(headers.text("References")) or None),
        (0x1000, pf.PT_UNICODE, text or None),
    ]
    if html_bytes is not None:
        props += [(0x1013, pf.PT_BINARY, html_bytes), (0x3FDE, pf.PT_I4, html_cp)]
    if sender_name or sender_addr:
        props += _address_props("sender", sender_name, sender_addr)
        props += _address_props("repr", sender_name, sender_addr)
    if flags & F_FORWARDED:
        props += [(0x1080, pf.PT_I4, 0x106), (0x1081, pf.PT_I4, 104)]
    elif flags & F_ANSWERED:
        props += [(0x1080, pf.PT_I4, 0x105), (0x1081, pf.PT_I4, 102)]
    if flags & F_FLAGGED:
        props += [(0x1090, pf.PT_I4, 2), (0x1095, pf.PT_I4, 6)]

    nid = pst.write_message(folder_nid, props, recipients, attachments)
    row = _Row(nid=nid, importance=importance, sensitivity=sensitivity,
               subject=_table_text(subject), submit=submit_ft, sender=_table_text(sender_name or sender_addr),
               topic=_table_text(topic), cc=_table_text(display_cc), to=_table_text(display_to),
               delivery=delivery_ft, flags=msg_flags, size=min(size, 0x7FFFFFFF))
    return row, notes


# ---------------------------------------------------------------------------
#  Дерево папок
# ---------------------------------------------------------------------------
class _Folder:
    __slots__ = ("key", "name", "parent", "role", "children")

    def __init__(self, key: Tuple[str, ...], name: str, parent, role: str) -> None:
        self.key = key
        self.name = name
        self.parent = parent            # ключ родителя (None — верх дерева)
        self.role = role                # inbox | trash | sent | ""
        self.children: List[Tuple[str, ...]] = []


def _split_folder(name: str, delimiter: str = "") -> Tuple[str, ...]:
    """Имя папки IMAP → уровни дерева. Разделитель сервера неизвестен — «/» и «\\»."""
    pieces = (name or "").split(delimiter) if delimiter else re.split(r"[/\\]", name or "")
    parts = tuple(p for p in (_one_line(x) for x in pieces) if p)
    return parts or ("INBOX",)


def plan_folders(names) -> Tuple[List[_Folder], Dict[str, Tuple[str, ...]]]:
    """Разложить папки IMAP в дерево .pst → (папки в порядке записи, имя IMAP → ключ).

    names — имена папок или словарь «имя → разделитель уровней на сервере».
    """
    delims = dict(names) if isinstance(names, dict) else {name: "" for name in names}
    keys = {name: _split_folder(name, delim) for name, delim in delims.items()}
    # Courier, Cyrus и часть Dovecot держат все папки внутри «INBOX.» (личное
    # пространство имён): почтовые программы показывают их рядом со «Входящими»,
    # так же раскладываем и в .pst.
    nested = [k for k in keys.values() if len(k) > 1]
    if nested and all(k[0].lower() == "inbox" for k in keys.values()) \
            and any(delims.get(n) == "." for n, k in keys.items() if len(k) > 1):
        keys = {n: (k[1:] if len(k) > 1 else k) for n, k in keys.items()}
    tops = {key[0].lower(): key[0] for key in keys.values()}
    trash = next((tops[n] for n in _TRASH_NAMES if n in tops), None)
    sent = next((tops[n] for n in _SENT_NAMES if n in tops), None)

    def norm(key: Tuple[str, ...]) -> Tuple[str, ...]:
        head = key[0]
        if head.lower() == "inbox":
            return ("\x00inbox",) + key[1:]
        if trash is not None and head == trash:
            return ("\x00trash",) + key[1:]
        return key

    mapping = {name: norm(key) for name, key in keys.items()}
    folders: Dict[Tuple[str, ...], _Folder] = {}
    for key in list(mapping.values()) + [("\x00inbox",)]:
        for depth in range(1, len(key) + 1):
            sub = key[:depth]
            if sub in folders:
                continue
            if sub == ("\x00inbox",):
                folder = _Folder(sub, INBOX_NAME, None, "inbox")
            elif sub == ("\x00trash",):
                folder = _Folder(sub, TRASH_NAME, None, "trash")
            else:
                role = "sent" if depth == 1 and sent is not None and sub[0] == sent else ""
                folder = _Folder(sub, sub[-1][:255] or "(без имени)", sub[:-1] or None, role)
            folders[sub] = folder
            if folder.parent is not None:
                folders[folder.parent].children.append(sub)

    # Имена соседей не должны совпадать (Outlook так не умеет).
    def sort_key(k):
        return (0 if k[0] == "\x00inbox" and len(k) == 1 else 1, folders[k].name.lower())

    top_keys = sorted((k for k in folders if len(k) == 1 and k[0] != "\x00trash"), key=sort_key)
    if ("\x00trash",) in folders:
        top_keys.append(("\x00trash",))
    reserved = {INBOX_NAME.lower(), TRASH_NAME.lower()}

    def dedupe(siblings: List[Tuple[str, ...]], taken: set) -> None:
        for k in siblings:
            folder = folders[k]
            if folder.role in ("inbox", "trash"):
                continue
            base = folder.name
            name, n = base, 1
            while name.lower() in taken:
                n += 1
                name = f"{base} ({n})"
            folder.name = name
            taken.add(name.lower())

    dedupe(top_keys, set(reserved))
    ordered: List[_Folder] = []

    def visit(k: Tuple[str, ...]) -> None:
        folder = folders[k]
        ordered.append(folder)
        kids = sorted(folder.children, key=lambda c: folders[c].name.lower())
        dedupe(kids, set())
        folder.children = kids
        for child in kids:
            visit(child)

    for k in top_keys:
        visit(k)
    return ordered, mapping


# ---------------------------------------------------------------------------
#  Движок
# ---------------------------------------------------------------------------
class _Spooled:
    """Лёгкая запись об одном письме; тело — во временном файле."""

    __slots__ = ("folder", "path", "size", "flags", "internaldate")

    def __init__(self, folder: str, path: str, size: int, flags: int,
                 internaldate: Optional[float]) -> None:
        self.folder = folder
        self.path = path
        self.size = size
        self.flags = flags
        self.internaldate = internaldate


class _SpoolIndex:
    """Сведения о сброшенных на диск письмах — в массивах, ~20 байт на письмо.

    На ящике в сотни тысяч писем объект на письмо со строкой пути занимал бы
    десятки мегабайт; здесь путь вычисляется по номеру письма.
    """

    def __init__(self, spool_dir: str) -> None:
        self.dir = spool_dir
        self.folders: List[str] = []
        self._folder_ids: Dict[str, int] = {}
        self.folder = array("I")
        self.size = array("Q")
        self.flags = array("B")
        self.date = array("d")

    def __len__(self) -> int:
        return len(self.size)

    def path(self, index: int) -> str:
        return os.path.join(self.dir, f"{index:08d}.eml")

    def add(self, folder: str, size: int, flags: int, internaldate: Optional[float]) -> None:
        fid = self._folder_ids.get(folder)
        if fid is None:
            fid = self._folder_ids[folder] = len(self.folders)
            self.folders.append(folder)
        self.folder.append(fid)
        self.size.append(max(0, int(size)))
        self.flags.append(flags & 0xFF)
        self.date.append(float(internaldate) if internaldate else math.nan)

    def get(self, index: int) -> _Spooled:
        date = self.date[index]
        return _Spooled(self.folders[self.folder[index]], self.path(index), self.size[index],
                        self.flags[index], None if math.isnan(date) else date)


#: Строк в таблице содержимого одной папки одной части не больше этого:
#: куча таблицы ограничена 65 535 блоками (формат), здесь — с большим запасом.
MAX_TABLE_ROWS = 500_000


class NativePstExportEngine(ExportEngine):
    name = "native"
    fmt = "pst"
    experimental = True

    @classmethod
    def available(cls):
        return True, ""

    @staticmethod
    def _message_flags(flags) -> int:
        """Флаги IMAP → PidTagMessageFlags (прочитано, черновик)."""
        bits = _flag_bits(flags)
        return (MSGFLAG_READ if bits & F_SEEN else 0) | (MSGFLAG_UNSENT if bits & F_DRAFT else 0)

    @staticmethod
    def _part_limit(options: dict) -> Tuple[int, int]:
        """(где кончаются данные части, заявленный размер части) в байтах."""
        limit = PART_LIMIT_BYTES
        try:
            wanted = int(options.get("pst_split_size_mb") or 0) * 1024 * 1024
        except (TypeError, ValueError):
            wanted = 0
        if wanted > 0:
            limit = min(limit, max(wanted, MIN_SPLIT_BYTES))
        # Файл растёт целыми областями карты размещения (248 КБ): последняя
        # запись должна оставлять место на ещё одну область под индексы.
        regions = max(2, (limit - pf.AMAP_FIRST) // pf.AMAP_SPAN - 1)
        return pf.AMAP_FIRST + regions * pf.AMAP_SPAN, limit

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        options = options or {}
        result = ExportResult(path=out_path, is_dir=False, engine=self.name, fmt="pst",
                              warning=BASE_WARNING)
        wanted_format = str(options.get("pst_format") or "").strip().lower()
        if wanted_format == "ansi":
            result.warning = ANSI_NOTE + result.warning
            log.info("Запрошен формат PST ANSI — встроенный движок пишет Unicode.")
        display = _one_line(options.get("pst_display_name") or "") or \
            os.path.splitext(os.path.basename(out_path))[0]
        allowed_end, nominal = self._part_limit(options)
        spool_dir = self._make_spool_dir(options)
        pst: Optional[pf.PstFile] = None
        done_ok = False
        try:
            spooled, delims, cancelled = self._spool(items, spool_dir, result, cancel_cb,
                                                     progress_cb, total_hint)
            if cancelled:
                return self._cancelled_result(result, out_path, progress_cb, total_hint)

            plan, mapping = plan_folders(delims)
            by_folder: Dict[Tuple[str, ...], array] = {}
            for index in range(len(spooled)):
                key = mapping[spooled.folders[spooled.folder[index]]]
                by_folder.setdefault(key, array("I")).append(index)
            order = [f for f in plan if f.key in by_folder]
            now_ft = pf.to_filetime(time.time())

            fi = mi = 0
            part_no = 0
            total_written = 0
            while True:
                while fi < len(order) and mi >= len(by_folder[order[fi].key]):
                    fi, mi = fi + 1, 0
                if part_no and fi >= len(order):
                    break                        # предыдущая часть взяла последние письма
                part_no += 1
                path = out_path if part_no == 1 else part_path(out_path, part_no)
                name = display if part_no == 1 else f"{display} (часть {part_no})"
                try:
                    # Предел для данных — с запасом в одну область: оценка размера
                    # письма бывает неточной, а часть не должна обрываться на полуслове.
                    pst = pf.PstFile(path, name, max_bytes=allowed_end + pf.AMAP_SPAN)
                except OSError as exc:
                    raise PstWriteError(f"Не удалось создать файл {path}: {exc}") from exc
                result.parts.append(path)
                nids = self._create_folders(pst, plan)
                part_msgs = 0
                part_full = cancelled = False
                while fi < len(order):
                    folder = order[fi]
                    folder_nid = nids[folder.key]
                    indexes = by_folder[folder.key]
                    table: Optional[pf.ContentsTable] = None
                    while mi < len(indexes):
                        if cancel_cb and cancel_cb():
                            cancelled = True
                            break
                        rec = spooled.get(indexes[mi])
                        # Письмо в .pst занимает не больше двух своих размеров
                        # (текст в UTF-16), плюс строка таблицы папки и индексы.
                        estimate = 2 * rec.size + 64 * 1024
                        if part_msgs and (pst.position + estimate + pst.overhead_estimate() > allowed_end
                                          or (table is not None and (len(table) >= MAX_TABLE_ROWS
                                                                     or table.nearly_full()))):
                            part_full = True
                            break
                        oversize = False
                        outcome = self._write_one(pst, rec, folder_nid, result, now_ft)
                        if outcome == "full":
                            if part_msgs:
                                part_full = True
                                break
                            # Письмо крупнее заявленного размера части: пишем его в
                            # эту (пустую) часть сверх него — лучше часть побольше,
                            # чем потерянное письмо. Следующие письма — в новую часть.
                            soft_limit = pst.ndb.max_bytes
                            pst.ndb.max_bytes = pf.HARD_LIMIT_BYTES - 64 * 1024 * 1024
                            try:
                                outcome = self._write_one(pst, rec, folder_nid, result, now_ft)
                            finally:
                                pst.ndb.max_bytes = soft_limit
                            oversize = True
                            if outcome == "full":
                                self._error(result, f"{rec.folder}: письмо ({human_size(rec.size)}) больше "
                                                    f"предела файла .pst — пропущено")
                                mi += 1
                                continue
                        mi += 1
                        if outcome is None:
                            continue
                        row, notes = outcome
                        if table is None:
                            table = pst.open_contents(folder_nid)
                        table.add(row.nid, row.cells(), not row.flags & MSGFLAG_READ)
                        part_msgs += 1
                        result.count += 1
                        for note in notes:
                            self._error(result, f"{rec.folder}: {note}", count=False)
                        if progress_cb and result.count % 20 == 0:
                            progress_cb(result.count, total_hint,
                                        f"PST: {result.count}" + (f", часть {part_no}" if part_no > 1 else ""))
                        if oversize:
                            part_full = True
                            break
                    if table is not None:
                        table.close()
                    if cancelled or part_full:
                        break
                    fi += 1
                    mi = 0
                if cancelled:
                    pst.abort()
                    pst = None
                    for extra in result.parts:
                        _unlink_quiet(extra)
                    result.parts = []
                    return self._cancelled_result(result, out_path, progress_cb, total_hint)
                try:
                    total_written += pst.close()
                except OSError as exc:
                    raise self._disk_error(exc) from exc
                pst = None
                if not part_full:
                    break
                if part_no == 1:
                    log.info("PST: файл делится на части до %s.", human_size(nominal))

            result.bytes_written = total_written
            if len(result.parts) > 1:
                first = part_path(out_path, 1)
                os.replace(out_path, first)
                result.parts[0] = first
                result.warning = (f"Файл разбит на {len(result.parts)} части(ей) до "
                                  f"{human_size(nominal)} — подключите в Outlook каждый файл. "
                                  ) + result.warning
            else:
                result.parts = []
            done_ok = True
        finally:
            if pst is not None:
                pst.abort()
            if not done_ok:
                _unlink_quiet(out_path)
                for extra in list(result.parts):
                    _unlink_quiet(extra)
            shutil.rmtree(spool_dir, ignore_errors=True)

        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "PST: готово")
        return result

    # -- одно письмо ---------------------------------------------------------
    def _write_one(self, pst: pf.PstFile, rec: _Spooled, folder_nid: int, result: ExportResult,
                   now_ft: int = 0):
        """→ (строка, замечания); None — письмо пропущено с ошибкой; "full" — нет места в части."""
        try:
            fh = open(rec.path, "rb")
        except OSError as exc:
            self._error(result, f"{rec.folder}: временный файл письма недоступен: {exc}")
            return None
        cp = pst.checkpoint()
        with fh:
            try:
                return write_message(pst, fh, folder_nid, flags=rec.flags,
                                     internaldate=rec.internaldate, size=rec.size, now_ft=now_ft)
            except pf.PstFullError:
                pst.rollback(cp)
                return "full"
            except OSError as exc:
                # Файл .pst перестал писаться (место, права) — дальше будут
                # сыпаться все письма подряд.
                raise self._disk_error(exc) from exc
            except MemoryError:
                raise
            except Exception as exc:  # noqa: BLE001 — одно кривое письмо не роняет весь экспорт
                pst.rollback(cp)
                log.warning("PST: письмо не выгружено (%s): %s", rec.folder, exc, exc_info=True)
                self._error(result, f"{rec.folder}: письмо не разобрано ({type(exc).__name__}: {exc})")
                return None

    @staticmethod
    def _disk_error(exc: OSError) -> PstWriteError:
        if getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
            return PstWriteError(f"Не хватило места для файла .pst: {exc}",
                                 hint="Освободите место в каталоге выгрузок или экспортируйте ящик по частям.")
        return PstWriteError(f"Ошибка записи файла .pst: {exc}")

    @staticmethod
    def _error(result: ExportResult, text: str, count: bool = True) -> None:
        if count:
            result.errors += 1
        if len(result.error_details) < MAX_ERROR_DETAILS:
            result.error_details.append(text)
        elif len(result.error_details) == MAX_ERROR_DETAILS:
            result.error_details.append("…остальные ошибки не перечисляются.")

    @staticmethod
    def _create_folders(pst: pf.PstFile, plan: List[_Folder]) -> Dict[Tuple[str, ...], int]:
        nids: Dict[Tuple[str, ...], int] = {}
        for folder in plan:
            if folder.role == "trash":
                pst.rename_folder(pf.NID_DELETED_ITEMS, folder.name)
                nids[folder.key] = pf.NID_DELETED_ITEMS
                continue
            parent = nids[folder.parent] if folder.parent is not None else pf.NID_IPM_SUBTREE
            nid = pst.add_folder(folder.name, parent)
            nids[folder.key] = nid
            if folder.role == "inbox":
                pst.inbox_nid = nid
            elif folder.role == "sent":
                pst.sent_nid = nid
        if not any(f.role == "trash" for f in plan):
            pst.rename_folder(pf.NID_DELETED_ITEMS, TRASH_NAME)
        return nids

    # -- подготовка ----------------------------------------------------------
    def _spool(self, items, spool_dir: str, result: ExportResult, cancel_cb, progress_cb, total_hint):
        """Тела писем — во временные файлы. → (записи, разделители папок, отменено ли)."""
        spooled = _SpoolIndex(spool_dir)
        spool_bytes = 0
        folder_names: Dict[str, str] = {}
        delims: Dict[str, str] = {}
        for item in items:
            if cancel_cb and cancel_cb():
                return spooled, delims, True
            path = spooled.path(len(spooled))
            try:
                with open(path, "wb") as fh:
                    fh.write(item.raw)
            except OSError as exc:
                if getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT, errno.EROFS):
                    raise PstWriteError(
                        f"Не удалось подготовить письма к экспорту: {exc}",
                        hint="Освободите место в каталоге временных файлов "
                             f"({spool_dir}) или укажите другой каталог.") from exc
                self._error(result, f"{item.folder}: не удалось сохранить тело во временный файл: {exc}")
                continue
            folder = folder_names.setdefault(item.folder, item.folder)
            if not delims.get(folder):
                delims[folder] = getattr(item, "delimiter", "") or ""
            spooled.add(folder, item.size or len(item.raw), _flag_bits(item.flags), item.internaldate)
            spool_bytes += len(item.raw)
            if len(spooled) % 500 == 0:
                free = disk_free_bytes(spool_dir)
                if free and free < spool_bytes:
                    raise PstWriteError(
                        f"Не хватит места для экспорта: подготовлено {human_size(spool_bytes)}, "
                        f"свободно {human_size(free)}.",
                        hint="Освободите место или экспортируйте ящик по частям (по папкам или по датам).")
                if progress_cb:
                    progress_cb(0, total_hint, f"PST: подготовка писем — {len(spooled)}")
        return spooled, delims, False

    @staticmethod
    def _make_spool_dir(options: Optional[dict]) -> str:
        """Каталог для временных тел писем (рядом с прочими временными файлами)."""
        tmp_dir = (options or {}).get("tmp_dir") or tempfile.gettempdir()
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError:
            tmp_dir = tempfile.gettempdir()
        return tempfile.mkdtemp(prefix="pstspool_", dir=tmp_dir)

    @staticmethod
    def _cancelled_result(result: ExportResult, out_path: str,
                          progress_cb: Optional[ProgressCB], total_hint: int) -> ExportResult:
        """Отмена: недоделанный .pst не должен остаться мусором в каталоге экспортов."""
        _unlink_quiet(out_path)
        result.bytes_written = 0
        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "PST: отменено")
        return result
