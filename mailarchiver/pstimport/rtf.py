"""
Тело письма в RTF: распаковка и извлечение из него HTML или текста.

Outlook хранит тело письма в сжатом RTF (свойство PidTagRtfCompressed,
[MS-OXRTFCP]). Если письмо пришло в HTML, этот RTF — лишь обёртка: исходный
HTML лежит в нём кусками в группах ``{\\*\\htmltag …}``, а всё, что нужно только
для показа как RTF, заключено между ``\\htmlrtf`` и ``\\htmlrtf0``
([MS-OXRTFEX]). Письмо, пришедшее простым текстом, обёрнуто так же
(``\\fromtext``). «Настоящий» RTF (письмо, набранное в Outlook в формате
«Форматированный текст») переводится в простой текст: оформление теряется,
слова остаются.
"""
from __future__ import annotations

import re
import struct
from typing import Dict, List, Optional, Tuple

#: Начальное содержимое словаря сжатия — ровно 207 байт ([MS-OXRTFCP] 2.1.3.1.1).
_PREBUF = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss "
           b"\\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New RomanCourier"
           b"{\\colortbl\\red0\\green0\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx")
_COMPRESSED = 0x75465A4C          # «LZFu»
_UNCOMPRESSED = 0x414C454D        # «MELA»
#: Больше этого распакованный RTF не разбираем (тело письма в десятки мегабайт — не тело).
MAX_RTF_BYTES = 16 * 1024 * 1024


class RtfError(Exception):
    """RTF не распаковывается или не разбирается."""


def decompress(data: bytes, limit: int = MAX_RTF_BYTES) -> bytes:
    """Распаковать PidTagRtfCompressed."""
    if len(data) < 16:
        raise RtfError("сжатый RTF короче заголовка")
    comp_size, raw_size, magic, _crc = struct.unpack_from("<IIII", data, 0)
    if magic == _UNCOMPRESSED:
        return data[16:16 + min(raw_size, limit)]
    if magic != _COMPRESSED:
        raise RtfError("неизвестный способ сжатия RTF")
    if raw_size > limit:
        raise RtfError(f"RTF больше {limit} байт")
    end = min(len(data), comp_size + 4)
    window = bytearray(4096)
    window[:len(_PREBUF)] = _PREBUF
    wpos = len(_PREBUF)
    out = bytearray()
    pos = 16
    while pos < end:
        control = data[pos]
        pos += 1
        for bit in range(8):
            if pos >= end:
                break
            if control & (1 << bit):
                if pos + 2 > end:
                    return bytes(out)
                ref = (data[pos] << 8) | data[pos + 1]
                pos += 2
                offset = ref >> 4
                if offset == wpos:                      # ссылка «на себя» — конец данных
                    return bytes(out)
                for _ in range((ref & 0x0F) + 2):
                    char = window[offset]
                    offset = (offset + 1) & 0xFFF
                    out.append(char)
                    window[wpos] = char
                    wpos = (wpos + 1) & 0xFFF
            else:
                char = data[pos]
                pos += 1
                out.append(char)
                window[wpos] = char
                wpos = (wpos + 1) & 0xFFF
        if len(out) > limit:
            raise RtfError(f"RTF больше {limit} байт")
    return bytes(out)


# ---------------------------------------------------------------------------
#  Разбор RTF
# ---------------------------------------------------------------------------
_TOKEN = re.compile(
    rb"\\([A-Za-z]{1,32})(-?[0-9]{1,10})? ?"      # управляющее слово с числом и пробелом-разделителем
    rb"|\\'([0-9A-Fa-f]{2})"                      # байт в шестнадцатеричном виде
    rb"|\\([^A-Za-z])"                            # управляющий символ
    rb"|([{}])"
    rb"|([\r\n]+)"                                # переводы строк в RTF ничего не значат
    rb"|([^\\{}\r\n]+)")                          # текст

#: \fcharsetN → кодовая страница Windows. Наборов 0 («ANSI»), 1 («по умолчанию») и 2
#: (Symbol) здесь нет намеренно: для них берётся кодировка документа (\ansicpg).
#: Exchange, пряча HTML в RTF, всегда пишет одну и ту же таблицу шрифтов с
#: \fcharset0, в какой бы кодировке ни был текст, — считать её «Западной Европой»
#: значило бы превратить всю кириллицу в кракозябры.
_CHARSETS = {77: 10000, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950,
             161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256, 186: 1257, 204: 1251,
             222: 874, 238: 1250, 254: 437, 255: 850}
