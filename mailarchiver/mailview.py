"""
Разбор письма (RFC 822) для просмотра в веб-интерфейсе: заголовки, текстовая и
HTML-часть, список вложений. HTML отображается на стороне клиента в изолированном
iframe (sandbox) — скрипты не выполняются.
"""
from __future__ import annotations

import base64
import email
import re
import urllib.parse
from email import policy
from typing import Dict, List, Optional, Tuple

#: Предел размера письма, которое разбирается целиком, байт.
#: email.message_from_bytes + get_payload(decode=True) расходуют примерно
#: десятикратный размер файла: письмо с вложением на 100 МБ давало пик 1,5 ГБ,
#: и несколько таких открытий подряд роняли службу вместе с идущими бэкапами.
#: Письма крупнее показываются в «облегчённом» виде: заголовки, список вложений
#: и предложение скачать .eml целиком.
MAX_PARSE_BYTES = 25 * 1024 * 1024

#: Предел размера ОДНОГО вложения, отдаваемого в память при скачивании.
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024

#: Картинки, вложенные в само письмо (ссылки «cid:» в HTML), встраиваются в
#: ответ как data:-адреса: так их показывает песочница письма, которая не
#: пускает никаких внешних загрузок. Только растровые форматы (SVG может нести
#: сценарии) и с пределами размера.
INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/pjpeg", "image/gif",
                                "image/webp", "image/bmp"})
MAX_INLINE_IMAGE = 5 * 1024 * 1024
MAX_INLINE_TOTAL = 15 * 1024 * 1024
_CID_ATTR_RE = re.compile(r"""(?P<pre>\b(?:src|background)\s*=\s*)(?P<q>["']?)cid:(?P<cid>[^"'\s>]+)(?P=q)""",
                          re.IGNORECASE)
_CID_CSS_RE = re.compile(r"""url\(\s*(?P<q>["']?)cid:(?P<cid>[^"')\s]+)(?P=q)\s*\)""", re.IGNORECASE)


def _content_id(part) -> str:
    try:
        value = str(part.get("Content-ID") or "")
    except Exception:  # noqa: BLE001
        return ""
    return value.strip().strip("<>").strip()


def embed_cid_images(html: str, cid_parts: Dict[str, object]) -> Tuple[str, set]:
    """Заменить ссылки «cid:…» на data:-адреса картинок из того же письма.

    Возвращает ``(html, множество встроенных Content-ID)``.
    """
    if not html or not cid_parts or "cid:" not in html.lower():
        return html, set()
    budget = [MAX_INLINE_TOTAL]
    cache: Dict[str, Optional[str]] = {}
    lowered = {k.lower(): v for k, v in cid_parts.items()}

    def data_uri(raw_cid: str) -> Optional[str]:
        cid = urllib.parse.unquote(raw_cid).strip().strip("<>")
        key = cid.lower()
        if key in cache:
            return cache[key]
        part = cid_parts.get(cid) or lowered.get(key)
        uri = None
        if part is not None:
            ctype = (part.get_content_type() or "").lower()
            if ctype in INLINE_IMAGE_TYPES and _estimated_size(part) <= MAX_INLINE_IMAGE * 1.1:
                data = _decoded_payload(part)
                if data and len(data) <= MAX_INLINE_IMAGE and len(data) <= budget[0]:
                    budget[0] -= len(data)
                    uri = f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"
        cache[key] = uri
        return uri

    def repl_attr(m):
        uri = data_uri(m.group("cid"))
        return f'{m.group("pre")}"{uri}"' if uri else m.group(0)

    def repl_css(m):
        uri = data_uri(m.group("cid"))
        # Без кавычек: url() часто стоит внутри атрибута style="…", и лишние
        # кавычки разорвали бы его. В base64 нет пробелов и скобок.
        return f"url({uri})" if uri else m.group(0)

    html = _CID_ATTR_RE.sub(repl_attr, html)
    html = _CID_CSS_RE.sub(repl_css, html)
    return html, {k for k, v in cache.items() if v}


