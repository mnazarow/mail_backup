"""
Аутентификация веб-интерфейса: сессии в БD, cookie с подписью, защита от
подбора пароля (блокировка после N неудач), первичная настройка администратора.
"""
from __future__ import annotations

import sqlite3
import threading
import time
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

#: Во сколько раз порог на сам источник (все имена пользователей с одного IP)
#: выше обычного: один адрес, перебирающий логины, блокирует только себя.
#: Порог с запасом — за NAT или обратным прокси с одного адреса приходят разные
#: люди (сам адрес берётся из request.client.host: при server.behind_proxy
#: uvicorn подставляет туда настоящий адрес клиента из X-Forwarded-For).
_IP_ATTEMPTS_FACTOR = 10
#: Во сколько раз выше порог на учётную запись ЦЕЛИКОМ (защита от
#: распределённого перебора). Превышение НЕ блокирует вход: оно лишь замедляет
#: ответ на неудачную попытку — иначе ботнет снова «выключал» бы чужую учётку.
_ACCOUNT_ATTEMPTS_FACTOR = 20
#: Шаг и потолок такой задержки, секунд (потолок нужен, чтобы сама задержка не
#: съедала пул потоков).
_THROTTLE_STEP_S = 0.1
_THROTTLE_MAX_S = 2.0

#: Сколько проверок входа по ящику (блокирующее IMAP-подключение с таймаутами
#: до 30/120 с) выполняется ОДНОВРЕМЕННО. Без ограничения сотня запросов
#: /api/login с валидным email и мусорным паролем занимает весь пул потоков и
#: весь REST API перестаёт отвечать без всякой аутентификации.
_MAILBOX_LOGIN_SLOTS = 4
_mailbox_login_sem = threading.BoundedSemaphore(_MAILBOX_LOGIN_SLOTS)


def get_services(request: Request):
    return request.app.state.services


def _now():
    return datetime.now(timezone.utc)


def current_user(request: Request) -> Optional[dict]:
    """Вернуть словарь пользователя по cookie сессии или None."""
    return user_from_cookies(request.app.state.services, request.cookies)


def user_from_cookies(services, cookies) -> Optional[dict]:
    """Пользователь по набору cookie.

    Вынесено отдельно от :func:`current_user`, чтобы ту же полноценную проверку
    сессии (срок, бездействие, роль, отключённый пользователь/ящик) мог
    выполнять и WebSocket, у которого нет объекта ``Request``.
    """
    if not bool(services.rt("security", "auth_enabled")):
        return {"id": 0, "username": "admin", "role": "admin", "anonymous_auth_disabled": True}
    raw = cookies.get(COOKIE_NAME) if cookies else None
    if not raw:
        return None
    token = unsign_value(raw, services.cfg.secret_key())
    if not token:
        return None
    return user_from_token(services, token)


def user_from_token(services, token: str) -> Optional[dict]:
    """Проверить сессию по токену и вернуть пользователя (или None)."""
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


def _brute_force_guard(services, username: str, ip: str) -> float:
    """Защита от подбора пароля. Вернуть задержку ответа на НЕУДАЧУ, секунд.

    Блокируется конкретный источник, а не сама учётная запись: раньше счёт шёл
    только по имени пользователя, и пять неверных попыток с чужого адреса
    выключали вход настоящему администратору на весь период блокировки (а
    повторяя их, его можно было держать заблокированным бесконечно).

      * пара «имя пользователя + IP» — обычный порог (max_login_attempts);
      * сам источник по всем именам — порог в _IP_ATTEMPTS_FACTOR раз выше
        (перебор логинов с одного адреса блокирует только этот адрес);
      * учётная запись целиком — порог в _ACCOUNT_ATTEMPTS_FACTOR раз выше и
        БЕЗ блокировки: только нарастающая задержка неудачного ответа, чтобы
        распределённый перебор был медленным, но чужой вход не закрывался.
    """
    max_attempts = int(services.rt("security", "max_login_attempts") or 5)
    lockout_min = int(services.rt("security", "lockout_minutes") or 15)
    since = (_now() - timedelta(minutes=lockout_min)).isoformat()
    if services.db.count_recent_failures(username, since, ip=ip) >= max_attempts:
        raise AuthError(f"Слишком много неудачных попыток с этого адреса. Повторите через {lockout_min} мин.",
                        hint="Это защита от подбора пароля; блокируется только текущий источник.")
    if services.db.count_recent_failures_by_ip(ip, since) >= max_attempts * _IP_ATTEMPTS_FACTOR:
        raise AuthError(f"Слишком много неудачных попыток с этого адреса. Повторите через {lockout_min} мин.",
                        hint="Это защита от перебора имён пользователей с одного адреса.")
    account_failures = services.db.count_recent_failures(username, since)
    over = account_failures - max_attempts * _ACCOUNT_ATTEMPTS_FACTOR
    if over < 0:
        return 0.0
    return min(_THROTTLE_MAX_S, _THROTTLE_STEP_S * (over + 1))


