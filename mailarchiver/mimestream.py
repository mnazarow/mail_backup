"""
Потоковый разбор структуры MIME — без загрузки письма в память.

Зачем. Модуль ``email`` строит полное дерево письма: строка на каждую строку
исходника, склеенные тела частей, декодированные вложения. На письме с
вложением в 100 МБ это полтора гигабайта памяти, поэтому крупные письма раньше
показывались только по заголовкам, а вложения из них не отдавались вовсе.

Здесь письмо читается ОДИН раз построчно из файла (обычного, gzip или
зашифрованного — лишь бы был объект с ``readline``). Для каждой конечной части
разбираются только её заголовки (они маленькие), а тело либо пропускается,
либо отдаётся порциями нужному потребителю: так скачивание вложения из
письма любого размера занимает память порядка одной строки.

Правила обхода повторяют ``mailview._iter_parts`` и ``mailview._is_attachment``:
части перечисляются в порядке документа, внутрь ``message/rfc822`` не заходим,
контейнеры ``multipart/*`` сами частями не считаются. Для одного письма один и
тот же путь разбора (потоковый — для крупных, ``email`` — для обычных)
используется и при показе списка вложений, и при скачивании, поэтому номера
вложений совпадают.
"""
from __future__ import annotations

import binascii
import email.parser
import re
from email import policy
from typing import Callable, Dict, Iterator, List, Tuple

#: Длиннее строки читаем порциями (бинарные части без переводов строк).
_MAX_LINE = 64 * 1024
#: Предел размера блока заголовков одной части.
_MAX_HEADER = 256 * 1024
#: Размер порции тела, отдаваемой потребителю.
_FLUSH = 64 * 1024
#: Как устроена строка заголовка — тот же шаблон, что в email.feedparser.
_HEADER_RE = re.compile(rb"^(From |[\041-\071\073-\176]*:|[\t ])")

_HEADER_PARSER = email.parser.BytesHeaderParser(policy=policy.default)


def _normalize_cte(headers) -> str:
    """Content-Transfer-Encoding части — так же, как его понимает модуль email.

    Политика email.policy.default разбирает заголовок и отдаёт нормализованное
    значение (``.cte``): «base64 (кодировка)», «Base64;» и т.п. — это base64.
    Сырая строка заголовка давала расхождения: email такую часть декодировал,
    а потоковый путь — нет.
    """
    try:
        value = headers.get("Content-Transfer-Encoding")
    except Exception:  # noqa: BLE001
        return ""
    if value is None:
        return ""
    cte = getattr(value, "cte", None)
    if not cte:
        cte = str(value)
    cte = str(cte).strip().lower()
    for sep in (";", "(", " ", "\t"):
        cte = cte.split(sep, 1)[0]
    return cte.strip()


