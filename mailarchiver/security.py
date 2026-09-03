"""
Безопасность: хеширование паролей пользователей, шифрование секретов ящиков,
подпись cookie сессий.

  * Пароли пользователей веб-интерфейса хранятся как PBKDF2-HMAC-SHA256
    (стандартная библиотека, соль на пользователя, 240 000 итераций).
  * Пароли/токены IMAP-ящиков шифруются симметрично (Fernet, AES-128-CBC +
    HMAC) ключом, производным от секретного ключа приложения. Это защищает
    данные «в покое»: даже при доступе к БД пароли нельзя прочитать без
    файла secret.key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .errors import AuthError

_PBKDF2_ROUNDS = 240_000
_PBKDF2_ALGO = "sha256"


# ---------------------------------------------------------------------------
# Пароли пользователей
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Вернуть строку вида ``pbkdf2$<rounds>$<salt_hex>$<hash_hex>``."""
    if not password:
        raise AuthError("Пароль не может быть пустым.")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против сохранённого хеша (защита от timing-атак)."""
    try:
        scheme, rounds_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, (password or "").encode("utf-8"), salt, rounds)
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Шифрование секретов ящиков (Fernet)
# ---------------------------------------------------------------------------

class SecretBox:
    """Обёртка над Fernet для шифрования паролей/токенов IMAP."""

    def __init__(self, app_secret: bytes) -> None:
        # Производим 32-байтовый ключ Fernet из секрета приложения.
        digest = hashlib.sha256(b"mailarchiver-fernet-v1:" + app_secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: Optional[str]) -> str:
        if plaintext is None:
            plaintext = ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: Optional[str]) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise AuthError(
                "Не удалось расшифровать сохранённый секрет ящика.",
                hint="Вероятно, изменился файл secret.key. Введите пароль ящика заново.",
                cause=exc,
            ) from exc


# ---------------------------------------------------------------------------
# Подпись значений (например, токен сессии) — HMAC
# ---------------------------------------------------------------------------

def sign_value(value: str, app_secret: bytes) -> str:
    mac = hmac.new(app_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def unsign_value(signed: str, app_secret: bytes) -> Optional[str]:
    try:
        value, mac = signed.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(app_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(mac, expected):
        return value
    return None


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def check_password_policy(password: str, min_length: int = 8) -> Optional[str]:
    """Вернуть текст ошибки, если пароль не проходит политику, иначе None."""
    if len(password) < min_length:
        return f"Пароль слишком короткий (минимум {min_length} символов)."
    if password.lower() in {"password", "12345678", "admin123", "qwerty12", "пароль123"}:
        return "Пароль слишком простой — выберите другой."
    return None