def _hdr(msg, name: str) -> str:
    try:
        v = msg[name]
        return str(v) if v is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def parse_message(raw: bytes, max_bytes: int = MAX_PARSE_BYTES) -> Dict:
    """Разобрать письмо. Возвращает заголовки, тело (text/html) и список вложений.

    Письма крупнее ``max_bytes`` разбираются только по заголовкам: полный разбор
    расходует около десятикратного объёма файла, и открытие одного письма с
    вложением в сотню мегабайт занимало полтора гигабайта памяти.
    """
    if max_bytes and len(raw) > max_bytes:
        return _headers_only(raw, len(raw), max_bytes)
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return {"headers": {}, "text": raw.decode("utf-8", "replace"), "html": "",
                "attachments": [], "truncated": False}

    headers = {
        "from": _hdr(msg, "From"),
        "to": _hdr(msg, "To"),
        "cc": _hdr(msg, "Cc"),
        "subject": _hdr(msg, "Subject"),
        "date": _hdr(msg, "Date"),
        "reply_to": _hdr(msg, "Reply-To"),
        "message_id": _hdr(msg, "Message-ID"),
    }
    text, html = "", ""
    attachments: List[Dict] = []
    cid_parts: Dict[str, object] = {}
    idx = 0
    for part in _iter_parts(msg):
        ctype = part.get_content_type()
        disp = part.get_content_disposition()  # 'attachment' | 'inline' | None
        filename = part.get_filename()
        cid = _content_id(part) if not (ctype or "").lower().startswith(("text/", "multipart/")) else ""
        if cid:
            cid_parts[cid] = part
        if _is_attachment(ctype, disp, filename):
            attachments.append({
                "index": idx,
                "filename": filename or _default_name(idx, ctype),
                "content_type": ctype,
                "size": _estimated_size(part),
                "content_id": cid,
            })
            idx += 1
        elif ctype == "text/plain" and not text.strip():
            # пустую (или из одних пробелов) часть пропускаем — берём следующую
            text = _get_text(part)
        elif ctype == "text/html" and not html.strip():
            html = _get_text(part)
    # если только HTML — оставим text пустым; клиент покажет HTML в песочнице
    embedded: set = set()
    if html and cid_parts:
        html, embedded = embed_cid_images(html, cid_parts)
    for item in attachments:
        # картинка уже показана в тексте письма — в списке вложений её можно не дублировать
        item["inline"] = bool(item.get("content_id")) and item["content_id"].lower() in embedded
    return {"headers": headers, "text": text, "html": html, "attachments": attachments,
            "truncated": False, "inline_images": len(embedded)}


def _headers_only(raw: bytes, size: int, max_bytes: int) -> Dict:
    """Облегчённый разбор большого письма: только заголовки, без тела."""
    import email.parser
    head = raw[:262144]
    try:
        msg = email.parser.BytesHeaderParser(policy=policy.default).parsebytes(head)
        headers = {
            "from": _hdr(msg, "From"), "to": _hdr(msg, "To"), "cc": _hdr(msg, "Cc"),
            "subject": _hdr(msg, "Subject"), "date": _hdr(msg, "Date"),
            "reply_to": _hdr(msg, "Reply-To"), "message_id": _hdr(msg, "Message-ID"),
        }
    except Exception:  # noqa: BLE001
        headers = {}
    limit_mb = max_bytes // (1024 * 1024)
    return {
        "headers": headers, "text": "", "html": "", "attachments": [],
        "truncated": True, "size": size,
        "notice": (f"Письмо слишком большое для показа в браузере "
                   f"(предел {limit_mb} МБ). Скачайте его целиком в виде .eml — "
                   f"вложения находятся внутри файла."),
    }



def _is_attachment(ctype: str, disp: Optional[str], filename: Optional[str]) -> bool:
    """Считать ли часть письма вложением.

    Раньше условие было ``disp == "attachment" or (filename and не text/*)``, и
    часть БЕЗ Content-Disposition и БЕЗ имени файла (просто
    ``Content-Type: application/pdf``) не попадала никуда — в карточке письма
    значилось «вложений нет», хотя файл в архиве есть.
    """
    if disp == "attachment":
        return True
    ctype = (ctype or "").lower()
    if filename and not ctype.startswith("text/"):
        return True
    if disp is None and ctype and not ctype.startswith("text/") and not ctype.startswith("multipart/"):
        # неименованная часть без Content-Disposition: картинка, pdf, rfc822
        return True
    return False


