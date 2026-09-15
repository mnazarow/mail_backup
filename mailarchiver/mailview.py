"""
Разбор письма (RFC 822) для просмотра в веб-интерфейсе: заголовки, текстовая и
HTML-часть, список вложений. HTML отображается на стороне клиента в изолированном
iframe (sandbox) — скрипты не выполняются.
"""
from __future__ import annotations

import email
from email import policy
from typing import Dict, List, Optional, Tuple


def _hdr(msg, name: str) -> str:
    try:
        v = msg[name]
        return str(v) if v is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def parse_message(raw: bytes) -> Dict:
    """Разобрать письмо. Возвращает заголовки, тело (text/html) и список вложений."""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return {"headers": {}, "text": raw.decode("utf-8", "replace"), "html": "", "attachments": []}

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
    idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()  # 'attachment' | 'inline' | None
        filename = part.get_filename()
        is_attachment = (disp == "attachment") or (filename and not ctype.startswith("text/"))
        if is_attachment:
            attachments.append({
                "index": idx,
                "filename": filename or f"attachment_{idx}",
                "content_type": ctype,
                "size": _estimated_size(part),
            })
            idx += 1
        elif ctype == "text/plain" and not text:
            text = _get_text(part)
        elif ctype == "text/html" and not html:
            html = _get_text(part)
    # если только HTML — оставим text пустым; клиент покажет HTML в песочнице
    return {"headers": headers, "text": text, "html": html, "attachments": attachments}


def _estimated_size(part) -> int:
    """
    Оценить размер вложения, НЕ декодируя его в память: письмо с вложением на
    сотни мегабайт иначе уложило бы сервис (decode=True создаёт полную копию).
    Точный размер отдаётся только при реальном скачивании — см. get_attachment().
    """
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


def _get_text(part) -> str:
    """Извлечь текст части, устойчиво к отсутствию/неверной кодировке."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        try:
            return part.get_content() or ""
        except Exception:  # noqa: BLE001
            return ""
    charset = part.get_content_charset()
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


def get_attachment(raw: bytes, index: int) -> Optional[Tuple[str, str, bytes]]:
    """Вернуть (имя, тип, содержимое) вложения по его порядковому номеру."""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return None
    idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        filename = part.get_filename()
        is_attachment = (disp == "attachment") or (filename and not ctype.startswith("text/"))
        if is_attachment:
            if idx == index:
                try:
                    payload = part.get_payload(decode=True) or b""
                except Exception:  # noqa: BLE001
                    payload = b""
                return (filename or f"attachment_{idx}", ctype, payload)
            idx += 1
    return None
