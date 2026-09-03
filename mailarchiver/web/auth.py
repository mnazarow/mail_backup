"""
Аутентификация веб-интерфейса: сессии в БD, cookie с подписью, защита от
подбора пароля (блокировка после N неудач), первичная настройка администратора.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response

from ..errors import AuthError
from ..security import (
    check_password_policy,
    hash_password,
    new_token,
    sign_value,
    unsign_value,
    verify_password,
)

COOKIE_NAME = "ma_session"


def get_services(request: Request):
    return request.app.state.services


def _now():
    return datetime.now(timezone.utc)


def current_user(request: Request) -> Optional[dict]:
    """Вернуть словарь пользователя по cookie сессии или None."""
    services = request.app.state.services
    if not bool(services.rt("security", "auth_enabled")):
        return {"id": 0, "username": "admin", "role": "admin", "anonymous_auth_disabled": True}
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    token = unsign_value(raw, services.cfg.secret_key())
    if not token:
        return None
    row = services.db.get_session(token)
    if row is None:
        return None
    # проверка срока и бездействия
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        last_seen = datetime.fromisoformat(row["last_seen"])
    except (ValueError, TypeError):
        services.db.delete_session(token)
        return None
    idle_min = int(services.rt("security", "session_idle_minutes") or 60)
    if _now() > expires or (_now() - last_seen) > timedelta(minutes=idle_min):
        services.db.delete_session(token)
        return None
    services.db.touch_session(token)
    role = (row["role"] or "admin")
    if role == "mailbox":
        # сессия пользователя, вошедшего по учётным данным ящика
        acc = services.db.get_account(row["account_id"])
        if acc is None or not acc.enabled:
            services.db.delete_session(token)
            return None
        return {"id": 0, "username": acc.username, "role": "mailbox",
                "account_id": row["account_id"], "account_name": acc.name}
    user = services.db.get_user_by_id(row["user_id"])
    if user is None or user["disabled"]:
        services.db.delete_session(token)
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


def create_session(services, response: Response, user_id: int, request: Request,
                   role: str = "admin", account_id=None) -> str:
    token = new_token()
    ttl_hours = int(services.rt("security", "session_ttl_hours") or 12)
    expires = (_now() + timedelta(hours=ttl_hours)).isoformat()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    services.db.create_session(token, user_id, expires, ip, ua, role=role, account_id=account_id)
    signed = sign_value(token, services.cfg.secret_key())
    secure = bool(services.rt("security", "secure_cookie"))
    response.set_cookie(COOKIE_NAME, signed, httponly=True, samesite="lax", secure=secure,
                        max_age=ttl_hours * 3600, path="/")
    return token


def destroy_session(services, request: Request, response: Response) -> None:
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        token = unsign_value(raw, services.cfg.secret_key())
        if token:
            services.db.delete_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")


def do_login(services, request: Request, response: Response, username: str, password: str) -> dict:
    """Проверить учётные данные с защитой от подбора и создать сессию."""
    username = (username or "").strip()
    max_attempts = int(services.rt("security", "max_login_attempts") or 5)
    lockout_min = int(services.rt("security", "lockout_minutes") or 15)
    since = (_now() - timedelta(minutes=lockout_min)).isoformat()
    failures = services.db.count_recent_failures(username, since)
    if failures >= max_attempts:
        raise AuthError(f"Слишком много неудачных попыток. Повторите через {lockout_min} мин.",
                        hint="Это защита от подбора пароля.")
    user = services.db.get_user_by_name(username)
    ip = request.client.host if request.client else ""
    # 1) вход администратора/пользователя веб-интерфейса
    if user is not None and not user["disabled"] and verify_password(password, user["password_hash"]):
        services.db.record_login_attempt(username, True, ip)
        services.db.clear_login_failures(username)
        services.db.set_last_login(user["id"])
        services.db.add_audit(username, "login", ip)
        create_session(services, response, user["id"], request, role=user["role"])
        return {"id": user["id"], "username": user["username"], "role": user["role"]}
    # 2) вход по учётным данным почтового ящика (проверка через IMAP)
    acc = _try_mailbox_login(services, username, password)
    if acc is not None:
        services.db.record_login_attempt(username, True, ip)
        services.db.clear_login_failures(username)
        services.db.add_audit(username, "login_mailbox", f"{ip} account={acc.id}")
        create_session(services, response, 0, request, role="mailbox", account_id=acc.id)
        return {"id": 0, "username": acc.username, "role": "mailbox",
                "account_id": acc.id, "account_name": acc.name}
    services.db.record_login_attempt(username, False, ip)
    services.db.add_audit(username, "login_failed", ip)
    raise AuthError("Неверный логин или пароль.",
                    hint="Для входа по ящику используйте email и пароль ящика; ящик должен быть добавлен и включён.")


def _try_mailbox_login(services, username: str, password: str):
    """Проверить учётные данные почтового ящика через IMAP. Вернуть Account или None."""
    from ..models import Account, AuthType
    from ..imap.client import ImapConnection
    acc = services.db.get_account_by_username(username)
    if acc is None or not acc.enabled or acc.auth_type != AuthType.PASSWORD or not password:
        return None
    trial = Account(name=acc.name, host=acc.host, port=acc.port, username=acc.username,
                    password=password, auth_type=AuthType.PASSWORD, security=acc.security)
    try:
        with ImapConnection(trial, services.connect_options()):
            pass
        return acc
    except Exception:  # noqa: BLE001
        return None


def create_first_admin(services, username: str, password: str) -> dict:
    if services.db.count_users() > 0:
        raise AuthError("Администратор уже создан.")
    username = (username or "").strip()
    if not username:
        raise AuthError("Укажите имя пользователя.")
    policy = check_password_policy(password, int(services.rt("security", "min_password_length") or 8))
    if policy:
        raise AuthError(policy)
    uid = services.db.create_user(username, hash_password(password), role="admin")
    services.db.add_audit(username, "create_admin", "")
    return {"id": uid, "username": username, "role": "admin"}
