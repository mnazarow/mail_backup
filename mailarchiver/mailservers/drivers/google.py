"""
Google Workspace — через Admin SDK (Directory API, Reports API) и Gmail API от имени сервисного аккаунта
с делегированием на уровне домена (domain-wide delegation).

Каждый токен выдаётся «от имени» одного пользователя (поле ``sub`` подписанного JWT): каталог и отчёты —
от имени администратора из подключения, настройки Gmail и вход по IMAP — от имени владельца ящика. Область
доступа (scope) у каждого токена одна: какую администратор не разрешил в «Делегировании домена», то и не
работает (с понятной причиной), остальное продолжает работать.

Учётные записи — пользователи каталога (``users.list``); вход закрыт — пользователь приостановлен
(``suspended``); последний вход — ``lastLoginTime``; занятое место — отчёт Reports API
(``accounts:gmail_used_quota_in_mb``, данные на 1–3 дня старее). Псевдонимы — ``users.aliases``: так адрес
удалённого сотрудника передаётся коллеге. Автоответ и пересылка — настройки Gmail пользователя
(``settings/vacation``, ``settings/autoForwarding``). Копирование ящиков — по IMAP (imap.gmail.com) токеном
сервисного аккаунта (XOAUTH2, область ``https://mail.google.com/``).

Чего API не умеет: квоту ящика (место общее для Gmail, Диска и Фото, предел задаётся в консоли
администратора по подразделениям) и пересылку больше чем на один адрес.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, ServerAuthError, ServerError, Status,
                    check_email, norm_email)
from ..http import TOKENS, HttpClient, Response, jwt_rs256, token_request

SCOPE_USER = "https://www.googleapis.com/auth/admin.directory.user"
SCOPE_USER_RO = "https://www.googleapis.com/auth/admin.directory.user.readonly"
SCOPE_CUSTOMER = "https://www.googleapis.com/auth/admin.directory.customer.readonly"
SCOPE_REPORTS = "https://www.googleapis.com/auth/admin.reports.usage.readonly"
SCOPE_BASIC = "https://www.googleapis.com/auth/gmail.settings.basic"
SCOPE_SHARING = "https://www.googleapis.com/auth/gmail.settings.sharing"
SCOPE_MAIL = "https://mail.google.com/"
#: области для «Делегирования домена»: с изменениями и только чтение
SCOPES_FULL = (SCOPE_USER, SCOPE_CUSTOMER, SCOPE_REPORTS, SCOPE_BASIC, SCOPE_SHARING, SCOPE_MAIL)
SCOPES_READ = (SCOPE_USER_RO, SCOPE_CUSTOMER, SCOPE_REPORTS, SCOPE_MAIL)

JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: «Никогда не входил» у lastLoginTime: в документации не описано, на деле — начало эпохи Unix
_NEVER = "1970-01-01"
MB = 1024 * 1024
#: users.list: не больше 500 на страницу (ограничения Directory API)
PAGE_SIZE = 500
MAX_PAGES = 400
#: даты отчётов Reports API — по тихоокеанскому стандартному времени (UTC−8)
PST = timezone(timedelta(hours=-8))
#: на сколько дней назад искать готовый отчёт (Gmail и хранилище — 1–3 дня, учётные записи — до 5)
REPORT_LOOKBACK = 6
USAGE_PARAMS = ("accounts:gmail_used_quota_in_mb", "accounts:used_quota_in_mb", "accounts:total_quota_in_mb",
                "accounts:drive_used_quota_in_mb")
_CUSTOMER_RE = re.compile(r"^[A-Za-z0-9]{4,40}$")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
#: последний день, за который отчёт был готов: (адрес Reports API, сервисный аккаунт) → (день, когда узнали)
_REPORT_DAY: Dict[Tuple[str, str], Tuple[str, float]] = {}
_REPORT_DAY_TTL_S = 6 * 3600


class ScopeDenied(ServerAuthError):
    """Google не выдал токен с этой областью: её нет в «Делегировании домена» или её запрещает политика."""


#: области, которые Google недавно не выдал: (сервер токенов, сервисный аккаунт, область) → (ошибка, когда) —
#: чтобы каждая сессия не просила заново область, которой нет в делегировании («Проверить подключение» — просит)
_DENIED: Dict[Tuple[str, str, str], Tuple[ScopeDenied, float]] = {}
_DENIED_TTL_S = 600


def parse_key(text: str) -> Dict[str, str]:
    """Ключ сервисного аккаунта (файл JSON из Google Cloud) → нужные поля. Текст ключа в ошибки не попадает."""
    try:
        data = json.loads(text or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ValidationError("Ключ сервисного аккаунта не читается: нужен файл JSON целиком — как его скачали из "
                              "Google Cloud console.")
    if data.get("type") != "service_account":
        raise ValidationError(f"Это не ключ сервисного аккаунта (type: «{str(data.get('type') or '—')[:40]}»).",
                              hint="Нужен JSON-ключ сервисного аккаунта (IAM и администрирование → Сервисные "
                                   "аккаунты → Ключи), а не учётные данные клиента OAuth.")
    email = norm_email(data.get("client_email"))
    pem = str(data.get("private_key") or "")
    if "@" not in email or "PRIVATE KEY" not in pem:
        raise ValidationError("В ключе сервисного аккаунта нет client_email или private_key — вставьте файл JSON "
                              "целиком.")
    # token_uri из файла не используется: подписанный запрос уходит только на сервер токенов Google
    return {"client_email": email, "private_key": pem, "private_key_id": str(data.get("private_key_id") or ""),
            "client_id": str(data.get("client_id") or ""), "token_uri": str(data.get("token_uri") or "")}


def _sha1(password: str) -> str:
    """Пароль для Directory API: шестнадцатеричный SHA-1 (hashFunction «SHA-1» — так рекомендует Google)."""
    return hashlib.sha1(password.encode("utf-8")).hexdigest()


def _date_ms(day: str) -> int:
    """«ГГГГ-ММ-ДД» → начало дня (UTC) в миллисекундах эпохи Unix."""
    try:
        d = date.fromisoformat(day)
    except ValueError as exc:
        raise ValidationError(f"Дата указана неверно: «{day}» (нужно ГГГГ-ММ-ДД).") from exc
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _ms_date(value: Any, *, end: bool = False) -> str:
    """Миллисекунды эпохи → «ГГГГ-ММ-ДД» (UTC); конец периода хранится как начало следующего дня."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if end:
        ms -= 1
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _int(value: Any) -> Optional[int]:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="@")