def _default_name(idx: int, ctype: str) -> str:
    if (ctype or "").lower() == "message/rfc822":
        return f"forwarded_{idx}.eml"
    ext = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
           "image/gif": ".gif", "application/zip": ".zip"}.get((ctype or "").lower(), "")
    return f"attachment_{idx}{ext}"


def _iter_parts(msg):
    """Обойти части письма, НЕ заходя внутрь приложенных писем.

    ``msg.walk()`` спускается внутрь ``message/rfc822`` и подмешивает вложения
    пересланного письма в общий список — нумерация переставала соответствовать
    тому, что видит пользователь, а само пересланное письмо (его чаще всего и
    нужно скачать) в списке не появлялось вовсе.
    """
    stack = [msg]
    while stack:
        part = stack.pop(0)
        ctype = (part.get_content_type() or "").lower()
        if ctype == "message/rfc822":
            yield part
            continue
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                stack = list(payload) + stack
            continue
        yield part


def _rfc822_bytes(part) -> bytes:
    """Байты приложенного письма (message/rfc822) для скачивания."""
    try:
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            return inner[0].as_bytes()
        if hasattr(inner, "as_bytes"):
            return inner.as_bytes()
    except Exception:  # noqa: BLE001
        pass
    try:
        return part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001
        return b""


def _estimated_size(part) -> int:
    """
    Оценить размер вложения, НЕ декодируя его в память: письмо с вложением на
    сотни мегабайт иначе уложило бы сервис (decode=True создаёт полную копию).
    Точный размер отдаётся только при реальном скачивании — см. get_attachment().
    """
    if (part.get_content_type() or "").lower() == "message/rfc822":
        # У приложенного письма payload — список объектов Message, и общая
        # ветка ниже давала размер 0: в карточке письма пересланное письмо
        # числилось пустым, хотя скачивалось нормально.
        return len(_rfc822_bytes(part))
    try:
        payload = part.get_payload(decode=False)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(payload, (str, bytes, bytearray)):
        return 0
    encoded_len = len(payload)
    enc = str(part.get("Content-Transfer-Encoding", "") or "").strip().lower()
    if enc == "base64":
        # 4 символа base64 = 3 байта данных (переводы строк дают небольшой запас)
        return max(0, encoded_len * 3 // 4)
    # для 7bit/8bit/binary размер совпадает, для quoted-printable это оценка сверху
    return encoded_len


def _decoded_payload(part) -> Optional[bytes]:
    """Содержимое части, декодированное так же, как потоковым разбором.

    Модуль email сравнивает Content-Transfer-Encoding как есть: «base64 »
    (с пробелом) или «base64 (комментарий)» он не декодирует и отдаёт
    закодированный текст. Нормализуем, как это делает потоковый путь, — иначе
    одно и то же письмо показывалось бы по-разному в зависимости от размера.
    """
    from . import mimestream
    try:
        raw_cte = str(part.get("Content-Transfer-Encoding", "") or "").lower()
    except Exception:  # noqa: BLE001
        raw_cte = ""
    norm = mimestream._normalize_cte(part)
    if norm in ("base64", "quoted-printable") and raw_cte != norm:
        try:
            body = part.get_payload(decode=False)
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, str):
            data = body.encode("ascii", "surrogateescape")
            dec = mimestream.decoder_for(norm)
            return dec.feed(data) + dec.close()
    try:
        return part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return None


def _get_text(part) -> str:
    """Извлечь текст части, устойчиво к отсутствию/неверной кодировке."""
    payload = _decoded_payload(part)
    if payload is None:
        try:
            return part.get_content() or ""
        except Exception:  # noqa: BLE001
            return ""
    return decode_text_bytes(payload, part.get_content_charset())


