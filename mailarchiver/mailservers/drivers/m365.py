"""
Microsoft 365 (Exchange Online) — через Microsoft Graph от имени приложения Entra ID.

Учётные записи — пользователи каталога с почтой (``mail``); занятое место и
квота — из отчёта об использовании ящиков (``getMailboxUsageDetail``, данные
на 1–3 дня старее); последний вход — ``signInActivity`` (нужны лицензия Entra
ID P1/P2 и право ``AuditLog.Read.All``; нет — сверка идёт без него).

Копирование ящиков — по IMAP с токеном приложения (XOAUTH2, право
``IMAP.AccessAsApp`` и доступ FullAccess к каждому ящику — выдаётся в Exchange
Online PowerShell).

Чего Graph не умеет: псевдонимы (``proxyAddresses`` только для чтения), квота
ящика и пересылка на уровне ящика (только PowerShell). Пересылка делается
правилом папки «Входящие» (правило с именем «MailArchiver: пересылка»).
Удаление — в «Удалённые пользователи» (30 дней можно восстановить в центре
администрирования).
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import time
import urllib.parse
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, ServerAuthError, ServerError, Status,
                    norm_email, split_email)
from ..http import TOKENS, HttpClient, _is_loopback, jwt_rs256, token_request

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
OUTLOOK_SCOPE = "https://outlook.office365.com/.default"
RULE_NAME = "MailArchiver: пересылка"
USER_FIELDS = ("id,userPrincipalName,mail,displayName,givenName,surname,accountEnabled,proxyAddresses,department,"
               "jobTitle,businessPhones,mobilePhone,officeLocation,companyName,userType,assignedLicenses,"
               "createdDateTime,onPremisesSyncEnabled")
#: «Никогда не входил» у signInActivity
_NEVER = ("", "0001-01-01T00:00:00Z")
#: Куда может вести ссылка на файл отчёта (ответ Graph 302)
REPORT_HOSTS = (".office.com", ".office.net", ".microsoft.com", ".office365.com")


class M365Driver(Driver):
    type = "m365"
    label = "Microsoft 365"
    summary = ("Exchange Online через Microsoft Graph: учётные записи, вход, пароли, ФИО, создание и удаление, "
               "автоответ, пересылка правилом; копирование ящиков по IMAP токеном приложения.")
    cloud = True
    #: адреса API (подменяются в тестах)
    GRAPH_URL = "https://graph.microsoft.com/v1.0"
    LOGIN_URL = "https://login.microsoftonline.com"
    fields = (
        Field("tenant_id", "Идентификатор клиента (Directory / tenant ID)", required=True,
              placeholder="00000000-0000-0000-0000-000000000000",
              help="Центр администрирования Entra → Обзор → «Идентификатор клиента», или основной домен."),
        Field("client_id", "Идентификатор приложения (Application / client ID)", required=True,
              placeholder="00000000-0000-0000-0000-000000000000"),
        Field("auth", "Чем приложение подтверждает себя", "select", default="secret",
              options=(("secret", "секрет клиента"), ("certificate", "сертификат"))),
        Field("client_secret", "Секрет клиента (значение, не идентификатор)", "password", secret=True,
              show_if={"auth": ["secret"]}, help="Entra → приложение → «Сертификаты и секреты». Срок у секрета "
                                                 "ограничен (до двух лет): когда он истечёт, проверка подключения "
                                                 "покажет отказ — создайте новый секрет и впишите его сюда."),
        Field("cert_key", "Закрытый ключ сертификата (PEM, без пароля)", "secret_text", secret=True,
              show_if={"auth": ["certificate"]}),
        Field("cert_pem", "Сертификат (PEM — тот, что загружен в приложение)", "textarea",
              show_if={"auth": ["certificate"]}),
        Field("usage_location", "Страна новых пользователей (две буквы)", default="RU", advanced=True,
              help="Нужна, чтобы новому пользователю можно было выдать лицензию."),
        Field("license_sku", "Лицензия новым пользователям", advanced=True, placeholder="например, O365_BUSINESS_PREMIUM",
              help="Название (skuPartNumber) или идентификатор (skuId) подписки — «Проверить подключение» покажет "
                   "доступные. Пусто — без лицензии (ящик не появится, пока её не выдадут). Такой подписки нет или "
                   "свободных лицензий нет — учётная запись не создаётся; не удалось назначить лицензию уже "
                   "созданной — она остаётся без лицензии (с предупреждением)."),
        Field("last_login", "Читать последний вход (Entra ID P1/P2, AuditLog.Read.All)", "bool", default=True,
              advanced=True),
        Field("usage_report", "Читать занятое место из отчёта об использовании ящиков", "bool", default=True,
              advanced=True, help="Право Reports.Read.All. Данные отчёта отстают на 1–3 дня."),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.MUST_CHANGE, Cap.CONTACT,
                      Cap.CREATE, Cap.DELETE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title", "phone", "mobile",
                      "company", "office")
    password_rule = "от 8 до 256 знаков, три вида из четырёх: заглавные, строчные, цифры, знаки"
    imap_default = ("outlook.office365.com", 993, "ssl")
    limits = (
        "Псевдонимы и квоту ящика Microsoft Graph не меняет — только центр администрирования или PowerShell.",
        "Пересылка — правилом папки «Входящие»; на внешние адреса её может запрещать политика защиты от спама "
        "(письма вернутся с ошибкой 5.7.520).",
        "Занятое место — из отчёта Microsoft 365: отстаёт на 1–3 дня; если в отчётах скрыты имена "
        "пользователей, место не сопоставить.",
        "Удалённый пользователь 30 дней лежит в «Удалённых пользователях», затем удаляется с ящиком навсегда.",
    )
    setup = (
        "Центр администрирования Entra → «Регистрация приложений» → «Новая регистрация» (одна организация). "
        "Запишите идентификаторы приложения и клиента.",
        "«Сертификаты и секреты»: создайте секрет клиента или загрузите сертификат.",
        "«Разрешения API» → Microsoft Graph → «Разрешения приложений»: User.ReadWrite.All, Organization.Read.All, "
        "LicenseAssignment.ReadWrite.All, MailboxSettings.ReadWrite, Reports.Read.All, AuditLog.Read.All "
        "(только чтение — User.Read.All вместо User.ReadWrite.All). «API, используемые организацией» → Office 365 "
        "Exchange Online → IMAP.AccessAsApp. Нажмите «Предоставить согласие администратора».",
        "Для смены паролей: «Роли и администраторы» → «Администратор пользователей» → назначьте приложению.",
        "Exchange Online PowerShell: New-ServicePrincipal -AppId <приложение> -ObjectId <объект корпоративного "
        "приложения>, затем Add-MailboxPermission <ящик> -User <субъект> -AccessRights FullAccess — для каждого "
        "ящика, который нужно копировать (и для новых).",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._graph: Optional[HttpClient] = None
        self._ids: Dict[str, str] = {}
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- токены ------------------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        if (settings.get("auth") or "secret") == "certificate":
            if not secrets.get("cert_key") or not (settings.get("cert_pem") or "").strip():
                raise ValidationError("Для входа сертификатом нужны и закрытый ключ, и сам сертификат.")
        elif not secrets.get("client_secret"):
            raise ValidationError("Укажите секрет клиента.")
        loc = str(settings.get("usage_location") or "").strip()
        if loc and (len(loc) != 2 or not loc.isalpha()):
            raise ValidationError("Страна новых пользователей — две латинские буквы (RU, KZ, US…).")
        return []

    def _tenant(self) -> str:
        tenant = str(self.setting("tenant_id") or "").strip()
        if not tenant or "/" in tenant or "?" in tenant:
            raise ValidationError("Идентификатор клиента (tenant) указан неверно.")
        return tenant

    def _assertion(self, tenant: str, client_id: str) -> str:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from ..http import b64url
        try:
            cert = x509.load_pem_x509_certificate(str(self.setting("cert_pem") or "").encode("utf-8"))
        except ValueError as exc:
            raise ServerAuthError("Сертификат приложения не читается (нужен PEM).", cause=exc) from exc
        der = cert.public_bytes(serialization.Encoding.DER)
        now = int(time.time())
        claims = {"aud": f"{self.LOGIN_URL}/{tenant}/oauth2/v2.0/token", "iss": client_id, "sub": client_id,
                  "jti": str(uuid.uuid4()), "nbf": now, "iat": now, "exp": now + 300}
        return jwt_rs256(claims, self.secret("cert_key"), {"x5t#S256": b64url(hashlib.sha256(der).digest())},
                         alg="PS256")

    def token(self, scope: str = GRAPH_SCOPE) -> str:
        tenant = self._tenant()
        client_id = str(self.setting("client_id") or "").strip()
        auth = self.setting("auth") or "secret"
        material = self.secret("client_secret") if auth == "secret" else self.secret("cert_key")
        key = ("m365", tenant, client_id, scope, hashlib.sha256(material.encode("utf-8")).hexdigest()[:16])
        cached = TOKENS.get(key)
        if cached:
            return cached
        form = {"client_id": client_id, "scope": scope, "grant_type": "client_credentials"}
        if auth == "secret":
            form["client_secret"] = material
        else:
            form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            form["client_assertion"] = self._assertion(tenant, client_id)
        login = HttpClient(self.LOGIN_URL, api=self.api, server_label=self.label)
        body = token_request(login, f"/{urllib.parse.quote(tenant)}/oauth2/v2.0/token", form, self.label)
        TOKENS.put(key, body["access_token"], body.get("expires_in"))
        return body["access_token"]

    @property
    def graph(self) -> HttpClient:
        if self._graph is None:
            self._graph = HttpClient(self.GRAPH_URL, api=self.api, server_label=self.label,
                                     headers={"Authorization": f"Bearer {self.token()}"})
        return self._graph

    # -- чтение --------------------------------------------------------------------------------
    def _pages(self, path: str, params: Optional[Dict[str, Any]] = None, limit_pages: int = 1000) -> List[Dict]:
        out: List[Dict] = []
        body = self.graph.get_json(path, params=params)
        for _ in range(limit_pages):
            out.extend(body.get("value") or [])
            nxt = body.get("@odata.nextLink")
            if not nxt:
                return out
            body = self.graph.get_json(nxt)
        self.incomplete = True
        return out

    @staticmethod
    def _primary(user: Dict) -> str:
        for p in user.get("proxyAddresses") or []:
            if str(p).startswith("SMTP:"):
                return norm_email(p[5:])
        return norm_email(user.get("mail"))

    def _account(self, user: Dict, usage: Optional[Dict[str, Dict]] = None,
                 sign_in_read: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Пользователь Graph → учётная запись. ``sign_in_read`` — последний вход запрашивался (``signInActivity``
        в $select); по умолчанию — если это поле в ответе есть. Не запрашивался — ``last_login`` в учётной записи
        нет вовсе: снимок сохранит прежнее значение из сверки, а не сотрёт его «никогда не входил»."""
        email = self._primary(user)
        if not email or (user.get("userType") or "Member") != "Member":
            return None
        aliases = [norm_email(p[5:]) for p in user.get("proxyAddresses") or []
                   if str(p).lower().startswith("smtp:") and not str(p).startswith("SMTP:")]
        phones = user.get("businessPhones") or []
        acc = {"email": email, "native_id": user.get("id") or "", "first_name": user.get("givenName") or "",
               "last_name": user.get("surname") or "", "display_name": user.get("displayName") or "",
               "aliases": aliases, "login_blocked": None if user.get("accountEnabled") is None
               else not user.get("accountEnabled"),
               "department": user.get("department") or "", "title": user.get("jobTitle") or "",
               "phone": phones[0] if phones else "", "mobile": user.get("mobilePhone") or "",
               "company": user.get("companyName") or "", "office": user.get("officeLocation") or "",
               "kind": "user" if user.get("assignedLicenses") else "unlicensed",
               "info": {"upn": user.get("userPrincipalName") or "", "synced": bool(user.get("onPremisesSyncEnabled")),
                        "created": user.get("createdDateTime") or "",
                        "licenses": len(user.get("assignedLicenses") or [])}}
        if sign_in_read is None:
            sign_in_read = "signInActivity" in user
        if sign_in_read:
            sign_in = user.get("signInActivity") or {}
            last = sign_in.get("lastSuccessfulSignInDateTime") or sign_in.get("lastSignInDateTime") or ""
            acc["last_login"] = "" if last in _NEVER else last
        upn = norm_email(user.get("userPrincipalName"))
        u = (usage or {}).get(upn) or (usage or {}).get(email)
        if u:
            acc.update({"used_bytes": u.get("used"), "messages": u.get("items"), "quota_bytes": u.get("quota")})
            acc["info"]["usage_date"] = u.get("date") or ""
        return acc

    def _users(self, with_sign_in: bool) -> List[Dict]:
        select = USER_FIELDS + (",signInActivity" if with_sign_in else "")
        return self._pages("/users", {"$select": select, "$top": 500 if with_sign_in else 999})

    def list_accounts(self) -> List[Dict[str, Any]]:
        users = None
        if self.setting("last_login", True):
            try:
                users = self._users(True)
            except ServerAuthError:
                self.api.note("последний вход недоступен (нужны лицензия Entra ID P1/P2 и право AuditLog.Read.All) — "
                              "сверка без него")
        sign_in_read = users is not None
        if users is None:
            users = self._users(False)
        usage = self.usage() if self.setting("usage_report", True) else {}
        out = []
        for user in users:
            acc = self._account(user, usage, sign_in_read)
            if acc is not None:
                self._ids[acc["email"]] = acc["native_id"]
                out.append(acc)
        return out

    def usage(self) -> Dict[str, Dict]:
        """Отчёт об использовании ящиков: UPN → {used, items, quota, date}. Не вышло — пусто (и заметка)."""
        try:
            resp = self.graph.request("GET", "/reports/getMailboxUsageDetail(period='D7')", expect=(200, 302))
            if resp.status == 302:
                location = resp.headers.get("location") or ""
                parsed = urllib.parse.urlsplit(location)
                host = (parsed.hostname or "").lower()
                graph_host = urllib.parse.urlsplit(self.GRAPH_URL).hostname or ""
                allowed = parsed.scheme == "https" and any(host.endswith(s) for s in REPORT_HOSTS)
                if not allowed and not (_is_loopback(graph_host) and _is_loopback(host)):
                    raise ServerError(f"{self.label}: отчёт ведёт на неожиданный адрес ({host}) — не прочитан.")
                # ссылка уже с доступом (токен — в самом адресе): заголовок Authorization туда НЕ отправляется, а в
                # журнал и в тексты ошибок адрес не попадает — только сервер
                dl = HttpClient(f"{parsed.scheme}://{parsed.netloc}", api=self.api, server_label=self.label)
                try:
                    resp = dl.request("GET", location, headers={"Accept": "text/csv"}, log_body=False,
                                      log_url=f"{parsed.scheme}://{host}/… (ссылка на файл отчёта — с доступом, "
                                              f"не показывается)")
                except ServerError as exc:
                    text = exc.message.replace(location, "…")
                    if parsed.path:
                        text = text.replace(parsed.path, "/…")
                    raise type(exc)(text, hint=exc.hint, retryable=exc.retryable) from None
            text = resp.body.decode("utf-8-sig", errors="replace")
        except ServerAuthError:
            self.api.note("отчёт об использовании ящиков недоступен (нужно право Reports.Read.All)")
            return {}
        except ServerError as exc:
            self.api.note(f"отчёт об использовании ящиков не прочитан: {exc.message}")
            return {}
        out: Dict[str, Dict] = {}
        hashed = 0
        for row in csv.DictReader(io.StringIO(text)):
            upn = norm_email(row.get("User Principal Name"))
            if not upn:
                continue
            if "@" not in upn:
                hashed += 1
                continue
            if str(row.get("Is Deleted") or "").strip().lower() == "true":
                continue

            def num(name: str) -> Optional[int]:
                try:
                    return int(float(row.get(name) or ""))
                except ValueError:
                    return None
            out[upn] = {"used": num("Storage Used (Byte)"), "items": num("Item Count"),
                        "quota": num("Prohibit Send/Receive Quota (Byte)"), "date": row.get("Report Refresh Date") or ""}
        if hashed and not out:
            self.sync_problems.append(
                "в отчётах Microsoft 365 скрыты имена пользователей — занятое место не сопоставить. Центр "
                "администрирования → Настройки → Параметры организации → Отчёты → снимите «Отображать скрытые "
                "имена пользователей, групп и сайтов во всех отчётах».")
        return out

    def _user(self, email: str, select: str = USER_FIELDS) -> Optional[Dict]:
        email = norm_email(email)
        uid = self._ids.get(email)
        try:
            if uid:
                return self.graph.get_json(f"/users/{uid}", params={"$select": select})
            return self.graph.get_json(f"/users/{urllib.parse.quote(email)}", params={"$select": select})
        except NotFound:
            pass
        found = self.graph.get_json("/users", params={"$filter": f"mail eq '{email.replace(chr(39), chr(39) * 2)}'",
                                                      "$select": select})
        users = found.get("value") or []
        return users[0] if users else None

    def _id(self, email: str) -> str:
        email = norm_email(email)
        if email not in self._ids:
            user = self._user(email, "id")
            if user is None:
                raise NotFound(f"{self.label}: учётной записи {email} нет.")
            self._ids[email] = user["id"]
        return self._ids[email]

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._user(email)
        if user is None:
            return None
        acc = self._account(user)
        if acc is None:
            return None
        self._ids[acc["email"]] = acc["native_id"]
        try:
            box = self.graph.get_json(f"/users/{acc['native_id']}/mailboxSettings/automaticRepliesSetting")
            acc["autoreply"] = str(box.get("status") or "").lower() in ("alwaysenabled", "scheduled")
        except ServerError:
            pass
        try:
            rule = self._rule(acc["native_id"])
            acc["forward_to"] = self._rule_targets(rule) if rule else []
        except ServerError:
            pass
        return acc

    # -- изменения -----------------------------------------------------------------------------
    def _patch(self, email: str, body: Dict[str, Any]) -> None:
        self.graph.request("PATCH", f"/users/{self._id(email)}", json_body=body, expect=(200, 204))

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        self._patch(email, {"accountEnabled": bool(enabled)})
        if enabled:
            return "вход открыт"
        note = ""
        try:
            self.graph.request("POST", f"/users/{self._id(email)}/revokeSignInSessions", json_body={},
                               expect=(200, 204))
            note = "; открытые сеансы завершены"
        except ServerError as exc:
            note = f"; открытые сеансы не завершены ({exc.message})"
        if not keep_receiving:
            note += "; приём писем Microsoft 365 при закрытом входе не останавливает"
        return "вход закрыт" + note

    def check_password(self, password: str) -> None:
        if not password or not 8 <= len(password) <= 256:
            raise ValidationError(f"Пароль не подходит Microsoft 365: {self.password_rule}.")
        kinds = sum((any(c.islower() for c in password), any(c.isupper() for c in password),
                     any(c.isdigit() for c in password), any(not c.isalnum() for c in password)))
        if kinds < 3:
            raise ValidationError(f"Пароль не подходит Microsoft 365: {self.password_rule}.")
        if any(c.isspace() for c in password):
            raise ValidationError("Пароль не должен содержать пробелов.")

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        self._patch(email, {"passwordProfile": {"password": password,
                                                "forceChangePasswordNextSignIn": bool(must_change)}})
        return "пароль сменён" + ("; при следующем входе — смена пароля" if must_change else "")

    def set_must_change(self, email: str, value: bool) -> str:
        self._patch(email, {"passwordProfile": {"forceChangePasswordNextSignIn": bool(value)}})
        return "при следующем входе потребуется сменить пароль" if value else "требование снято"

    _CONTACT = {"first_name": "givenName", "last_name": "surname", "display_name": "displayName",
                "department": "department", "title": "jobTitle", "mobile": "mobilePhone", "company": "companyName",
                "office": "officeLocation"}

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        body: Dict[str, Any] = {}
        for key, value in values.items():
            if key == "phone":
                body["businessPhones"] = [value] if value else []
            elif key in self._CONTACT:
                body[self._CONTACT[key]] = value
        if body:
            self._patch(email, body)
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        local, _domain = split_email(email)
        display = " ".join(x for x in (first_name, last_name) if x) or local
        body = {"accountEnabled": True, "displayName": display, "mailNickname": local, "userPrincipalName": email,
                "passwordProfile": {"password": password, "forceChangePasswordNextSignIn": bool(must_change)}}
        if first_name:
            body["givenName"] = first_name
        if last_name:
            body["surname"] = last_name
        location = str(self.setting("usage_location") or "").strip().upper()
        if location:
            body["usageLocation"] = location
        # лицензия — ДО создания: нет такой подписки или свободных лицензий — учётная запись не создаётся вовсе
        sku = str(self.setting("license_sku") or "").strip()
        sku_id = self._sku_id(sku, email) if sku else ""
        try:
            user = self.graph.request("POST", "/users", json_body=body, expect=(201,)).json() or {}
        except ServerError as exc:
            if "already exists" in exc.message.lower() or "ObjectConflict" in exc.message:
                raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.") from exc
            raise
        self._ids[norm_email(email)] = user.get("id") or ""
        warning = ""
        if sku_id and user.get("id"):
            # учётная запись уже создана: отказ в лицензии — предупреждение при созданной учётной записи, а не
            # ошибка (иначе её сочли бы не созданной, а повтор ответил бы «уже есть»)
            try:
                self.graph.request("POST", f"/users/{user['id']}/assignLicense",
                                   json_body={"addLicenses": [{"skuId": sku_id, "disabledPlans": []}],
                                              "removeLicenses": []}, expect=(200,))
                user["assignedLicenses"] = [{"skuId": sku_id}]
            except ServerError as exc:
                warning = (f"лицензия «{sku}» не назначена ({exc.message}) — учётная запись создана без лицензии: "
                           f"ящика не будет, пока лицензию не назначат в центре администрирования")
                self.api.note(f"{email}: {warning}")
        if not user.get("mail") and not user.get("proxyAddresses"):
            user["mail"] = email                    # Graph отдаёт mail: null, пока ящик не создан
        user.setdefault("accountEnabled", True)
        acc = self._account(user) or {"email": norm_email(email), "native_id": user.get("id") or "",
                                      "first_name": first_name or "", "last_name": last_name or ""}
        if warning:
            acc["warning"] = warning
            acc["info"] = dict(acc.get("info") or {}, license_error=warning)
        return acc

    def _sku_id(self, sku: str, email: str) -> str:
        """Подписка для новых пользователей → skuId; её нет или свободных лицензий нет — ошибка (до создания)."""
        try:
            items = self._pages("/subscribedSkus")
        except ServerAuthError as exc:
            raise ServerAuthError(f"{self.label}: подписки (лицензии) не прочитаны — учётная запись {email} не "
                                  f"создана: {exc.message}",
                                  hint="Нужно право Organization.Read.All или LicenseAssignment.Read.All — или "
                                       "очистите «Лицензия новым пользователям» в подключении.") from exc
        for item in items:
            if sku.lower() in (str(item.get("skuId") or "").lower(), str(item.get("skuPartNumber") or "").lower()):
                enabled = int((item.get("prepaidUnits") or {}).get("enabled") or 0)
                if enabled and int(item.get("consumedUnits") or 0) >= enabled:
                    raise ValidationError(f"{self.label}: свободных лицензий «{item.get('skuPartNumber')}» нет — "
                                          f"учётная запись {email} не создана.",
                                          hint="Купите или освободите лицензии в центре администрирования либо "
                                               "очистите «Лицензия новым пользователям» в подключении.")
                return str(item["skuId"])
        raise ValidationError(f"{self.label}: подписки «{sku}» нет — учётная запись {email} не создана.",
                              hint="Проверьте «Лицензия новым пользователям» в подключении: «Проверить подключение» "
                                   "покажет доступные подписки.")

    def delete_account(self, email: str) -> str:
        self.graph.request("DELETE", f"/users/{self._id(email)}", expect=(204,))
        self._ids.pop(norm_email(email), None)
        return "учётная запись удалена (30 дней её можно восстановить в центре администрирования)"

    # -- автоответ и пересылка ----------------------------------------------------------------
    @staticmethod
    def _html(subject: str, text: str) -> str:
        body = "<br>".join(html.escape(line) for line in (text or "").splitlines())
        head = f"<p><b>{html.escape(subject)}</b></p>" if subject else ""
        return f"<html><body>{head}<p>{body}</p></body></html>"

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        st = self.graph.get_json(f"/users/{self._id(email)}/mailboxSettings/automaticRepliesSetting")
        return {"enabled": str(st.get("status") or "").lower() in ("alwaysenabled", "scheduled"),
                "text": st.get("externalReplyMessage") or st.get("internalReplyMessage") or "",
                "start": ((st.get("scheduledStartDateTime") or {}).get("dateTime") or "")[:10],
                "end": ((st.get("scheduledEndDateTime") or {}).get("dateTime") or "")[:10]}

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        if not enabled:
            setting: Dict[str, Any] = {"status": "disabled"}
        else:
            message = self._html(subject, text)
            setting = {"status": "alwaysEnabled", "externalAudience": "all", "internalReplyMessage": message,
                       "externalReplyMessage": message}
            if start or end:
                first = start or date.today().isoformat()
                last = end or (date.fromisoformat(first) + timedelta(days=3650)).isoformat()
                setting.update({"status": "scheduled",
                                "scheduledStartDateTime": {"dateTime": f"{first}T00:00:00", "timeZone": "UTC"},
                                "scheduledEndDateTime": {"dateTime": f"{last}T23:59:59", "timeZone": "UTC"}})
        self.graph.request("PATCH", f"/users/{self._id(email)}/mailboxSettings",
                           json_body={"automaticRepliesSetting": setting}, expect=(200, 204))
        return "автоответ включён" if enabled else "автоответ выключен"

    def _rule(self, uid: str) -> Optional[Dict]:
        rules = self.graph.get_json(f"/users/{uid}/mailFolders/inbox/messageRules").get("value") or []
        return next((r for r in rules if r.get("displayName") == RULE_NAME), None)

    @staticmethod
    def _rule_targets(rule: Dict) -> List[str]:
        actions = rule.get("actions") or {}
        out = []
        for item in (actions.get("forwardTo") or []) + (actions.get("redirectTo") or []):
            address = norm_email((item.get("emailAddress") or {}).get("address"))
            if address:
                out.append(address)
        return out if rule.get("isEnabled", True) else []

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        rule = self._rule(self._id(email))
        if not rule:
            return {"addresses": [], "keep_copy": True}
        return {"addresses": self._rule_targets(rule), "keep_copy": not (rule.get("actions") or {}).get("delete")}

    def set_forwarding(self, email: str, *, addresses, keep_copy: bool = True) -> str:
        uid = self._id(email)
        rule = self._rule(uid)
        base = f"/users/{uid}/mailFolders/inbox/messageRules"
        if not addresses:
            if rule:
                self.graph.request("DELETE", f"{base}/{rule['id']}", expect=(204,))
            return "пересылка выключена"
        # «delete» — всегда явно: PATCH меняет только присланные свойства (и вложенные в actions тоже), и прежнее
        # «delete: true» без этого осталось бы при «копия остаётся в ящике»
        body = {"displayName": RULE_NAME, "sequence": 1, "isEnabled": True,
                "actions": {"forwardTo": [{"emailAddress": {"address": a}} for a in addresses],
                            "stopProcessingRules": False, "delete": not keep_copy}}
        if rule:
            # правило могли поменять в Outlook: перенаправление (redirectTo) — убрать, остаётся только пересылка
            body["actions"]["redirectTo"] = []
            self.graph.request("PATCH", f"{base}/{rule['id']}", json_body=body, expect=(200,))
        else:
            self.graph.request("POST", base, json_body=body, expect=(201,))
        text = f"пересылка на {', '.join(addresses)} (правило «{RULE_NAME}» во «Входящих»)"
        if not keep_copy:
            text += "; письма уходят в «Удалённые»"
        return text

    # -- IMAP и состояние ----------------------------------------------------------------------
    def imap_auth(self, email: str) -> ImapAuth:
        return ImapAuth(mech="xoauth2", user=norm_email(email), secret=self.token(OUTLOOK_SCOPE),
                        host=self.imap_default[0])

    def test(self) -> Status:
        st = Status()
        org = (self.graph.get_json("/organization").get("value") or [{}])[0]
        st.version = "Microsoft Graph v1.0"
        if org.get("displayName"):
            st.details.append(f"Организация: {org['displayName']}")
        domains = [d.get("name") for d in org.get("verifiedDomains") or [] if d.get("name")]
        if domains:
            st.details.append("Домены: " + ", ".join(domains[:10]))
        self.graph.get_json("/users", params={"$select": "id", "$top": 1})
        try:
            skus = self._pages("/subscribedSkus")
        except ServerAuthError:
            skus = []
            st.problems.append(("licenses", "warning", "лицензии не прочитаны (право LicenseAssignment.Read.All или "
                                                       "Organization.Read.All)"))
        total = used = 0
        for sku in skus:
            if (sku.get("appliesTo") or "User") != "User":
                continue
            units = sku.get("prepaidUnits") or {}
            total += int(units.get("enabled") or 0)
            used += int(sku.get("consumedUnits") or 0)
            name = sku.get("skuPartNumber") or sku.get("skuId")
            st.details.append(f"Лицензия {name}: занято {sku.get('consumedUnits')} из {units.get('enabled')}")
            if int(units.get("warning") or 0) or int(units.get("suspended") or 0) or \
                    str(sku.get("capabilityStatus") or "Enabled") != "Enabled":
                st.problems.append(("license_state", "warning", f"подписка {name}: {sku.get('capabilityStatus')} — "
                                                                f"проверьте оплату"))
        if skus:
            st.seats_total, st.seats_used = total, used
        try:
            self.token(OUTLOOK_SCOPE)
        except ServerError as exc:
            st.problems.append(("imap_token", "warning", f"токен для IMAP не получен — копирование ящиков без паролей "
                                                         f"не пойдёт: {exc.message}"))
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = M365Driver