class GoogleDriver(Driver):
    type = "google"
    label = "Google Workspace"
    summary = ("Gmail через Admin SDK и Gmail API сервисным аккаунтом с делегированием домена: учётные записи, вход, "
               "пароли, псевдонимы, ФИО, создание и удаление, автоответ, пересылка; копирование ящиков по IMAP "
               "токеном сервисного аккаунта.")
    cloud = True
    #: адреса API (подменяются в тестах)
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    DIRECTORY_URL = "https://admin.googleapis.com/admin/directory/v1"
    REPORTS_URL = "https://admin.googleapis.com/admin/reports/v1"
    GMAIL_URL = "https://gmail.googleapis.com/gmail/v1"
    fields = (
        Field("sa_key", "Ключ сервисного аккаунта (файл JSON целиком)", "secret_text", required=True, secret=True,
              placeholder='{"type": "service_account", "client_email": "…", "private_key": "-----BEGIN PRIVATE KEY…"}',
              help="Google Cloud console → IAM и администрирование → Сервисные аккаунты → аккаунт → «Ключи» → "
                   "«Добавить ключ» → JSON. Вставьте содержимое скачанного файла целиком. Ключ даёт доступ ко всем "
                   "ящикам организации — храните файл как пароль администратора."),
        Field("admin_email", "Адрес администратора Google Workspace", required=True, placeholder="admin@company.ru",
              help="От его имени MailArchiver читает и меняет каталог пользователей: суперадминистратор (нужен, "
                   "чтобы менять других администраторов) или администратор с правами на пользователей и отчёты."),
        Field("customer", "Домен или идентификатор клиента (Customer ID)", placeholder="company.ru или C01abc23d",
              help="Пусто — все домены организации. Домен — только учётные записи этого домена. Идентификатор "
                   "клиента — консоль администратора → Аккаунт → Настройки аккаунта → Профиль."),
        Field("org_unit", "Подразделение новых пользователей", advanced=True, placeholder="/",
              help="Путь организационного подразделения, например /Сотрудники. Пусто — корневое (/)."),
        Field("usage_report", "Читать занятое место из отчётов Google (Reports API)", "bool", default=True,
              advanced=True, help="Нужна область admin.reports.usage.readonly. Данные отчётов отстают на 1–3 дня."),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.MUST_CHANGE, Cap.ALIASES,
                      Cap.CONTACT, Cap.CREATE, Cap.DELETE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title", "phone", "mobile",
                      "company", "office")
    password_rule = ("от 8 до 100 знаков: латинские буквы, цифры и знаки, без пробелов (политика паролей "
                     "организации может требовать больше)")
    imap_default = ("imap.gmail.com", 993, "ssl")
    limits = (
        "Квоту ящика API Google не задаёт: место общее для Gmail, Диска и Google Фото, предел задаётся в консоли "
        "администратора по подразделениям. В сверке «занято» — только почта; общий объём и предел хранилища — в "
        "сведениях учётной записи.",
        "Занятое место — из отчётов Google (Reports API): данные отстают на 1–3 дня, иногда до 5.",
        "Пересылка — только на один адрес (так устроена автопересылка Gmail). Адрес вне доменов организации нужно "
        "подтвердить: Gmail пришлёт его владельцу письмо со ссылкой, и пока её не нажмут, пересылка не включится — "
        "действие завершится ошибкой с этой причиной, после подтверждения повторите его. Автопересылку может "
        "запрещать и политика Gmail в консоли администратора.",
        "Закрытый вход — приостановка пользователя (suspended): новые письма Google такому пользователю не "
        "доставляет. Учётная запись в архиве (лицензия Archived User) тоже показывается с закрытым входом: войти в "
        "неё нельзя, а открыть вход можно, только вернув её из архива в консоли администратора.",
        "Пускает ли Google по IMAP в ящик приостановленного пользователя, в документации не сказано. Если не "
        "пускает, MailArchiver не удалит учётную запись: после закрытия входа ящик не сверить с архивом. Тогда "
        "сверьте архив сами и удалите пользователя в консоли администратора.",
        "Удалённого пользователя 20 дней может восстановить суперадминистратор в консоли администратора, затем "
        "его почта и Диск удаляются навсегда — перенесите данные Диска коллеге заранее.",
        "Псевдонимы — только в доменах организации, не больше 30 у учётной записи; новый адрес начинает получать "
        "почту в течение 24 часов. Адрес удалённого пользователя Google может принять псевдонимом не сразу — тогда "
        "повторите «Передать адрес коллеге» позже.",
        "Имя и фамилия при создании учётной записи обязательны (так требует Google).",
        "Смена пароля при следующем входе не действует, если вход идёт через внешнего поставщика (SSO).",
        "Число лицензий (занято / всего) не показывается: API Google отдаёт только назначенные лицензии.",
    )
    setup = (
        "Google Cloud console → создайте (или выберите) проект → «API и сервисы» → «Включить API»: Admin SDK API "
        "и Gmail API.",
        "IAM и администрирование → «Сервисные аккаунты» → «Создать сервисный аккаунт» (роли в проекте не нужны). "
        "В нём: «Ключи» → «Добавить ключ» → «Создать ключ» → JSON; содержимое файла вставьте в поле «Ключ "
        "сервисного аккаунта». Запишите уникальный идентификатор аккаунта (Client ID — число).",
        "Консоль администратора Google (admin.google.com, суперадминистратор) → Безопасность → Доступ к данным и "
        "управление ими → Управление API → «Делегирование домена» → «Добавить» → Client ID и области через "
        "запятую: " + ",".join(SCOPES_FULL) + ". Только чтение (без изменений на сервере): "
        + ",".join(SCOPES_READ) + ". Изменения применяются до 24 часов.",
        "Укажите адрес администратора, от имени которого работать: суперадминистратор или администратор с "
        "правами на управление пользователями и на отчёты.",
        "Консоль администратора → Приложения → Google Workspace → Gmail → Доступ для конечных пользователей: "
        "разрешите доступ по IMAP (для копирования ящиков), а для «Пересылки писем» — автоматическую пересылку.",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._sa: Optional[Dict[str, str]] = None
        self._clients: Dict[Tuple[str, str], HttpClient] = {}
        self._ids: Dict[str, str] = {}
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки -----------------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        from cryptography.hazmat.primitives import serialization
        sa = parse_key(secrets.get("sa_key") or "")
        try:
            serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Закрытый ключ (private_key) в файле сервисного аккаунта не читается — скачайте "
                                  "ключ заново.") from exc
        check_email(settings.get("admin_email") or "", "Адрес администратора")
        cls._customer_params(str(settings.get("customer") or ""))
        unit = str(settings.get("org_unit") or "").strip()
        if unit and not unit.startswith("/"):
            raise ValidationError("Подразделение новых пользователей — путь от корня, например /Сотрудники.")
        return []

    @staticmethod
    def _customer_params(value: str) -> Dict[str, str]:
        """Поле «Домен или идентификатор клиента» → параметры users.list (customer или domain)."""
        value = value.strip()
        if not value:
            return {"customer": "my_customer"}
        if "." in value:
            try:
                domain = value.lower().encode("idna").decode("ascii")
            except UnicodeError:
                domain = ""
            if not _DOMAIN_RE.match(domain):
                raise ValidationError(f"Домен указан неверно: «{value}».")
            return {"domain": domain}
        if not _CUSTOMER_RE.match(value):
            raise ValidationError(f"Идентификатор клиента указан неверно: «{value}» (например, C01abc23d).")
        return {"customer": value}

    def _scope_params(self) -> Dict[str, str]:
        return self._customer_params(str(self.setting("customer") or ""))

    def _customer_key(self) -> str:
        return self._scope_params().get("customer") or "my_customer"

    def _admin(self) -> str:
        return check_email(self.setting("admin_email") or "", "Адрес администратора")

    def _note(self, text: str) -> None:
        if self.api is not None:
            self.api.note(text)

    # -- токены --------------------------------------------------------------------------------
    def _key(self) -> Dict[str, str]:
        if self._sa is None:
            self._sa = parse_key(self.secret("sa_key"))
            if self.api is not None:
                self.api.secret(self._sa["private_key"])
        return self._sa

    def token(self, sub: str, scope: str) -> str:
        """Токен доступа от имени ``sub`` с одной областью (кэш — до истечения)."""
        sa = self._key()
        sub = norm_email(sub)
        material = hashlib.sha256(sa["private_key"].encode("utf-8")).hexdigest()[:16]
        key = ("google", sa["client_email"], material, self.TOKEN_URL, sub, scope)
        cached = TOKENS.get(key)
        if cached:
            if self.api is not None:
                self.api.secret(cached)
            return cached
        now = int(time.time())
        claims = {"iss": sa["client_email"], "scope": scope, "aud": self.TOKEN_URL, "iat": now, "exp": now + 3600,
                  "sub": sub}
        head = {"kid": sa["private_key_id"]} if sa["private_key_id"] else None
        assertion = jwt_rs256(claims, sa["private_key"], head)
        parsed = urllib.parse.urlsplit(self.TOKEN_URL)
        client = HttpClient(f"{parsed.scheme}://{parsed.netloc}", api=self.api, server_label=self.label)
        try:
            body = token_request(client, parsed.path or "/token", {"grant_type": JWT_GRANT, "assertion": assertion},
                                 self.label)
        except ServerAuthError as exc:
            error = self._token_error(exc, sub, scope)
            if isinstance(error, ScopeDenied):
                _DENIED[self._denied_key(scope)] = (error, time.time())
            raise error from exc
        _DENIED.pop(self._denied_key(scope), None)
        token = str(body["access_token"])
        if self.api is not None:
            self.api.secret(token)
        TOKENS.put(key, token, body.get("expires_in"))
        return token

    def _token_error(self, exc: ServerAuthError, sub: str, scope: str) -> ServerAuthError:
        low = exc.message.lower()
        sa = self._key()
        client = sa["client_id"] or sa["client_email"]
        where = ("Консоль администратора → Безопасность → Доступ к данным и управление ими → Управление API → "
                 "«Делегирование домена»")
        if any(code in low for code in ("unauthorized_client", "access_denied", "admin_policy_enforced",
                                        "invalid_scope")):
            return ScopeDenied(
                f"{self.label}: Google не разрешил сервисному аккаунту область {scope} (от имени {sub}) — её нет в "
                f"«Делегировании домена» для клиента {client} или её запрещает политика организации. Ответ: "
                f"{exc.message}",
                hint=f"{where}: добавьте области клиенту {client} (Client ID — число из сервисного аккаунта). "
                     f"Изменения применяются до 24 часов.")
        if "invalid jwt signature" in low:
            text = "ключ сервисного аккаунта не принят: он удалён, отключён или от другого аккаунта"
            hint = "Создайте новый JSON-ключ у сервисного аккаунта и вставьте его в подключение."
        elif "invalid email" in low or "not a valid email" in low or "user id" in low:
            text = f"пользователя {sub} в Google Workspace нет"
            hint = "Проверьте «Адрес администратора» в подключении." \
                if sub == norm_email(self.setting("admin_email")) else None
        elif any(code in low for code in ("invalid_client", "deleted_client", "disabled_client")):
            text = "сервисный аккаунт удалён или отключён (или ключ не от него)"
            hint = "Google Cloud console → IAM и администрирование → Сервисные аккаунты."
        elif "invalid_grant" in low:
            text = "Google не принял подписанный запрос токена"
            hint = "Проверьте точное время на этой машине (расхождение больше нескольких минут) и ключ."
        else:
            text = "токен не выдан"
            hint = f"Проверьте ключ сервисного аккаунта и {where}."
        return ServerAuthError(f"{self.label}: {text}. Ответ: {exc.message}", hint=hint)

    def _denied_key(self, scope: str) -> Tuple[str, str, str]:
        return self.TOKEN_URL, self._key()["client_email"], scope

    def _first_token(self, sub: str, scopes: Sequence[str]) -> str:
        """Токен с первой из областей, которую Google выдаёт (недавно не выданные — не просим заново)."""
        first: Optional[ScopeDenied] = None
        for scope in scopes:
            hit = _DENIED.get(self._denied_key(scope))
            if hit is not None and time.time() - hit[1] < _DENIED_TTL_S:
                first = first or hit[0]
                continue
            try:
                return self.token(sub, scope)
            except ScopeDenied as exc:
                first = first or exc
        raise first or ScopeDenied(f"{self.label}: нет подходящей области доступа.")

    def _http(self, base: str, token: str) -> HttpClient:
        key = (base, token)
        if key not in self._clients:
            self._clients[key] = HttpClient(base, api=self.api, server_label=self.label,
                                            headers={"Authorization": f"Bearer {token}"})
        return self._clients[key]

    def _dir(self, write: bool = False) -> HttpClient:
        """Directory API от имени администратора: запись — admin.directory.user, чтение — та область, что выдана."""
        if write:
            token = self.token(self._admin(), SCOPE_USER)
        else:
            order = (SCOPE_USER, SCOPE_USER_RO) if self.conn.allow_changes else (SCOPE_USER_RO, SCOPE_USER)
            token = self._first_token(self._admin(), order)
        return self._http(self.DIRECTORY_URL, token)

    def _read_scopes(self) -> Tuple[str, ...]:
        """Чтение настроек Gmail: gmail.settings.basic или https://mail.google.com/ (что есть в делегировании)."""
        return (SCOPE_BASIC, SCOPE_MAIL) if self.conn.allow_changes else (SCOPE_MAIL, SCOPE_BASIC)

    def _gmail(self, email: str, scopes: Sequence[str]) -> HttpClient:
        return self._http(self.GMAIL_URL, self._first_token(email, scopes))

    @staticmethod
    def _settings(email: str, what: str) -> str:
        return f"/users/{_quote(norm_email(email))}/settings/{what}"

    # -- запросы -------------------------------------------------------------------------------
    def _call(self, client: HttpClient, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
              body: Any = None, expect: Sequence[int] = (200, 201, 204)) -> Response:
        try:
            if body is None:
                return client.request(method, path, params=params, expect=expect)
            return client.request(method, path, params=params, json_body=body, expect=expect)
        except ServerAuthError as exc:
            raise self._explain(exc) from exc

    def _get(self, client: HttpClient, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call(client, "GET", path, params=params, expect=(200,)).json() or {}

    def _explain(self, exc: ServerAuthError) -> ServerError:
        """Отказ 401/403 Google → что с ним делать (ограничение частоты Google тоже отдаёт кодом 403)."""
        low = exc.message.lower()
        if "rate limit" in low or "quota exceeded" in low or "ratelimitexceeded" in low:
            return ServerError(f"{exc.message} — Google ограничил частоту запросов.", hint="Повторите задание позже.",
                               retryable=True)
        if "has not been used in project" in low or "accessnotconfigured" in low or "it is disabled" in low:
            return ServerAuthError(f"{exc.message} — API не включён в проекте сервисного аккаунта.",
                                   hint="Google Cloud console → «API и сервисы» → включите Admin SDK API и Gmail API.")
        if "not authorized to access this resource" in low:
            return ServerAuthError(f"{exc.message} — у администратора {self._admin()} нет прав на это.",
                                   hint="Укажите суперадминистратора или выдайте роль с правами на пользователей.")
        if "insufficient" in low and "scope" in low:
            return ServerAuthError(f"{exc.message} — токену не хватает области доступа.",
                                   hint="Проверьте «Делегирование домена» в консоли администратора.")
        return exc

    # -- чтение --------------------------------------------------------------------------------
    @staticmethod
    def _primary_item(items: Sequence[Dict]) -> Optional[Dict]:
        for item in items:
            if isinstance(item, dict) and item.get("primary"):
                return item
        return next((i for i in items if isinstance(i, dict)), None)

    @staticmethod
    def _phone(phones: Sequence[Dict], types: Sequence[str]) -> str:
        for p in phones:
            if isinstance(p, dict) and p.get("type") in types and p.get("value"):
                return str(p["value"])
        return ""

    def _account(self, user: Dict, usage: Optional[Dict[str, Dict]] = None) -> Optional[Dict[str, Any]]:
        email = norm_email(user.get("primaryEmail"))
        if not email:
            return None
        name = user.get("name") or {}
        orgs = [o for o in user.get("organizations") or [] if isinstance(o, dict)]
        org = self._primary_item(orgs) or {}
        phones = [p for p in user.get("phones") or [] if isinstance(p, dict)]
        last = str(user.get("lastLoginTime") or "")
        kind = "admin" if user.get("isAdmin") else "user"
        if user.get("archived"):
            kind = "archived"
        if user.get("isMailboxSetup") is False:
            kind = "no_mailbox"
        info = {"org_unit": user.get("orgUnitPath") or "", "created": user.get("creationTime") or "",
                "admin": bool(user.get("isAdmin")), "delegated_admin": bool(user.get("isDelegatedAdmin")),
                "archived": bool(user.get("archived")), "mailbox": user.get("isMailboxSetup"),
                "two_step": user.get("isEnrolledIn2Sv")}
        if user.get("suspensionReason"):
            info["suspension_reason"] = user["suspensionReason"]
        if user.get("nonEditableAliases"):
            info["non_editable_aliases"] = [norm_email(a) for a in user["nonEditableAliases"]]
        # вход закрыт: пользователь приостановлен — или в архиве (лицензия Archived User: войти в него нельзя, пока
        # его не вернут из архива)
        if user.get("archived"):
            blocked: Optional[bool] = True
        else:
            blocked = None if user.get("suspended") is None else bool(user.get("suspended"))
        acc: Dict[str, Any] = {
            "email": email, "native_id": str(user.get("id") or ""), "first_name": name.get("givenName") or "",
            "last_name": name.get("familyName") or "",
            "display_name": name.get("displayName") or name.get("fullName") or "",
            "aliases": [norm_email(a) for a in user.get("aliases") or []],
            "login_blocked": blocked,
            "last_login": "" if (not last or last.startswith(_NEVER)) else last,
            "department": org.get("department") or "", "title": org.get("title") or "",
            "company": org.get("name") or "", "office": org.get("location") or "",
            "phone": self._phone(phones, ("work",)) or next(
                (str(p.get("value")) for p in phones if p.get("primary") and p.get("value")
                 and p.get("type") not in ("mobile", "work_mobile")), ""),
            "mobile": self._phone(phones, ("mobile", "work_mobile")),
            "kind": kind, "info": info}
        if user.get("changePasswordAtNextLogin") is not None:
            acc["must_change"] = bool(user.get("changePasswordAtNextLogin"))
        u = (usage or {}).get(email)
        if u:
            acc["used_bytes"] = u.get("gmail")
            info.update({k: u[k] for k in ("storage_used_mb", "storage_total_mb", "drive_used_mb", "usage_date")
                         if u.get(k) is not None})
        return acc

    def _users(self) -> List[Dict]:
        client = self._dir()
        params: Dict[str, Any] = dict(self._scope_params(), maxResults=PAGE_SIZE, orderBy="email")
        out: List[Dict] = []
        for _ in range(MAX_PAGES):
            body = self._get(client, "/users", params)
            out.extend(u for u in body.get("users") or [] if isinstance(u, dict))
            nxt = body.get("nextPageToken")
            if not nxt:
                return out
            params["pageToken"] = nxt
        self.incomplete = True
        return out

    def list_accounts(self) -> List[Dict[str, Any]]:
        users = self._users()
        usage = self.usage("all")
        out = []
        for user in users:
            acc = self._account(user, usage)
            if acc is not None:
                self._ids[acc["email"]] = acc["native_id"]
                out.append(acc)
        return out

    def _report_key(self) -> Tuple[str, str]:
        return self.REPORTS_URL, self._key()["client_email"]

    def _report_days(self, single: bool) -> List[str]:
        """Дни, за которые пробовать отчёт: от вчерашнего (по PST) назад; известный готовый день — сразу."""
        today = datetime.now(PST).date()
        days = [(today - timedelta(days=n)).isoformat() for n in range(1, REPORT_LOOKBACK + 1)]
        hit = _REPORT_DAY.get(self._report_key())
        if hit and hit[0] in days and time.time() - hit[1] < _REPORT_DAY_TTL_S:
            # одна учётная запись — только за этот день (новой учётной записи в отчёте может ещё не быть)
            return [hit[0]] if single else days[days.index(hit[0]):]
        return days

    def usage(self, who: str = "all") -> Dict[str, Dict]:
        """Отчёт Reports API: адрес → {gmail (байты), storage_used_mb, …, usage_date}. Не вышло — пусто."""
        if not self.setting("usage_report", True):
            return {}
        try:
            client = self._http(self.REPORTS_URL, self._first_token(self._admin(), (SCOPE_REPORTS,)))
        except ScopeDenied:
            text = (f"занятое место не прочитано: Google не выдал токен с областью {SCOPE_REPORTS} (добавьте её в "
                    f"«Делегирование домена» или выключите «Читать занятое место» в подключении)")
            self._note(text)
            if who == "all":
                self.sync_problems.append(text)
            return {}
        params: Dict[str, Any] = {"parameters": ",".join(USAGE_PARAMS)}
        last = ""
        customer = self._customer_key()
        if customer != "my_customer":
            params["customerId"] = customer
        path = f"/usage/users/{_quote(norm_email(who) if who != 'all' else 'all')}/dates/"
        for day in self._report_days(who != "all"):
            try:
                resp = self._call(client, "GET", path + day, params=dict(params), expect=(200, 400))
            except NotFound:
                return {}
            except ServerError as exc:
                self._note(f"отчёт об использовании не прочитан: {exc.message}")
                if who == "all" and isinstance(exc, ServerAuthError):
                    self.sync_problems.append(f"занятое место не прочитано: {exc.message}")
                return {}
            if resp.status == 400:
                # за этот день данных ещё нет («Data for dates later than … is not yet available») — день назад
                try:
                    last = str(((resp.json() or {}).get("error") or {}).get("message") or "")
                except (ServerError, AttributeError):
                    last = ""
                continue
            body = resp.json() or {}
            out = self._usage_rows(body.get("usageReports") or [], day)
            if not any(r.get("gmail") is not None for r in out.values()):
                continue
            nxt = body.get("nextPageToken")
            for _ in range(MAX_PAGES):
                if not nxt:
                    break
                try:
                    page = self._get(client, path + day, dict(params, pageToken=nxt))
                except ServerError as exc:
                    # что прочитано — остаётся; у остальных занятое место будет неизвестно
                    self._note(f"отчёт об использовании прочитан не полностью: {exc.message}")
                    break
                out.update(self._usage_rows(page.get("usageReports") or [], day))
                nxt = page.get("nextPageToken")
            _REPORT_DAY[self._report_key()] = (day, time.time())
            return out
        self._note(f"отчёт об использовании не получен ни за один из последних {REPORT_LOOKBACK} дней"
                   + (f" (ответ: {last[:300]})" if last else ""))
        return {}

    @staticmethod
    def _usage_rows(reports: Sequence[Dict], day: str) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        for rep in reports:
            if not isinstance(rep, dict):
                continue
            email = norm_email((rep.get("entity") or {}).get("userEmail"))
            if not email:
                continue
            values: Dict[str, Any] = {}
            for p in rep.get("parameters") or []:
                if isinstance(p, dict) and p.get("name"):
                    values[p["name"]] = p.get("intValue")
            gmail = _int(values.get("accounts:gmail_used_quota_in_mb"))
            out[email] = {"gmail": gmail * MB if gmail is not None else None,
                          "storage_used_mb": _int(values.get("accounts:used_quota_in_mb")),
                          "storage_total_mb": _int(values.get("accounts:total_quota_in_mb")),
                          "drive_used_mb": _int(values.get("accounts:drive_used_quota_in_mb")),
                          "usage_date": rep.get("date") or day}
        return out

    def _user(self, email: str) -> Optional[Dict]:
        """Пользователь, у которого это основной адрес (по псевдониму Google вернул бы владельца псевдонима)."""
        email = norm_email(email)
        key = self._ids.get(email) or email
        try:
            user = self._get(self._dir(), f"/users/{_quote(key)}")
        except NotFound:
            return None
        if norm_email(user.get("primaryEmail")) != email:
            return None
        self._ids[email] = str(user.get("id") or "")
        return user

    def _key_of(self, email: str) -> str:
        email = norm_email(email)
        if not self._ids.get(email):
            user = self._user(email)
            if user is None:
                raise NotFound(f"{self.label}: учётной записи {email} нет.")
        return _quote(self._ids[email] or email)

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._user(email)
        if user is None:
            return None
        usage = self.usage(norm_email(email))
        acc = self._account(user, usage)
        if acc is None:
            return None
        if acc["kind"] != "no_mailbox":
            self._read_mail_settings(acc)
        return acc

    def _read_mail_settings(self, acc: Dict[str, Any]) -> None:
        """Автоответ и пересылка — настройки Gmail пользователя (не вышло — остаются неизвестными)."""
        email = acc["email"]
        try:
            client = self._gmail(email, self._read_scopes())
        except ServerError as exc:
            self._note(f"настройки Gmail {email} не прочитаны: {exc.message}")
            return
        try:
            acc["autoreply"] = self._autoreply_on(self._get(client, self._settings(email, "vacation")))
        except ServerError as exc:
            self._note(f"автоответ {email} не прочитан: {exc.message}")
        try:
            fwd = self._get(client, self._settings(email, "autoForwarding"))
            acc["forward_to"] = [norm_email(fwd["emailAddress"])] if fwd.get("enabled") and fwd.get("emailAddress") \
                else []
        except ServerError as exc:
            self._note(f"пересылка {email} не прочитана: {exc.message}")

    @staticmethod
    def _autoreply_on(st: Dict[str, Any]) -> bool:
        if not st.get("enableAutoReply"):
            return False
        end = _int(st.get("endTime"))
        return end is None or end > time.time() * 1000

    # -- изменения -----------------------------------------------------------------------------
    def _patch(self, email: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call(self._dir(True), "PATCH", f"/users/{self._key_of(email)}", body=body,
                          expect=(200,)).json() or {}

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        self._patch(email, {"suspended": not enabled})
        if enabled:
            return "вход открыт (приостановка пользователя снята)"
        text = "вход закрыт (пользователь приостановлен)"
        if keep_receiving:
            text += "; новые письма Google приостановленному пользователю не доставляет"
        return text

    def check_password(self, password: str) -> None:
        if not password or not 8 <= len(password) <= 100:
            raise ValidationError(f"Пароль не подходит Google Workspace: {self.password_rule}.")
        if any(c.isspace() for c in password):
            raise ValidationError("Пароль не должен содержать пробелов.")
        if any(ord(c) < 33 or ord(c) > 126 for c in password):
            raise ValidationError(f"Пароль не подходит Google Workspace: {self.password_rule}.")

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        self._patch(email, {"password": _sha1(password), "hashFunction": "SHA-1",
                            "changePasswordAtNextLogin": bool(must_change)})
        return "пароль сменён" + ("; при следующем входе — смена пароля" if must_change else "")

    def set_must_change(self, email: str, value: bool) -> str:
        self._patch(email, {"changePasswordAtNextLogin": bool(value)})
        return "при следующем входе потребуется сменить пароль" if value else "требование снято"

    _ORG = {"department": "department", "title": "title", "company": "name", "office": "location"}

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        user = self._user(email)
        if user is None:
            raise NotFound(f"{self.label}: учётной записи {email} нет.")
        body: Dict[str, Any] = {}
        old = user.get("name") or {}
        name = {k: old[k] for k in ("givenName", "familyName", "displayName") if old.get(k)}
        for key, field in (("first_name", "givenName"), ("last_name", "familyName"), ("display_name", "displayName")):
            if key in values:
                name[field] = (values[key] or "").strip()
        if any(k in values for k in ("first_name", "last_name", "display_name")):
            if not name.get("givenName") or not name.get("familyName"):
                raise ValidationError("Имя и фамилию в Google Workspace нельзя оставить пустыми.")
            if not name.get("displayName"):
                name.pop("displayName", None)
            body["name"] = name
        if any(k in values for k in self._ORG):
            orgs = [dict(o) for o in user.get("organizations") or [] if isinstance(o, dict)]
            org = self._primary_item(orgs)
            if org is None:
                org = {"primary": True, "type": "work"}
                orgs.append(org)
            for key, field in self._ORG.items():
                if key in values:
                    if values[key]:
                        org[field] = values[key]
                    else:
                        org.pop(field, None)
            body["organizations"] = orgs
        if "phone" in values or "mobile" in values:
            phones = [dict(p) for p in user.get("phones") or [] if isinstance(p, dict)]
            for key, kind in (("phone", "work"), ("mobile", "mobile")):
                if key not in values:
                    continue
                current = next((p for p in phones if p.get("type") == kind), None)
                if values[key] and current is not None:
                    current["value"] = values[key]
                elif values[key]:
                    phones.append({"value": values[key], "type": kind, "primary": not phones})
                elif current is not None:
                    phones.remove(current)
            body["phones"] = phones
        if body:
            # update (PUT): поля, которых нет в запросе, сохраняются, а списки (телефоны, организации) заменяются
            # целиком — patch очистить их не может
            self._call(self._dir(True), "PUT", f"/users/{self._key_of(email)}", body=body, expect=(200,))
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        email = check_email(email)
        self.check_password(password)
        first, last = (first_name or "").strip(), (last_name or "").strip()
        if not first or not last:
            raise ValidationError("Google Workspace требует имя и фамилию новой учётной записи — укажите их в строке "
                                  "списка: «адрес; имя; фамилия».")
        body: Dict[str, Any] = {"primaryEmail": email, "name": {"givenName": first[:60], "familyName": last[:60]},
                                "password": _sha1(password), "hashFunction": "SHA-1",
                                "changePasswordAtNextLogin": bool(must_change)}
        unit = str(self.setting("org_unit") or "").strip()
        if unit:
            body["orgUnitPath"] = unit
        resp = self._call(self._dir(True), "POST", "/users", body=body, expect=(200, 201, 409, 412))
        if resp.status == 409:
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть (или адрес занят псевдонимом или "
                                f"группой).")
        if resp.status == 412:
            raise ServerError(f"{self.label}: свободных лицензий нет — учётная запись {email} не создана.",
                              hint="Купите лицензии или освободите их в консоли администратора.")
        user = resp.json() or {}
        self._ids[email] = str(user.get("id") or "")
        user.setdefault("primaryEmail", email)
        user.setdefault("suspended", False)
        return self._account(user) or {"email": email}

    def delete_account(self, email: str) -> str:
        self._call(self._dir(True), "DELETE", f"/users/{self._key_of(email)}", expect=(200, 204))
        self._ids.pop(norm_email(email), None)
        return ("учётная запись удалена (20 дней её может восстановить суперадминистратор в консоли "
                "администратора, затем почта и Диск удаляются навсегда)")

    def _aliases_now(self, email: str) -> Optional[List[str]]:
        user = self._user(email)
        return None if user is None else [norm_email(a) for a in user.get("aliases") or []]

    def add_alias(self, email: str, alias: str) -> Optional[List[str]]:
        alias = check_email(alias, "Псевдоним")
        resp = self._call(self._dir(True), "POST", f"/users/{self._key_of(email)}/aliases", body={"alias": alias},
                          expect=(200, 201, 409))
        if resp.status == 409:
            raise AlreadyExists(f"{self.label}: адрес {alias} уже занят (учётной записью, группой или псевдонимом).")
        after = self._aliases_now(email)
        # Google принял псевдоним, но в учётной записи его ещё не видно — список неизвестен
        return after if after is not None and alias in after else None

    def remove_alias(self, email: str, alias: str) -> Optional[List[str]]:
        alias = norm_email(alias)
        self._call(self._dir(True), "DELETE", f"/users/{self._key_of(email)}/aliases/{_quote(alias)}",
                   expect=(200, 204))
        return self._aliases_now(email)

    # -- автоответ и пересылка ----------------------------------------------------------------
    def get_autoreply(self, email: str) -> Dict[str, Any]:
        st = self._get(self._gmail(email, self._read_scopes()), self._settings(email, "vacation"))
        text = st.get("responseBodyPlainText") or ""
        if not text and st.get("responseBodyHtml"):
            text = html.unescape(re.sub(r"<[^>]+>", " ", str(st["responseBodyHtml"]))).strip()
        return {"enabled": self._autoreply_on(st), "subject": st.get("responseSubject") or "", "text": text,
                "start": _ms_date(st.get("startTime")) if st.get("startTime") else "",
                "end": _ms_date(st.get("endTime"), end=True) if st.get("endTime") else ""}

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        if not enabled:
            body: Dict[str, Any] = {"enableAutoReply": False}
        else:
            if not (subject or "").strip() and not (text or "").strip():
                raise ValidationError("Для автоответа нужен текст или тема.")
            body = {"enableAutoReply": True, "responseSubject": subject or "", "responseBodyPlainText": text or "",
                    "restrictToContacts": False, "restrictToDomain": False}
            if start:
                body["startTime"] = str(_date_ms(start))
            if end:
                body["endTime"] = str(_date_ms(end) + 86400 * 1000)     # до конца дня «по» включительно (UTC)
            if start and end and int(body["startTime"]) >= int(body["endTime"]):
                raise ValidationError("Дата окончания автоответа раньше даты начала.")
        self._call(self._gmail(email, (SCOPE_BASIC,)), "PUT", self._settings(email, "vacation"), body=body,
                   expect=(200,))
        if not enabled:
            return "автоответ выключен"
        period = ""
        if start or end:
            period = f" ({'с ' + start if start else ''}{' ' if start and end else ''}{'по ' + end if end else ''})"
        return f"автоответ включён{period}"

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        st = self._get(self._gmail(email, self._read_scopes()), self._settings(email, "autoForwarding"))
        on = bool(st.get("enabled")) and bool(st.get("emailAddress"))
        return {"addresses": [norm_email(st["emailAddress"])] if on else [],
                "keep_copy": str(st.get("disposition") or "") != "trash"}

    def _forwarding_status(self, email: str, target: str) -> Optional[str]:
        """Статус адреса пересылки у пользователя (accepted, pending…) или None — адрес не заведён."""
        body = self._get(self._gmail(email, self._read_scopes()), self._settings(email, "forwardingAddresses"))
        for item in body.get("forwardingAddresses") or []:
            if isinstance(item, dict) and norm_email(item.get("forwardingEmail")) == target:
                return str(item.get("verificationStatus") or "")
        return None

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        targets = [check_email(a, "Адрес пересылки") for a in addresses or []]
        if len(targets) > 1:
            raise ValidationError("Gmail пересылает все письма только на один адрес — укажите один.")
        sharing = self._gmail(email, (SCOPE_SHARING,))
        if not targets:
            self._call(sharing, "PUT", self._settings(email, "autoForwarding"), body={"enabled": False},
                       expect=(200,))
            return "пересылка выключена"
        target = targets[0]
        try:
            status = self._forwarding_status(email, target)
        except ServerError as exc:
            self._note(f"адреса пересылки не прочитаны ({exc.message}) — адрес будет заведён")
            status = None
        if status is None:
            resp = self._call(sharing, "POST", self._settings(email, "forwardingAddresses"),
                              body={"forwardingEmail": target}, expect=(200, 201, 409))
            if resp.status == 409:
                status = self._forwarding_status(email, target) or ""
            else:
                status = str((resp.json() or {}).get("verificationStatus") or "")
        if status != "accepted":
            # не включено — ошибка, а не «сделано» с оговоркой: иначе задание засчитало бы пересылку и записало бы
            # её в снимок
            if status == "pending":
                text = (f"пересылка НЕ включена: адрес {target} ждёт подтверждения — Gmail отправил на него письмо со "
                        f"ссылкой; когда владелец адреса её нажмёт, запустите «Пересылку писем» ещё раз")
            else:
                text = f"пересылка НЕ включена: Gmail не подтвердил адрес {target} (статус «{status or '—'}»)"
            try:
                now = self.get_forwarding(email)["addresses"]
            except ServerError:
                now = []
            if now:
                text += f"; прежняя пересылка на {', '.join(now)} осталась включённой"
            raise ServerError(f"{self.label}: {text}.")
        body = {"enabled": True, "emailAddress": target, "disposition": "leaveInInbox" if keep_copy else "trash"}
        self._call(sharing, "PUT", self._settings(email, "autoForwarding"), body=body, expect=(200,))
        text = f"пересылка на {target}"
        if not keep_copy:
            text += "; после пересылки письма уходят в «Корзину»"
        return text

    # -- IMAP и состояние ----------------------------------------------------------------------
    def imap_auth(self, email: str) -> ImapAuth:
        email = norm_email(email)
        return ImapAuth(mech="xoauth2", user=email, secret=self.token(email, SCOPE_MAIL), host=self.imap_default[0])

    def test(self) -> Status:
        st = Status(version="Admin SDK Directory API v1, Gmail API v1")
        sa = self._key()
        admin = self._admin()
        for key in [k for k in _DENIED if k[:2] == (self.TOKEN_URL, sa["client_email"])]:
            _DENIED.pop(key, None)                  # проверка — заново по всем областям (делегирование могли поправить)
        st.details.append(f"Сервисный аккаунт: {sa['client_email']}"
                          + (f" (Client ID {sa['client_id']})" if sa["client_id"] else ""))
        st.details.append(f"Администратор: {admin}")
        # каталог — без него подключение не работает: ошибка уходит в «Проверить подключение» как есть
        self._get(self._dir(), "/users", dict(self._scope_params(), maxResults=1))
        try:
            client = self._http(self.DIRECTORY_URL, self._first_token(admin, (SCOPE_CUSTOMER,)))
            cust = self._get(client, f"/customers/{_quote(self._customer_key())}")
            org = (cust.get("postalAddress") or {}).get("organizationName")
            if org:
                st.details.append(f"Организация: {org}")
            if cust.get("customerDomain"):
                st.details.append(f"Основной домен: {cust['customerDomain']}")
            if cust.get("id"):
                st.details.append(f"Идентификатор клиента: {cust['id']}")
        except ScopeDenied:
            st.problems.append(("customer", "warning", f"сведения об организации не прочитаны: области "
                                                       f"{SCOPE_CUSTOMER} нет в «Делегировании домена»"))
        except ServerError as exc:
            st.problems.append(("customer", "warning", f"сведения об организации не прочитаны: {exc.message}"))
        sa_client = sa["client_id"] or sa["client_email"]
        checks = []
        if self.conn.allow_changes:
            checks.append(("directory_write", SCOPE_USER, "изменения учётных записей не пройдут"))
        if self.setting("usage_report", True):
            checks.append(("usage", SCOPE_REPORTS, "занятое место не читается"))
        checks.append(("imap_token", SCOPE_MAIL, "копирование ящиков без паролей не пойдёт"))
        if self.conn.allow_changes:
            checks += [("gmail_basic", SCOPE_BASIC, "автоответ не настроить"),
                       ("gmail_sharing", SCOPE_SHARING, "пересылку не настроить")]
        for key, scope, what in checks:
            try:
                self.token(admin, scope)            # проверка делегирования: токен от имени администратора
            except ScopeDenied:
                st.problems.append((key, "warning", f"{what}: области {scope} нет в «Делегировании домена» для "
                                                    f"клиента {sa_client} (или её запрещает политика)"))
            except ServerError as exc:
                st.problems.append((key, "warning", f"{what}: токен с областью {scope} не выдан — {exc.message}"))
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = GoogleDriver