class Leaf:
    """Конечная часть письма (не контейнер multipart)."""

    __slots__ = ("headers", "ctype", "disp", "filename", "cte", "charset",
                 "encoded_len", "eq_count", "want", "top")

    def __init__(self, headers, default_type: str, top: bool) -> None:
        if default_type != "text/plain":
            try:
                headers.set_default_type(default_type)
            except Exception:  # noqa: BLE001
                pass
        self.headers = headers
        self.top = top
        try:
            self.ctype = (headers.get_content_type() or "text/plain").lower()
        except Exception:  # noqa: BLE001
            self.ctype = "text/plain"
        try:
            self.disp = headers.get_content_disposition()
        except Exception:  # noqa: BLE001
            self.disp = None
        try:
            self.filename = headers.get_filename()
        except Exception:  # noqa: BLE001
            self.filename = None
        self.cte = _normalize_cte(headers)
        try:
            self.charset = headers.get_content_charset()
        except Exception:  # noqa: BLE001
            self.charset = None
        #: длина закодированного тела без переводов строк (для оценки размера)
        self.encoded_len = 0
        #: число знаков «=» в теле quoted-printable (каждый =XX — один байт)
        self.eq_count = 0
        #: потребитель выставляет True, если ему нужно тело этой части
        self.want = False

    def estimated_size(self) -> int:
        """Размер содержимого после декодирования (оценка без декодирования)."""
        if self.cte == "base64":
            return max(0, self.encoded_len * 3 // 4)
        if self.cte == "quoted-printable":
            return max(0, self.encoded_len - 2 * self.eq_count)
        return self.encoded_len


class _Reader:
    """Построчное чтение с учётом позиции и признака «начало строки»."""

    __slots__ = ("fh", "at_bol", "_back", "_peek")

    def __init__(self, fh) -> None:
        self.fh = fh
        self.at_bol = True
        self._back = None
        self._peek = b""

    def next(self):
        if self._back is not None:
            item, self._back = self._back, None
            return item
        if self._peek:
            line, self._peek = self._peek, b""
            if not line.endswith(b"\n"):
                line += self.fh.readline(_MAX_LINE)
        else:
            line = self.fh.readline(_MAX_LINE)
        if not line:
            return None
        if line.endswith(b"\r"):
            # Строку разрезал предел длины ровно между \r и \n: подбираем \n,
            # иначе \r попадал в содержимое части лишним байтом.
            nxt = self.fh.read(1)
            if nxt == b"\n":
                line += nxt
            elif nxt:
                self._peek = nxt
        bol = self.at_bol
        self.at_bol = line.endswith(b"\n")
        return line, bol

    def push_back(self, item) -> None:
        self._back = item


def _delimiter(line: bytes, bol: bool, stack: List[bytes]):
    """Строка-разделитель одной из границ стека → (уровень, закрывающий) или None.

    Формат тот же, что понимает email.feedparser: ``--граница``, необязательные
    ``--`` закрытия, пробелы/табуляции и перевод строки.
    """
    if not bol or not stack or not line.startswith(b"--"):
        return None
    body = line.rstrip(b"\r\n").rstrip(b" \t")
    for level in range(len(stack) - 1, -1, -1):
        sep = b"--" + stack[level]
        if body == sep:
            return level, False
        if body == sep + b"--":
            return level, True
    return None


def scan(fh) -> Iterator[Tuple[str, Leaf, bytes]]:
    """Обойти письмо. События:

    * ``("leaf", leaf, b"")`` — разобраны заголовки очередной конечной части;
      потребитель может выставить ``leaf.want = True``, чтобы получить тело;
    * ``("data", leaf, порция)`` — очередная порция тела (только для want);
    * ``("end", leaf, b"")`` — часть закончилась.

    Первое событие особое: ``("top", leaf_или_None, заголовки)`` — заголовки
    письма целиком (нужны для шапки). Для одиночного (не multipart) письма
    следом идёт обычное ``leaf``-событие для его тела.
    """
    rd = _Reader(fh)
    yield from _entity(rd, [], "text/plain", top=True)


def _read_headers(rd: _Reader, stack: List[bytes]) -> bytes:
    """Блок заголовков части. Сверх _MAX_HEADER сохраняются только Content-*.

    Блок бывает огромным (сотни строк Received, ARC, одна гигантская строка
    X-…): раньше всё сверх 256 КБ отбрасывалось, и тип с именем вложения,
    стоящие в конце, терялись вместе с самим вложением.
    """
    hdr = bytearray()
    cur_content = True
    total_cap = _MAX_HEADER * 4
    while True:
        item = rd.next()
        if item is None:
            break
        line, bol = item
        if bol and _delimiter(line, bol, stack):
            rd.push_back(item)            # заголовки оборвались разделителем
            break
        if bol and line[:1] in (b"\r", b"\n"):
            break                         # пустая строка — конец заголовков
        if bol and not _HEADER_RE.match(line):
            # Не похоже на заголовок: тело без разделяющей пустой строки
            # (так же поступает email.feedparser — дефект MissingHeaderBodySeparator).
            rd.push_back(item)
            break
        if bol and line[:1] not in (b" ", b"\t"):
            cur_content = line[:8].lower() == b"content-"
        if len(hdr) < _MAX_HEADER or (cur_content and len(hdr) < total_cap):
            if bol and hdr and not hdr.endswith(b"\n"):
                hdr += b"\r\n"          # предыдущую строку пришлось обрезать
            hdr += line
    return bytes(hdr)


def _entity(rd: _Reader, stack: List[bytes], default_type: str, top: bool = False):
    raw_headers = _read_headers(rd, stack)
    try:
        headers = _HEADER_PARSER.parsebytes(raw_headers)
    except Exception:  # noqa: BLE001
        headers = _HEADER_PARSER.parsebytes(b"")
    if top:
        yield ("top", None, headers)

    maintype = ""
    try:
        if headers.get("Content-Type") is None and default_type != "text/plain":
            maintype = default_type.split("/", 1)[0]
        else:
            maintype = headers.get_content_maintype()
    except Exception:  # noqa: BLE001
        maintype = ""
    boundary = None
    if maintype == "multipart":
        try:
            boundary = headers.get_boundary()
        except Exception:  # noqa: BLE001
            boundary = None

    if maintype == "multipart" and boundary:
        yield from _multipart(rd, stack, headers, boundary)
        return

    if maintype == "message":
        # Как в email.feedparser: любой message/* кроме rfc822 разбирается как
        # вложенное письмо, и обход идёт ВНУТРЬ него; message/delivery-status —
        # набор блоков заголовков без тел (ни текста, ни вложений). Только
        # message/rfc822 остаётся непрозрачной частью-вложением.
        try:
            subtype = headers.get_content_subtype() if headers.get("Content-Type") is not None \
                else default_type.split("/", 1)[1]
        except Exception:  # noqa: BLE001
            subtype = "rfc822"
        if subtype == "delivery-status":
            _skip_body(rd, stack)
            return
        if subtype != "rfc822":
            yield from _entity(rd, stack, "text/plain")
            return

    leaf = Leaf(headers, default_type, top)
    qp = leaf.cte == "quoted-printable"
    yield ("leaf", leaf, b"")
    pending_eol = b""
    # Порции копятся до _FLUSH байт: событие на каждую строку (у base64 —
    # 76 символов) давало миллионы переключений генератора на крупном вложении.
    out = bytearray()
    while True:
        item = rd.next()
        if item is None:
            break
        line, bol = item
        if bol and stack and line.startswith(b"--") and _delimiter(line, bol, stack):
            # перевод строки перед разделителем принадлежит разделителю (RFC 2046)
            rd.push_back(item)
            pending_eol = b""
            break
        if line.endswith(b"\r\n"):
            content, eol = line[:-2], b"\r\n"
        elif line.endswith(b"\n"):
            content, eol = line[:-1], b"\n"
        else:
            content, eol = line, b""
        leaf.encoded_len += len(content)
        if qp:
            leaf.eq_count += content.count(b"=")
        if leaf.want:
            if pending_eol:
                out += pending_eol
            out += content
            if len(out) >= _FLUSH:
                yield ("data", leaf, bytes(out))
                out.clear()
        elif out:
            out.clear()                  # потребитель отказался от тела на ходу
        pending_eol = eol
    if leaf.want:
        if pending_eol and not stack:
            # у одиночного письма тело идёт до конца файла вместе с последним переводом строки
            out += pending_eol
        if out:
            yield ("data", leaf, bytes(out))
    yield ("end", leaf, b"")


def _skip_body(rd: _Reader, stack: List[bytes]) -> None:
    """Пропустить тело до разделителя внешнего уровня или конца файла."""
    while True:
        item = rd.next()
        if item is None:
            return
        if _delimiter(item[0], item[1], stack):
            rd.push_back(item)
            return


def _multipart(rd: _Reader, stack: List[bytes], headers, boundary: str):
    sep = boundary.encode("utf-8", "surrogateescape")
    child_stack = stack + [sep]
    try:
        child_default = "message/rfc822" if headers.get_content_subtype() == "digest" else "text/plain"
    except Exception:  # noqa: BLE001
        child_default = "text/plain"
    while True:
        item = rd.next()
        if item is None:
            return                         # конец файла без закрывающей границы
        line, bol = item
        found = _delimiter(line, bol, child_stack)
        if found is None:
            continue                       # преамбула
        level, closing = found
        if level < len(child_stack) - 1:
            rd.push_back(item)             # граница внешнего уровня — неявно закрываемся
            return
        if closing:
            # эпилог — до границы внешнего уровня или конца файла
            while True:
                item = rd.next()
                if item is None:
                    return
                if _delimiter(item[0], item[1], stack):
                    rd.push_back(item)
                    return
        yield from _entity(rd, child_stack, child_default)


# ---------------------------------------------------------------------------
#  Декодеры Content-Transfer-Encoding, работающие порциями
# ---------------------------------------------------------------------------
_B64_KEEP = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_B64_DELETE = bytes(b for b in range(256) if b not in _B64_KEEP)


class Base64Stream:
    """Поблочное декодирование base64 — ровно по правилам модуля email.

    email декодирует base64 функцией ``binascii.a2b_base64`` в нестрогом режиме:
    посторонние символы пропускаются, а «=» считается концом данных, ТОЛЬКО
    если он дополняет начатую четвёрку (стоит на 3-й или 4-й позиции). Раньше
    здесь любой «=» считался концом, и одиночный «=» посреди base64 (или хвост
    вида «?id=1» в подписи рассылки) обрезал вложение — 11 МБ из 30. Состояние
    (неполная четвёрка и счётчик «=») переносится между порциями.
    """

    def __init__(self) -> None:
        self._buf = b""       # 0–3 символа начатой четвёрки
        self._pads = 0        # сколько «=» подряд после начатой четвёрки
        self._done = False

    def feed(self, data: bytes) -> bytes:
        if self._done or not data:
            return b""
        data = data.translate(None, _B64_DELETE)
        out = bytearray()
        pos = 0
        while pos < len(data):
            eq = data.find(b"=", pos)
            segment = data[pos:] if eq == -1 else data[pos:eq]
            if segment:
                self._pads = 0            # значимый символ обнуляет счёт «=»
                buf = self._buf + segment
                usable = len(buf) - len(buf) % 4
                if usable:
                    out += binascii.a2b_base64(buf[:usable])
                self._buf = buf[usable:]
            if eq == -1:
                break
            pos = eq + 1
            quad = len(self._buf)
            if quad >= 2:
                self._pads += 1
                if quad + self._pads >= 4:
                    # четвёрка дополнена — конец данных, дальше не читаем
                    out += binascii.a2b_base64(self._buf + b"=" * (4 - quad))
                    self._buf = b""
                    self._done = True
                    break
            # «=» на 1-й или 2-й позиции четвёрки — мусор, пропускаем
        return bytes(out)

    def close(self) -> bytes:
        if self._done or not self._buf:
            return b""
        rest, self._buf = self._buf, b""
        if len(rest) == 1:
            return b""                  # один лишний символ декодировать нельзя
        return binascii.a2b_base64(rest + b"=" * (4 - len(rest)))


class QuotedPrintableStream:
    """Поблочное декодирование quoted-printable (порции режутся по строкам)."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> bytes:
        data = self._buf + data
        cut = data.rfind(b"\n")
        if cut == -1:
            if len(data) > 1024 * 1024:     # патологически длинная строка
                # Режем так, чтобы не разорвать «=XX» и не оставить хвостовые
                # пробелы (их значение зависит от того, что идёт дальше).
                end = len(data)
                eq = data.rfind(b"=", max(0, end - 2))
                if eq != -1:
                    end = eq
                while end > 0 and data[end - 1:end] in (b" ", b"\t"):
                    end -= 1
                if end <= 0:
                    self._buf = data
                    return b""
                self._buf = data[end:]
                return binascii.a2b_qp(data[:end])
            self._buf = data
            return b""
        self._buf = data[cut + 1:]
        return binascii.a2b_qp(data[:cut + 1])

    def close(self) -> bytes:
        rest, self._buf = self._buf, b""
        return binascii.a2b_qp(rest) if rest else b""


class IdentityStream:
    def feed(self, data: bytes) -> bytes:
        return data

    def close(self) -> bytes:
        return b""


def decoder_for(cte: str):
    cte = (cte or "").strip().lower()
    if cte == "base64":
        return Base64Stream()
    if cte == "quoted-printable":
        return QuotedPrintableStream()
    return IdentityStream()


def iter_decoded(events: Iterator, leaf: Leaf) -> Iterator[bytes]:
    """Декодированное содержимое части ``leaf`` из потока событий ``scan``."""
    dec = decoder_for(leaf.cte)
    for kind, item, chunk in events:
        if item is not leaf:
            continue
        if kind == "data":
            out = dec.feed(chunk)
            if out:
                yield out
        elif kind == "end":
            break
    tail = dec.close()
    if tail:
        yield tail


#: Сколько вложений перечислять в карточке письма (остальные — только числом).
MAX_LISTED_ATTACHMENTS = 500
#: Сколько частей письма разбирать для просмотра. Письмо из сотен тысяч частей
#: (такое проходит и через почтовые серверы) раньше разбиралось минутами и
#: съедало гигабайт памяти на один просмотр.
MAX_PARTS = 20000


def summarize(fh, is_attachment: Callable, text_cap: int) -> Dict:
    """Один проход: заголовки, первые непустые текстовая и HTML-части (до
    ``text_cap`` байт закодированного тела каждая) и список вложений с оценкой
    размера. По вложению хранится только номер, имя, тип и размер — не весь
    объект заголовков части.
    """
    top_headers = None
    attachments: List[Dict] = []
    attachments_total = 0
    captured: Dict[str, bytearray] = {}
    capture_leaf: Dict[str, Leaf] = {}
    cut: Dict[str, bool] = {}
    done_types: set = set()
    listed: Dict[int, Dict] = {}          # id(leaf) → запись вложения (размер — в конце части)
    idx = 0
    parts = 0
    parts_truncated = False
    events = scan(fh)
    try:
        for kind, leaf, payload in events:
            if kind == "top":
                top_headers = payload
                continue
            if kind == "leaf":
                parts += 1
                if parts > MAX_PARTS:
                    parts_truncated = True
                    break
                if is_attachment(leaf.ctype, leaf.disp, leaf.filename):
                    leaf.want = False
                    attachments_total += 1
                    if len(attachments) < MAX_LISTED_ATTACHMENTS:
                        item = {"index": idx, "ctype": leaf.ctype, "filename": leaf.filename, "size": 0}
                        attachments.append(item)
                        listed[id(leaf)] = item
                    idx += 1
                elif leaf.ctype in ("text/plain", "text/html") and leaf.ctype not in done_types \
                        and leaf.ctype not in capture_leaf:
                    capture_leaf[leaf.ctype] = leaf
                    captured[leaf.ctype] = bytearray()
                    leaf.want = True
                continue
            if kind == "data":
                buf = captured.get(leaf.ctype)
                if buf is None or capture_leaf.get(leaf.ctype) is not leaf:
                    continue
                if len(buf) < text_cap:
                    buf += payload[: text_cap - len(buf)]
                    if len(buf) >= text_cap:
                        cut[leaf.ctype] = True
                else:
                    cut[leaf.ctype] = True
                    leaf.want = False       # дальше тело не нужно — не гоняем порции
                continue
            if kind == "end":
                item = listed.pop(id(leaf), None)
                if item is not None:
                    item["size"] = leaf.estimated_size()
                if capture_leaf.get(leaf.ctype) is leaf:
                    if bytes(captured[leaf.ctype]).strip(b" \t\r\n"):
                        done_types.add(leaf.ctype)
                    else:
                        # Пустая текстовая часть: как и модуль email, берём
                        # следующую непустую, а не показываем пустой текст.
                        del capture_leaf[leaf.ctype]
                        del captured[leaf.ctype]
                        cut.pop(leaf.ctype, None)
    finally:
        events.close()
    return {"headers": top_headers, "attachments": attachments, "attachments_total": attachments_total,
            "captured": captured, "capture_leaf": capture_leaf, "cut": cut,
            "parts_truncated": parts_truncated}
