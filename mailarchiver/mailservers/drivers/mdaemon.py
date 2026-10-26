"""
MDaemon — через XML API (``/MdMgmtWS`` на веб-сервере удалённого администрирования или на сайте Webmail в IIS).

Сеанс: операция ``Logon`` с HTTP Basic (логин и пароль администратора) отдаёт токен ``Result/Token`` — дальше он
идёт в заголовке ``X-MDAPI-TOKEN`` каждого запроса (Basic — только у Logon); токен не принят — вход заново, один
раз на вызов; по окончании — ``Logoff``. Каждый ответ несёт ``<Status id=".." value="0x…" message=".."/>``:
id 0 — успех, иначе — ошибка с текстом сервера. Имена элементов чувствительны к регистру.

Публично (сообщения сотрудников MDaemon на форуме, справка) описаны: GetDomainList, GetDomainInfo (учётные записи
домена с разделами Quotas/Details/Aliases), GetUserInfo, CreateUser (Details: Password, FirstName, LastName) и
UpdateUser с блоками Quotas (ApplyQuotas, MaxDiskSpace — МБ), Forwarding (Enabled, RetainMail, Addresses/Address,
Schedule) и AutoResponder (Enabled, Start, End, Response). Полный справочник (MD-API.html) есть только в поставке
сервера, поэтому то, чего публично нет (состояние учётной записи, пароль и ФИО в UpdateUser, псевдонимы, удаление,
последний вход, занятое место, лицензии, очередь), драйвер не делает.
"""
from __future__ import annotations

import base64
import copy
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...errors import ValidationError
from ..base import (EMAIL_RE, AlreadyExists, Cap, Driver, Field, NotFound, ServerAuthError, ServerError,
                    ServerUnreachable, Status, imap_fields, norm_email, split_email, tls_fields)
from ..http import HttpClient

ENDPOINT = "/MdMgmtWS"
#: версия запроса Logon, пока сервер не назвал свою (дальше — major.minor из ответа сервера)
DEFAULT_VERSION = "24.5"
#: «без даты» в расписаниях пересылки и автоответа
NO_DATE = "0000-00-00T00:00:00.000Z"
MB = 1024 * 1024
#: текст отказа, после которого стоит войти заново (токен истёк или не принят)
SESSION_RX = re.compile(r"token|session|logon|log on|logged|expired|сеанс|токен", re.IGNORECASE)
VERSION_RX = re.compile(r"^\d+(\.\d+){0,3}$")
#: операции только для чтения: их можно повторить после 502/503/504 (изменения — нет: сервер мог их выполнить)
READ_OPERATIONS = frozenset({"GetDomainList", "GetDomainInfo", "GetUserInfo"})
#: элементы запроса с секретом (Details/Password): тело с ними в журнал не пишется — только копия без значений
SECRET_TAG = re.compile(r"(password|secret|token)$", re.IGNORECASE)
LOGON_HINT = ("Проверьте логин и пароль администратора MDaemon (пароль приложения не даёт прав администратора) и что "
              "адрес MailArchiver разрешён: Setup → XML API Management → Address Restrictions.")


