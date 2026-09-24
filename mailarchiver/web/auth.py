"""
Аутентификация веб-интерфейса: сессии в БD, cookie с подписью, защита от
подбора пароля (блокировка после N неудач), первичная настройка администратора.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import HTTPException, Request, Response

from ..errors import AuthError
from ..logging_setup import get_logger
from .proxy import client_ip
from ..security import (
    check_password_policy,
    hash_password,
    new_token,
    sign_value,
    unsign_value,
    verify_password,
)

COOKIE_NAME = "ma_session"

log = get_logger("auth")

#: Во сколько раз порог на сам источник (все имена пользователей с одного IP)
#: выше обычного: один адрес, перебирающий логины, блокирует только себя.
#: Порог с запасом — за NAT или обратным прокси с одного адреса приходят разные
#: люди. Сам адрес берёт :func:`mailarchiver.web.proxy.client_ip`: при
#: server.behind_proxy это первый СПРАВА недоверенный хоп X-Forwarded-For
#: (подделать его нельзя — цепочку слева дописывает клиент), иначе адрес
#: TCP-соединения.
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

#: Неудачных проверок пароля ОДНОГО ящика на IMAP-сервере за lockout_minutes
#: (в max_login_attempts раз). Дальше пароль этого ящика на почтовом сервере не
#: проверяется до конца окна: иначе страница входа служила бы распределённым
#: «перебиральщиком» паролей почты, а сотни неудачных входов с адреса
#: MailArchiver — поводом для защиты почтового сервера (fail2ban) заблокировать
#: сам MailArchiver, и остановились бы все резервные копии.
_MAILBOX_ACCOUNT_FACTOR = 2
#: То же по ВСЕМ ящикам вместе (в max_login_attempts раз) — защита от перебора
#: «по одному паролю на каждый ящик».
_MAILBOX_GLOBAL_FACTOR = 12

#: Второй шаг входа (код 2FA). Злоумышленнику здесь уже известен пароль, поэтому
#: лимит на учётную запись — настоящая блокировка ввода кодов из приложения, а не
#: задержка, как у пароля: без неё распределённый перебор со ста адресов
#: подбирал шестизначный код за сутки с вероятностью ≈13 %. Резервные коды эта
#: блокировка не затрагивает: их не перебрать (≈50 бит на код), и владелец
#: войдёт ими даже во время атаки.
_OTP_WINDOW_FACTOR = 2      # неверных кодов за lockout_minutes: max_login_attempts × 2
_OTP_DAILY_FACTOR = 10      # неверных кодов за сутки:           max_login_attempts × 10
#: Попыток ввода кода по одному «билету» второго шага; дальше — снова пароль.
OTP_TICKET_ATTEMPTS = 3


def get_services(request: Request):
    return request.app.state.services


def _now():
    return datetime.now(timezone.utc)


#: Заголовок фоновых запросов интерфейса (запасной опрос состояния, ход
#: заданий): такие запросы НЕ продлевают сессию. Иначе «Тайм-аут бездействия»
#: не срабатывал никогда, пока вкладка просто открыта.
BACKGROUND_HEADER = "x-ma-background"


def current_user(request: Request) -> Optional[dict]:
    """Вернуть словарь пользователя по cookie сессии или None."""
    touch = request.headers.get(BACKGROUND_HEADER, "") != "1"
    return user_from_cookies(request.app.state.services, request.cookies, touch=touch)


def user_from_cookies(services, cookies, touch: bool = True) -> Optional[dict]:
    """Пользователь по набору cookie.

    Вынесено отдельно от :func:`current_user`, чтобы ту же полноценную проверку
    сессии (срок, бездействие, роль, отключённый пользователь/ящик) мог
    выполнять и WebSocket, у которого нет объекта ``Request``. ``touch=False`` —
    проверить, не продлевая сессию (фоновые запросы, перепроверка WebSocket).
    """
    if not bool(services.rt("security", "auth_enabled")):
        return {"id": 0, "username": "admin", "role": "admin", "anonymous_auth_disabled": True}
    raw = cookies.get(COOKIE_NAME) if cookies else None
    if not raw or not isinstance(raw, str) or len(raw) > 4096:
        return None
    token = unsign_value(raw, services.cfg.secret_key())
    if not token:
        return None
    return user_from_token(services, token, touch=touch)


def user_from_token(services, token: str, touch: bool = True) -> Optional[dict]:
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
    if touch:
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
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "totp_enabled": bool(_row_get(user, "totp_enabled", 0))}


def _row_get(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


#: Пути, доступные администратору, которому по настройкам обязателен
#: двухфакторный вход, но который его ещё не включил: всё, что нужно, чтобы
#: включить 2FA или выйти, и ничего больше.
_ENROLL_PATHS = ("/api/me", "/api/logout", "/api/help", "/api/needs-setup")


def two_factor_required(services, user: dict) -> bool:
    """Обязан ли этот пользователь включить 2FA прежде чем работать."""
    if not user or user.get("role") != "admin" or user.get("anonymous_auth_disabled"):
        return False
    if user.get("totp_enabled"):
        return False
    try:
        return bool(services.rt("security", "require_2fa"))
    except Exception:  # noqa: BLE001
        return False


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    if two_factor_required(request.app.state.services, user):
        path = request.url.path
        if not (path in _ENROLL_PATHS or path.startswith("/api/me/2fa")):
            raise HTTPException(status_code=403, detail={
                "error": True, "code": "2fa_required",
                "message": "Для работы необходимо включить двухфакторный вход.",
                "hint": "Так требует настройка «Обязательный двухфакторный вход» (раздел «Безопасность»)."})
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
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    services.db.create_session(token, user_id, expires, ip, ua, role=role, account_id=account_id)
    signed = sign_value(token, services.cfg.secret_key())
    secure = bool(services.rt("security", "secure_cookie"))
    if not secure:
        # Если ТЕКУЩИЙ запрос пришёл по https, помечаем cookie Secure: иначе
        # сессия уехала бы по http при первом же обращении к голому адресу.
        # Схему берём именно у запроса, а не из server.public_url: с настройкой
        # вход по http (SSH-туннель на 127.0.0.1 — рекомендуемый аварийный
        # способ) молча ломался бы — сервер отвечает «вход выполнен», а браузер
        # выбрасывает Secure-cookie на http-странице.
        try:
            secure = str(request.url.scheme or "").lower() in ("https", "wss")
        except Exception:  # noqa: BLE001
            secure = False
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


def guard_password_check(services, username: str, ip: str) -> None:
    """Проверка пароля ВНЕ страницы входа (например, при отключении 2FA): те же
    блокировки по источнику, что и у входа. Бросает :class:`AuthError`."""
    _brute_force_guard(services, username, ip)


def do_login(services, request: Request, response: Response, username: str, password: str) -> dict:
    """Проверить учётные данные с защитой от подбора и создать сессию."""
    username = (username or "").strip()
    ip = client_ip(request)
    throttle = _brute_force_guard(services, username, ip)
    user = services.db.get_user_by_name(username)
    # 1) вход администратора/пользователя веб-интерфейса
    if user is not None and not user["disabled"] and verify_password(password, user["password_hash"]):
        if _row_get(user, "totp_enabled", 0):
            # Пароль верен, но нужен второй фактор. Сессию не создаём и
            # счётчик неудач НЕ сбрасываем: иначе, чередуя верный пароль с
            # подбором кода, можно было бы перебирать коды без блокировки.
            services.db.add_audit(username, "login_password_ok", ip)
            return {"otp_required": True, "challenge": _make_challenge(services, user, ip),
                    "username": user["username"]}
        services.db.record_login_attempt(username, True, ip)
        services.db.clear_login_failures(username, ip=ip)
        services.db.set_last_login(user["id"])
        services.db.add_audit(username, "login", ip)
        create_session(services, response, user["id"], request, role=user["role"])
        return {"id": user["id"], "username": user["username"], "role": user["role"],
                "totp_enabled": False}
    # 2) вход по учётным данным почтового ящика (проверка через IMAP).
    #    Факт попытки фиксируем ДО обращения к IMAP: проверка занимает до 30/120 с,
    #    и если писать неудачу только после неё, сотня одновременных запросов
    #    проходит проверку лимита разом (счётчик ещё пуст) и занимает весь пул
    #    потоков. Успешный вход эту запись тут же убирает.
    candidate = _mailbox_candidate(services, username, password)
    attempt_id = services.db.record_login_attempt(username, False, ip,
                                                  kind="imap" if candidate is not None else "")
    acc = None
    if candidate is not None:
        refusal = _mailbox_limit_reached(services, username)
        if refusal:
            services.db.add_audit(username, "login_mailbox_limited", ip)
            raise AuthError(refusal, hint="Это защита от перебора паролей почты: проверка на почтовом "
                                          "сервере временно приостановлена. Администраторов с отдельной "
                                          "учётной записью это не касается.")
        try:
            acc = _try_mailbox_login(services, candidate, password)
        except AuthError:
            # проверка не выполнялась (нет свободных слотов) — это не неудача пароля
            services.db.delete_login_attempt(attempt_id)
            services.db.add_audit(username, "login_busy", ip)
            raise
    if acc is not None:
        services.db.delete_login_attempt(attempt_id)
        services.db.record_login_attempt(username, True, ip, kind="imap")
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


#: Сколько секунд даётся на ввод кода после верного пароля.
OTP_CHALLENGE_TTL_S = 300


def _pw_fingerprint(password_hash: str) -> str:
    """Отпечаток хеша пароля: билет второго шага действителен, только пока
    пароль не менялся."""
    import hashlib
    return hashlib.sha256(("otp-ticket|" + (password_hash or "")).encode("utf-8")).hexdigest()[:32]


def _make_challenge(services, user, ip: str) -> str:
    """Подписанный «билет» второго шага: пользователь, срок, адрес.

    Без него второй шаг пришлось бы делать повторной отправкой пароля. Билет
    действует 5 минут, только с того же адреса, ОДНОРАЗОВЫЙ (гасится при
    успешном входе), с ограниченным числом попыток и отзывается сменой
    пароля: его состояние хранится в БД (таблица otp_challenges).
    """
    expires = int(time.time()) + OTP_CHALLENGE_TTL_S
    nonce = new_token(16)
    services.db.create_otp_challenge(nonce, user["id"], expires, _pw_fingerprint(user["password_hash"]))
    payload = f"otp|{user['id']}|{expires}|{ip}|{nonce}"
    return sign_value(payload, services.cfg.secret_key())


def _open_challenge(services, challenge: str, ip: str):
    """``(user_id, nonce)`` из подписанного билета или None (подделан, истёк, чужой адрес)."""
    value = unsign_value(challenge or "", services.cfg.secret_key())
    if not value:
        return None
    try:
        kind, user_id, expires, bound_ip, nonce = value.split("|", 4)
        if kind != "otp" or int(expires) < int(time.time()) or bound_ip != ip or not nonce:
            return None
        return int(user_id), nonce
    except ValueError:
        return None


def _otp_lock_reason(services, username: str) -> Optional[str]:
    """Текст отказа, если ввод кодов из приложения для учётной записи заблокирован.

    Считаются неудачи второго шага со ВСЕХ адресов (попытка текущего запроса
    уже записана — отсюда строгое «больше»).
    """
    max_attempts = int(services.rt("security", "max_login_attempts") or 5)
    lockout_min = int(services.rt("security", "lockout_minutes") or 15)
    now = _now()
    recent = services.db.count_recent_failures(
        username, (now - timedelta(minutes=lockout_min)).isoformat(), kind="otp")
    if recent > max_attempts * _OTP_WINDOW_FACTOR:
        return (f"Слишком много неверных кодов для этой учётной записи — ввод кодов из приложения "
                f"заблокирован на {lockout_min} мин.")
    daily = services.db.count_recent_failures(username, (now - timedelta(hours=24)).isoformat(), kind="otp")
    if daily > max_attempts * _OTP_DAILY_FACTOR:
        return ("Слишком много неверных кодов для этой учётной записи за сутки — ввод кодов из "
                "приложения заблокирован до конца суток.")
    return None


_otp_notified_lock = threading.Lock()


def _notify_otp_attack(services, username: str, ip: str) -> None:
    """Сообщить администраторам: пароль учётной записи, по-видимому, известен
    постороннему (перебирают код второго шага). Не чаще раза в окно блокировки.

    Время последнего уведомления хранится в базе (meta), а не в памяти: после
    перезапуска службы то же самое письмо не уходило бы повторно.
    """
    try:
        lockout_s = 60 * int(services.rt("security", "lockout_minutes") or 15)
        now = time.time()
        meta_key = "otp_notified:" + username.lower()[:200]
        with _otp_notified_lock:
            try:
                last = float(services.db.get_meta(meta_key) or 0)
            except (TypeError, ValueError):
                last = 0.0
            if last and now - last < lockout_s:
                return
            services.db.set_meta(meta_key, str(now))
        log.warning("Подбор кода 2FA для учётной записи %s (адрес %s): ввод кодов заблокирован.", username, ip)
        notifier = getattr(services, "notifier", None)
        if notifier is not None:
            notifier.notify_job_async(
                "security", "failed", f"[MailArchiver] Подбор кода 2FA: {username}",
                f"Для учётной записи «{username}» много раз введён неверный код двухфакторного входа "
                f"(последний адрес: {ip}). Пароль этой учётной записи, по-видимому, известен "
                f"постороннему: смените его. Ввод кодов из приложения временно заблокирован; "
                f"владелец может войти резервным кодом.")
    except Exception:  # noqa: BLE001 — уведомление не должно мешать ответу
        pass


# ---------------------------------------------------------------------------
#  Журнал безопасности: действующие блокировки и их снятие
# ---------------------------------------------------------------------------
#: Действия аудита, которые показываются в журнале безопасности.
SECURITY_AUDIT_ACTIONS = (
    "login", "login_failed", "login_password_ok", "login_2fa", "login_2fa_failed", "login_2fa_locked",
    "login_mailbox", "login_mailbox_limited", "login_busy", "2fa_enabled", "2fa_disabled", "2fa_reset",
    "2fa_code_failed", "2fa_disable_bad_password", "2fa_recovery_regenerated", "user_password",
    "user_logout_all", "user_disable", "user_create", "user_delete", "create_admin",
    "account_logout_sessions", "security_unblock", "search")


def security_overview(services) -> dict:
    """Действующие блокировки (по тем же правилам, что у входа) и последние попытки."""
    db = services.db
    max_attempts = int(services.rt("security", "max_login_attempts") or 5)
    lockout_min = int(services.rt("security", "lockout_minutes") or 15)
    now = _now()
    window = (now - timedelta(minutes=lockout_min)).isoformat()
    day = (now - timedelta(hours=24)).isoformat()
    blocks = []
    for r in db.query("SELECT LOWER(username) AS u, MAX(username) AS name, ip, COUNT(*) AS c, MAX(ts) AS last "
                      "FROM login_attempts WHERE success=0 AND ts>=? GROUP BY LOWER(username), ip", (window,)):
        if r["c"] >= max_attempts:
            blocks.append({"kind": "pair", "username": r["name"], "ip": r["ip"], "failures": r["c"],
                           "last": r["last"], "text": f"Вход «{r['name']}» с адреса {r['ip'] or '?'} заблокирован"})
    for r in db.query("SELECT ip, COUNT(*) AS c, MAX(ts) AS last FROM login_attempts WHERE success=0 AND ts>=? "
                      "AND ip<>'' GROUP BY ip", (window,)):
        if r["c"] >= max_attempts * _IP_ATTEMPTS_FACTOR:
            blocks.append({"kind": "ip", "username": "", "ip": r["ip"], "failures": r["c"], "last": r["last"],
                           "text": f"Адрес {r['ip']} заблокирован целиком (перебор имён пользователей)"})
    otp_window = {r["u"]: r for r in db.query(
        "SELECT LOWER(username) AS u, MAX(username) AS name, COUNT(*) AS c, MAX(ts) AS last FROM login_attempts "
        "WHERE success=0 AND kind='otp' AND ts>=? GROUP BY LOWER(username)", (window,))}
    for r in db.query("SELECT LOWER(username) AS u, MAX(username) AS name, COUNT(*) AS c, MAX(ts) AS last "
                      "FROM login_attempts WHERE success=0 AND kind='otp' AND ts>=? GROUP BY LOWER(username)", (day,)):
        recent = otp_window.get(r["u"])
        if (recent is not None and recent["c"] > max_attempts * _OTP_WINDOW_FACTOR) \
                or r["c"] > max_attempts * _OTP_DAILY_FACTOR:
            blocks.append({"kind": "otp", "username": r["name"], "ip": "", "failures": r["c"], "last": r["last"],
                           "text": f"Ввод кодов 2FA для «{r['name']}» заблокирован (резервные коды работают)"})
    imap_total = 0
    for r in db.query("SELECT LOWER(username) AS u, MAX(username) AS name, COUNT(*) AS c, MAX(ts) AS last "
                      "FROM login_attempts WHERE success=0 AND kind='imap' AND ts>=? GROUP BY LOWER(username)",
                      (window,)):
        imap_total += r["c"]
        if r["c"] > max_attempts * _MAILBOX_ACCOUNT_FACTOR:
            blocks.append({"kind": "imap", "username": r["name"], "ip": "", "failures": r["c"], "last": r["last"],
                           "text": f"Проверка пароля ящика «{r['name']}» на почтовом сервере приостановлена"})
    if imap_total > max_attempts * _MAILBOX_GLOBAL_FACTOR:
        blocks.append({"kind": "imap_all", "username": "", "ip": "", "failures": imap_total, "last": "",
                       "text": "Вход сотрудников по паролю ящика приостановлен для всех (много неудач)"})
    recent = [dict(r) for r in db.query(
        "SELECT ts, username, ip, kind, success FROM login_attempts ORDER BY id DESC LIMIT 200")]
    top_ips = [dict(r) for r in db.query(
        "SELECT ip, COUNT(*) AS failures, COUNT(DISTINCT LOWER(username)) AS users, MAX(ts) AS last "
        "FROM login_attempts WHERE success=0 AND ts>=? AND ip<>'' GROUP BY ip ORDER BY failures DESC LIMIT 10",
        (day,))]
    week = (now - timedelta(days=7)).isoformat()
    marks = ",".join("?" * len(SECURITY_AUDIT_ACTIONS))
    counts = {r["action"]: int(r["c"]) for r in db.query(
        f"SELECT action, COUNT(*) AS c FROM audit WHERE ts>=? AND action IN ({marks}) GROUP BY action",
        (week, *SECURITY_AUDIT_ACTIONS))}
    events = [dict(r) for r in db.query(
        f"SELECT ts, user, action, detail FROM audit WHERE action IN ({marks}) ORDER BY id DESC LIMIT 200",
        tuple(SECURITY_AUDIT_ACTIONS))]
    return {"blocks": blocks, "recent_attempts": recent, "top_ips": top_ips, "week_counts": counts,
            "events": events, "limits": {"max_login_attempts": max_attempts, "lockout_minutes": lockout_min}}


def security_unblock(services, kind: str, username: str = "", ip: str = "") -> int:
    """Снять блокировку: удалить записи о неудачах, из-за которых она действует."""
    db = services.db
    if kind == "pair" and username:
        cur = db.execute("DELETE FROM login_attempts WHERE success=0 AND username=? COLLATE NOCASE AND ip=?",
                         (username, ip or ""))
    elif kind == "ip" and ip:
        cur = db.execute("DELETE FROM login_attempts WHERE success=0 AND ip=?", (ip,))
    elif kind in ("otp", "imap") and username:
        cur = db.execute("DELETE FROM login_attempts WHERE success=0 AND kind=? AND username=? COLLATE NOCASE",
                         (kind, username))
    elif kind == "imap_all":
        cur = db.execute("DELETE FROM login_attempts WHERE success=0 AND kind='imap'")
    else:
        raise AuthError("Непонятно, какую блокировку снять.")
    return int(cur.rowcount or 0)


def check_second_factor(services, user_id: int, username: str, ip: str, code: str, *,
                        allow_recovery: bool = True, state: Optional[dict] = None) -> Tuple[Optional[str], int]:
    """Проверить код второго фактора: ``(способ | None, id записи о попытке)``.

    Способ — 'totp' или 'recovery'; None — код неверный (неудача уже записана).
    Попытка записывается ДО проверки, поэтому залп параллельных запросов не
    проверит больше кодов, чем позволяет лимит. При успехе запись о неудаче
    удаляет вызывающий (или она остаётся — если вход дальше не состоялся).

    Бросает :class:`AuthError`, если ввод кодов из приложения для учётной
    записи заблокирован или секрет TOTP не расшифровывается (резервные коды
    хранятся как хеши и работают в обоих случаях).
    """
    from .. import totp as totp_mod
    if state is None:
        state = services.db.totp_state(user_id) or {}
    attempt_id = services.db.record_login_attempt(username, False, ip, kind="otp")
    if allow_recovery and totp_mod.looks_like_recovery(code):
        norm = totp_mod.normalize_recovery(code)
        for stored in list(state.get("recovery") or []):
            if verify_password(norm, stored) and services.db.consume_totp_recovery(user_id, stored):
                return "recovery", attempt_id
        return None, attempt_id
    locked = _otp_lock_reason(services, username)
    if locked:
        services.db.add_audit(username, "login_2fa_locked", ip)
        _notify_otp_attack(services, username, ip)
        raise AuthError(locked, hint=("Войдите резервным кодом — они продолжают действовать. " if allow_recovery else "")
                        + "Если коды вводили не вы, ваш пароль известен постороннему: смените его.")
    if not state.get("secret"):
        services.db.delete_login_attempt(attempt_id)
        raise AuthError("Код из приложения сейчас проверить нельзя: секрет двухфакторного входа "
                        "не расшифровывается.",
                        hint="Так бывает после замены или потери файла secret.key. Войдите резервным "
                             "кодом либо попросите администратора отключить 2FA командой "
                             "«mailarchiver reset-2fa -u имя» на сервере.")
    step = totp_mod.verify(state["secret"], code, last_step=state.get("last_step", 0))
    if step is not None and services.db.claim_totp_step(user_id, step):
        return "totp", attempt_id
    return None, attempt_id


def do_login_otp(services, request: Request, response: Response, challenge: str, code: str) -> dict:
    """Второй шаг входа: код из приложения или резервный код."""
    ip = client_ip(request)
    ticket = _open_challenge(services, challenge, ip)
    if ticket is None:
        raise AuthError("Время на ввод кода истекло. Войдите заново.",
                        hint="После ввода пароля код нужно ввести в течение 5 минут.")
    user_id, nonce = ticket
    user = services.db.get_user_by_id(user_id)
    if user is None or user["disabled"]:
        services.db.drop_otp_challenge(nonce)
        raise AuthError("Учётная запись недоступна.")
    username = user["username"]
    throttle = _brute_force_guard(services, username, ip)
    # Попытка по билету засчитывается ДО проверки кода и атомарно: залп
    # параллельных запросов с одним билетом не получит лишних попыток.
    taken = services.db.take_otp_attempt(nonce, user_id, OTP_TICKET_ATTEMPTS)
    if taken is None:
        services.db.drop_otp_challenge(nonce)
        raise AuthError("Время на ввод кода истекло или попытки исчерпаны. Войдите заново.",
                        hint=f"На один вход даётся {OTP_TICKET_ATTEMPTS} попытки ввода кода; "
                             f"после этого снова введите пароль.")
    ticket_pw, attempt_no = taken
    if ticket_pw != _pw_fingerprint(user["password_hash"]):
        services.db.drop_otp_challenge(nonce)
        raise AuthError("Пароль учётной записи изменился. Войдите заново.")
    state = services.db.totp_state(user_id) or {}
    if not state.get("enabled"):
        services.db.drop_otp_challenge(nonce)
        raise AuthError("Двухфакторный вход для этой учётной записи отключён. Войдите заново.")
    how, attempt_id = check_second_factor(services, user_id, username, ip, code, state=state)
    if how is None:
        services.db.add_audit(username, "login_2fa_failed", ip)
        if throttle:
            time.sleep(throttle)
        if attempt_no >= OTP_TICKET_ATTEMPTS:
            services.db.drop_otp_challenge(nonce)
            raise AuthError("Неверный код подтверждения. Попытки для этого входа исчерпаны — войдите заново.",
                            hint="Проверьте, что время на телефоне установлено точно (автоматически).")
        raise AuthError("Неверный код подтверждения.",
                        hint="Введите 6 цифр из приложения-аутентификатора или один из резервных кодов.")
    services.db.drop_otp_challenge(nonce)
    services.db.delete_login_attempt(attempt_id)
    services.db.record_login_attempt(username, True, ip, kind="otp")
    services.db.clear_login_failures(username, ip=ip)
    services.db.set_last_login(user_id)
    services.db.add_audit(username, "login_2fa" if how == "totp" else "login_2fa_recovery", ip)
    create_session(services, response, user_id, request, role=user["role"])
    left = None
    if how == "recovery":
        left = len((services.db.totp_state(user_id) or {}).get("recovery") or [])
    return {"id": user_id, "username": username, "role": user["role"], "totp_enabled": True,
            "recovery_left": left}


def _mailbox_candidate(services, username: str, password: str):
    """Ящик, в который можно войти по его паролю, или None (дешёвая проверка по БД)."""
    from ..models import AuthType
    if not password or not bool(services.rt("security", "mailbox_login")):
        return None
    acc = services.db.get_account_by_username(username)
    # Ящики, которые копируются входом администратора почты, тоже годятся:
    # пароль сотрудника всё равно проверяется обычным входом в его ящик.
    if acc is None or not acc.enabled or acc.auth_type not in (AuthType.PASSWORD, AuthType.MASTER):
        return None
    return acc


def _mailbox_limit_reached(services, username: str) -> Optional[str]:
    """Текст отказа, если проверять пароль ящика на IMAP-сервере сейчас нельзя.

    Попытка текущего запроса уже записана — отсюда строгое «больше».
    """
    max_attempts = int(services.rt("security", "max_login_attempts") or 5)
    lockout_min = int(services.rt("security", "lockout_minutes") or 15)
    since = (_now() - timedelta(minutes=lockout_min)).isoformat()
    if services.db.count_recent_failures(username, since, kind="imap") > max_attempts * _MAILBOX_ACCOUNT_FACTOR:
        return (f"Слишком много неудачных попыток входа в этот ящик. Повторите через {lockout_min} мин.")
    if services.db.count_recent_failures_of_kind("imap", since) > max_attempts * _MAILBOX_GLOBAL_FACTOR:
        return (f"Вход по паролю почтового ящика временно приостановлен: слишком много неудачных попыток. "
                f"Повторите через {lockout_min} мин.")
    return None


def _try_mailbox_login(services, acc, password: str):
    """Проверить пароль почтового ящика через IMAP. Вернуть Account или None.

    Одновременных проверок не больше :data:`_MAILBOX_LOGIN_SLOTS`: подключение
    блокирующее и долгое, а свободных слотов нет — сразу отказ (AuthError), а не
    ожидание. Иначе поток запросов на /api/login выводит из строя весь REST API.
    """
    from ..models import Account, AuthType
    from ..imap.client import ImapConnection
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
