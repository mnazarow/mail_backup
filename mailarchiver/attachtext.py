"""
Текст вложений для поиска (1.20.0) — без внешних программ и библиотек.

Поддерживаются: PDF (текстовый слой, в том числе шрифты с таблицей ToUnicode — так
устроены почти все PDF из Word, LibreOffice и браузеров), Word (.docx, .docm, .dotx),
Excel (.xlsx, .xlsm), PowerPoint (.pptx), OpenDocument (.odt, .ods, .odp), RTF, текст
(.txt, .csv, .log, .md, .json, .xml), HTML и вложенные письма (.eml).

Сканы (картинки внутри PDF) не распознаются — для этого нужно распознавание текста.
Зашифрованные PDF и документы с паролем пропускаются. Разбор ограничен по размеру и
времени: вложение, которое не удалось прочитать, просто не даёт текста — индекс от
этого не ломается.
"""
from __future__ import annotations

import html as html_mod
import io
import re
import zipfile
import zlib
from typing import Dict, List, Optional, Tuple

from .logging_setup import get_logger

log = get_logger("attachtext")

#: Сколько распакованных данных читать из одного файла (защита от «zip-бомб»).
MAX_UNPACKED = 64 * 1024 * 1024
TEXT_EXT = {".txt", ".csv", ".log", ".md", ".json", ".xml", ".tsv", ".ini", ".yaml", ".yml", ".sql"}
HTML_EXT = {".html", ".htm"}
OOXML_EXT = {".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".pptx", ".pptm", ".ppsx"}
ODF_EXT = {".odt", ".ods", ".odp", ".ott"}
SUPPORTED = TEXT_EXT | HTML_EXT | OOXML_EXT | ODF_EXT | {".pdf", ".rtf", ".eml"}
_CTYPE_EXT = {"application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv", "text/html": ".html",
              "application/rtf": ".rtf", "text/rtf": ".rtf", "message/rfc822": ".eml",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
              "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
              "application/vnd.oasis.opendocument.text": ".odt",
              "application/vnd.oasis.opendocument.spreadsheet": ".ods",
              "application/vnd.oasis.opendocument.presentation": ".odp"}


def kind_of(filename: str, ctype: str) -> str:
    """Расширение, по которому разбирать вложение ('' — не поддерживается)."""
    name = (filename or "").lower().strip()
    dot = name.rfind(".")
    ext = name[dot:] if dot >= 0 else ""
    if ext in SUPPORTED:
        return ext
    return _CTYPE_EXT.get((ctype or "").lower().split(";")[0].strip(), "")


def extract(filename: str, ctype: str, data: bytes, cap_chars: int = 65536) -> str:
    """Текст вложения (обрезанный до ``cap_chars``) или ''."""
    kind = kind_of(filename, ctype)
    if not kind or not data:
        return ""
    try:
        if kind in TEXT_EXT:
            text = _decode(data[:cap_chars * 4])
        elif kind in HTML_EXT:
            text = html_text(_decode(data[:cap_chars * 8]))
        elif kind in OOXML_EXT:
            text = _ooxml(data, kind)
        elif kind in ODF_EXT:
            text = _zip_xml(data, ["content.xml"])
        elif kind == ".rtf":
            from .pstimport import rtf
            how, body = rtf.extract(data)
            text = html_text(body) if how == "html" else body
        elif kind == ".eml":
            text = _eml(data, cap_chars)
        elif kind == ".pdf":
            text = pdf_text(data)
        else:
            text = ""
    except Exception as exc:  # noqa: BLE001 - битый файл не должен ломать индексацию
        log.debug("Текст вложения %s не извлечён: %s", filename, exc)
        return ""
    text = re.sub(r"[ \t\r\f\v ]+", " ", text or "")
    text = re.sub(r" *\n[ \n]*", "\n", text).strip()
    return text[:cap_chars]


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


_TAG_RE = re.compile(r"<[^>]*>")


def html_text(value: str) -> str:
    value = re.sub(r"<(script|style|head)[^>]*>.*?</\1\s*>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<\s*(?:br|/p|/div|/tr|/li|/h\d)\s*/?\s*>", "\n", value, flags=re.I)
    return html_mod.unescape(_TAG_RE.sub(" ", value))


def _xml_text(xml: str) -> str:
    """Текст XML документа: абзацы, строки таблиц и переносы — с новой строки."""
    xml = re.sub(r"<(?:w:tab|text:tab)\b[^>]*/>", "\t", xml)
    xml = re.sub(r"</(?:w:p|a:p|text:p|text:h|table:table-row|row|si)>|<(?:w:br|text:line-break)\b[^>]*/>", "\n",
                 xml)
    xml = re.sub(r"</(?:c|table:table-cell)>", " ", xml)
    return html_mod.unescape(_TAG_RE.sub("", xml))


def _read_member(zf: zipfile.ZipFile, name: str, budget: List[int]) -> str:
    info = zf.getinfo(name)
    if info.file_size > budget[0]:
        return ""
    budget[0] -= info.file_size
    return zf.read(name).decode("utf-8", "replace")


def _zip_xml(data: bytes, names: List[str]) -> str:
    budget = [MAX_UNPACKED]
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        have = set(zf.namelist())
        for name in names:
            if name in have:
                out.append(_xml_text(_read_member(zf, name, budget)))
    return "\n".join(out)


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _ooxml(data: bytes, kind: str) -> str:
    budget = [MAX_UNPACKED]
    out: List[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if kind in (".docx", ".docm", ".dotx"):
            parts = ["word/document.xml"] + sorted(n for n in names if re.match(r"word/(header|footer|footnotes|"
                                                                                 r"endnotes)\d*\.xml$", n))
        elif kind in (".xlsx", ".xlsm"):
            parts = ["xl/sharedStrings.xml"] + sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                                                      key=_natural)
        else:
            parts = sorted((n for n in names if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
                           key=_natural)
        for name in parts:
            if name not in names:
                continue
            xml = _read_member(zf, name, budget)
            if name.startswith("xl/worksheets/"):
                # в листах — только строки, записанные прямо в ячейку (inlineStr) и числа
                vals = []
                for attrs, inner in re.findall(r"<c\b([^>]*)>(.*?)</c>", xml, flags=re.S):
                    m_t = re.search(r'\bt="(\w+)"', attrs)
                    typ = m_t.group(1) if m_t else ""
                    if typ == "inlineStr":
                        vals.append(_xml_text(inner))
                    elif typ in ("", "n"):
                        m = re.search(r"<v>([^<]*)</v>", inner)
                        if m:
                            vals.append(m.group(1))
                xml_text = " ".join(vals)
            else:
                xml_text = _xml_text(xml)
            out.append(xml_text)
    return "\n".join(out)


def _eml(data: bytes, cap: int) -> str:
    from email import message_from_bytes
    from email.policy import compat32
    from .mailview import _get_text, _iter_parts
    from .util import decode_mime_header
    msg = message_from_bytes(data[:8 * 1024 * 1024], policy=compat32)
    parts = [decode_mime_header(str(msg.get("Subject", "") or "")), decode_mime_header(str(msg.get("From", "") or ""))]
    for part in _iter_parts(msg):
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain":
            parts.append(_get_text(part))
        elif ctype == "text/html":
            parts.append(html_text(_get_text(part)))
        if sum(len(p) for p in parts) > cap:
            break
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  PDF
# ---------------------------------------------------------------------------
_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_REF_RE = re.compile(rb"(\d+)\s+0\s+R")
#: Сколько операторов текста разбирать в одном PDF (защита от огромных файлов).
MAX_PDF_OPS = 2_000_000


class _Pdf:
    """Минимальный разбор PDF: объекты, потоки, шрифты и их таблицы ToUnicode."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.objs: Dict[int, bytes] = {}
        self.unpacked = 0
        self._scan()

    # --- объекты ---
    def _scan(self) -> None:
        data = self.data
        positions = [(int(m.group(1)), m.end()) for m in _OBJ_RE.finditer(data)]
        for i, (num, start) in enumerate(positions):
            end = data.find(b"endobj", start)
            if end < 0:
                end = positions[i + 1][1] if i + 1 < len(positions) else len(data)
            self.objs[num] = data[start:end]
        # объекты, упакованные в потоки объектов (PDF 1.5+): там часто лежат шрифты и страницы
        for num, body in list(self.objs.items()):
            if b"/ObjStm" not in body[:400]:
                continue
            raw = self.stream(body)
            if raw is None:
                continue
            n = _int_key(body, b"/N")
            first = _int_key(body, b"/First")
            if n is None or first is None:
                continue
            head = raw[:first].split()
            pairs = [(int(head[k]), int(head[k + 1])) for k in range(0, min(len(head) - 1, 2 * n), 2)]
            for k, (onum, off) in enumerate(pairs):
                end = pairs[k + 1][1] if k + 1 < len(pairs) else len(raw) - first
                self.objs.setdefault(onum, raw[first + off:first + end])

    def get(self, ref: Optional[int]) -> bytes:
        return self.objs.get(ref, b"") if ref is not None else b""

    def stream(self, body: bytes) -> Optional[bytes]:
        pos = body.find(b"stream")
        if pos < 0:
            return None
        start = pos + 6
        if body[start:start + 2] == b"\r\n":
            start += 2
        elif body[start:start + 1] in (b"\n", b"\r"):
            start += 1
        end = body.rfind(b"endstream")
        raw = body[start:end if end > start else len(body)]
        head = body[:pos]
        if b"/Encrypt" in head:
            return None
        filters = re.findall(rb"/(FlateDecode|Fl|ASCIIHexDecode|AHx|ASCII85Decode|A85|DCTDecode|JPXDecode|"
                             rb"CCITTFaxDecode|JBIG2Decode|LZWDecode|RunLengthDecode)\b", head)
        for f in filters:
            if f in (b"FlateDecode", b"Fl"):
                d = zlib.decompressobj()
                try:
                    raw = d.decompress(raw, MAX_UNPACKED - self.unpacked)
                except zlib.error:
                    try:
                        raw = zlib.decompressobj(-15).decompress(raw[2:], MAX_UNPACKED - self.unpacked)
                    except zlib.error:
                        return None
            elif f in (b"ASCIIHexDecode", b"AHx"):
                raw = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", raw.split(b">")[0]).decode())
            else:
                return None                       # картинки и редкие фильтры — не текст
            self.unpacked += len(raw)
            if self.unpacked >= MAX_UNPACKED:
                return None
        return raw

    # --- словари ---
    def value(self, body: bytes, key: bytes) -> bytes:
        """Значение ключа словаря: ссылка разворачивается в тело объекта."""
        m = re.search(re.escape(key) + rb"(?![A-Za-z0-9])\s*", body)
        if not m:
            return b""
        rest = body[m.end():]
        ref = re.match(rb"(\d+)\s+0\s+R", rest)
        if ref:
            return self.get(int(ref.group(1)))
        if rest.startswith(b"<<"):
            return _dict_at(rest)
        if rest.startswith(b"["):
            close = rest.find(b"]")
            return rest[:close + 1] if close >= 0 else rest
        return rest[:64]

    def ref(self, body: bytes, key: bytes) -> Optional[int]:
        m = re.search(re.escape(key) + rb"(?![A-Za-z0-9])\s*(\d+)\s+0\s+R", body)
        return int(m.group(1)) if m else None

    # --- страницы ---
    def pages(self) -> List[int]:
        root_tree = None
        for num, body in self.objs.items():
            if re.search(rb"/Type\s*/Catalog\b", body[:2000]):
                root_tree = self.ref(body, b"/Pages")
                break
        out: List[int] = []
        seen = set()

        def walk(num: Optional[int]) -> None:
            if num is None or num in seen or len(out) > 5000:
                return
            seen.add(num)
            body = self.get(num)
            head = _dict_at(body.lstrip()) if body.lstrip().startswith(b"<<") else body[:4000]
            if re.search(rb"/Type\s*/Pages\b", head):
                kids = self.value(head, b"/Kids")
                for m in _REF_RE.finditer(kids):
                    walk(int(m.group(1)))
            elif re.search(rb"/Type\s*/Page\b", head):
                out.append(num)
        walk(root_tree)
        if not out:                                  # нет каталога — все страницы подряд
            out = sorted(n for n, b in self.objs.items() if re.search(rb"/Type\s*/Page\b", b[:4000]))
        return out

    def resources(self, page: int) -> bytes:
        num: Optional[int] = page
        for _ in range(20):
            body = self.get(num)
            head = _dict_at(body.lstrip()) if body.lstrip().startswith(b"<<") else body
            res = self.value(head, b"/Resources")
            if res:
                return res
            num = self.ref(head, b"/Parent")
            if num is None:
                break
        return b""

    def fonts(self, resources: bytes) -> Dict[bytes, "_Font"]:
        font_dict = self.value(resources, b"/Font")
        out: Dict[bytes, _Font] = {}
        for m in re.finditer(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R", font_dict):
            out[m.group(1)] = self.font(int(m.group(2)))
        return out

    def font(self, num: int) -> "_Font":
        cache = self.__dict__.setdefault("_fonts", {})
        if num not in cache:
            body = self.get(num)
            head = _dict_at(body.lstrip()) if body.lstrip().startswith(b"<<") else body
            two_byte = b"/Identity-H" in head or b"/Identity-V" in head or b"/Type0" in head
            mapping: Dict[int, str] = {}
            width = 2 if two_byte else 1
            cmap = self.ref(head, b"/ToUnicode")
            if cmap is not None:
                raw = self.stream(self.get(cmap))
                if raw:
                    mapping, cmap_width = _parse_cmap(raw)
                    width = max(width, cmap_width)
            font = _Font(mapping, width)
            try:
                self._widths(head, font, two_byte)
            except (ValueError, IndexError):
                pass
            cache[num] = font
        return cache[num]

    def _array(self, body: bytes, key: bytes) -> bytes:
        """Массив по ключу (прямо в словаре или по ссылке) — без внешних скобок."""
        m = re.search(re.escape(key) + rb"(?![A-Za-z0-9])\s*(\d+)\s+0\s+R", body)
        if m:
            text = self.get(int(m.group(1))).strip()
        else:
            m = re.search(re.escape(key) + rb"(?![A-Za-z0-9])\s*\[", body)
            if not m:
                return b""
            text = body[m.end() - 1:]
        if not text.startswith(b"["):
            return b""
        depth = 0
        for i, ch in enumerate(text):
            if ch == 0x5B:
                depth += 1
            elif ch == 0x5D:
                depth -= 1
                if depth == 0:
                    return text[1:i]
        return text[1:]

    def _widths(self, head: bytes, font: "_Font", two_byte: bool) -> None:
        """Ширины глифов: по ним понятно, где между кусками строки пробел."""
        if not two_byte:
            first = _int_key(head, b"/FirstChar")
            arr = self._array(head, b"/Widths")
            if first is not None and arr:
                for k, w in enumerate(re.findall(rb"[-+]?\d*\.?\d+", arr)):
                    font.widths[first + k] = float(w)
            return
        desc = self._array(head, b"/DescendantFonts")
        ref = _REF_RE.search(desc)
        if not ref:
            return
        body = self.get(int(ref.group(1)))
        dhead = _dict_at(body.lstrip()) if body.lstrip().startswith(b"<<") else body
        dw = re.search(rb"/DW\s+([-+]?\d*\.?\d+)", dhead)
        if dw:
            font.default_w = float(dw.group(1))
        arr = self._array(dhead, b"/W")
        toks = re.findall(rb"\[[^\]]*\]|[-+]?\d*\.?\d+", arr)
        i = 0
        while i < len(toks) and len(font.widths) < 200000:
            if i + 1 < len(toks) and toks[i + 1].startswith(b"["):
                start = int(float(toks[i]))
                for k, w in enumerate(re.findall(rb"[-+]?\d*\.?\d+", toks[i + 1])):
                    font.widths[start + k] = float(w)
                i += 2
            elif i + 2 < len(toks):
                lo, hi, w = int(float(toks[i])), int(float(toks[i + 1])), float(toks[i + 2])
                for code in range(lo, min(hi, lo + 65535) + 1):
                    font.widths[code] = w
                i += 3
            else:
                break

    def contents(self, page: int) -> bytes:
        body = self.get(page)
        head = _dict_at(body.lstrip()) if body.lstrip().startswith(b"<<") else body
        m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+0\s+R)", head)
        if not m:
            return b""
        chunks = []
        for r in _REF_RE.finditer(m.group(1)):
            raw = self.stream(self.get(int(r.group(1))))
            if raw:
                chunks.append(raw)
        return b"\n".join(chunks)


class _Font:
    def __init__(self, mapping: Dict[int, str], width: int) -> None:
        self.map = mapping
        self.width = width
        self.widths: Dict[int, float] = {}
        self.default_w = 500.0 if width == 1 else 1000.0

    def advance(self, raw: bytes) -> float:
        """Ширина строки в тысячных долях кегля."""
        w = self.width
        total = 0.0
        for i in range(0, len(raw) - w + 1, w):
            total += self.widths.get(int.from_bytes(raw[i:i + w], "big"), self.default_w)
        return total

    def decode(self, raw: bytes) -> str:
        if not self.map:
            if self.width == 2:
                return ""                              # коды глифов без таблицы — текста не восстановить
            return raw.decode("cp1252", "replace")
        out = []
        w = self.width
        for i in range(0, len(raw) - w + 1, w):
            code = int.from_bytes(raw[i:i + w], "big")
            out.append(self.map.get(code, ""))
        return "".join(out)


def _int_key(body: bytes, key: bytes) -> Optional[int]:
    m = re.search(re.escape(key) + rb"\s+(\d+)", body[:2000])
    return int(m.group(1)) if m else None


def _dict_at(data: bytes) -> bytes:
    """Словарь ``<< … >>`` с начала строки (с учётом вложенных словарей)."""
    depth = 0
    i = 0
    n = len(data)
    while i < n - 1:
        two = data[i:i + 2]
        if two == b"<<":
            depth += 1
            i += 2
            continue
        if two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return data[:i]
            continue
        if data[i:i + 1] == b"(":
            i = _skip_literal(data, i)
            continue
        i += 1
    return data


def _skip_literal(data: bytes, i: int) -> int:
    depth = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:                                   # «\»
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _hex_to_int(h: bytes) -> int:
    return int(h, 16) if h else 0


def _utf16(h: bytes) -> str:
    raw = bytes.fromhex(h.decode()) if h else b""
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _parse_cmap(raw: bytes) -> Tuple[Dict[int, str], int]:
    mapping: Dict[int, str] = {}
    width = 1
    m = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", raw)
    if m:
        width = max(1, len(m.group(1)) // 2)
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", raw, flags=re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            mapping[_hex_to_int(src)] = _utf16(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", raw, flags=re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            a, b = _hex_to_int(lo), _hex_to_int(hi)
            if b - a > 65535:
                continue
            if dst.startswith(b"["):
                for k, item in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", dst)):
                    mapping[a + k] = _utf16(item)
            else:
                base = dst[1:-1]
                start = _hex_to_int(base)
                nbytes = max(2, len(base) // 2)
                for k in range(b - a + 1):
                    try:
                        mapping[a + k] = (start + k).to_bytes(nbytes, "big").decode("utf-16-be")
                    except (UnicodeDecodeError, OverflowError):
                        mapping[a + k] = ""
    return mapping, width


_TOKEN_RE = re.compile(rb"\(|<<|>>|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()%{}]*|[-+]?\d*\.?\d+|[A-Za-z'\"*]+[0-9]?|%[^\n\r]*")


def _literal(data: bytes, i: int) -> Tuple[bytes, int]:
    """Строка ``( … )`` с экранированием → байты и позиция после неё."""
    out = bytearray()
    depth = 1
    i += 1
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x5C and i + 1 < n:
            nxt = data[i + 1]
            esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}.get(nxt)
            if esc is not None:
                out.append(esc)
                i += 2
            elif 0x30 <= nxt <= 0x37:
                j = i + 1
                k = 0
                while j < n and k < 3 and 0x30 <= data[j] <= 0x37:
                    j += 1
                    k += 1
                out.append(int(data[i + 1:j], 8) & 0xFF)
                i = j
            elif nxt in (0x0A, 0x0D):
                i += 2
            else:
                out.append(nxt)
                i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out.append(c)
        i += 1
    return bytes(out), n


def _page_text(content: bytes, fonts: Dict[bytes, _Font], budget: List[int]) -> str:
    out: List[str] = []
    font = _Font({}, 1)
    size = 10.0
    stack: List = []
    i = 0
    n = len(content)
    # положение в строке: начало строки (по Tm/Td) и сколько уже «напечатано» от него
    line_x = 0.0
    line_y: Optional[float] = None
    cur = 0.0
    scale = 1.0

    def space() -> None:
        if out and not out[-1].endswith((" ", "\n")):
            out.append(" ")

    def newline() -> None:
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    def show(raw: bytes) -> None:
        nonlocal cur
        out.append(font.decode(raw))
        cur += font.advance(raw) / 1000.0 * size

    while i < n and budget[0] > 0:
        m = _TOKEN_RE.search(content, i)
        if not m:
            break
        tok = m.group(0)
        i = m.end()
        if tok == b"(":
            s_, i = _literal(content, m.start())
            stack.append(("s", s_))
            continue
        if tok.startswith(b"%"):
            continue
        if tok.startswith(b"<") and tok != b"<<":
            hexs = re.sub(rb"\s", b"", tok[1:-1])
            if len(hexs) % 2:
                hexs += b"0"
            try:
                stack.append(("s", bytes.fromhex(hexs.decode())))
            except ValueError:
                pass
            continue
        if tok == b"[":
            stack.append(("[", None))
            continue
        if tok == b"]":
            arr = []
            while stack and stack[-1][0] != "[":
                arr.append(stack.pop())
            if stack:
                stack.pop()
            stack.append(("a", list(reversed(arr))))
            continue
        if tok.startswith(b"/"):
            stack.append(("n", tok[1:]))
            continue
        if tok[:1].isdigit() or tok[:1] in (b"-", b"+", b"."):
            try:
                stack.append(("f", float(tok)))
            except ValueError:
                pass
            continue
        if tok in (b"<<", b">>"):
            continue
        budget[0] -= 1
        nums = [v for k, v in stack if k == "f"]
        if tok == b"Tf":
            names = [v for k, v in stack if k == "n"]
            if names:
                font = fonts.get(names[-1], _Font({}, 1))
            if nums:
                size = abs(nums[-1]) or 10.0
        elif tok == b"Tj":
            strs = [v for k, v in stack if k == "s"]
            if strs:
                show(strs[-1])
        elif tok in (b"'", b'"'):
            newline()
            cur = 0.0
            strs = [v for k, v in stack if k == "s"]
            if strs:
                show(strs[-1])
        elif tok == b"TJ":
            arrs = [v for k, v in stack if k == "a"]
            if arrs:
                for k, v in arrs[-1]:
                    if k == "s":
                        show(v)
                    elif k == "f":
                        if -v / 1000.0 > 0.2:          # большой отступ внутри строки — пробел между словами
                            space()
                        cur -= v / 1000.0 * size
        elif tok in (b"Td", b"TD") and len(nums) >= 2:
            dx, dy = nums[-2], nums[-1]
            if abs(dy) > 0.01:
                newline()
            elif dx - cur > 0.15 * size:
                space()
            line_x += dx * scale
            cur = 0.0
        elif tok == b"Tm" and len(nums) >= 6:
            a, x, y = nums[-6], nums[-2], nums[-1]
            new_scale = abs(a) or 1.0
            if line_y is not None and abs(y - line_y) > 0.5 * new_scale:
                newline()
            elif line_y is not None and (x - (line_x + cur * scale)) / new_scale > 0.15 * size:
                space()
            line_x, line_y, scale, cur = x, y, new_scale, 0.0
        elif tok == b"T*":
            newline()
            cur = 0.0
        elif tok == b"BT":
            line_x, cur, scale = 0.0, 0.0, 1.0
        elif tok == b"ET":
            newline()
        stack = []
    return "".join(out)


def pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF"):
        head = data.find(b"%PDF", 0, 1024)
        if head < 0:
            return ""
        data = data[head:]
    if re.search(rb"/Encrypt\s+\d+\s+0\s+R", data[-4096:]) or re.search(rb"/Encrypt\s*<<", data[-4096:]):
        return ""
    pdf = _Pdf(data)
    budget = [MAX_PDF_OPS]
    texts: List[str] = []
    for page in pdf.pages():
        content = pdf.contents(page)
        if not content:
            continue
        texts.append(_page_text(content, pdf.fonts(pdf.resources(page)), budget))
        if budget[0] <= 0:
            break
    return "\n".join(texts)