def decode_text_bytes(payload: bytes, charset: Optional[str]) -> str:
    """Байты текстовой части → строка с учётом объявленной кодировки."""
    if charset:
        try:
            # Кодировка ОБЪЯВЛЕНА — декодируем именно ею, битые байты заменяем.
            # Перебор здесь недопустим: koi8-r (и latin-1) успешно «декодируют»
            # любые 256 байт, и одно испорченное UTF-8 письмо превратилось бы в
            # сплошную абракадабру вместо пары символов-заменителей.
            return payload.decode(charset, "replace")
        except LookupError:
            pass  # кодировка неизвестна Python — переходим к перебору
    # charset не указан или неизвестен — пробуем распространённые кодировки
    for enc in ("utf-8", "cp1251", "koi8-r", "latin-1"):
        try:
            return payload.decode(enc, "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def get_attachment(raw: bytes, index: int,
                   max_bytes: int = MAX_ATTACHMENT_BYTES) -> Optional[Tuple[str, str, bytes]]:
    """Вернуть (имя, тип, содержимое) вложения по его порядковому номеру.

    ``max_bytes`` ограничивает размер письма, из которого вообще достаётся
    вложение: разбор занимает около десятикратного объёма файла.
    """
    if max_bytes and len(raw) > max_bytes:
        raise ValueError(f"Письмо слишком большое для извлечения вложения "
                         f"({len(raw) // (1024 * 1024)} МБ). Скачайте письмо целиком (.eml).")
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return None
    idx = 0
    for part in _iter_parts(msg):
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        filename = part.get_filename()
        if _is_attachment(ctype, disp, filename):
            if idx == index:
                if ctype == "message/rfc822":
                    payload = _rfc822_bytes(part)
                else:
                    payload = _decoded_payload(part) or b""
                return (filename or _default_name(idx, ctype), ctype, payload)
            idx += 1
    return None


# ===========================================================================
#  Просмотр и скачивание по ИСТОЧНИКУ письма (файлу), а не по байтам в памяти
# ===========================================================================
#: Сколько байт закодированного текста/HTML показывать у крупного письма.
LARGE_TEXT_CAP = 2 * 1024 * 1024
#: Размер порции при потоковой отдаче.
STREAM_CHUNK = 256 * 1024


def summarize_source(open_fn, size: int, max_bytes: Optional[int] = None) -> Dict:
    """Разобрать письмо для просмотра.

    ``open_fn()`` возвращает новый двоичный поток с содержимым письма, ``size`` —
    его размер. Обычные письма разбираются модулем ``email`` целиком, крупные —
    потоковым разбором (память порядка одной строки): у них теперь есть и
    список вложений, и начало текста, а не одни заголовки.
    """
    if max_bytes is None:
        max_bytes = MAX_PARSE_BYTES
    if not max_bytes or size <= max_bytes:
        with open_fn() as fh:
            raw = fh.read()
        return parse_message(raw, max_bytes=0)
    return _summarize_large(open_fn, size)


def _summarize_large(open_fn, size: int) -> Dict:
    from . import mimestream
    with open_fn() as fh:
        res = mimestream.summarize(fh, _is_attachment, LARGE_TEXT_CAP)
    top = res["headers"]
    headers = {}
    if top is not None:
        headers = {
            "from": _hdr(top, "From"), "to": _hdr(top, "To"), "cc": _hdr(top, "Cc"),
            "subject": _hdr(top, "Subject"), "date": _hdr(top, "Date"),
            "reply_to": _hdr(top, "Reply-To"), "message_id": _hdr(top, "Message-ID"),
        }
    texts = {}
    for ctype, buf in res["captured"].items():
        leaf = res["capture_leaf"][ctype]
        dec = mimestream.decoder_for(leaf.cte)
        data = dec.feed(bytes(buf)) + dec.close()
        texts[ctype] = decode_text_bytes(data, leaf.charset)
    attachments = []
    for item in res["attachments"]:
        attachments.append({
            "index": item["index"],
            "filename": item["filename"] or _default_name(item["index"], item["ctype"]),
            "content_type": item["ctype"],
            "size": item["size"],
        })
    out = {"headers": headers, "text": texts.get("text/plain", ""), "html": texts.get("text/html", ""),
           "attachments": attachments, "truncated": False, "large": True, "size": size}
    notes = []
    if res["cut"]:
        cap_mb = LARGE_TEXT_CAP // (1024 * 1024)
        notes.append(f"Письмо крупное ({size // (1024 * 1024)} МБ): показано только начало "
                     f"текста (до {cap_mb} МБ). Полностью — в файле .eml.")
    hidden = int(res.get("attachments_total", len(attachments))) - len(attachments)
    if hidden > 0:
        notes.append(f"Показаны первые {len(attachments)} вложений из {res['attachments_total']} — "
                     f"остальные есть в файле .eml.")
    if res.get("parts_truncated"):
        notes.append(f"В письме слишком много частей: разобраны первые {mimestream.MAX_PARTS}. "
                     f"Полностью письмо — в файле .eml.")
    if notes:
        out["notice"] = " ".join(notes)
    return out


