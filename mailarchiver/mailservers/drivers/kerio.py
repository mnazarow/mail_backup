"""
Kerio Connect — через API администрирования (JSON-RPC 2.0 по HTTPS, порт 4040, ``/admin/api/jsonrpc/``).

Сеанс: ``Session.login`` отдаёт токен (``result.token``, дальше — заголовок
``X-Token``) и cookie ``SESSION_CONNECT_WEBADMIN`` — оба нужны в каждом
следующем вызове; истёкший сеанс (ошибка -32001) — вход заново, один раз на
вызов; по окончании — ``Session.logout``.

Учётные записи — ``Users.get`` по каждому домену (``Domains.get``), страницами;
отдел, должность и телефоны — в личном контакте пользователя
(``Users.getPersonalContact``). Изменения — ``Users.set`` по образцу (вложенные
структуры — целиком, как требует IDL), ``Users.create``, ``Users.remove``,
``Users.setPersonalContact``. Ошибки внутри ответа 200 (``error`` и
``result.errors``) — исключения с текстом сервера.

Чего API администрирования не умеет: автоответ (только сам пользователь в Kerio
Connect Client), «сменить пароль при следующем входе». Письма API не отдаёт —
ящики копируются по IMAP; вход без пароля сотрудника (Master Authentication,
команды X-MASTERAUTH/X-SETUSER) общий IMAP-код MailArchiver пока не умеет,
поэтому ящики копируются с паролями сотрудников.
"""
from __future__ import annotations

import itertools
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ... import __version__
from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, ServerAuthError, ServerError, Status,
                    imap_fields, norm_email, split_email, tls_fields)
from ..http import HttpClient

ENDPOINT = "/admin/api/jsonrpc/"
DEFAULT_PORT = 4040
#: JSON-RPC: сеанс истёк — войти заново
SESSION_EXPIRED = -32001
METHOD_NOT_FOUND = -32601
#: коды ошибок приложения Kerio (Error.code)
E_EXISTS, E_NO_ENTITY, E_NOT_PERMITTED, E_ACCESS_DENIED = 1001, 1002, 1003, 1004
#: методы только для чтения: их можно повторить после 502/503/504 (изменения — нет: сервер мог их выполнить)
READ_METHODS = frozenset({"Users.get", "Domains.get", "Users.getPersonalContact", "Session.getDomain",
                          "Session.whoAmI", "Server.getProductInfo", "Server.getAboutInfo", "Queue.get",
                          "Server.getAlertList", "ProductRegistration.getFullStatus"})
#: сколько ждать ответа на Users.remove: папка с письмами удаляется до ответа — у большого ящика это минуты
DELETE_TIMEOUT_S = 600.0
#: пользователей за запрос и предел страниц на домен (дальше — сверка неполная)
PAGE = 500
MAX_PAGES = 200
UNITS = {"Bytes": 1, "KiloBytes": 1024, "MegaBytes": 1024 ** 2, "GigaBytes": 1024 ** 3, "TeraBytes": 1024 ** 4,
         "PetaBytes": 1024 ** 5}
USER_FIELDS = ["id", "domainId", "loginName", "fullName", "description", "isEnabled", "itemSource", "authType",
               "role", "emailAddresses", "emailForwarding", "diskSizeLimit", "consumedSize", "consumedItems",
               "lastLoginInfo"]
DOMAIN_FIELDS = ["id", "name", "aliasList", "isPrimary", "description"]
INTERNAL_AUTH = "UInternalAuth"
LDAP_SOURCE = "DSLDAPSource"
FORWARD_OFF, FORWARD_ONLY, FORWARD_KEEP = "UForwardNone", "UForwardYes", "UForwardDeliver"
ROLES = {"UserRole": "без прав администратора", "Auditor": "только чтение", "AccountAdmin": "управление учётными "
         "записями", "FullAdmin": "полные права", "BuiltInAdmin": "встроенный администратор (полные права)",
         "BuiltInDomainAdmin": "встроенный администратор домена"}
REG_TYPES = {"rsNoRegistration": "не зарегистрирован", "rsTrialRegistered": "пробная версия",
             "rsTrialExpired": "пробный период истёк", "rsProductRegistered": "зарегистрирован"}
#: поля «ФИО, отдел, должность» → личный контакт пользователя (PersonalContact)
CONTACT = {"first_name": "firstName", "last_name": "surName", "department": "departmentName", "title": "profession",
           "phone": "phoneNumberWorkVoice", "mobile": "phoneNumberMobile", "company": "companyName"}
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite", "version", "comment",
                 "priority", "partitioned"}