#: Глубже этого группы RTF не вкладываются (мегабайты «{» иначе съели бы память).
MAX_DEPTH = 1000

#: Группы, содержимое которых — не текст письма.
_SKIP_DESTINATIONS = frozenset((
    "colortbl", "stylesheet", "info", "pict", "object", "objdata", "datastore", "themedata",
    "colorschememapping", "latentstyles", "listtable", "listoverridetable", "rsidtbl", "generator",
    "xmlnstbl", "filetbl", "revtbl", "header", "footer", "headerl", "headerr", "headerf",
    "footerl", "footerr", "footerf", "ftnsep", "ftnsepc", "ftncn", "aftnsep", "aftnsepc", "aftncn",
    "pntext", "pntxta", "pntxtb", "fldinst", "nonshppict", "shpinst", "bkmkstart", "bkmkend",
    "mhtmltag", "private", "operator", "author", "title", "subject", "keywords", "comment",
    "doccomm", "company", "manager", "category", "hlinkbase", "template", "xe", "tc", "tcn",
    "annotation", "atnid", "atnauthor", "atnref", "atntime", "atrfstart", "atrfend", "falt",
    "panose", "fname", "fontemb", "fontfile", "passwordhash", "wgrffmtfilter", "pgdsctbl",
    "protusertbl", "userprops", "docvar", "background", "shp", "sp", "sn", "sv"))

#: Управляющие слова — знаки. Второй столбец — замена в HTML, третий — в тексте.
_SPECIAL = {
    "par": ("\r\n", "\n"), "line": ("\r\n", "\n"), "sect": ("\r\n", "\n"), "page": ("\r\n", "\n"),
    "tab": ("\t", "\t"), "cell": ("\t", "\t"), "row": ("\r\n", "\n"),
    "lquote": ("&lsquo;", "‘"), "rquote": ("&rsquo;", "’"),
    "ldblquote": ("&ldquo;", "“"), "rdblquote": ("&rdquo;", "”"),
    "bullet": ("&bull;", "•"), "endash": ("&ndash;", "–"), "emdash": ("&mdash;", "—"),
    "emspace": ("&emsp;", " "), "enspace": ("&ensp;", " "),
}
_SYMBOLS = {b"~": ("&nbsp;", " "), b"_": ("&shy;", "‑"), b"-": ("", ""),
            b"\\": ("\\", "\\"), b"{": ("{", "{"), b"}": ("}", "}"),
            b"\r": ("\r\n", "\n"), b"\n": ("\r\n", "\n")}


def _codec(codepage: int) -> str:
    from .reader import codepage_codec
    return codepage_codec(codepage) or "cp1252"


class _State:
    __slots__ = ("skip", "htmltag", "htmlrtf", "font", "uc", "fonttbl")

    def __init__(self) -> None:
        self.skip = False          # группа не содержит текста письма
        self.htmltag = False       # внутри {\*\htmltag …}: содержимое — разметка HTML как есть
        self.htmlrtf = False       # между \htmlrtf и \htmlrtf0: только для показа RTF
        self.font = -1
        self.uc = 1                # сколько знаков-замен идёт после \uN
        self.fonttbl = False

    def copy(self) -> "_State":
        other = _State()
        for name in self.__slots__:
            setattr(other, name, getattr(self, name))
        return other