def attachment_from_source(open_fn, size: int, index: int, max_bytes: Optional[int] = None):
    """Вложение по номеру: ``(имя, тип, итератор_порций, длина_или_None)`` или None.

    Номера совпадают с :func:`summarize_source` для того же письма: путь
    разбора выбирается по тому же размеру.
    """
    if max_bytes is None:
        max_bytes = MAX_PARSE_BYTES
    if not max_bytes or size <= max_bytes:
        with open_fn() as fh:
            raw = fh.read()
        att = get_attachment(raw, index, max_bytes=0)
        del raw
        if att is None:
            return None
        name, ctype, data = att
        return name, ctype, _slices(data), len(data)
    return _attachment_large(open_fn, index)


def _slices(data: bytes):
    view = memoryview(data)
    for pos in range(0, len(view), STREAM_CHUNK):
        yield bytes(view[pos:pos + STREAM_CHUNK])


def _attachment_large(open_fn, index: int):
    from . import mimestream
    # Первый проход — найти часть и её метаданные (имя, тип); тело не читается.
    target = None
    idx = 0
    with open_fn() as fh:
        for kind, leaf, _payload in mimestream.scan(fh):
            if kind != "leaf":
                continue
            if _is_attachment(leaf.ctype, leaf.disp, leaf.filename):
                if idx == index:
                    target = leaf
                    break
                idx += 1
    if target is None:
        return None
    name = target.filename or _default_name(index, target.ctype)
    ctype = target.ctype

    def _gen():
        # Второй проход — поток содержимого нужной части. Файл открывается
        # заново: объект первого прохода уже закрыт, а gzip/шифрованный поток
        # всё равно перемотать дешевле, чем держать всё в памяти.
        position = 0
        with open_fn() as fh:
            events = mimestream.scan(fh)
            try:
                for kind, leaf, _payload in events:
                    if kind != "leaf":
                        continue
                    if _is_attachment(leaf.ctype, leaf.disp, leaf.filename):
                        if position == index:
                            leaf.want = True
                            for chunk in mimestream.iter_decoded(events, leaf):
                                yield chunk
                            return
                        position += 1
            finally:
                events.close()

    return name, ctype, _gen(), None


def iter_source(open_fn, chunk: int = STREAM_CHUNK):
    """Поток байт письма целиком (для скачивания .eml)."""
    with open_fn() as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            yield data


def open_source_stream(open_fn, chunk: int = STREAM_CHUNK):
    """Как :func:`iter_source`, но файл открывается и первая порция читается
    СРАЗУ, до отправки заголовков ответа.

    Иначе ошибка (нет ключа шифрования, файл пропал, повреждён заголовок)
    возникала уже после «200 OK» с объявленным Content-Length: браузер получал
    обрыв передачи вместо понятного сообщения.
    """
    fh = open_fn()
    try:
        first = fh.read(chunk)
    except BaseException:
        fh.close()
        raise

    def _gen():
        try:
            data = first
            while data:
                yield data
                data = fh.read(chunk)
        finally:
            fh.close()

    return _gen()