class _KerioHttp(HttpClient):
    """HttpClient, который запоминает все заголовки Set-Cookie ответа (у входа их может быть несколько)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.set_cookies: List[str] = []

    def _connection(self):
        conn = super()._connection()
        original = conn.getresponse

        def getresponse(*a, **kw):
            resp = original(*a, **kw)
            self.set_cookies = list(resp.headers.get_all("Set-Cookie") or [])
            return resp
        conn.getresponse = getresponse
        return conn


def session_cookies(headers: Sequence[str]) -> Dict[str, str]:
    """Заголовки Set-Cookie → {имя: значение} (без атрибутов path, secure…)."""
    out: Dict[str, str] = {}
    for header in headers:
        name, sep, value = str(header).split(";", 1)[0].partition("=")
        name = name.strip()
        if sep and name and name.lower() not in _COOKIE_ATTRS:
            out[name] = value.strip()
    return out


def size_bytes(value: Any) -> Optional[int]:
    """ByteValueWithUnits {value, units} → байты (None — неизвестно)."""
    if not isinstance(value, dict):
        return None
    try:
        return int(value.get("value")) * UNITS[str(value.get("units") or "Bytes")]
    except (KeyError, TypeError, ValueError):
        return None


def size_limit(quota_bytes: Optional[int]) -> Dict[str, Any]:
    """Квота → SizeLimit целиком (так требует IDL для set): в самых крупных единицах, которыми она делится."""
    if not quota_bytes:
        return {"isActive": False, "limit": {"value": 0, "units": "MegaBytes"}}
    for units in ("TeraBytes", "GigaBytes", "MegaBytes", "KiloBytes", "Bytes"):
        if quota_bytes % UNITS[units] == 0:
            return {"isActive": True, "limit": {"value": quota_bytes // UNITS[units], "units": units}}
    return {"isActive": True, "limit": {"value": int(quota_bytes), "units": "Bytes"}}  # pragma: no cover


def stamp_iso(value: Any) -> str:
    """DateTimeStamp (секунды с 1970 года; миллисекунды тоже понимаем) → ISO; 0 — «никогда»."""
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    if stamp > 10 ** 11:
        stamp //= 1000
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _out(result: Any, *names: str) -> Any:
    """Выходной параметр метода: по имени, а если имя в документации не названо — единственное значение."""
    if not isinstance(result, dict):
        return {}
    for name in names:
        if name in result:
            return result[name]
    values = [v for k, v in result.items() if k != "errors"]
    return values[0] if len(values) == 1 else {}


def _code(err: Dict[str, Any]) -> Optional[int]:
    try:
        return int(err.get("code"))
    except (TypeError, ValueError):
        return None


def error_text(err: Dict[str, Any]) -> str:
    """Текст ошибки Kerio: «%1» в сообщении заменяется параметрами (messageParameters.positionalParameters)."""
    text = str(err.get("message") or err.get("errorMessage") or "").strip()
    params = err.get("messageParameters")
    data = err.get("data")
    if not isinstance(params, dict) and isinstance(data, dict):
        params = data.get("messageParameters")
    positional = params.get("positionalParameters") if isinstance(params, dict) else None
    positional = positional if isinstance(positional, list) else []
    for i in range(len(positional), 0, -1):
        text = text.replace(f"%{i}", str(positional[i - 1]))
    return text or f"код {err.get('code')}"


class KerioDriver(Driver):
    type = "kerio"
    label = "Kerio Connect"
    summary = ("API администрирования Kerio Connect (JSON-RPC): учётные записи всех доменов, место и квота, последний "
               "вход, закрытие входа, пароли, квота, псевдонимы, ФИО и отдел, создание и удаление, пересылка; ящики "
               "копируются по IMAP мастер-паролем (Master Authentication) или с паролями сотрудников.")
    cloud = False
    fields = (
        Field("host", "Адрес сервера Kerio Connect", required=True, placeholder="mail.company.ru",
              help="Имя или IP-адрес сервера (как в адресе Webadmin). Можно с портом и схемой: "
                   "https://mail.company.ru:4040."),
        Field("port", "Порт администрирования", "int", default=DEFAULT_PORT, advanced=True,
              help="Порт Webadmin и API администрирования (HTTPS), обычно 4040."),
        Field("username", "Учётная запись администратора", required=True, placeholder="mailarchiver",
              help="Логин учётной записи с правами на администрирование; не из основного домена — полным адресом "
                   "(admin@company.ru). Встроенная учётная запись — Admin."),
        Field("password", "Пароль администратора", "password", required=True, secret=True,
              help="Обычный пароль учётной записи: пароль приложения API администрирования не принимает."),
        Field("read_contacts", "Читать отдел, должность и телефоны (личные контакты пользователей)", "bool",
              default=True, advanced=True, help="При сверке — ещё один запрос на каждые 500 пользователей."),
        Field("allow_plain", "Разрешить подключение без шифрования (http)", "bool", default=False, advanced=True,
              help="Только для доверенной сети: пароль администратора уйдёт открытым текстом. Адрес тогда — "
                   "с http://."),
        Field("master_password", "Мастер-пароль (Master Authentication)", "password", secret=True,
              help="Для копирования ящиков без паролей сотрудников: Webadmin → Configuration → Advanced Options → "
                   "Master Authentication. Пусто — ящики копируются с паролями сотрудников."),
        *imap_fields(),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.QUOTA, Cap.ALIASES,
                      Cap.CONTACT, Cap.CREATE, Cap.DELETE, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title", "phone", "mobile", "company")
    password_rule = ("от 8 до 64 знаков, без пробелов; если в домене включена политика сложности паролей — ещё и по "
                     "её правилам")
    limits = (
        "Автоответ («нет на месте») API администрирования Kerio не задаёт: его включает сам сотрудник в Kerio Connect "
        "Client, а сразу для всех — правило фильтра сообщений в Webadmin.",
        "Потребовать смену пароля при следующем входе Kerio через API не умеет — такого признака у учётной записи нет.",
        "Ящики копируются без паролей сотрудников мастер-паролем (Master Authentication, команды IMAP X-MASTERAUTH и "
        "X-SETUSER) — только с адресов из группы IP, заданной в Kerio. Без мастер-пароля — с паролями сотрудников, и "
        "тогда удалить учётную запись из раздела «Ящики на сервере» нельзя: перед удалением ящик сверяется с архивом "
        "по IMAP, а после закрытия входа пароль сотрудника не пускает.",
        "Закрытый вход (выключенная учётная запись) мастер-паролем, скорее всего, тоже не открыть — проверьте на своём "
        "сервере: если последняя копия перед удалением не удаётся, учётная запись не удаляется.",
        "«Закрыть вход» выключает учётную запись целиком: по документации Kerio выключенная учётная запись не "
        "принимает и новые письма.",
        "Отдел, должность, телефоны и организация хранятся в личном контакте пользователя; офис (почтовый адрес) "
        "MailArchiver не задаёт.",
        "Число писем API администрирования не отдаёт: у учётной записи есть только число всех элементов (письма, "
        "контакты, события, задачи) — оно показано в карточке учётной записи, а в сверке и отчёте писем нет.",
        "У учётных записей из службы каталогов (Active Directory, LDAP) пароль проверяет каталог — MailArchiver его не "
        "меняет и такие учётные записи не удаляет (это делается в каталоге).",
        "Удаление учётной записи удаляет и её папку с письмами на сервере — восстановить её нельзя (письма остаются "
        "в архиве).",
        "Псевдонимы — дополнительные адреса самой учётной записи; псевдонимы домена (Aliases), ведущие на неё, "
        "MailArchiver не показывает и не меняет.",
    )
    setup = (
        "Webadmin Kerio Connect (https://сервер:4040/admin) → Configuration → Administration Settings: включите «Allow "
        "administration from remote host» и ограничьте его группой IP-адресов, в которой есть адрес MailArchiver; "
        "откройте порт 4040 (HTTPS) в межсетевом экране.",
        "Accounts → Users: заведите для MailArchiver отдельную учётную запись (не личную) с обычным надёжным паролем. "
        "Вкладка Rights: «Whole server read/write» — для изменений, «Whole server read only» — только для сверки; "
        "для одного домена хватит прав администратора домена.",
        "Если сертификат сервера самоподписанный — «Проверить подключение» покажет его отпечаток SHA-256: сверьте "
        "его с Webadmin (Configuration → SSL Certificates) и нажмите «Доверять этому сертификату».",
        "Configuration → Services: служба IMAPS (993) должна работать — по ней копируются ящики.",
        "Чтобы копировать ящики без паролей сотрудников: Configuration → Advanced Options → Master Authentication — "
        "включите, задайте мастер-пароль и разрешите вход только группе IP-адресов с адресом MailArchiver; впишите "
        "мастер-пароль в подключение.",
        "Если сеансы обрываются на долгой сверке — Configuration → Advanced Options → Kerio Connect Client → Session "
        "security: увеличьте «Maximum session duration» (MailArchiver входит заново сам, но не больше раза на вызов).",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._http: Optional[_KerioHttp] = None
        self._token = ""
        self._cookie = ""
        self._ids = itertools.count(1)
        self._domain_list: Optional[List[Dict[str, Any]]] = None
        self._uids: Dict[str, str] = {}
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- адрес и сеанс ---------------------------------------------------------------------------
    @classmethod
    def parse_host(cls, host: Any, port: Any = None) -> Tuple[str, str, int]:
        """«mail.company.ru», «mail.company.ru:4040» или «https://…» → (схема, имя, порт)."""
        raw = str(host or "").strip()
        if not raw:
            raise ValidationError("Укажите адрес сервера Kerio Connect.")
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
        try:
            explicit = parsed.port
        except ValueError as exc:
            raise ValidationError(f"Порт в адресе сервера указан неверно: «{raw}».") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username \
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValidationError(f"Адрес сервера Kerio Connect указан неверно: «{raw}».",
                                  hint="Только имя или IP-адрес (можно с портом), без пути: mail.company.ru.")
        try:
            number = int(explicit or port or DEFAULT_PORT)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Порт администрирования — целое число.") from exc
        if not 1 <= number <= 65535:
            raise ValidationError("Порт администрирования — от 1 до 65535.")
        return parsed.scheme, parsed.hostname, number

    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        cls.parse_host(settings.get("host"), settings.get("port"))
        return []

    def base_url(self) -> str:
        scheme, host, port = self.parse_host(self.setting("host"), self.setting("port"))
        return f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"

    def imap_server(self) -> Tuple[str, int, str]:
        host = str(self.setting("imap_host") or "").strip()
        if not host:
            try:
                host = self.parse_host(self.setting("host"), self.setting("port"))[1]
            except ValidationError:
                host = ""
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")

    @property
    def http(self) -> _KerioHttp:
        if self._http is None:
            self._http = _KerioHttp(self.base_url(), api=self.api, verify_tls=bool(self.setting("verify_tls", True)),
                                    cert_sha256=self.setting("cert_sha256") or "",
                                    allow_plain=bool(self.setting("allow_plain")), server_label=self.label)
        return self._http

    def imap_admin_problem(self) -> str:
        return "" if self.secret("master_password") else ("не задан мастер-пароль (Master Authentication) — ящики "
                                                          "копируются с паролями сотрудников")

    def imap_auth(self, email: str) -> ImapAuth:
        problem = self.imap_admin_problem()
        if problem:
            raise ServerError(f"{self.label}: {problem}.",
                              hint="Подключение → «Мастер-пароль (Master Authentication)».")
        return ImapAuth(mech="kerio_master", user="", secret=self.secret("master_password"),
                        authzid=norm_email(email), host=self.imap_server()[0])

    @property
    def seen_fingerprint(self) -> str:
        return self._http.seen_fingerprint if self._http is not None else ""

    def _headers(self, auth: bool = True) -> Dict[str, str]:
        headers = {"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc"}
        if auth:
            headers.update({"X-Token": self._token, "Cookie": self._cookie})
        return headers

    def _post(self, body: Dict[str, Any], auth: bool = True, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        # истёкший сеанс Kerio отдаёт кодом 500 с ошибкой JSON-RPC внутри — её разбираем сами
        resp = self.http.request("POST", ENDPOINT, json_body=body, headers=self._headers(auth), expect=(200, 500),
                                 timeout=timeout, idempotent=body.get("method") in READ_METHODS)
        try:
            data = resp.json()
        except ServerError:
            data = None
        if not isinstance(data, dict) or not ("result" in data or "error" in data):
            if resp.status == 500:
                raise ServerError(f"{self.label}: ошибка сервера (500) при вызове {body.get('method')}.",
                                  retryable=True)
            raise ServerError(f"{self.label}: ответ на {body.get('method')} — не JSON-RPC: {resp.text()[:200]!r}",
                              hint="Проверьте адрес и порт: нужен API администрирования (обычно порт 4040).")
        return data

    def _login(self) -> None:
        user = str(self.setting("username") or "").strip()
        password = self.secret("password")
        if not user or not password:
            raise ServerAuthError(f"{self.label}: не заданы логин и пароль администратора.")
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "Session.login",
                "params": {"userName": user, "password": password,
                           "application": {"name": "MailArchiver", "vendor": "MailArchiver",
                                           "version": __version__}}}
        data = self._post(body, auth=False)
        if data.get("error"):
            raise ServerAuthError(
                f"{self.label}: вход администратора «{user}» отклонён: {error_text(data['error'])}",
                hint="Проверьте логин (не из основного домена — полным адресом) и обычный пароль (пароль приложения "
                     "не подойдёт), права администратора у учётной записи и что удалённое администрирование "
                     "разрешено с адреса MailArchiver.")
        token = str((data.get("result") or {}).get("token") or "")
        cookies = session_cookies(self.http.set_cookies)
        if not token or not any(name.upper().startswith("SESSION") for name in cookies):
            raise ServerError(f"{self.label}: сервер не прислал {'токен' if not token else 'cookie'} сеанса после "
                              f"входа.", hint="Проверьте, что адрес ведёт на Webadmin Kerio Connect (порт 4040), "
                                              "а не на прокси, который убирает заголовки.")
        self.api.secret(token)
        for value in cookies.values():
            self.api.secret(value)
        self._token = token
        self._cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def _error(self, err: Dict[str, Any], method: str) -> ServerError:
        code = _code(err)
        text = error_text(err)
        if code == E_EXISTS:
            return AlreadyExists(f"{self.label}: {text}")
        if code == E_NO_ENTITY:
            return NotFound(f"{self.label}: не найдено ({method}): {text}")
        if code == E_ACCESS_DENIED:
            return ServerAuthError(f"{self.label}: недостаточно прав ({method}): {text}",
                                   hint="Выдайте учётной записи подключения права «Whole server read/write» (или "
                                        "права администратора нужного домена).")
        if code == METHOD_NOT_FOUND:
            return ServerError(f"{self.label}: сервер не знает метод {method}: {text}",
                               hint="Возможно, версия Kerio Connect слишком старая.")
        return ServerError(f"{self.label}: {method} — ошибка {code if code is not None else '?'}: {text}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, *, _again: bool = True,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Вызов метода API; ошибка JSON-RPC — исключение; истёкший сеанс — вход заново (один раз)."""
        if not self._token:
            self._login()
        body: Dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        data = self._post(body, timeout=timeout)
        err = data.get("error")
        if isinstance(err, dict):
            if _code(err) == SESSION_EXPIRED and _again:
                self.api.note("сеанс Kerio Connect истёк — вход заново")
                self._token = self._cookie = ""
                return self.call(method, params, _again=False, timeout=timeout)
            raise self._error(err, method)
        if err:
            raise ServerError(f"{self.label}: {method} — ошибка: {err}")
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    def _checked(self, method: str, params: Dict[str, Any], *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Вызов изменения: непустой ``result.errors`` в ответе 200 — исключение с текстом первой ошибки."""
        result = self.call(method, params, timeout=timeout)
        errors = result.get("errors") or []
        if errors:
            raise self._error(errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}, method)
        return result

    def close(self) -> None:
        if not self._token or self._http is None:
            return
        try:
            self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "Session.logout"})
        except ServerError:
            pass
        finally:
            self._token = self._cookie = ""

    # -- чтение --------------------------------------------------------------------------------
    def _pages(self, method: str, params: Dict[str, Any], fields: List[str], order: str) -> Iterator[List[Dict]]:
        """Страницы списка (SearchQuery start/limit, totalItems); предел страниц — сверка неполная."""
        start = 0
        for _ in range(MAX_PAGES):
            query = {"fields": fields, "start": start, "limit": PAGE,
                     "orderBy": [{"columnName": order, "direction": "Asc", "caseSensitive": False}]}
            result = self.call(method, dict(params, query=query))
            items = [i for i in result.get("list") or [] if isinstance(i, dict)]
            if items:
                yield items
            start += len(items)
            try:
                total = int(result.get("totalItems"))
            except (TypeError, ValueError):
                total = None
            if not items or (total is not None and start >= total):
                return
        self.incomplete = True
        self.api.note(f"{method}: больше {MAX_PAGES} страниц — список прочитан не полностью")

    def _domains(self) -> List[Dict[str, Any]]:
        if self._domain_list is None:
            out: List[Dict[str, Any]] = []
            try:
                for page in self._pages("Domains.get", {}, DOMAIN_FIELDS, "name"):
                    out.extend(d for d in page if d.get("id") and d.get("name"))
            except ServerAuthError:
                # администратору домена список доменов может быть закрыт — тогда его собственный домен
                own = _out(self.call("Session.getDomain"), "domain") or {}
                out = [own] if own.get("id") and own.get("name") else []
            self._domain_list = out
        return self._domain_list

    def _domain(self, name: str) -> Optional[Dict[str, Any]]:
        name = norm_email(name)
        return next((d for d in self._domains() if norm_email(d.get("name")) == name), None)

    @staticmethod
    def _full(address: Any, domain: str) -> str:
        """Адрес из emailAddresses: без «@» — имя в домене учётной записи."""
        text = norm_email(address)
        return text if "@" in text else (f"{text}@{domain}" if text else "")

    def _contacts(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Личные контакты пользователей: id → контакт (порядок ответа — порядок запроса, без неудачных)."""
        if not ids:
            return {}
        result = self.call("Users.getPersonalContact", {"userIds": ids})
        failed = set()
        for err in result.get("errors") or []:
            try:
                failed.add(int(err.get("inputIndex")))
            except (AttributeError, TypeError, ValueError):
                pass
        ok_ids = [uid for i, uid in enumerate(ids) if i not in failed]
        contacts = result.get("contacts") or []
        if len(contacts) != len(ok_ids):
            self.api.note("личные контакты не сопоставить с пользователями — отдел и должность не прочитаны")
            return {}
        return {uid: c for uid, c in zip(ok_ids, contacts, strict=True) if isinstance(c, dict)}

    def _account(self, user: Dict[str, Any], domain: str, contact: Optional[Dict[str, Any]] = None
                 ) -> Optional[Dict[str, Any]]:
        login = norm_email(user.get("loginName"))
        if not login:
            return None
        email = f"{login}@{domain}"
        fwd = user.get("emailForwarding") or {}
        mode = str(fwd.get("mode") or FORWARD_OFF)
        quota = user.get("diskSizeLimit") or {}
        last = user.get("lastLoginInfo") or {}
        enabled = user.get("isEnabled")
        role = (user.get("role") or {}).get("userRole") or ""
        acc: Dict[str, Any] = {
            "email": email, "native_id": user.get("id") or "", "display_name": user.get("fullName") or "",
            "aliases": [a for a in (self._full(x, domain) for x in user.get("emailAddresses") or []) if a],
            "login_blocked": None if enabled is None else not enabled,
            "quota_bytes": size_bytes(quota.get("limit")) if quota.get("isActive") else None,
            "used_bytes": size_bytes(user.get("consumedSize")),
            "last_login": stamp_iso(last.get("dateTime")),
            "forward_to": [] if mode == FORWARD_OFF else [norm_email(a) for a in fwd.get("emailAddresses") or [] if a],
            "kind": "user",
            "info": {"source": "служба каталогов" if user.get("itemSource") == LDAP_SOURCE else "Kerio",
                     "auth": user.get("authType") or "", "role": ROLES.get(role, role),
                     "forwarding": mode, "last_login_protocol": last.get("protocol") or "",
                     "description": user.get("description") or ""},
        }
        # consumedItems — все элементы (письма, контакты, события, задачи), а не письма: в «писем» не идёт
        if user.get("consumedItems") is not None:
            acc["info"]["items"] = user.get("consumedItems")
        if contact:
            for key, name in CONTACT.items():
                acc[key] = str(contact.get(name) or "")
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        with_contacts = bool(self.setting("read_contacts", True))
        out: List[Dict[str, Any]] = []
        for dom in self._domains():
            name = norm_email(dom["name"])
            try:
                for page in self._pages("Users.get", {"domainId": dom["id"]}, USER_FIELDS, "loginName"):
                    contacts: Dict[str, Dict] = {}
                    if with_contacts:
                        try:
                            contacts = self._contacts([u["id"] for u in page if u.get("id")])
                        except ServerError as exc:
                            self.api.note(f"личные контакты домена {name} не прочитаны: {exc.message}")
                    for user in page:
                        acc = self._account(user, name, contacts.get(user.get("id")))
                        if acc is not None:
                            self._uids[acc["email"]] = acc["native_id"]
                            out.append(acc)
            except ServerAuthError as exc:
                # домен без прав — не читаем, но и пропавшими его учётные записи не считаем
                self.incomplete = True
                self.sync_problems.append(f"домен {name} не прочитан: {exc.message}")
        return out

    def _find(self, email: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Пользователь по адресу (свежие данные с сервера) и имя его домена."""
        local, domain = split_email(email)
        dom = self._domain(domain)
        if dom is None or not local:
            return None, domain
        query = {"fields": USER_FIELDS, "start": 0, "limit": 20, "combining": "And",
                 "conditions": [{"fieldName": "loginName", "comparator": "Eq", "value": local}]}
        result = self.call("Users.get", {"query": query, "domainId": dom["id"]})
        for user in result.get("list") or []:
            if norm_email(user.get("loginName")) == local and user.get("id"):
                self._uids[f"{local}@{domain}"] = user["id"]
                return user, domain
        return None, domain

    def _user(self, email: str) -> Tuple[Dict[str, Any], str]:
        user, domain = self._find(email)
        if user is None:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        return user, domain

    def _uid(self, email: str) -> str:
        email = norm_email(email)
        if email not in self._uids:
            self._user(email)
        return self._uids[email]

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user, domain = self._find(email)
        if user is None:
            return None
        contact = None
        if self.setting("read_contacts", True):
            try:
                contact = self._contacts([user["id"]]).get(user["id"])
            except ServerError:
                contact = None
        return self._account(user, domain, contact)

    # -- изменения -----------------------------------------------------------------------------
    def _set(self, email: str, pattern: Dict[str, Any]) -> None:
        self._checked("Users.set", {"userIds": [self._uid(email)], "pattern": pattern})

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        self._set(email, {"isEnabled": bool(enabled)})
        if enabled:
            return "вход открыт (учётная запись включена)"
        text = "вход закрыт (учётная запись выключена)"
        if keep_receiving:
            text += "; принимать письма выключенная учётная запись Kerio не будет"
        return text

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        user, _domain = self._user(email)
        auth = user.get("authType") or INTERNAL_AUTH
        if auth != INTERNAL_AUTH:
            raise ServerError(f"{self.label}: пароль учётной записи {norm_email(email)} проверяет служба каталогов "
                              f"({auth}) — меняйте его в каталоге.")
        self._set(email, {"password": password})
        return "пароль сменён"

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        limit = size_limit(quota_bytes)
        self._set(email, {"diskSizeLimit": limit})
        if not limit["isActive"]:
            return "квота снята"
        return f"квота {limit['limit']['value']} {limit['limit']['units']}"

    def _write_aliases(self, email: str, user: Dict[str, Any], stored: List[str], domain: str) -> List[str]:
        self._checked("Users.set", {"userIds": [user["id"]], "pattern": {"emailAddresses": stored}})
        return [a for a in (self._full(x, domain) for x in stored) if a]

    def add_alias(self, email: str, alias: str) -> List[str]:
        user, domain = self._user(email)
        alias = norm_email(alias)
        current = [str(x) for x in user.get("emailAddresses") or [] if x]
        if alias in (self._full(x, domain) for x in current):
            return [self._full(x, domain) for x in current]
        local, alias_domain = split_email(alias)
        full_style = any("@" in x for x in current)
        if not full_style and alias_domain != domain:
            raise ServerError(f"{self.label}: псевдоним учётной записи — только в её домене ({domain}), а не "
                              f"{alias_domain}.")
        # пишем так же, как сервер хранит уже заданные адреса (обычно — без домена)
        return self._write_aliases(email, user, current + [alias if full_style else local], domain)

    def remove_alias(self, email: str, alias: str) -> List[str]:
        user, domain = self._user(email)
        alias = norm_email(alias)
        current = [str(x) for x in user.get("emailAddresses") or [] if x]
        keep = [x for x in current if self._full(x, domain) != alias]
        if len(keep) == len(current):
            return [self._full(x, domain) for x in current]
        return self._write_aliases(email, user, keep, domain)

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        uid = self._uid(email)
        if "display_name" in values:
            self._set(email, {"fullName": values["display_name"]})
        change = {CONTACT[k]: v for k, v in values.items() if k in CONTACT}
        if change:
            # личный контакт записывается целиком: прочитать, поменять нужное, записать (прочее не теряется)
            current = self._contacts([uid]).get(uid)
            if current is None:
                raise ServerError(f"{self.label}: личный контакт {norm_email(email)} не прочитан — не записан.")
            if current.get("isReadOnly"):
                raise ServerError(f"{self.label}: личный контакт {norm_email(email)} только для чтения (из службы "
                                  f"каталогов) — меняйте его в каталоге.")
            contact = {k: v for k, v in current.items() if k != "isReadOnly"}
            contact.update(change)
            self._checked("Users.setPersonalContact", {"userIds": [uid], "contact": contact})
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        email = norm_email(email)
        local, domain = split_email(email)
        dom = self._domain(domain)
        if dom is None:
            raise NotFound(f"{self.label}: домена {domain} на сервере нет.")
        if self._find(email)[0] is not None:
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.")
        full = " ".join(x for x in (first_name, last_name) if x) or local
        user: Dict[str, Any] = {"domainId": dom["id"], "loginName": local, "fullName": full, "password": password,
                                "isEnabled": True}
        if quota_bytes:
            user["diskSizeLimit"] = size_limit(quota_bytes)
        result = self._checked("Users.create", {"users": [user]})
        created = [r for r in result.get("result") or [] if isinstance(r, dict) and r.get("id")]
        if not created:
            raise ServerError(f"{self.label}: сервер не подтвердил создание {email} (нет идентификатора).")
        uid = created[0]["id"]
        self._uids[email] = uid
        if first_name or last_name:
            try:
                current = self._contacts([uid]).get(uid) or {}
                contact = {k: v for k, v in current.items() if k != "isReadOnly"}
                contact.update({"firstName": first_name, "surName": last_name})
                self._checked("Users.setPersonalContact", {"userIds": [uid], "contact": contact})
            except ServerError as exc:
                self.api.note(f"имя и фамилия в личный контакт {email} не записаны: {exc.message}")
        return {"email": email, "native_id": uid, "display_name": full, "first_name": first_name,
                "last_name": last_name, "login_blocked": False, "quota_bytes": quota_bytes or None, "kind": "user"}

    def delete_account(self, email: str) -> str:
        user, _domain = self._user(email)
        if user.get("itemSource") == LDAP_SOURCE:
            raise ServerError(f"{self.label}: учётная запись {norm_email(email)} — из службы каталогов; удалите её в "
                              f"каталоге (MailArchiver не удаляет записи каталога).")
        self._checked("Users.remove", {"requests": [{"userId": user["id"], "method": "UDeleteFolder",
                                                     "removeReferences": True, "targetUserId": "",
                                                     "mode": "DSModeDelete"}]}, timeout=DELETE_TIMEOUT_S)
        self._uids.pop(norm_email(email), None)
        return "учётная запись и её папка с письмами удалены на сервере Kerio (письма остаются в архиве)"

    # -- пересылка -------------------------------------------------------------------------------
    def get_forwarding(self, email: str) -> Dict[str, Any]:
        user, _domain = self._user(email)
        fwd = user.get("emailForwarding") or {}
        mode = str(fwd.get("mode") or FORWARD_OFF)
        if mode == FORWARD_OFF:
            return {"addresses": [], "keep_copy": True}
        return {"addresses": [norm_email(a) for a in fwd.get("emailAddresses") or [] if a],
                "keep_copy": mode == FORWARD_KEEP}

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        targets = [norm_email(a) for a in addresses if a]
        if not targets:
            self._set(email, {"emailForwarding": {"mode": FORWARD_OFF, "emailAddresses": []}})
            return "пересылка выключена"
        mode = FORWARD_KEEP if keep_copy else FORWARD_ONLY
        self._set(email, {"emailForwarding": {"mode": mode, "emailAddresses": targets}})
        text = f"пересылка на {', '.join(targets)}"
        return text + ("; копия остаётся в ящике" if keep_copy else "; в ящик письма больше не доставляются")

    # -- состояние -------------------------------------------------------------------------------
    def test(self) -> Status:
        st = Status()
        me = _out(self.call("Session.whoAmI"), "userDetails") or {}
        role = (me.get("effectiveRole") or {}).get("userRole") or ""
        who = me.get("fullName") or me.get("loginName") or self.setting("username")
        st.details.append(f"Администратор: {who} — {ROLES.get(role, role or 'права неизвестны')}")
        if role == "UserRole":
            st.problems.append(("rights", "error", "у учётной записи подключения нет прав администратора — "
                                                   "выдайте их на вкладке Rights"))
        elif role == "Auditor" and self.conn.allow_changes:
            st.problems.append(("rights", "warning", "у учётной записи подключения права только на чтение — "
                                                     "изменения на сервере не пройдут"))
        try:
            info = _out(self.call("Server.getProductInfo"), "info") or {}
            st.version = " ".join(str(x) for x in (info.get("productName") or self.label, info.get("version")) if x)
            if info.get("buildNumber"):
                st.version += f" (сборка {info['buildNumber']})"
            if info.get("osName"):
                st.details.append(f"ОС сервера: {info['osName']}")
        except ServerError as exc:
            st.version = self.label
            st.problems.append(("version", "warning", f"версия сервера не прочитана: {exc.message}"))
        domains = [d["name"] for d in self._domains()]
        if domains:
            st.details.append(f"Домены ({len(domains)}): " + ", ".join(domains[:10]) + (" …" if len(domains) > 10
                                                                                         else ""))
        else:
            st.problems.append(("domains", "warning", "учётной записи подключения не виден ни один домен"))
        self._status_licence(st)
        try:
            queue = self.call("Queue.get", {"query": {"start": 0, "limit": 1}})
            st.queue = int(queue.get("totalItems") or 0)
        except (ServerError, TypeError, ValueError) as exc:
            st.problems.append(("queue", "warning", f"очередь писем не прочитана: {getattr(exc, 'message', exc)}"))
        try:
            alerts = _out(self.call("Server.getAlertList"), "alerts", "list") or []
        except ServerError:
            alerts = []
        for alert in alerts if isinstance(alerts, list) else []:
            level = {"Critical": "error", "Warning": "warning"}.get(str(alert.get("alertType") or ""))
            if level:
                value = f" ({alert.get('currentValue')} из {alert.get('criticalValue')})" \
                    if alert.get("criticalValue") not in (None, "") else ""
                st.problems.append((f"alert_{alert.get('alertName')}", level,
                                    f"сервер предупреждает: {alert.get('alertName')}{value}"))
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st

    def _status_licence(self, st: Status) -> None:
        """Лицензии (занято/всего) и регистрация: истекающие лицензия и подписка — проблемы."""
        try:
            about = _out(self.call("Server.getAboutInfo"), "aboutInfo", "info") or {}
            limit = about.get("allowedUsers") or {}
            st.seats_used = int(about["currentUsers"]) if about.get("currentUsers") is not None else None
            allowed = limit.get("allowedUsers", limit.get("limit")) if isinstance(limit, dict) else limit
            if not (isinstance(limit, dict) and limit.get("isUnlimited")) and allowed is not None \
                    and int(allowed) >= 0:
                st.seats_total = int(allowed)
        except (ServerError, TypeError, ValueError) as exc:
            st.problems.append(("licenses", "warning", f"число лицензий не прочитано: {getattr(exc, 'message', exc)}"))
        try:
            reg = _out(self.call("ProductRegistration.getFullStatus"), "status") or {}
        except ServerError as exc:
            st.problems.append(("registration", "warning", f"регистрация не прочитана: {exc.message}"))
            return
        reg_type = str(reg.get("regType") or "")
        if reg_type:
            st.details.append(f"Регистрация: {REG_TYPES.get(reg_type, reg_type)}"
                              + (f" ({reg['company']})" if reg.get("company") else ""))
        if reg_type == "rsTrialExpired":
            st.problems.append(("registration", "error", "пробный период Kerio Connect истёк"))
        elif reg_type in ("rsNoRegistration", "rsTrialRegistered"):
            st.problems.append(("registration", "warning", f"Kerio Connect {REG_TYPES[reg_type]}"))
        for item in reg.get("expirations") or []:
            if not isinstance(item, dict) or item.get("isUnlimited"):
                continue
            what = "лицензия" if item.get("type") == "License" else "подписка на обновления"
            try:
                days = int(item.get("remainingDays"))
            except (TypeError, ValueError):
                continue
            if days < 0:
                st.problems.append((f"expire_{item.get('type')}", "error" if what == "лицензия" else "warning",
                                    f"{what} Kerio Connect истекла"))
            elif days <= 30:
                st.problems.append((f"expire_{item.get('type')}", "warning",
                                    f"{what} Kerio Connect истекает через {days} дн."))


DRIVER = KerioDriver