def do_login(services, request: Request, response: Response, username: str, password: str) -> dict:
    """Проверить учётные данные с защитой от подбора и создать сессию."""
    username = (username or "").strip()
    ip = request.client.host if request.client else ""
    throttle = _brute_force_guard(services, username, ip)
    user = services.db.get_user_by_name(username)
    # 1) вход администратора/пользователя веб-интерфейса
    if user is not None and not user["disabled"] and verify_password(password, user["password_hash"]):
        services.db.record_login_attempt(username, True, ip)
        services.db.clear_login_failures(username, ip=ip)
        services.db.set_last_login(user["id"])
        services.db.add_audit(username, "login", ip)
        create_session(services, response, user["id"], request, role=user["role"])
        return {"id": user["id"], "username": user["username"], "role": user["role"]}
    # 2) вход по учётным данным почтового ящика (проверка через IMAP).
    #    Факт попытки фиксируем ДО обращения к IMAP: проверка занимает до 30/120 с,
    #    и если писать неудачу только после неё, сотня одновременных запросов
    #    проходит проверку лимита разом (счётчик ещё пуст) и занимает весь пул
    #    потоков. Успешный вход эту запись тут же убирает.
    services.db.record_login_attempt(username, False, ip)
    try:
        acc = _try_mailbox_login(services, username, password)
    except AuthError:
        services.db.add_audit(username, "login_busy", ip)
        raise
    if acc is not None:
        services.db.record_login_attempt(username, True, ip)
        services.db.clear_login_failures(username, ip=ip)
        services.db.add_audit(username, "login_mailbox", f"{ip} account={acc.id}")
        create_session(services, response, 0, request, role="mailbox", account_id=acc.id)
        return {"id": 0, "username": acc.username, "role": "mailbox",
                "account_id": acc.id, "account_name": acc.name}
    services.db.add_audit(username, "login_failed", ip)
    if throttle:
        # замедляем только неудачный ответ: правильный пароль проходит сразу,
        # поэтому чужие попытки не мешают владельцу учётной записи войти
        time.sleep(throttle)
    raise AuthError("Неверный логин или пароль.",
                    hint="Для входа по ящику используйте email и пароль ящика; ящик должен быть добавлен и включён.")


def _try_mailbox_login(services, username: str, password: str):
    """Проверить учётные данные почтового ящика через IMAP. Вернуть Account или None.

    Одновременных проверок не больше :data:`_MAILBOX_LOGIN_SLOTS`: подключение
    блокирующее и долгое, а свободных слотов нет — сразу отказ (AuthError), а не
    ожидание. Иначе поток запросов на /api/login выводит из строя весь REST API.
    Дешёвая проверка по БД идёт ДО захвата слота, чтобы обычный неверный логин
    слоты не занимал.
    """
    from ..models import Account, AuthType
    from ..imap.client import ImapConnection
    acc = services.db.get_account_by_username(username)
    if acc is None or not acc.enabled or acc.auth_type != AuthType.PASSWORD or not password:
        return None
    if not _mailbox_login_sem.acquire(blocking=False):
        raise AuthError("Сейчас выполняется слишком много проверок входа по ящику. Повторите через несколько секунд.",
                        hint="Ограничение одновременных подключений к IMAP защищает сервис от перегрузки.")
    try:
        trial = Account(name=acc.name, host=acc.host, port=acc.port, username=acc.username,
                        password=password, auth_type=AuthType.PASSWORD, security=acc.security)
        try:
            with ImapConnection(trial, services.connect_options()):
                pass
            return acc
        except Exception:  # noqa: BLE001
            return None
    finally:
        _mailbox_login_sem.release()


def create_first_admin(services, username: str, password: str) -> dict:
    if services.db.count_users() > 0:
        raise AuthError("Администратор уже создан.")
    username = (username or "").strip()
    if not username:
        raise AuthError("Укажите имя пользователя.")
    policy = check_password_policy(password, int(services.rt("security", "min_password_length") or 8))
    if policy:
        raise AuthError(policy)
    # Проверка «пользователей ещё нет» и вставка выполняются одним оператором SQL
    # (плюс UNIQUE(username)): два одновременных POST /api/setup на свежей
    # установке больше не создают двух администраторов — второй получит отказ.
    try:
        uid = services.db.create_first_user(username, hash_password(password), role="admin")
    except sqlite3.IntegrityError:
        raise AuthError("Такое имя пользователя уже занято.",
                        hint="Первичная настройка уже выполнена — войдите под существующей учётной записью.")
    if uid is None:
        raise AuthError("Администратор уже создан.",
                        hint="Первичная настройка уже выполнена — войдите под существующей учётной записью.")
    services.db.add_audit(username, "create_admin", "")
    return {"id": uid, "username": username, "role": "admin"}