def _child(parent: ET.Element, tag: str, text: Any = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _text(el: Optional[ET.Element], path: str) -> str:
    if el is None:
        return ""
    node = el.find(path)
    return (node.text or "").strip() if node is not None else ""


def _attr_or_child(el: ET.Element, name: str) -> str:
    """Значение атрибута или одноимённого элемента (сотрудники MDaemon читают ``$d.id`` — годится и то и другое)."""
    return (el.get(name) or _text(el, name) or "").strip()


def _flag(value: str) -> Optional[bool]:
    low = (value or "").strip().lower()
    if low in ("yes", "true", "1", "on"):
        return True
    if low in ("no", "false", "0", "off"):
        return False
    return None


def _int(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _date(value: str) -> str:
    """Дата из «2024-12-15T17:00:00.000Z» (пусто — для «0000-00-00…»)."""
    day = (value or "")[:10]
    return "" if not day or day.startswith("0000") else day


def forwarding_block(block: Optional[ET.Element]) -> Optional[Dict[str, Any]]:
    """Блок Forwarding (как его пишет UpdateUser) → {enabled, addresses, keep_copy}; нет блока — None."""
    if block is None:
        return None
    addresses = [norm_email(a.text) for a in block.findall("Addresses/Address") if (a.text or "").strip()]
    return {"enabled": bool(_flag(_text(block, "Enabled"))), "addresses": addresses,
            "keep_copy": _flag(_text(block, "RetainMail")) is not False}


def autoresponder_block(block: Optional[ET.Element]) -> Optional[Dict[str, Any]]:
    """Блок AutoResponder → {enabled, text, start, end}; нет блока — None."""
    if block is None:
        return None
    return {"enabled": bool(_flag(_text(block, "Enabled"))), "text": _text(block, "Response"),
            "start": _date(_text(block, "Start")), "end": _date(_text(block, "End"))}


def aliases_block(block: Optional[ET.Element], email: str) -> List[str]:
    """Адреса из раздела Aliases: состав раздела публично не описан — берём всё, что выглядит как адрес (без
    подстановочных * и ?)."""
    if block is None:
        return []
    out: List[str] = []
    for el in block.iter():
        for value in [el.text or "", *el.attrib.values()]:
            address = norm_email(value)
            if address and address != email and "*" not in address and "?" not in address \
                    and EMAIL_RE.match(address) and address not in out:
                out.append(address)
    return out


class MDaemonDriver(Driver):
    type = "mdaemon"
    label = "MDaemon"
    summary = ("XML API MDaemon: учётные записи всех доменов (квота, число писем, псевдонимы), квота, создание, "
               "автоответ и пересылка; ящики копируются по IMAP с паролями сотрудников.")
    cloud = False
    fields = (
        Field("base_url", "Адрес XML API MDaemon", required=True, placeholder="https://mail.company.ru:444",
              help="Веб-сервер удалённого администрирования MDaemon (HTTPS — обычно порт 444, HTTP — 1000) или сайт "
                   "Webmail в IIS. Путь /MdMgmtWS добавится сам."),
        Field("username", "Учётная запись администратора", required=True, placeholder="admin@company.ru",
              help="Глобальный администратор (администратору домена видны только его домены)."),
        Field("password", "Пароль администратора", "password", required=True, secret=True,
              help="Обычный пароль: вход по паролю приложения — без прав администратора."),
        Field("api_version", "Версия запросов XML API", advanced=True, placeholder="как у сервера",
              help="Атрибут version запросов (например, 25.0). Пусто — по версии, которую сервер называет при входе."),
        Field("allow_plain", "Разрешить подключение без шифрования (http)", "bool", default=False, advanced=True,
              help="Только для доверенной сети: пароль администратора уйдёт открытым текстом."),
        *imap_fields(),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.QUOTA, Cap.CREATE, Cap.AUTOREPLY, Cap.FORWARD, Cap.STATUS})
    contact_fields = ()
    password_rule = ("от 8 до 64 знаков, без пробелов; если в MDaemon включены надёжные пароли — ещё заглавные, "
                     "строчные и цифры, без имени ящика")
    limits = (
        "Закрыть вход, сменить пароль, ФИО или псевдонимы и удалить учётную запись XML API умеет, но нужные элементы "
        "не описаны публично (только в справочнике MD-API.html из поставки сервера) — MailArchiver этого пока не "
        "делает: используйте MDaemon Remote Administration.",
        "Копирования без паролей сотрудников у MDaemon нет: мастер-пароля IMAP нет, а вход глобального администратора "
        "в чужие ящики не описан. Ящики копируются с паролями сотрудников.",
        "Последний вход и занятое место XML API не показывает (видны квота и число писем) — при смене квоты проверка "
        "«занято больше новой квоты» не выполняется.",
        "Лицензии и очередь писем XML API публично не описывает — в состоянии сервера только версия и домены.",
        "Тему автоответа XML API не задаёт — у ответа будет тема, которую ставит сервер.",
        "Требование сменить пароль при первом входе XML API публично не описывает — новые учётные записи создаются "
        "без него.",
    )
    setup = (
        "MDaemon Remote Administration → Setup → Web & IM Services → Remote Administration: включите веб-сервер "
        "удалённого администрирования, лучше «HTTPS only» (порт 444), — или запустите XML API на сайте Webmail в IIS "
        "(обработчик MDMgmtws.dll, путь /MdMgmtWS).",
        "Setup → XML API Management → Address Restrictions: разрешите адрес MailArchiver (с версии 24.0 XML API "
        "отказывает всем адресам, которые не разрешены явно).",
        "Учётная запись для MailArchiver: Account Editor → Administrative Roles → «Account is a global administrator» "
        "(или администратор домена — тогда видны только его домены); обычный надёжный пароль, не пароль приложения.",
        "Если сертификат самоподписанный — «Проверить подключение» покажет его отпечаток SHA-256: сверьте его и "
        "нажмите «Доверять этому сертификату».",
        "IMAP (IMAPS, порт 993) должен быть включён — по нему ящики копируются с паролями сотрудников.",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._http: Optional[HttpClient] = None
        self._path = ENDPOINT
        self._token = ""
        self._timeout = ""
        self._product = ""
        self._service = ""
        self._domain_names: Optional[List[str]] = None
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- адрес и сеанс ---------------------------------------------------------------------------
    @classmethod
    def parse_url(cls, value: Any) -> Tuple[str, str]:
        """Адрес из настроек → (схема://сервер:порт, путь к /MdMgmtWS)."""
        raw = str(value or "").strip()
        if not raw:
            raise ValidationError("Укажите адрес XML API MDaemon.")
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
        try:
            _port = parsed.port                       # неверный порт («:abc») — ValueError
        except ValueError as exc:
            raise ValidationError(f"Порт в адресе XML API указан неверно: «{raw}».") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query \
                or parsed.fragment:
            raise ValidationError(f"Адрес XML API MDaemon указан неверно: «{raw}».",
                                  hint="Например: https://mail.company.ru:444")
        path = parsed.path.rstrip("/")
        if not path.lower().endswith(ENDPOINT.lower()):
            path += ENDPOINT
        return f"{parsed.scheme}://{parsed.netloc}", path

    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        cls.parse_url(settings.get("base_url"))
        version = str(settings.get("api_version") or "").strip()
        if version and not VERSION_RX.match(version):
            raise ValidationError("Версия запросов XML API — числа через точку, например 25.0.")
        return []

    def imap_server(self) -> Tuple[str, int, str]:
        host = str(self.setting("imap_host") or "").strip()
        if not host:
            try:
                host = urllib.parse.urlsplit(self.parse_url(self.setting("base_url"))[0]).hostname or ""
            except ValidationError:
                host = ""
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            origin, self._path = self.parse_url(self.setting("base_url"))
            self._http = HttpClient(origin, api=self.api, verify_tls=bool(self.setting("verify_tls", True)),
                                    cert_sha256=self.setting("cert_sha256") or "",
                                    allow_plain=bool(self.setting("allow_plain")), server_label=self.label)
        return self._http

    @property
    def seen_fingerprint(self) -> str:
        return self._http.seen_fingerprint if self._http is not None else ""

    def _version(self) -> str:
        own = str(self.setting("api_version") or "").strip()
        if own:
            return own
        parts = self._product.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]) else DEFAULT_VERSION

    def _envelope(self, operation: str, params: Optional[ET.Element]) -> bytes:
        root = ET.Element("MDaemon")
        request = ET.SubElement(_child(root, "API"), "Request", {"version": self._version(), "echo": "0",
                                                                   "verbose": "0"})
        _child(request, "Operation", operation)
        request.append(params if params is not None else ET.Element("Parameters"))
        return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")

    @staticmethod
    def _blanked(params: Optional[ET.Element]) -> Optional[ET.Element]:
        """Копия Parameters, где значения секретных элементов (Password…) заменены на «••••»; секретов нет — None."""
        if params is None:
            return None
        shown = copy.deepcopy(params)
        found = False
        for el in shown.iter():
            if SECRET_TAG.search(el.tag) and (el.text or "").strip():
                el.text = "••••"
                found = True
        return shown if found else None

    def _post(self, operation: str, params: Optional[ET.Element], headers: Dict[str, str], *,
              log_body: bool = True) -> ET.Element:
        """Запрос → элемент Response (Status в нём проверяет вызывающий).

        Запрос с паролем (CreateUser…) в журнал не пишется: вместо тела — копия с «••••» вместо пароля и Status
        ответа. Экранирование XML (``&amp;``, ``&lt;``) меняет запись пароля, и по самому паролю его в теле не найти.
        """
        http = self.http
        hdrs = {"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"}
        hdrs.update(headers)
        blanked = self._blanked(params)
        if blanked is not None:
            log_body = False
            self.api.note(f"{operation}: запрос (пароль скрыт): "
                          + self._envelope(operation, blanked).decode("utf-8").split("\n", 1)[-1])
        resp = http.request("POST", self._path, data=self._envelope(operation, params), headers=hdrs, expect=(200,),
                            log_body=log_body, idempotent=operation in READ_OPERATIONS)
        try:
            root = ET.fromstring(resp.body)
        except ET.ParseError as exc:
            raise ServerError(f"{self.label}: ответ на {operation} — не XML: {resp.text()[:200]!r}",
                              hint="Проверьте адрес: нужен XML API MDaemon (…/MdMgmtWS).") from exc
        api = root.find("API") if root.tag == "MDaemon" else None
        response = api.find("Response") if api is not None else None
        if response is None:
            raise ServerError(f"{self.label}: ответ на {operation} — не в формате XML API MDaemon.",
                              hint="Проверьте адрес: нужен XML API MDaemon (…/MdMgmtWS).")
        if api.get("productversion"):
            self._product = api.get("productversion") or ""
            self._service = api.get("serviceversion") or ""
        if blanked is not None:
            st = response.find("Status")
            self.api.note(f"{operation}: ответ — Status " + (" ".join(
                f'{k}="{st.get(k)}"' for k in ("id", "value", "message") if st.get(k) is not None)
                if st is not None else "нет"))
        return response

    @staticmethod
    def _status(response: ET.Element) -> Tuple[bool, str, str]:
        """Status ответа → (успех, код 0x…, текст)."""
        st = response.find("Status")
        if st is None:
            return False, "", "в ответе нет Status"
        sid = (st.get("id") or "").strip()
        value = (st.get("value") or "").strip()
        return sid == "0" or (not sid and value.lower() == "0x00000000"), value, (st.get("message") or "").strip()

    def _logon(self) -> None:
        user = str(self.setting("username") or "").strip()
        password = self.secret("password")
        if not user or not password:
            raise ServerAuthError(f"{self.label}: не заданы логин и пароль администратора.")
        basic = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.api.secret(basic)
        try:
            # ответ несёт токен — тело в журнал не пишется
            response = self._post("Logon", None, {"Authorization": f"Basic {basic}"}, log_body=False)
        except ServerAuthError as exc:
            raise ServerAuthError(f"{self.label}: вход администратора «{user}» отклонён: {exc.message}",
                                  hint=LOGON_HINT) from exc
        ok, value, message = self._status(response)
        if not ok:
            raise ServerAuthError(f"{self.label}: вход администратора «{user}» отклонён: {message or value}",
                                  hint=LOGON_HINT)
        token = _text(response, "Result/Token")
        if not token:
            raise ServerError(f"{self.label}: сервер не прислал токен сеанса после входа.")
        self.api.secret(token)
        self._token = token
        self._timeout = _text(response, "Result/Timeout")

    def _error(self, operation: str, value: str, message: str) -> ServerError:
        text = message or value or "без текста"
        code = f" ({value})" if value and message else ""
        if operation == "CreateUser" and "exist" in message.lower():
            return AlreadyExists(f"{self.label}: {text}")
        return ServerError(f"{self.label}: {operation} — сервер ответил ошибкой{code}: {text}")

    def call(self, operation: str, params: Optional[ET.Element] = None, *, _again: bool = True) -> ET.Element:
        """Операция XML API → Response; Status не 0 — исключение; токен не принят — вход заново (один раз)."""
        if not self._token:
            self._logon()
        try:
            response = self._post(operation, params, {"X-MDAPI-TOKEN": self._token})
        except ServerAuthError:
            if not _again:
                raise
            self.api.note("токен XML API не принят — вход заново")
            self._token = ""
            return self.call(operation, params, _again=False)
        ok, value, message = self._status(response)
        if ok:
            return response
        if _again and SESSION_RX.search(message):
            self.api.note("сеанс XML API истёк — вход заново")
            self._token = ""
            return self.call(operation, params, _again=False)
        raise self._error(operation, value, message)

    def close(self) -> None:
        if not self._token or self._http is None:
            return
        try:
            self._post("Logoff", None, {"X-MDAPI-TOKEN": self._token})
        except ServerError:
            pass
        finally:
            self._token = ""

    # -- чтение --------------------------------------------------------------------------------
    @staticmethod
    def _params(**values: str) -> ET.Element:
        params = ET.Element("Parameters")
        for tag, value in values.items():
            _child(params, tag, value)
        return params

    def _domains(self) -> List[str]:
        if self._domain_names is None:
            names: List[str] = []
            for el in self.call("GetDomainList").findall("Result/Domains/Domain"):
                name = norm_email(_attr_or_child(el, "id"))
                if name and name not in names:
                    names.append(name)
            self._domain_names = names
        return self._domain_names

    def _domain_users(self, domain: str, sections: Sequence[str] = ("Quotas", "Details", "Aliases")
                      ) -> List[ET.Element]:
        params = self._params(Domain=domain)
        users = _child(_child(params, "Get"), "Users")
        for section in sections:
            _child(users, section)
        return self.call("GetDomainInfo", params).findall("Result/Domain/Users/User")

    def _account(self, user: ET.Element, domain: str) -> Optional[Dict[str, Any]]:
        mailbox = norm_email(_attr_or_child(user, "id"))
        if not mailbox:
            return None                                 # служебная строка (MailboxRoot)
        email = mailbox if "@" in mailbox else f"{mailbox}@{norm_email(user.get('domain') or domain)}"
        quotas = user.find("Quotas")
        apply = _flag(_text(quotas, "ApplyQuotas"))
        max_mb = _int(_text(quotas, "MaxDiskSpace"))
        info = {"apply_quotas": apply, "max_messages": _int(_text(quotas, "MaxMessageCount"))}
        acc: Dict[str, Any] = {
            "email": email, "native_id": mailbox, "display_name": user.get("displayname") or "",
            "first_name": _text(user, "Details/FirstName"), "last_name": _text(user, "Details/LastName"),
            "aliases": aliases_block(user.find("Aliases"), email),
            "quota_bytes": max_mb * MB if apply and max_mb else None,
            "messages": _int(_text(quotas, "Usage/Items")), "kind": "user",
            "info": {k: v for k, v in info.items() if v is not None},
        }
        fwd = forwarding_block(user.find("Forwarding"))
        if fwd is not None:
            acc["forward_to"] = fwd["addresses"] if fwd["enabled"] else []
        auto = autoresponder_block(user.find("AutoResponder"))
        if auto is not None:
            acc["autoreply"] = auto["enabled"]
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for domain in self._domains():
            try:
                users = self._domain_users(domain)
            except ServerUnreachable:
                raise
            except ServerError as exc:
                # домен не прочитан — его учётные записи пропавшими не считаем
                self.incomplete = True
                self.sync_problems.append(f"домен {domain} не прочитан: {exc.message}")
                continue
            for user in users:
                acc = self._account(user, domain)
                if acc is not None:
                    out.append(acc)
        return out

    def _user_info(self, email: str) -> Optional[ET.Element]:
        """Элемент User из GetUserInfo (все разделы); учётной записи нет — None (проверено по списку домена)."""
        local, domain = split_email(email)
        if not local or domain not in self._domains():
            return None
        params = self._params(Domain=domain, Mailbox=local)
        _child(_child(params, "Get"), "All")
        try:
            response = self.call("GetUserInfo", params)
        except (ServerUnreachable, ServerAuthError):
            raise
        except ServerError:
            # по тексту ошибки «нет такой» не определить — смотрим список учётных записей домена
            if not self._has_mailbox(domain, local):
                return None
            raise
        for user in response.iter("User"):
            if norm_email(_attr_or_child(user, "id")) in (local, f"{local}@{domain}"):
                return user
        if not self._has_mailbox(domain, local):
            return None
        raise ServerError(f"{self.label}: GetUserInfo не вернул учётную запись {local}@{domain}.")

    def _has_mailbox(self, domain: str, local: str) -> bool:
        names = {norm_email(_attr_or_child(u, "id")) for u in self._domain_users(domain, ())}
        return local in names or f"{local}@{domain}" in names

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._user_info(email)
        return self._account(user, split_email(email)[1]) if user is not None else None

    def _require(self, email: str) -> ET.Element:
        user = self._user_info(email)
        if user is None:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        return user

    # -- изменения -----------------------------------------------------------------------------
    def _update(self, email: str, *blocks: ET.Element) -> None:
        local, domain = split_email(email)
        params = self._params(Domain=domain, Mailbox=local)
        for block in blocks:
            params.append(block)
        self.call("UpdateUser", params)

    @staticmethod
    def _quotas(quota_bytes: Optional[int]) -> ET.Element:
        block = ET.Element("Quotas")
        if not quota_bytes:
            _child(block, "ApplyQuotas", "No")
            return block
        _child(block, "ApplyQuotas", "Yes")
        _child(block, "MaxDiskSpace", -(-int(quota_bytes) // MB))      # МБ, с округлением вверх
        return block

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        before = self._account(self._require(email), split_email(email)[1]) or {}
        self._update(email, self._quotas(quota_bytes))
        if not quota_bytes:
            return "ограничения ящика выключены (ApplyQuotas = No)"
        text = f"квота {-(-int(quota_bytes) // MB)} МБ"
        info = before.get("info") or {}
        if info.get("apply_quotas") is False and info.get("max_messages"):
            text += f"; вместе с ней включился и предел числа писем ({info['max_messages']})"
        return text + "; занятое место MDaemon через API не показывает — что ящик меньше квоты, не проверено"

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        email = norm_email(email)
        local, domain = split_email(email)
        if domain not in self._domains():
            raise NotFound(f"{self.label}: домена {domain} на сервере нет.")
        # CreateUser в старых версиях мог «создать» уже существующую учётную запись — проверяем сами
        if self._user_info(email) is not None:
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.")
        self.api.secret(password)
        # тело с паролем в журнал не пишется (см. _post): только копия с «••••»
        params = self._params(Domain=domain, Mailbox=local)
        details = _child(params, "Details")
        _child(details, "Password", password)
        if first_name:
            _child(details, "FirstName", first_name)
        if last_name:
            _child(details, "LastName", last_name)
        self.call("CreateUser", params)
        if quota_bytes:
            try:
                self._update(email, self._quotas(quota_bytes))
            except ServerError as exc:
                self.api.note(f"квота {email} не задана: {exc.message}")
                quota_bytes = None
        return {"email": email, "native_id": local, "first_name": first_name, "last_name": last_name,
                "display_name": " ".join(x for x in (first_name, last_name) if x), "quota_bytes": quota_bytes or None,
                "kind": "user"}

    # -- автоответ и пересылка ------------------------------------------------------------------
    def get_autoreply(self, email: str) -> Dict[str, Any]:
        auto = autoresponder_block(self._require(email).find("AutoResponder"))
        if auto is None:
            raise ServerError(f"{self.label}: сервер не показал автоответ {norm_email(email)}.")
        return auto

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        block = ET.Element("AutoResponder")
        _child(block, "Enabled", "Yes" if enabled else "No")
        if enabled:
            # даты — всегда: иначе осталось бы прежнее (возможно, прошедшее) расписание
            _child(block, "Start", f"{start}T00:00:00.000Z" if start else NO_DATE)
            _child(block, "End", f"{end}T23:59:59.000Z" if end else NO_DATE)
            _child(block, "Response", text)
        self._update(email, block)
        if not enabled:
            return "автоответ выключен"
        out = "автоответ включён"
        if start or end:
            out += f" ({start or '…'} — {end or '…'})"
        if subject:
            out += "; тему автоответа XML API MDaemon не задаёт — у ответа будет тема сервера"
        return out

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        fwd = forwarding_block(self._require(email).find("Forwarding"))
        if fwd is None:
            raise ServerError(f"{self.label}: сервер не показал пересылку {norm_email(email)}.")
        return {"addresses": fwd["addresses"] if fwd["enabled"] else [], "keep_copy": fwd["keep_copy"]}

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        targets = [norm_email(a) for a in addresses if a]
        block = ET.Element("Forwarding")
        if not targets:
            _child(block, "Enabled", "false")
        else:
            _child(block, "Enabled", "true")
            _child(block, "RetainMail", "true" if keep_copy else "false")
            listed = _child(block, "Addresses")
            for address in targets:                    # по одному Address на адрес — строка через запятую не работает
                _child(listed, "Address", address)
            # всегда, без дат и во все дни: прежнее расписание не должно выключать пересылку
            schedule = _child(block, "Schedule")
            _child(schedule, "Start", NO_DATE)
            _child(schedule, "End", NO_DATE)
            ET.SubElement(schedule, "DaysActive", {"mask": "0x7F"})
        self._update(email, block)
        after = self.get_account(email)
        shown = after.get("forward_to") if after is not None else None
        if shown is not None and sorted(shown) != sorted(targets):
            raise ServerError(f"{self.label}: после записи сервер показывает пересылку "
                              f"{', '.join(shown) or 'выключенной'} вместо {', '.join(targets) or 'выключенной'} — "
                              f"проверьте в MDaemon Remote Administration.")
        if not targets:
            return "пересылка выключена"
        return f"пересылка на {', '.join(targets)}" + ("; копия остаётся в ящике" if keep_copy else
                                                       "; копия в ящике не остаётся")

    # -- состояние -------------------------------------------------------------------------------
    def test(self) -> Status:
        st = Status()
        domains = self._domains()
        st.version = f"MDaemon {self._product}" if self._product else "MDaemon"
        if self._service:
            st.details.append(f"Служба XML API: {self._service}")
        st.details.append(f"Администратор: {self.setting('username')}")
        if domains:
            st.details.append(f"Домены ({len(domains)}): " + ", ".join(domains[:10]) + (" …" if len(domains) > 10
                                                                                         else ""))
        else:
            st.problems.append(("domains", "warning", "учётной записи подключения не виден ни один домен"))
        if self._timeout:
            st.details.append(f"Сеанс XML API без запросов живёт {self._timeout} с")
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = MDaemonDriver