def extract(rtf: bytes) -> Tuple[str, str]:
    """RTF → (вид, содержимое).

    Вид: ``"html"`` — в RTF был спрятан HTML, содержимое — он; ``"text"`` — простой
    текст (спрятанный в RTF или полученный из настоящего RTF).
    """
    if not rtf.lstrip()[:5].startswith(b"{\\rtf"):
        raise RtfError("это не RTF")
    if len(rtf) > MAX_RTF_BYTES:
        raise RtfError(f"RTF больше {MAX_RTF_BYTES} байт")
    mode = "rtf"
    default_cp = 1252
    fonts: Dict[int, int] = {}
    font_being_defined: Optional[int] = None
    state = _State()
    stack: List[_State] = []
    out: List[str] = []
    pending = bytearray()              # байты текста, ещё не переведённые в знаки
    pending_cp = default_cp
    skip_chars = 0                     # сколько знаков-замен после \uN ещё надо пропустить
    high_surrogate: Optional[int] = None
    after_brace = False                # только что открылась группа — её первое слово решает, что это
    star = False                       # в начале группы стоит \* — необязательное назначение

    def visible() -> bool:
        return not state.skip and (state.htmltag or not state.htmlrtf) and not state.fonttbl

    def current_cp() -> int:
        return fonts.get(state.font, default_cp)

    def flush() -> None:
        nonlocal high_surrogate
        if pending:
            out.append(bytes(pending).decode(_codec(pending_cp), "replace"))
            pending.clear()
        high_surrogate = None

    def emit(text: str) -> None:
        if visible():
            flush()
            out.append(text)

    pos = 0
    size = len(rtf)
    while pos < size:
        match = _TOKEN.match(rtf, pos)
        if match is None:                              # одиночная «\» в конце
            pos += 1
            continue
        pos = match.end()
        word, param, hexbyte, symbol, brace, newline, text = match.groups()
        if newline is not None:
            continue

        if brace is not None:
            flush()
            skip_chars = 0
            if brace == b"{":
                if len(stack) >= MAX_DEPTH:
                    raise RtfError(f"вложенность групп RTF больше {MAX_DEPTH}")
                stack.append(state.copy())
                after_brace, star = True, False
            else:
                if stack:
                    state = stack.pop()
                after_brace = star = False
                if not stack:
                    break                               # документ закрыт; дальше — мусор (Word дописывает NUL)
            continue

        if word is not None:
            name = word.decode("ascii")
            number = int(param) if param is not None else None
            first, is_star = after_brace, star
            after_brace = star = False
            skip_chars = 0
            if name == "bin":                           # \binN: дальше N сырых байт
                pos = min(size, pos + max(0, number or 0))
                continue
            if first:
                if name == "htmltag" and mode == "html":
                    state.htmltag = True
                    continue
                if name == "fonttbl":
                    state.fonttbl = True
                    continue
                if is_star or name in _SKIP_DESTINATIONS:
                    state.skip = True
                    continue
            if state.fonttbl:
                if name == "f" and number is not None:
                    font_being_defined = number
                elif name == "fcharset" and number is not None and font_being_defined is not None:
                    if number in _CHARSETS:
                        fonts[font_being_defined] = _CHARSETS[number]
                    else:
                        fonts.pop(font_being_defined, None)      # кодировка документа
                elif name == "cpg" and number and font_being_defined is not None:
                    fonts[font_being_defined] = number
                continue
            if name == "htmlrtf":
                flush()
                state.htmlrtf = number != 0
            elif name == "fromhtml" and number != 0:
                mode = "html"
            elif name == "fromtext":
                mode = "text"
            elif name == "ansicpg" and number:
                default_cp = number
            elif name == "f" and number is not None:
                flush()
                state.font = number
            elif name == "plain":
                flush()
                state.font = -1
            elif name == "uc" and number is not None:
                state.uc = max(0, min(number, 16))
            elif name == "u" and number is not None:
                code = number & 0xFFFF                  # число со знаком, 16 бит
                skip_chars = state.uc
                if not visible():
                    continue
                if 0xD800 <= code <= 0xDBFF:
                    flush()
                    high_surrogate = code
                    continue
                if 0xDC00 <= code <= 0xDFFF:
                    high = high_surrogate
                    flush()
                    if high is not None:
                        out.append(chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00)))
                    continue
                flush()
                out.append(chr(code) if code else "")
            elif name in _SPECIAL:
                emit(_SPECIAL[name][0 if mode == "html" else 1])
            continue

        if symbol is not None:
            first = after_brace
            after_brace = False
            if symbol == b"*" and first:
                star = True
                after_brace = True                      # слово-назначение идёт следом
                continue
            star = False
            skip_chars = 0
            if symbol in _SYMBOLS:
                emit(_SYMBOLS[symbol][0 if mode == "html" else 1])
            continue

        after_brace = star = False
        if hexbyte is not None:
            data = bytes((int(hexbyte, 16),))
        else:
            data = text
        if skip_chars:
            drop = min(skip_chars, len(data))
            skip_chars -= drop
            data = data[drop:]
        if not data or not visible():
            continue
        cp = current_cp()
        if pending and cp != pending_cp:
            flush()
        pending_cp = cp
        high_surrogate = None
        pending += data
    flush()
    result = "".join(out)
    if mode == "html":
        return "html", result
    return "text", result.replace("\r\n", "\n").strip("\n") + ("\n" if result.strip() else "")


def body_from_compressed(data: bytes) -> Tuple[str, str]:
    """PidTagRtfCompressed → (вид, содержимое), см. :func:`extract`."""
    return extract(decompress(data))
