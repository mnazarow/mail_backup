"""
Одноразовые коды TOTP (RFC 6238) для двухфакторного входа.

Совместимо со всеми распространёнными приложениями-аутентификаторами:
Яндекс Ключ, Google Authenticator, Microsoft Authenticator, FreeOTP, KeePassXC.
Параметры стандартные: HMAC-SHA1, шаг 30 секунд, 6 цифр.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import List, Optional
from urllib.parse import quote

STEP_S = 30
DIGITS = 6
#: Сколько соседних шагов принимать: часы телефона и сервера расходятся.
WINDOW = 1
#: Длина секрета в байтах (160 бит — рекомендация RFC 4226).
SECRET_BYTES = 20

_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # без 0/o, 1/l/i
_ASCII_DIGITS = frozenset("0123456789")
_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
#: Длиннее этого введённый код не разбираем (пробелы и дефисы — с запасом).
MAX_CODE_LEN = 64


def new_secret() -> str:
    """Новый секрет в base32 (как его показывают приложения)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    clean = (secret_b32 or "").replace(" ", "").upper()
    clean += "=" * (-len(clean) % 8)
    return base64.b32decode(clean)


def code_at(secret_b32: str, step: int, digits: int = DIGITS) -> str:
    """Код для номера шага ``step`` (время // 30)."""
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def verify(secret_b32: str, code: str, *, last_step: int = 0,
           now: Optional[float] = None, window: int = WINDOW) -> Optional[int]:
    """Проверить код. Вернуть номер совпавшего шага или None.

    Шаги не новее ``last_step`` отвергаются: код, уже использованный для входа,
    нельзя предъявить второй раз (подсмотренный или перехваченный код не
    работает повторно в пределах своих 30 секунд).
    """
    # Только ASCII-цифры: str.isdigit() пропускал «１２３４５６» и «²», а
    # hmac.compare_digest на не-ASCII строке падает TypeError (ответ 500, и
    # попытка не засчитывалась). Слишком длинный ввод — сразу мимо.
    code = code or ""
    if len(code) > MAX_CODE_LEN:
        return None
    digits = "".join(ch for ch in code if ch in _ASCII_DIGITS)
    if len(digits) != DIGITS or not secret_b32:
        return None
    base = current_step(now)
    for delta in range(-window, window + 1):
        step = base + delta
        if step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret_b32, step), digits):
            return step
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str = "MailArchiver") -> str:
    """Ссылка otpauth:// — её кодирует QR-код для приложения."""
    label = quote(f"{issuer}:{account}", safe="")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


def new_recovery_codes(count: int = 10) -> List[str]:
    """Резервные коды вида ``abcde-fghjk`` (≈ 50 бит каждый)."""
    out = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def normalize_recovery(code: str) -> str:
    code = code or ""
    if len(code) > MAX_CODE_LEN:
        return ""
    return "".join(ch for ch in code.lower() if ch in _ASCII_ALNUM)


def looks_like_recovery(code: str) -> bool:
    norm = normalize_recovery(code)
    return len(norm) == 10 and not norm.isdigit()
