"""
Поддержка OAuth2 (XOAUTH2) для провайдеров, отключивших вход по паролю
(Gmail, Microsoft 365). Access-токен получается по refresh-токену на стороне
сервиса. Refresh-токен пользователь получает заранее (см. документацию,
docs/ru/04-configuration.md, раздел «OAuth2»).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple

from ..errors import ImapAuthError

# Стандартные конечные точки токенов
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def build_xoauth2_string(user: str, access_token: str) -> str:
    """Сформировать строку авторизации XOAUTH2 (для imapclient.oauth2_login)."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def refresh_access_token(token_url: str, client_id: str, client_secret: str, refresh_token: str,
                         timeout: int = 30) -> Tuple[str, int]:
    """
    Обновить access-токен по refresh-токену (grant_type=refresh_token).
    Возвращает (access_token, expires_in_seconds).
    """
    if not token_url:
        raise ImapAuthError("Не задан URL конечной точки токена OAuth2.",
                            hint="Укажите oauth_token_url (Google/Microsoft) в настройках ящика.")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise ImapAuthError(
            f"OAuth2: сервер токенов вернул ошибку {exc.code}.",
            hint=f"Проверьте client_id/secret и refresh_token. Ответ: {body[:300]}",
            cause=exc,
        ) from exc
    except urllib.error.URLError as exc:
        raise ImapAuthError(f"OAuth2: не удалось соединиться с сервером токенов: {exc.reason}", cause=exc) from exc

    access = payload.get("access_token")
    if not access:
        raise ImapAuthError("OAuth2: сервер не вернул access_token.", hint=str(payload)[:300])
    return access, int(payload.get("expires_in", 3600))
