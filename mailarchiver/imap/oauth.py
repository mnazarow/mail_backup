"""
Поддержка OAuth2 (XOAUTH2) для провайдеров, отключивших вход по паролю
(Gmail, Microsoft 365). Access-токен получается по refresh-токену на стороне
сервиса. Refresh-токен пользователь получает заранее (см. документацию,
docs/ru/04-configuration.md, раздел «OAuth2»).
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ImapAuthError, ImapConnectionError

# Стандартные конечные точки токенов
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def _safe_oauth_error(exc) -> str:
    """Короткое описание ошибки из тела ответа БЕЗ секретов.

    Наружу отдаём только стандартные поля error/error_description; всё
    остальное (id_token, новый refresh_token) остаётся внутри.
    """
    try:
        body = exc.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    code = str(data.get("error") or "")[:80]
    desc = str(data.get("error_description") or "")[:200]
    if code and desc:
        return f"Ответ: {code} — {desc}"
    return f"Ответ: {code or desc}" if (code or desc) else ""


def build_xoauth2_string(user: str, access_token: str) -> str:
    """Сформировать строку авторизации XOAUTH2 (для imapclient.oauth2_login)."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def refresh_access_token(token_url: str, client_id: str, client_secret: str, refresh_token: str,
                         timeout: int = 30, with_refresh: bool = False):
    """
    Обновить access-токен по refresh-токену (grant_type=refresh_token).

    Возвращает ``(access_token, expires_in_seconds)``, а при ``with_refresh=True``
    — ``(access_token, expires_in_seconds, new_refresh_token_or_None)``:
    Microsoft 365 выдаёт новый refresh-токен при каждом обновлении, и его нужно
    сохранить, иначе через несколько месяцев вход перестанет работать.

    Ошибки делятся на два вида: временные (сервер токенов недоступен, HTTP 5xx
    и 429, таймаут) — :class:`ImapConnectionError`, задание повторится; и
    ошибки настройки (неверные client_id/secret, отозванный токен) —
    :class:`ImapAuthError`, повторять бессмысленно.
    """
    if not token_url:
        raise ImapAuthError("Не задан URL конечной точки токена OAuth2.",
                            hint="Укажите oauth_token_url (Google/Microsoft) в настройках ящика.")
    # Только HTTPS: по http client_secret и refresh_token ушли бы открытым текстом.
    if not str(token_url).lower().startswith("https://"):
        raise ImapAuthError("URL конечной точки токена OAuth2 должен начинаться с https://",
                            hint="По http секрет клиента и refresh-токен передавались бы открытым текстом.")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST",
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1024 * 1024)
            ctype = str(resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        # В теле ответа могут быть id_token/новый refresh_token, а текст ошибки
        # уходит и в API, и в лог, и в события задания. Наружу отдаём только
        # машиночитаемый код ошибки OAuth2 (error/error_description).
        if exc.code >= 500 or exc.code == 429:
            raise ImapConnectionError(
                f"OAuth2: сервер токенов временно недоступен (HTTP {exc.code}).",
                hint="Это сбой на стороне почтового провайдера; задание повторится. "
                     + _safe_oauth_error(exc),
                cause=exc,
            ) from exc
        raise ImapAuthError(
            f"OAuth2: сервер токенов вернул ошибку {exc.code}.",
            hint="Проверьте client_id/secret и refresh_token. " + _safe_oauth_error(exc),
            cause=exc,
        ) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ImapConnectionError("OAuth2: сервер токенов не ответил вовремя.",
                                      hint="Проверьте доступ сервера архива в интернет; задание повторится.",
                                      cause=exc) from exc
        raise ImapConnectionError(f"OAuth2: не удалось соединиться с сервером токенов: {reason}",
                                  hint="Проверьте доступ сервера архива в интернет (прокси, файрвол, DNS).",
                                  cause=exc) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ImapConnectionError("OAuth2: сервер токенов не ответил вовремя.",
                                  hint="Проверьте доступ сервера архива в интернет; задание повторится.",
                                  cause=exc) from exc
    except OSError as exc:
        raise ImapConnectionError(f"OAuth2: обрыв связи с сервером токенов: {exc}",
                                  hint="Задание повторится.", cause=exc) from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        looks_html = "html" in ctype or body.lstrip()[:1] == b"<"
        raise ImapAuthError(
            "OAuth2: сервер токенов ответил не в формате JSON"
            + (" (пришла HTML-страница)." if looks_html else "."),
            hint="Проверьте адрес oauth_token_url: он должен вести на конечную точку токена "
                 "(Google: https://oauth2.googleapis.com/token, Microsoft: "
                 "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token). HTML-страницу "
                 "обычно отдаёт прокси или страница входа, а не сервер токенов.")

    access = payload.get("access_token")
    if not access:
        # Раньше в подсказку клался ВЕСЬ ответ сервера токенов — вместе с
        # id_token и новым refresh_token, которые затем попадали в логи.
        keys = ", ".join(sorted(str(k) for k in payload)[:10]) or "—"
        raise ImapAuthError("OAuth2: сервер не вернул access_token.",
                            hint=f"Поля ответа: {keys}. Проверьте область доступа (scope) приложения.")
    try:
        expires = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires = 3600
    if with_refresh:
        new_refresh = payload.get("refresh_token")
        return access, expires, (str(new_refresh) if new_refresh else None)
    return access, expires
