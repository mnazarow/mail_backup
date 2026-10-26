"""
Zimbra Collaboration и Zextras Carbonio — через SOAP API администратора (JSON, порт 7071).

Вход — ``AuthRequest`` администратора (глобального или делегированного);
токен сеанса живёт до 12 часов, хранится в кэше токенов и в журнал не
попадает. Истёк раньше (``service.AUTH_EXPIRED``) — вход заново, один раз.

Учётные записи — ``SearchDirectoryRequest`` (types=accounts, страницами по
``limit``/``offset``), занятое место и квота — ``GetQuotaUsageRequest``,
одна учётная запись — ``GetAccountRequest`` и ``GetMailboxRequest``.
Изменения — ``ModifyAccountRequest`` (атрибуты), ``SetPasswordRequest``,
``AddAccountAliasRequest``/``RemoveAccountAliasRequest``,
``CreateAccountRequest``, ``DeleteAccountRequest``. Автоответ и пересылка —
атрибуты ``zimbraPrefOutOfOffice*`` и ``zimbraPrefMailForwardingAddress``.

Копирование ящиков — по IMAP: SASL PLAIN, где логин и пароль — администратора,
а authzid — адрес ящика (нужно право ``adminLoginAs``; у глобального
администратора оно есть).

Carbonio — ответвление Zimbra 8.8.15 с теми же запросами; лицензии
(``GetLicenseRequest``) у Carbonio и Zimbra FOSS нет — проверка подключения
это учитывает.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, NotSupported, ServerAuthError,
                    ServerError, Status, check_email, imap_fields, norm_email, split_email, tls_fields)
from ..http import TOKENS, HttpClient, _is_loopback

SOAP_PATH = "/service/admin/soap"
#: Учётных записей на страницу поиска и предел страниц (дальше — «прочитано не всё»).
PAGE = 500
MAX_PAGES = 1000
#: Атрибуты учётной записи, которые читаем (остальные сервер не присылает).
ACCOUNT_ATTRS = (
    "zimbraId", "displayName", "givenName", "sn", "zimbraAccountStatus", "zimbraMailAlias", "zimbraMailQuota",
    "zimbraLastLogonTimestamp", "company", "title", "telephoneNumber", "mobile", "ou", "physicalDeliveryOfficeName",
    "zimbraPasswordMustChange", "zimbraPrefOutOfOfficeReplyEnabled", "zimbraPrefMailForwardingAddress",
    "zimbraFeatureMailForwardingEnabled", "zimbraPrefMailLocalDeliveryDisabled", "zimbraMailForwardingAddress",
    "zimbraIsAdminAccount", "zimbraIsDelegatedAdminAccount", "zimbraIsSystemResource", "zimbraIsSystemAccount",
    "zimbraMailHost", "zimbraCreateTimestamp", "zimbraCOSId", "zimbraMailStatus",
)
#: Атрибуты автоответа — только при чтении и записи автоответа (в сверку текст не тянем).
OOO_ATTRS = ("zimbraPrefOutOfOfficeReply", "zimbraPrefOutOfOfficeFromDate", "zimbraPrefOutOfOfficeUntilDate",
             "zimbraFeatureOutOfOfficeReplyEnabled")
#: Поля «ФИО, отдел, должность» → атрибуты Zimbra (отдел — ou: атрибута department в схеме Zimbra нет).
CONTACT_ATTRS = {"first_name": "givenName", "last_name": "sn", "display_name": "displayName", "department": "ou",
                 "title": "title", "phone": "telephoneNumber", "mobile": "mobile", "company": "company",
                 "office": "physicalDeliveryOfficeName"}
#: Ошибки SOAP, после которых нужно войти заново.
EXPIRED = ("service.AUTH_EXPIRED", "service.AUTH_REQUIRED")
#: Запросы только на чтение: их можно повторить после 502/503/504 (изменения — нет: сервер мог их выполнить).
READ_REQUESTS = frozenset({"SearchDirectory", "GetAccount", "GetMailbox", "GetQuotaUsage", "GetVersionInfo",
                           "GetLicense", "GetAllServers", "GetMailQueueInfo", "GetAllDomains"})
#: Сколько ждать ответа на DeleteAccount: Zimbra удаляет ящик (письма, файлы) до ответа — у большого это минуты.
DELETE_TIMEOUT_S = 600.0
_GENTIME = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\.\d+)?Z$")
#: Предел текста автоответа (zimbraPrefOutOfOfficeReply).
OOO_MAX = 8192


# ---------------------------------------------------------------------------
#  JSON SOAP: значения бывают «голыми», {"_content": …} и списком из них
# ---------------------------------------------------------------------------
def _v(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("_content")
    return "" if value is None else str(value)


def _list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    return [v for v in value or [] if isinstance(v, dict)]


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _truthy(value: Any) -> bool:
    return str(_v(value)).strip().lower() in ("1", "true", "yes")


def attrs_of(obj: Dict[str, Any]) -> Dict[str, Optional[List[str]]]:
    """Атрибуты ``<a n="…">`` объекта → {имя: [значения]}; None — читать атрибут нет прав (``pd="1"``)."""
    out: Dict[str, Optional[List[str]]] = {}
    for item in _list(obj.get("a")):
        name = item.get("n")
        if not name:
            continue
        if _truthy(item.get("pd")):
            out[name] = None
            continue
        values = out.setdefault(name, [])
        if values is not None and item.get("_content") is not None:
            values.append(str(item["_content"]))
    extra = obj.get("_attrs")
    if isinstance(extra, dict):
        for name, value in extra.items():
            values = out.setdefault(name, [])
            if values is not None:
                values.extend(str(v) for v in (value if isinstance(value, list) else [value]))
    return out


def gentime_iso(value: str) -> str:
    """``20261020080000Z`` → ``2026-10-20T08:00:00Z`` (не время — пусто)."""
    m = _GENTIME.match((value or "").strip())
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"


def gentime(day: str, end: bool = False) -> str:
    """``2026-11-01`` → ``20261101000000Z`` (конец дня — ``…235959Z``); пусто — пусто."""
    if not day:
        return ""
    try:
        d = date.fromisoformat(day)
    except ValueError as exc:
        raise ValidationError(f"Дата указана неверно: «{day}» (нужно ГГГГ-ММ-ДД).") from exc
    return d.strftime("%Y%m%d") + ("235959Z" if end else "000000Z")


def _addresses(value: str) -> List[str]:
    return [norm_email(a) for a in re.split(r"[\s,;]+", value or "") if a.strip()]


def _int(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_host(raw: str, default_port: Any = 7071) -> Tuple[str, str, int]:
    """Адрес из подключения («mail.example.ru», «mail:7071», «https://mail:7071») → (схема, имя, порт)."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("Укажите адрес сервера Zimbra.")
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    path = parsed.path.rstrip("/")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.username \
            or path not in ("", SOAP_PATH):
        raise ValidationError(f"Адрес сервера Zimbra указан неверно: «{raw}».",
                              hint="Например: mail.example.ru или https://mail.example.ru:7071")
    try:
        port = parsed.port or int(default_port or 7071)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Порт указан неверно: «{raw}».") from exc
    if not 0 < port < 65536:
        raise ValidationError(f"Порт указан неверно: {port}.")
    return parsed.scheme, parsed.hostname, port


class ZimbraDriver(Driver):
    type = "zimbra"
    label = "Zimbra / Carbonio"
    summary = ("SOAP API администратора Zimbra и Carbonio: учётные записи, вход, пароли, квота, псевдонимы, ФИО, "
               "создание и удаление, автоответ, пересылка; копирование ящиков по IMAP входом администратора "
               "«от имени» ящика.")
    cloud = False
    fields = (
        Field("host", "Адрес сервера Zimbra / Carbonio", required=True, placeholder="mail.example.ru",
              help="Сервер с почтовым хранилищем (mailbox), где работает SOAP администратора. Можно с портом или "
                   "целиком: https://mail.example.ru:7071."),
        Field("port", "Порт SOAP администратора", "int", default=7071, advanced=True,
              help="7071 — служба администрирования Zimbra и Carbonio (HTTPS)."),
        Field("admin", "Администратор", required=True, placeholder="backup-admin@example.ru",
              help="Учётная запись администратора: глобального или делегированного с правами на нужные домены."),
        Field("admin_password", "Пароль администратора", "password", secret=True, required=True),
        Field("domains", "Домены", placeholder="example.ru, example.com", advanced=True,
              help="Через запятую — работать только с этими доменами (обязательно для делегированного "
                   "администратора). Пусто — все домены сервера."),
        Field("allow_plain", "Разрешить без шифрования (http)", "bool", default=False, advanced=True,
              help="Только для доверенной сети и адреса вида http://…: пароль администратора и токен пойдут по сети "
                   "открытым текстом."),
        *imap_fields(),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.MUST_CHANGE, Cap.QUOTA,
                      Cap.ALIASES, Cap.CONTACT, Cap.CREATE, Cap.DELETE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN,
                      Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title", "phone", "mobile",
                      "company", "office")
    password_rule = ("от 8 до 64 знаков без пробелов; класс обслуживания (COS) может требовать заглавные буквы, "
                     "цифры и знаки")
    limits = (
        "«Заблокировать вход» ставит статус locked: вход закрыт, письма принимаются. Статус closed (письма "
        "отклоняются, адрес исключается из всех списков рассылки) MailArchiver не ставит — если приём нужно "
        "остановить, закройте учётную запись в консоли администратора.",
        "Последний вход Zimbra записывает не чаще раза в 7 дней (zimbraLastLogonTimestampFrequency) — это примерная "
        "дата; при частоте 0 её нет вовсе.",
        "Число писем в ящике SOAP администратора не отдаёт — в сверке только занятое место и квота.",
        "Отдел записывается в атрибут ou (подразделение): атрибута department в схеме Zimbra нет.",
        "Темы у автоответа Zimbra нет — она ставится первой строкой текста; даты автоответа — по UTC. Ответ один "
        "для всех отправителей (отдельный текст для внешних выключается).",
        "Удаление необратимо: учётная запись удаляется вместе с ящиком (вернуть — только из резервной копии "
        "сервера).",
        "Перед удалением архив сверяется с ящиком по IMAP уже после закрытия входа: проверьте на своей версии, что "
        "вход администратора «от имени» в ящик со статусом locked проходит («Проверить вход» в карточке ящика). "
        "Если нет — удаление остановится, ничего не удалив.",
        "Лицензия читается только у Zimbra Network Edition; у Carbonio и Zimbra FOSS её нет.",
    )
    setup = (
        "Заведите администратора для MailArchiver. Глобальный: zmprov ma backup-admin@example.ru "
        "zimbraIsAdminAccount TRUE. Делегированный (меньше прав): zmprov ma backup-admin@example.ru "
        "zimbraIsDelegatedAdminAccount TRUE и на каждый домен — zmprov grr domain example.ru usr "
        "backup-admin@example.ru domainAdminRights; домены перечислите в «Домены».",
        "Для копирования ящиков без паролей сотрудников делегированному администратору нужно право adminLoginAs "
        "(«View Mail»): zmprov grr domain example.ru usr backup-admin@example.ru adminLoginAs. У глобального "
        "администратора оно есть.",
        "Откройте порт 7071 (HTTPS, SOAP администратора) только для сервера MailArchiver. В Carbonio это внутренний "
        "порт хранилища — давайте доступ через доверенную сеть или VPN.",
        "IMAP: порт 993 (IMAPS) должен быть доступен, у учётных записей — включён IMAP (zimbraImapEnabled и "
        "zimbraPrefImapEnabled). Вход без шифрования (zimbraImapCleartextLoginEnabled) не включайте.",
        "Самоподписанный сертификат: «Проверить подключение» покажет его отпечаток — нажмите «Доверять этому "
        "сертификату».",
        "Чтобы последний вход был точнее: zmprov mcf zimbraLastLogonTimestampFrequency 1d.",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._http: Optional[HttpClient] = None
        self._ids: Dict[str, str] = {}
        self._denied: set = set()
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки ----------------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        scheme, host, _port = parse_host(str(settings.get("host") or ""), settings.get("port") or 7071)
        for d in cls._split_domains(settings.get("domains")):
            check_email(f"x@{d}", "Домен")
        if scheme == "http" and not settings.get("allow_plain") and not _is_loopback(host):
            raise ValidationError("Адрес с http:// — без шифрования: включите «Разрешить без шифрования» (только для "
                                  "доверенной сети) или укажите https.")
        return []

    @staticmethod
    def _split_domains(value: Any) -> List[str]:
        return [d.strip().lower() for d in re.split(r"[\s,;]+", str(value or "")) if d.strip()]

    def _endpoint(self) -> Tuple[str, str, int]:
        return parse_host(str(self.setting("host") or ""), self.setting("port") or 7071)

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            scheme, host, port = self._endpoint()
            shown = f"[{host}]" if ":" in host else host
            self._http = HttpClient(f"{scheme}://{shown}:{port}", api=self.api,
                                    verify_tls=bool(self.setting("verify_tls", True)),
                                    cert_sha256=str(self.setting("cert_sha256") or ""),
                                    allow_plain=bool(self.setting("allow_plain")), server_label=self.label)
        return self._http

    @property
    def seen_fingerprint(self) -> str:
        return self._http.seen_fingerprint if self._http is not None else ""

    def imap_server(self) -> Tuple[str, int, str]:
        host = str(self.setting("imap_host") or "").strip()
        if not host:
            try:
                host = self._endpoint()[1]
            except ValidationError:
                host = ""
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")

    # -- SOAP ---------------------------------------------------------------------------------
    def _fault(self, name: str, fault: Dict[str, Any]) -> ServerError:
        code = _v(_dig(fault, "Detail", "Error", "Code")) or _v(fault.get("faultcode"))
        reason = _v(_dig(fault, "Reason", "Text")) or _v(fault.get("faultstring")) or code or "без описания"
        label = self.label
        if code == "account.AUTH_FAILED":
            exc: ServerError = ServerAuthError(
                f"{label}: сервер не принял логин или пароль администратора ({reason}).",
                hint="Проверьте администратора и пароль; учётная запись должна быть администратором "
                     "(zimbraIsAdminAccount или zimbraIsDelegatedAdminAccount).")
        elif code == "service.PERM_DENIED":
            exc = ServerAuthError(f"{label}: не хватает прав администратора на {name}Request: {reason}.",
                                  hint="Выдайте учётной записи права (раздел «Что настроить на сервере») или "
                                       "перечислите в «Домены» только те, где права есть.")
        elif code in EXPIRED:
            exc = ServerAuthError(f"{label}: сервер не принял токен администратора ({reason}).")
        elif code in ("account.NO_SUCH_ACCOUNT", "account.NO_SUCH_ALIAS"):
            exc = NotFound(f"{label}: нет такой учётной записи или псевдонима ({reason}).")
        elif code in ("account.ACCOUNT_EXISTS", "account.ALIAS_EXISTS", "account.DISTRIBUTION_LIST_EXISTS"):
            exc = AlreadyExists(f"{label}: адрес уже занят ({reason}).")
        elif code == "account.NO_SUCH_DOMAIN":
            exc = ServerError(f"{label}: такого домена на сервере нет ({reason}).")
        elif code == "service.UNKNOWN_DOCUMENT":
            exc = NotSupported(f"{label}: сервер не знает запроса {name}Request ({reason}).")
        elif code == "account.TOO_MANY_SEARCH_RESULTS":
            exc = ServerError(f"{label}: слишком много учётных записей для одного поиска ({reason}).",
                              hint="Перечислите домены в «Домены» или увеличьте zimbra_directory_max_search_result.")
        elif code == "account.MAINTENANCE_MODE":
            exc = ServerError(f"{label}: учётная запись в режиме обслуживания ({reason}).", retryable=True)
        elif code == "service.INVALID_REQUEST":
            exc = ServerError(f"{label}: запрос {name}Request отклонён: {reason}.")
        else:
            exc = ServerError(f"{label}: ошибка {name}Request ({code or 'без кода'}): {reason}.",
                              retryable=code == "service.FAILURE")
        exc.zcode = code  # type: ignore[attr-defined]
        return exc

    def _post(self, name: str, body: Dict[str, Any], token: str, *, log_body: bool = True,
              timeout: Optional[float] = None) -> Dict[str, Any]:
        context: Dict[str, Any] = {"_jsns": "urn:zimbra"}
        if token:
            context["authToken"] = token
        envelope = {"Header": {"context": context},
                    "Body": {f"{name}Request": dict(body, _jsns="urn:zimbraAdmin")}}
        resp = self.http.request("POST", SOAP_PATH, json_body=envelope, expect=(200, 500), log_body=log_body,
                                 timeout=timeout, idempotent=name in READ_REQUESTS)
        try:
            data = resp.json()
        except ServerError as exc:
            raise ServerError(f"{self.label}: на {name}Request сервер ответил не SOAP ({resp.status}): "
                              f"{resp.text()[:200]}", retryable=resp.status >= 500) from exc
        out_body = data.get("Body") if isinstance(data, dict) else None
        if not isinstance(out_body, dict):
            raise ServerError(f"{self.label}: на {name}Request пришёл ответ без Body ({resp.status}).")
        if out_body.get("Fault") is not None:
            raise self._fault(name, out_body["Fault"] if isinstance(out_body["Fault"], dict) else {})
        out = out_body.get(f"{name}Response")
        if out is None:
            raise ServerError(f"{self.label}: в ответе нет {name}Response ({resp.status}).")
        return out if isinstance(out, dict) else {}

    def _token_key(self) -> Tuple:
        material = self.secret("admin_password")
        return ("zimbra", self.http.host, self.http.port, norm_email(self.setting("admin")),
                hashlib.sha256(material.encode("utf-8")).hexdigest()[:16])

    def token(self, *, fresh: bool = False) -> str:
        """Токен администратора (из кэша или ``AuthRequest``); в журнал не попадает."""
        key = self._token_key()
        if not fresh:
            cached = TOKENS.get(key)
            if cached:
                self.api.secret(cached)
                return cached
        TOKENS.drop(key)
        admin = str(self.setting("admin") or "").strip()
        password = self.secret("admin_password")
        if not admin or not password:
            raise ServerAuthError(f"{self.label}: не заданы администратор и его пароль.")
        resp = self._post("Auth", {"name": admin, "password": password}, "", log_body=False)
        token = _v(resp.get("authToken"))
        if not token:
            raise ServerAuthError(f"{self.label}: сервер не выдал токен администратора.")
        self.api.secret(token)
        lifetime_ms = _int(_v(resp.get("lifetime")))
        TOKENS.put(key, token, (lifetime_ms / 1000.0) if lifetime_ms else 12 * 3600)
        return token

    def call(self, name: str, body: Optional[Dict[str, Any]] = None, *, log_body: bool = True,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Запрос ``<name>Request`` администратора → ``<name>Response``; истёк токен — вход заново (один раз)."""
        try:
            return self._post(name, body or {}, self.token(), log_body=log_body, timeout=timeout)
        except ServerError as exc:
            if getattr(exc, "zcode", "") not in EXPIRED:
                raise
        self.api.note("токен администратора истёк — вход заново")
        return self._post(name, body or {}, self.token(fresh=True), log_body=log_body, timeout=timeout)

    # -- чтение ---------------------------------------------------------------------------------
    def _domains(self) -> List[str]:
        return self._split_domains(self.setting("domains"))

    def _account(self, raw: Dict[str, Any], usage: Optional[Dict[str, Tuple]] = None) -> Optional[Dict[str, Any]]:
        email = norm_email(raw.get("name"))
        if not email:
            return None
        at = attrs_of(raw)
        self._denied.update(k for k, v in at.items() if v is None)

        def one(name: str) -> Optional[str]:
            values = at.get(name, [])
            return None if values is None else (values[0] if values else "")

        def flag(name: str) -> Optional[bool]:
            value = one(name)
            return None if value is None else value.strip().upper() in ("TRUE", "1")

        status = (one("zimbraAccountStatus") or "").strip().lower()
        forwarding = _addresses(one("zimbraPrefMailForwardingAddress") or "")
        feature = flag("zimbraFeatureMailForwardingEnabled")
        if flag("zimbraIsSystemResource") or flag("zimbraIsSystemAccount"):
            kind = "system"
        elif flag("zimbraIsAdminAccount") or flag("zimbraIsDelegatedAdminAccount"):
            kind = "admin"
        else:
            kind = "user"
        quota = one("zimbraMailQuota")
        info = {"status": status, "mail_host": one("zimbraMailHost") or "", "cos_id": one("zimbraCOSId") or "",
                "created": gentime_iso(one("zimbraCreateTimestamp") or "")}
        hidden = at.get("zimbraMailForwardingAddress") or []
        if hidden:
            info["hidden_forward"] = ", ".join(hidden)
        if feature is False and forwarding:
            info["forwarding_disabled"] = "пересылка запрещена (zimbraFeatureMailForwardingEnabled), адреса: " + \
                                          ", ".join(forwarding)
        if (one("zimbraMailStatus") or "").lower() == "disabled":
            info["mail_status"] = "disabled"
        acc: Dict[str, Any] = {
            "email": email, "native_id": raw.get("id") or one("zimbraId") or "",
            "first_name": one("givenName") or "", "last_name": one("sn") or "",
            "display_name": one("displayName") or "",
            "login_blocked": None if not status else status != "active",
            "quota_bytes": _int(quota) if quota else None,
            "last_login": gentime_iso(one("zimbraLastLogonTimestamp") or ""),
            "must_change": flag("zimbraPasswordMustChange"),
            "autoreply": flag("zimbraPrefOutOfOfficeReplyEnabled"),
            "forward_to": [] if feature is False else forwarding,
            "kind": kind, "info": info,
        }
        aliases = at.get("zimbraMailAlias", [])
        if aliases is not None:
            # нет права читать псевдонимы — ключа нет: в снимке остаются прежние, а не «псевдонимов нет»
            acc["aliases"] = aliases
        for key, attr in CONTACT_ATTRS.items():
            if key not in ("first_name", "last_name", "display_name"):
                acc[key] = one(attr) or ""
        u = (usage or {}).get(email)
        if u:
            acc["used_bytes"] = u[0]
            if acc["quota_bytes"] is None and u[1]:
                acc["quota_bytes"] = u[1]
        return acc

    def _search(self, domain: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = {"limit": PAGE, "offset": offset, "types": "accounts", "applyCos": "1", "sortBy": "name",
                    "sortAscending": "1", "attrs": ",".join(ACCOUNT_ATTRS)}
            if domain:
                body["domain"] = domain
            resp = self.call("SearchDirectory", body)
            page = _list(resp.get("account"))
            out.extend(page)
            if not _truthy(resp.get("more")) or not page:
                return out
            offset += len(page)
        self.incomplete = True
        return out

    def _all_accounts(self) -> List[Dict[str, Any]]:
        domains = self._domains()
        if domains:
            return [a for d in domains for a in self._search(d)]
        try:
            return self._search("")
        except ServerError as exc:
            if getattr(exc, "zcode", "") != "account.TOO_MANY_SEARCH_RESULTS":
                raise
        self.api.note("учётных записей больше предела одного поиска — читаем по доменам")
        names = [str(d.get("name") or "") for d in _list(self.call("GetAllDomains").get("domain"))]
        return [a for d in names if d for a in self._search(d)]

    def usage(self) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
        """Занятое место и квота: адрес → (байт занято, предел). Нет прав — пусто и проблема сверки."""
        out: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
        try:
            for domain in self._domains() or [""]:
                offset = 0
                for _ in range(MAX_PAGES):
                    body: Dict[str, Any] = {"limit": PAGE, "offset": offset, "allServers": "1"}
                    if domain:
                        body["domain"] = domain
                    resp = self.call("GetQuotaUsage", body)
                    page = _list(resp.get("account"))
                    for item in page:
                        out[norm_email(item.get("name"))] = (_int(_v(item.get("used"))), _int(_v(item.get("limit"))))
                    if not _truthy(resp.get("more")) or not page:
                        break
                    offset += len(page)
        except ServerAuthError as exc:
            self.sync_problems.append(f"занятое место не прочитано — нет права getDomainQuotaUsage: {exc.message}")
            return {}
        except ServerError as exc:
            self.api.note(f"занятое место не прочитано: {exc.message}")
            return {}
        return out

    def list_accounts(self) -> List[Dict[str, Any]]:
        raws = self._all_accounts()
        usage = self.usage()
        out = []
        seen = set()
        for raw in raws:
            acc = self._account(raw, usage)
            if acc is None or acc["email"] in seen:
                continue
            seen.add(acc["email"])
            self._ids[acc["email"]] = acc["native_id"]
            out.append(acc)
        if self._denied:
            self.sync_problems.append("администратору не хватает прав читать атрибуты: " +
                                      ", ".join(sorted(self._denied)) + " — эти поля в сверке пустые" +
                                      ("; псевдонимы — как при прошлой сверке" if "zimbraMailAlias" in self._denied
                                       else ""))
        return out

    def _get(self, email: str, extra: Tuple[str, ...] = ()) -> Optional[Dict[str, Any]]:
        email = norm_email(email)
        try:
            resp = self.call("GetAccount", {"account": {"by": "name", "_content": email}, "applyCos": "1",
                                            "attrs": ",".join(ACCOUNT_ATTRS + tuple(extra))})
        except NotFound:
            return None
        raws = _list(resp.get("account"))
        # по псевдониму сервер отдаёт учётную запись-владельца: это не та учётная запись
        if not raws or norm_email(raws[0].get("name")) != email:
            return None
        self._ids[email] = str(raws[0].get("id") or "")
        return raws[0]

    def _need(self, email: str, extra: Tuple[str, ...] = ()) -> Dict[str, Any]:
        raw = self._get(email, extra)
        if raw is None:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        return raw

    def _id(self, email: str) -> str:
        email = norm_email(email)
        if not self._ids.get(email):
            self._need(email)
        return self._ids[email]

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        raw = self._get(email)
        if raw is None:
            return None
        acc = self._account(raw)
        try:
            box = _list(self.call("GetMailbox", {"mbox": {"id": acc["native_id"]}}).get("mbox"))
            if box and _int(_v(box[0].get("s"))) is not None:
                acc["used_bytes"] = _int(_v(box[0].get("s")))
        except ServerError as exc:
            self.api.note(f"занятое место {acc['email']} не прочитано: {exc.message}")
        return acc

    # -- изменения --------------------------------------------------------------------------------
    def _modify(self, email: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
        items = []
        for name, value in attrs.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values or [""]:
                items.append({"n": name, "_content": "" if item is None else str(item)})
        return self.call("ModifyAccount", {"id": {"_content": self._id(email)}, "a": items})

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        if enabled:
            self._modify(email, {"zimbraAccountStatus": "active"})
            return "вход открыт (статус active)"
        self._modify(email, {"zimbraAccountStatus": "locked"})
        text = "вход закрыт (статус locked: письма по-прежнему принимаются)"
        if not keep_receiving:
            text += ("; приём писем не остановлен — статус closed исключил бы адрес из всех списков рассылки, его "
                     "ставят вручную в консоли администратора")
        return text

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        self.api.secret(password)
        resp = self.call("SetPassword", {"id": {"_content": self._id(email)}, "newPassword": {"_content": password}})
        self._modify(email, {"zimbraPasswordMustChange": "TRUE" if must_change else "FALSE"})
        text = "пароль сменён" + ("; при следующем входе — смена пароля" if must_change else "")
        note = _v(resp.get("message"))
        if note:
            text += f"; сервер предупредил о политике паролей: {note}"
        return text

    def set_must_change(self, email: str, value: bool) -> str:
        self._modify(email, {"zimbraPasswordMustChange": "TRUE" if value else "FALSE"})
        return "при следующем входе потребуется сменить пароль" if value else "требование снято"

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        value = int(quota_bytes) if quota_bytes else 0
        if value < 0:
            raise ValidationError("Квота не может быть отрицательной.")
        self._modify(email, {"zimbraMailQuota": str(value)})
        return "" if value else "квота снята (без предела)"

    def add_alias(self, email: str, alias: str) -> Optional[List[str]]:
        """→ псевдонимы после изменения; None — прочитать их нет прав (снимок не трогать)."""
        alias = check_email(alias, "Псевдоним")
        raw = self._need(email)
        self.call("AddAccountAlias", {"id": raw.get("id"), "alias": alias})
        known = attrs_of(raw).get("zimbraMailAlias", [])
        if known is None:
            return None
        have = [norm_email(a) for a in known]
        return have + ([alias] if alias not in have else [])

    def remove_alias(self, email: str, alias: str) -> Optional[List[str]]:
        """→ псевдонимы после изменения; None — прочитать их нет прав (снимок не трогать)."""
        alias = norm_email(alias)
        raw = self._need(email)
        self.call("RemoveAccountAlias", {"id": raw.get("id"), "alias": alias})
        known = attrs_of(raw).get("zimbraMailAlias", [])
        if known is None:
            return None
        return [a for a in (norm_email(x) for x in known) if a != alias]

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        attrs = {CONTACT_ATTRS[k]: (v or "") for k, v in values.items() if k in CONTACT_ATTRS}
        if attrs:
            self._modify(email, attrs)
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        email = check_email(email)
        self.check_password(password)
        self.api.secret(password)
        local, _domain = split_email(email)
        attrs = [{"n": "displayName", "_content": " ".join(x for x in (first_name, last_name) if x) or local}]
        if first_name:
            attrs.append({"n": "givenName", "_content": first_name})
        if last_name:
            attrs.append({"n": "sn", "_content": last_name})
        if must_change:
            attrs.append({"n": "zimbraPasswordMustChange", "_content": "TRUE"})
        if quota_bytes:
            attrs.append({"n": "zimbraMailQuota", "_content": str(int(quota_bytes))})
        try:
            resp = self.call("CreateAccount", {"name": {"_content": email}, "password": {"_content": password},
                                               "a": attrs})
        except AlreadyExists as exc:
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.") from exc
        raws = _list(resp.get("account"))
        acc = self._account(raws[0]) if raws else None
        if acc is None:
            return {"email": email, "first_name": first_name, "last_name": last_name}
        self._ids[email] = acc["native_id"]
        return acc

    def delete_account(self, email: str) -> str:
        self.call("DeleteAccount", {"id": {"_content": self._id(email)}}, timeout=DELETE_TIMEOUT_S)
        self._ids.pop(norm_email(email), None)
        return "учётная запись удалена вместе с ящиком (вернуть — только из резервной копии сервера)"

    # -- автоответ и пересылка ----------------------------------------------------------------
    def get_autoreply(self, email: str) -> Dict[str, Any]:
        at = attrs_of(self._need(email, OOO_ATTRS))

        def one(name: str) -> str:
            return (at.get(name) or [""])[0]
        return {"enabled": one("zimbraPrefOutOfOfficeReplyEnabled").upper() == "TRUE", "subject": "",
                "text": one("zimbraPrefOutOfOfficeReply"),
                "start": gentime_iso(one("zimbraPrefOutOfOfficeFromDate"))[:10],
                "end": gentime_iso(one("zimbraPrefOutOfOfficeUntilDate"))[:10]}

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        if not enabled:
            self._modify(email, {"zimbraPrefOutOfOfficeReplyEnabled": "FALSE"})
            return "автоответ выключен"
        body = (text or "").replace("\r\n", "\n").strip()
        if (subject or "").strip():
            body = f"{subject.strip()}\n\n{body}"
        if not body:
            raise ValidationError("Текст автоответа пуст.")
        if len(body) > OOO_MAX:
            raise ValidationError(f"Текст автоответа длиннее {OOO_MAX} знаков — Zimbra его не примет.")
        attrs = {"zimbraPrefOutOfOfficeReplyEnabled": "TRUE", "zimbraPrefOutOfOfficeReply": body,
                 "zimbraPrefOutOfOfficeExternalReplyEnabled": "FALSE",
                 "zimbraPrefOutOfOfficeSuppressExternalReply": "FALSE",
                 "zimbraPrefOutOfOfficeFromDate": gentime(start), "zimbraPrefOutOfOfficeUntilDate": gentime(end, True)}
        note = ""
        if (attrs_of(self._need(email, OOO_ATTRS)).get("zimbraFeatureOutOfOfficeReplyEnabled")
                or [""])[0].upper() == "FALSE":
            attrs["zimbraFeatureOutOfOfficeReplyEnabled"] = "TRUE"
            note = "; автоответ был запрещён учётной записи — разрешён"
        self._modify(email, attrs)
        period = f" ({start or '…'} — {end or '…'}, UTC)" if start or end else ""
        return f"автоответ включён{period}{note}"

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        at = attrs_of(self._need(email))
        addresses = _addresses((at.get("zimbraPrefMailForwardingAddress") or [""])[0])
        local_off = (at.get("zimbraPrefMailLocalDeliveryDisabled") or [""])[0].upper() == "TRUE"
        return {"addresses": addresses, "keep_copy": not local_off}

    def set_forwarding(self, email: str, *, addresses, keep_copy: bool = True) -> str:
        targets = [check_email(a, "Адрес пересылки") for a in addresses or []]
        if not targets:
            # без адреса пересылки и с выключенной доставкой в ящик письма пропадали бы — доставку включаем
            self._modify(email, {"zimbraPrefMailForwardingAddress": "", "zimbraPrefMailLocalDeliveryDisabled": "FALSE"})
            return "пересылка выключена"
        attrs = {"zimbraPrefMailForwardingAddress": ",".join(targets),
                 "zimbraPrefMailLocalDeliveryDisabled": "FALSE" if keep_copy else "TRUE"}
        note = ""
        if (attrs_of(self._need(email)).get("zimbraFeatureMailForwardingEnabled") or [""])[0].upper() == "FALSE":
            attrs["zimbraFeatureMailForwardingEnabled"] = "TRUE"
            note = "; пересылка была запрещена учётной записи — разрешена"
        self._modify(email, attrs)
        return (f"пересылка на {', '.join(targets)}; " +
                ("копия остаётся в ящике" if keep_copy else "в ящике письма не остаются") + note)

    # -- IMAP и состояние ----------------------------------------------------------------------
    def imap_auth(self, email: str) -> ImapAuth:
        admin = str(self.setting("admin") or "").strip()
        password = self.secret("admin_password")
        if not admin or not password:
            raise ServerAuthError(f"{self.label}: не заданы администратор и его пароль.")
        return ImapAuth(mech="plain_authzid", user=admin, secret=password, authzid=norm_email(email),
                        host=self.imap_server()[0])

    def _license(self, st: Status) -> None:
        try:
            lic = self.call("GetLicense")
        except NotSupported:
            st.details.append("Лицензия: нет (Zimbra FOSS)")
            return
        except ServerAuthError:
            st.problems.append(("license", "warning", "лицензия не прочитана — администратору не хватает прав"))
            return
        except ServerError as exc:
            st.problems.append(("license", "warning", f"лицензия не прочитана: {exc.message}"))
            return
        def flat(name: str) -> Dict[str, str]:
            items = _list(lic.get(name))
            return {k: (v or [""])[0] for k, v in attrs_of(items[0]).items()} if items else {}
        terms, info = flat("license"), flat("info")
        used = _int(info.get("TotalAccounts", ""))
        total = _int(terms.get("AccountsLimit", ""))
        if used is not None and used >= 0:
            st.seats_used = used
        if total is not None and total > 0:
            st.seats_total = total
        line = "Лицензия Network Edition"
        if st.seats_used is not None or st.seats_total is not None:
            line += f": занято {'?' if st.seats_used is None else st.seats_used} из " \
                    f"{'?' if st.seats_total is None else st.seats_total}"
        if terms.get("ValidUntil"):
            line += f", действует до {gentime_iso(terms['ValidUntil'])[:10] or terms['ValidUntil']}"
        st.details.append(line)
        state = (info.get("Status") or "").strip()
        if state and state.upper() not in ("OK", "ACTIVE", "VALID"):
            st.problems.append(("license_state", "warning", f"лицензия Zimbra: {state} — проверьте её в консоли"))

    def _queue(self, st: Status) -> None:
        try:
            servers = _list(self.call("GetAllServers").get("server"))
        except ServerError as exc:
            st.problems.append(("queue", "warning", f"очередь писем не прочитана: {exc.message}"))
            return
        mta = [s for s in servers if "mta" in [x.lower() for x in attrs_of(s).get("zimbraServiceEnabled") or []]]
        total = 0
        seen = False
        for server in mta or servers:
            name = str(server.get("name") or "")
            if not name:
                continue
            try:
                resp = self.call("GetMailQueueInfo", {"server": {"name": name}})
            except ServerError as exc:
                if mta:
                    st.problems.append(("queue", "warning", f"очередь писем на {name} не прочитана: {exc.message}"))
                continue
            for srv in _list(resp.get("server")):
                counts = {str(q.get("name") or ""): _int(_v(q.get("n"))) or 0 for q in _list(srv.get("queue"))}
                seen = True
                total += sum(counts.values())
                if counts:
                    st.details.append(f"Очередь {srv.get('name') or name}: " +
                                      ", ".join(f"{k} {v}" for k, v in counts.items()))
                if counts.get("hold") or counts.get("corrupt"):
                    st.problems.append(("queue_hold", "warning",
                                        f"в очереди {srv.get('name') or name} задержано {counts.get('hold', 0)} и "
                                        f"повреждено {counts.get('corrupt', 0)} писем"))
        if seen:
            st.queue = total

    def test(self) -> Status:
        st = Status()
        info = _list(self.call("GetVersionInfo").get("info"))
        info0 = info[0] if info else {}
        version = _v(info0.get("version"))
        carbonio = "zextras" in version.lower() or "carbonio" in str(info0.get("type") or "").lower()
        st.version = ("Carbonio " if carbonio else "Zimbra ") + (version or "?")
        if info0.get("host"):
            st.details.append(f"Сервер: {info0['host']}")
        build = " ".join(str(info0.get(k)) for k in ("release", "buildDate") if info0.get(k))
        if build:
            st.details.append(f"Сборка: {build}")
        total = 0
        for domain in self._domains() or [""]:
            body: Dict[str, Any] = {"limit": 1, "offset": 0, "types": "accounts", "attrs": "zimbraId"}
            if domain:
                body["domain"] = domain
            resp = self.call("SearchDirectory", body)
            total += _int(_v(resp.get("searchTotal"))) or len(_list(resp.get("account")))
        st.details.append(f"Учётных записей: {total}" + (f" (домены: {', '.join(self._domains())})"
                                                         if self._domains() else ""))
        try:
            body = {"limit": 1, "offset": 0}
            if self._domains():
                body["domain"] = self._domains()[0]
            self.call("GetQuotaUsage", body)
        except ServerAuthError:
            st.problems.append(("usage", "warning", "занятое место не прочитать — нет права getDomainQuotaUsage"))
        if carbonio:
            st.details.append("Лицензия: у Carbonio не читается")
        else:
            self._license(st)
        self._queue(st)
        if self.http.scheme == "http" and not _is_loopback(self.http.host):
            st.problems.append(("plain", "warning", "SOAP администратора — без шифрования (http)"))
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = ZimbraDriver
