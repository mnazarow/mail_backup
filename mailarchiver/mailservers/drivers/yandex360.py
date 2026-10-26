"""
Яндекс 360 для бизнеса — через API 360 от имени администратора организации (OAuth-токен).

Хосты API (https://yandex.ru/dev/api360/doc/ru/access.md): прежний ``api360.yandex.net`` (методы работают,
но больше не обновляются) и актуальный ``cloud-api.yandex.net``. Каждый метод вызывается на том хосте, который
указан на его странице документации:

* api360.yandex.net — сотрудники (UserService: список, одна учётная запись, создание, изменение: вход, пароль,
  ФИО, должность, подразделение), домены, организации, правила автоответа и пересылки (MailUserSettingsService),
  завершение сеансов (DomainSessionsService), список сервисных приложений;
* cloud-api.yandex.net — поиск сотрудника по адресу и подразделения (Directory v2), псевдонимы и контакты
  (Directory v1), настройки почты организации.

Ошибки приходят в трёх видах (``{"code","message","details"}`` у прежнего хоста, ``{"error","message",…}`` у
Directory v1, только код ответа у v2) — разбирает их общий :class:`HttpClient`.

Копирование ящиков без паролей сотрудников — сервисным приложением
(https://yandex.ru/support/yandex-360/business/admin/ru/security-service-applications): владелец организации
регистрирует OAuth-приложение с правом ``mail:imap_full``, а MailArchiver обменивает его ClientID и секрет на
временный (1 час) токен конкретного сотрудника (``grant_type=…token-exchange``, ``subject_token`` — адрес) и входит
по IMAP механизмом XOAUTH2.

Чего API 360 не умеет: квота, занятое место и последний вход ящика, удаление сотрудника (метода нет в
документации), период и тема автоответа; пересылка — только на домены организации.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, NotSupported, ServerAuthError, ServerError,
                    Status, norm_email, split_email)
from ..http import TOKENS, HttpClient, token_request

RULE_AUTOREPLY = "MailArchiver: автоответ"
RULE_FORWARD = "MailArchiver: пересылка"
#: обмен токена сервисного приложения на токен сотрудника
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_EMAIL = "urn:yandex:params:oauth:token-type:email"
#: права сервисного приложения для IMAP (чтение и удаление / только чтение)
IMAP_FULL, IMAP_RO = "mail:imap_full", "mail:imap_ro"
#: права OAuth-приложения администратора (для подсказок)
SCOPES = ("directory:read_organization, directory:read_users, directory:write_users, directory:read_departments, "
          "directory:read_domains, ya360_admin:mail_read_user_settings, ya360_admin:mail_write_user_settings, "
          "ya360_admin:mail_read_organization_settings, ya360_security:domain_sessions_write")
#: страниц списка — не больше (защита от бесконечного цикла)
MAX_PAGES = 200


def _g(user: Dict[str, Any], camel: str, snake: str, default: Any = None) -> Any:
    """Поле сотрудника: прежний хост отдаёт camelCase, Directory v2 — snake_case."""
    if camel in user:
        return user[camel]
    return user.get(snake, default)


class Yandex360Driver(Driver):
    type = "yandex360"
    label = "Яндекс 360 для бизнеса"
    summary = ("API 360: сотрудники, вход, пароли, ФИО, должность и подразделение, псевдонимы, создание, автоответ и "
               "пересылка; копирование ящиков по IMAP токеном сервисного приложения.")
    cloud = True
    #: адреса API (подменяются в тестах): прежний хост, актуальный хост и сервер OAuth
    API_URL = "https://api360.yandex.net"
    CLOUD_URL = "https://cloud-api.yandex.net"
    OAUTH_URL = "https://oauth.yandex.ru"
    fields = (
        Field("org_id", "Идентификатор организации (orgId)", required=True, placeholder="например, 1234567",
              help="Число из консоли администратора admin.yandex.ru. Не знаете — укажите любое и нажмите «Проверить "
                   "подключение»: будут показаны организации, доступные токену."),
        Field("token", "OAuth-токен администратора организации", "password", required=True, secret=True,
              help="Токен приложения, созданного на oauth.yandex.ru администратором организации, с правами API 360 "
                   "(см. шаги настройки). Токен действует долго; перестанет — получите новый тем же способом."),
        Field("client_id", "ClientID сервисного приложения (копирование ящиков)", "password", secret=True,
              help="Приложение с правом mail:imap_full, которое владелец организации сделал сервисным. Пусто — "
                   "ящики без паролей сотрудников копироваться не будут."),
        Field("client_secret", "Client secret сервисного приложения", "password", secret=True),
        Field("imap_host", "IMAP-сервер для копирования ящиков", "select", default="imap.yandex.ru", advanced=True,
              options=(("imap.yandex.ru", "imap.yandex.ru"), ("imap.yandex.com", "imap.yandex.com")),
              help="Токен сотрудника уходит только на этот сервер; у ящиков в архиве должен быть указан он же."),
        Field("department_id", "Подразделение новых сотрудников (номер)", "int", default=1, advanced=True,
              help="1 — корневое подразделение «Все сотрудники»."),
    )
    caps = frozenset({Cap.READ, Cap.BLOCK, Cap.PASSWORD, Cap.MUST_CHANGE, Cap.CONTACT, Cap.CREATE, Cap.ALIASES,
                      Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title", "phone")
    password_rule = "не короче 8 знаков: латинские буквы, цифры и символы (все три вида), без пробелов"
    imap_default = ("imap.yandex.ru", 993, "ssl")
    limits = (
        "Квоту ящика, занятое место и последний вход API 360 не показывает и не задаёт — в сверке их нет.",
        "Удалить сотрудника API 360 не позволяет (метода в документации нет) — удаляйте в консоли admin.yandex.ru. "
        "Письма и Диск удалённого сотрудника восстановить нельзя: сначала убедитесь, что ящик полностью в архиве.",
        "Автоответ — только текст: тему и период действия API не задаёт (тема ставится первой строкой текста). "
        "Включение заменяет все правила автоответа ящика, выключение — удаляет их.",
        "Пересылка — только на адреса доменов организации (ограничение API 360). Включение заменяет все правила "
        "пересылки ящика, выключение — удаляет их. После закрытия входа пересылка продолжает работать.",
        "Учётные записи создаются только в основном домене организации, имя и фамилия обязательны. Требуется ли "
        "смена пароля при входе, API только задаёт — прочитать нельзя.",
        "Псевдонимы — только в домене учётной записи, у ящика их не больше 10.",
        "Копирование без паролей — токеном сервисного приложения: только на тарифах «Основной», «Продвинутый» и "
        "«Корпоративный»; владелец организации обязан уведомить сотрудников (п. 3.6 оферты). Пока ClientID и Client "
        "secret приложения не заданы, ящики в архив через подключение не заводятся.",
        "Служебные учётные записи (роботы), уволенные сотрудники и общие ящики в сверку не попадают.",
        "Действия выполняются только по основному адресу сотрудника: адрес, который стал псевдонимом другого "
        "сотрудника (например, передан коллеге после удаления в консоли), считается отсутствующим — изменения не "
        "уйдут коллеге, а «Перечитать» пометит учётную запись удалённой.",
    )
    setup = (
        "Администратором организации откройте oauth.yandex.ru/client/new: платформа «Веб-сервисы», Redirect URI — "
        "«Подставить URL для отладки», права: " + SCOPES + ". Только сверка — без прав write.",
        "Получите токен: откройте https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID этого "
        "приложения>, войдите администратором и скопируйте токен в «OAuth-токен администратора». Изменили права "
        "приложения — получите новый токен.",
        "Для копирования ящиков без паролей (тариф «Основной», «Продвинутый» или «Корпоративный»): создайте на "
        "oauth.yandex.ru ещё одно приложение с правом mail:imap_full, запишите его ClientID и Client secret.",
        "Владелец организации уведомляет сотрудников (п. 3.6 оферты), получает свой токен с правом "
        "ya360_security:service_applications_write и включает сервисные приложения: POST "
        "https://api360.yandex.net/security/v1/org/<orgId>/service_applications/activate.",
        "Затем регистрирует приложение: POST https://api360.yandex.net/security/v1/org/<orgId>/service_applications с "
        "телом {\"applications\": [{\"id\": \"<ClientID>\", \"scopes\": [\"mail:imap_full\"]}]}. Запрос заменяет весь "
        "список — перечислите в нём и уже зарегистрированные приложения (их видно запросом GET по тому же адресу).",
        "Доступ по IMAP должен быть разрешён: настройка почты организации «IMAP» (действует на новых сотрудников), "
        "а у сотрудников, которым его выключали, — в их настройках Почты («Почтовые программы»).",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._legacy: Optional[HttpClient] = None
        self._cloud: Optional[HttpClient] = None
        self._ids: Dict[str, str] = {}
        self._deps: Optional[Dict[Any, str]] = None
        self._domains: Optional[List[Dict]] = None
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки и клиенты -------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        org = str(settings.get("org_id") or "").strip()
        if not org.isdigit():
            raise ValidationError("Идентификатор организации — число (orgId из консоли admin.yandex.ru).")
        if bool(secrets.get("client_id")) != bool(secrets.get("client_secret")):
            raise ValidationError("Для сервисного приложения нужны и ClientID, и Client secret (или ни того, ни "
                                  "другого).")
        return []

    def _org(self) -> int:
        org = str(self.setting("org_id") or "").strip()
        if not org.isdigit():
            raise ValidationError("Идентификатор организации (orgId) указан неверно — нужно число.")
        return int(org)

    def _client(self, base: str) -> HttpClient:
        token = self.secret("token")
        if not token:
            raise ServerAuthError(f"{self.label}: не задан OAuth-токен администратора.",
                                  hint="Откройте подключение и вставьте токен.")
        return HttpClient(base, api=self.api, server_label=self.label, headers={"Authorization": f"OAuth {token}"})

    @property
    def legacy(self) -> HttpClient:
        """api360.yandex.net — прежний хост."""
        if self._legacy is None:
            self._legacy = self._client(self.API_URL)
        return self._legacy

    @property
    def cloud_api(self) -> HttpClient:
        """cloud-api.yandex.net — актуальный хост."""
        if self._cloud is None:
            self._cloud = self._client(self.CLOUD_URL)
        return self._cloud

    def _call(self, client: HttpClient, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
              body: Any = None, expect: Sequence[int] = (200, 201, 204)) -> Any:
        """Запрос к API 360 → JSON; отказ в доступе — с подсказкой, какие права нужны токену."""
        try:
            if body is None:
                resp = client.request(method, path, params=params, expect=expect)
            else:
                resp = client.request(method, path, params=params, json_body=body, expect=expect)
        except ServerAuthError as exc:
            raise ServerAuthError(exc.message, hint="Токен должен быть выдан администратору организации (или "
                                                    "менеджеру с нужной ролью) приложением с правами: " + SCOPES
                                                    + ". Изменили права — получите новый токен.") from exc
        return resp.json() if resp.body else {}

    def _users_path(self, uid: str = "") -> str:
        path = f"/directory/v1/org/{self._org()}/users"
        return f"{path}/{urllib.parse.quote(str(uid), safe='')}" if uid else path

    # -- чтение --------------------------------------------------------------------------------
    def _all_users(self) -> List[Dict]:
        """Все сотрудники (прежний хост: страницы по 1000)."""
        out: List[Dict] = []
        page = 1
        for _ in range(MAX_PAGES):
            body = self._call(self.legacy, "GET", self._users_path(), params={"page": page, "perPage": 1000})
            out.extend(body.get("users") or [])
            pages = int(body.get("pages") or 1)
            if page >= pages or not body.get("users"):
                return out
            page += 1
        self.incomplete = True
        return out

    def departments(self) -> Dict[Any, str]:
        """Подразделения: номер → название (Directory v2). Не прочитались — пусто (и заметка)."""
        if self._deps is not None:
            return self._deps
        deps: Dict[Any, str] = {}
        path = f"/v1/directory/organizations/{self._org()}/departments"
        offset = 0
        try:
            for _ in range(MAX_PAGES):
                body = self._call(self.cloud_api, "GET", path, params={"limit": 100, "offset": offset})
                items = body.get("items") or []
                for d in items:
                    if not d.get("removed"):
                        deps[d.get("id")] = str(d.get("name") or "")
                step = int(body.get("limit") or 0) or len(items)
                offset += step
                if not items or not step or offset >= int(body.get("total") or 0):
                    break
        except ServerError as exc:
            if self.api is not None:
                self.api.note(f"подразделения не прочитаны (нужно право directory:read_departments): {exc.message}")
            self._deps = {}
            return self._deps
        self._deps = deps
        return deps

    def domains(self) -> List[Dict]:
        """Домены организации (прежний хост, страницы по 10)."""
        if self._domains is not None:
            return self._domains
        out: List[Dict] = []
        page = 1
        for _ in range(MAX_PAGES):
            body = self._call(self.legacy, "GET", f"/directory/v1/org/{self._org()}/domains",
                              params={"page": page, "perPage": 10})
            out.extend(body.get("domains") or [])
            if page >= int(body.get("pages") or 1) or not body.get("domains"):
                break
            page += 1
        self._domains = out
        return out

    def _domain_names(self) -> List[str]:
        return [str(d.get("name") or "").lower() for d in self.domains() if d.get("name")]

    def _account(self, user: Dict, deps: Optional[Dict[Any, str]] = None) -> Optional[Dict[str, Any]]:
        email = norm_email(user.get("email"))
        if not email or _g(user, "isRobot", "is_robot") or _g(user, "isDismissed", "is_dismissed"):
            return None
        domain = split_email(email)[1]
        contacts = [c for c in user.get("contacts") or [] if isinstance(c, dict)]
        aliases: List[str] = []
        for a in user.get("aliases") or []:
            a = str(a).strip().lower()
            if a:
                aliases.append(a if "@" in a else f"{a}@{domain}")
        for c in contacts:
            # для каждого псевдонима сервер сам заводит контакт типа email с признаком alias
            value = norm_email(c.get("value"))
            if c.get("type") == "email" and c.get("alias") and "@" in value and value not in aliases:
                aliases.append(value)
        name = user.get("name") or {}
        phone = next((str(c.get("value") or "") for c in contacts
                      if c.get("type") == "phone" and not c.get("synthetic")), "")
        enabled = _g(user, "isEnabled", "is_enabled")
        dep_id = _g(user, "departmentId", "department_id")
        acc: Dict[str, Any] = {
            "email": email, "native_id": str(user.get("id") or ""), "first_name": name.get("first") or "",
            "last_name": name.get("last") or "", "display_name": _g(user, "displayName", "display_name") or "",
            "aliases": aliases, "login_blocked": None if enabled is None else not enabled,
            "title": user.get("position") or "", "phone": phone,
            "kind": "admin" if _g(user, "isAdmin", "is_admin") else "user",
            "info": {"nickname": user.get("nickname") or "", "middle_name": name.get("middle") or "",
                     "department_id": dep_id, "created": _g(user, "createdAt", "created_at") or "",
                     "enabled_changed": _g(user, "isEnabledUpdatedAt", "is_enabled_updated_at") or ""}}
        if deps:
            acc["department"] = deps.get(dep_id, "")
        two_fa = _g(user, "is2faEnabled", "is_2fa_enabled")
        if two_fa is not None:
            acc["info"]["2fa"] = bool(two_fa)
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        users = self._all_users()
        deps = self.departments()
        out = []
        for user in users:
            acc = self._account(user, deps)
            if acc is not None:
                self._ids[acc["email"]] = acc["native_id"]
                out.append(acc)
        return out

    @staticmethod
    def _is_primary(user: Dict, email: str) -> bool:
        return norm_email(user.get("email")) == email

    @staticmethod
    def _has_alias(user: Dict, email: str) -> bool:
        """Адрес — псевдоним этого сотрудника (в API псевдоним — имя без домена или полный адрес)."""
        local, domain = split_email(email)
        own = split_email(norm_email(user.get("email")))[1]
        for a in user.get("aliases") or []:
            a = str(a).strip().lower()
            if a == email or (domain == own and a == local):
                return True
        for c in user.get("contacts") or []:
            if isinstance(c, dict) and c.get("type") == "email" and c.get("alias") \
                    and norm_email(c.get("value")) == email:
                return True
        return False

    def _find(self, email: str, *, aliases: bool) -> Optional[Dict]:
        """Сотрудник с этим основным адресом (поиском Directory v2, иначе — по полному списку); ``aliases`` —
        если такого нет, то и тот, у кого адрес — псевдоним."""
        def pick(users: List[Dict]) -> Optional[Dict]:
            users = [u for u in users if isinstance(u, dict)]
            hit = next((u for u in users if self._is_primary(u, email)), None)
            if hit is None and aliases:
                hit = next((u for u in users if self._has_alias(u, email)), None)
            return hit

        found = None
        try:
            body = self._call(self.cloud_api, "GET", f"/v1/directory/organizations/{self._org()}/users",
                              params={"email": email})
            found = pick(body.get("items") or [])
        except NotFound:
            found = None
        if found is None:
            found = pick(self._all_users())
        return found

    def _user(self, email: str) -> Optional[Dict]:
        """Сотрудник, у которого это ОСНОВНОЙ адрес: по номеру (если известен), поиском Directory v2, иначе — по
        полному списку. Псевдоним не подходит: адрес удалённого сотрудника, переданный коллеге псевдонимом, привёл
        бы к коллеге — и вход закрылся бы (пароль сменился) у него."""
        email = norm_email(email)
        uid = self._ids.get(email)
        if uid:
            try:
                user = self._call(self.legacy, "GET", self._users_path(uid))
            except NotFound:
                user = None
            if isinstance(user, dict) and self._is_primary(user, email):
                return user
            self._ids.pop(email, None)              # номер устарел: у сотрудника с ним теперь другой адрес
        found = self._find(email, aliases=False)
        if found is not None and found.get("id") is not None:
            self._ids[email] = str(found["id"])
        return found

    def _id(self, email: str) -> str:
        email = norm_email(email)
        if email not in self._ids:
            if self._user(email) is None:
                raise NotFound(f"{self.label}: учётной записи {email} нет.")
        return self._ids[email]

    def _rules(self, uid: str) -> Dict[str, List[Dict]]:
        path = f"/admin/v1/org/{self._org()}/mail/users/{urllib.parse.quote(uid, safe='')}/settings/user_rules"
        body = self._call(self.legacy, "GET", path)
        return {"autoreplies": list(body.get("autoreplies") or []), "forwards": list(body.get("forwards") or [])}

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._user(email)
        if user is None:
            return None
        acc = self._account(user, self.departments())
        if acc is None:
            return None
        self._ids[acc["email"]] = acc["native_id"]
        try:
            rules = self._rules(acc["native_id"])
            acc["autoreply"] = bool(rules["autoreplies"])
            acc["forward_to"] = [norm_email(f.get("address")) for f in rules["forwards"] if f.get("address")]
        except ServerError:
            pass
        return acc

    # -- изменения -----------------------------------------------------------------------------
    def _patch(self, email: str, body: Dict[str, Any]) -> Dict:
        return self._call(self.legacy, "PATCH", self._users_path(self._id(email)), body=body)

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        self._patch(email, {"isEnabled": bool(enabled)})
        if enabled:
            return "вход открыт"
        path = f"/security/v1/org/{self._org()}/domain_sessions/users/{urllib.parse.quote(self._id(email))}/logout"
        try:
            self._call(self.legacy, "PUT", path)
            note = "; сеансы на всех устройствах завершены"
        except ServerError as exc:
            note = f"; открытые сеансы не завершены ({exc.message})"
        if not keep_receiving:
            note += "; приём писем API 360 при закрытом входе не останавливает"
        return "вход закрыт" + note

    def check_password(self, password: str) -> None:
        if not password or not 8 <= len(password) <= 255:
            raise ValidationError(f"Пароль не подходит Яндекс 360: {self.password_rule}.")
        if any(c.isspace() for c in password):
            raise ValidationError("Пароль не должен содержать пробелов.")
        if any(not 33 <= ord(c) <= 126 for c in password):
            raise ValidationError(f"Пароль не подходит Яндекс 360: только латинские буквы, цифры и символы "
                                  f"({self.password_rule}).")
        if not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)
                and any(not c.isalnum() for c in password)):
            raise ValidationError(f"Пароль не подходит Яндекс 360: {self.password_rule}.")

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        self._patch(email, {"password": password, "passwordChangeRequired": bool(must_change)})
        return "пароль сменён" + ("; при следующем входе — смена пароля" if must_change else "")

    def set_must_change(self, email: str, value: bool) -> str:
        self._patch(email, {"passwordChangeRequired": bool(value)})
        return "при следующем входе потребуется сменить пароль" if value else "требование снято"

    def _department_id(self, name: str) -> int:
        want = (name or "").strip().lower()
        if not want:
            raise ValidationError(f"{self.label}: подразделение не может быть пустым.")
        deps = self.departments()
        if not deps:
            raise ServerError(f"{self.label}: подразделения не прочитаны — задать подразделение нечем.",
                              hint="Нужно право directory:read_departments у токена администратора.")
        for dep_id, dep_name in deps.items():
            if dep_name.strip().lower() == want:
                return int(dep_id)
        raise ValidationError(f"{self.label}: подразделения «{name}» в организации нет — создайте его в консоли "
                              f"admin.yandex.ru или исправьте название.")

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        body: Dict[str, Any] = {}
        user = None
        if "first_name" in values or "last_name" in values:
            user = self._user(email)
            if user is None:
                raise NotFound(f"{self.label}: учётной записи {email} нет.")
            name = user.get("name") or {}
            first = values.get("first_name", name.get("first") or "")
            last = values.get("last_name", name.get("last") or "")
            if not first or not last:
                raise ValidationError(f"{self.label}: имя и фамилия обязательны — пустыми их не сделать.")
            body["name"] = {"first": first, "last": last}
            if name.get("middle"):
                body["name"]["middle"] = name["middle"]
        if "display_name" in values:
            body["displayName"] = values["display_name"]
        if "title" in values:
            body["position"] = values["title"]
        if "department" in values:
            body["departmentId"] = self._department_id(values["department"])
        if body:
            self._patch(email, body)
        if "phone" in values:
            self._set_phone(email, values["phone"], user)
        return ""

    def _set_phone(self, email: str, phone: str, user: Optional[Dict]) -> None:
        """Телефон — контактом типа phone. Список контактов заменяется целиком: остальные сохраняем."""
        user = user or self._user(email)
        if user is None:
            raise NotFound(f"{self.label}: учётной записи {email} нет.")
        keep = []
        for c in user.get("contacts") or []:
            # автоматические контакты (основной адрес, псевдонимы) сервер ведёт сам — их не трогаем
            if c.get("synthetic") or c.get("alias") or c.get("main") or c.get("type") == "phone":
                continue
            item = {"type": c.get("type"), "value": c.get("value")}
            if c.get("label"):
                item["label"] = c["label"]
            keep.append(item)
        if phone:
            keep.append({"type": "phone", "value": phone})
        path = f"/v1/api360/directory/org/{self._org()}/users/{urllib.parse.quote(self._id(email))}/contacts"
        self._call(self.cloud_api, "PUT", path, body={"contacts": keep})

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        email = norm_email(email)
        local, domain = split_email(email)
        master = next((str(d.get("name") or "").lower() for d in self.domains() if d.get("master")), "")
        if master and domain != master:
            if domain in self._domain_names():
                raise ValidationError(f"{self.label}: учётные записи создаются в основном домене {master}, а "
                                      f"{domain} — дополнительный домен организации.")
            raise ValidationError(f"{self.label}: домена {domain} в организации нет.")
        if not first_name or not last_name:
            raise ValidationError(f"{self.label}: для новой учётной записи нужны имя и фамилия (строка «адрес; имя; "
                                  f"фамилия»).")
        owner = self._find(email, aliases=True)
        if owner is not None:
            if self._is_primary(owner, email):
                raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.")
            raise AlreadyExists(f"{self.label}: адрес {email} занят — это псевдоним учётной записи "
                                f"{norm_email(owner.get('email')) or owner.get('id')}.")
        try:
            dep = int(self.setting("department_id", 1) or 1)
        except (TypeError, ValueError):
            dep = 1
        body = {"nickname": local, "password": password, "passwordChangeRequired": bool(must_change),
                "departmentId": dep, "name": {"first": first_name, "last": last_name}}
        try:
            user = self._call(self.legacy, "POST", self._users_path(), body=body)
        except ServerError as exc:
            low = exc.message.lower()
            if "already" in low or "exist" in low or "занят" in low or "уже" in low:
                raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.") from exc
            raise
        user.setdefault("email", email)
        if user.get("id") is not None:
            self._ids[email] = str(user["id"])
        return self._account(user, self.departments()) or {"email": email, "first_name": first_name,
                                                           "last_name": last_name}

    def _alias_local(self, email: str, alias: str) -> str:
        local, domain = split_email(alias)
        own = split_email(email)[1]
        if domain and domain != own:
            raise ValidationError(f"{self.label}: псевдоним — только в домене учётной записи ({own}), а не {domain}.")
        return local

    def add_alias(self, email: str, alias: str) -> Optional[List[str]]:
        local = self._alias_local(email, alias)
        path = f"/v1/api360/directory/org/{self._org()}/users/{urllib.parse.quote(self._id(email))}/aliases"
        try:
            user = self._call(self.cloud_api, "POST", path, body={"alias": local})
        except ServerError as exc:
            if "already_exists" in exc.message:
                raise AlreadyExists(f"{self.label}: адрес {norm_email(alias)} уже занят.") from exc
            raise
        acc = self._account(user) if isinstance(user, dict) and user.get("email") else None
        return acc["aliases"] if acc else None

    def remove_alias(self, email: str, alias: str) -> Optional[List[str]]:
        local = self._alias_local(email, alias)
        base = f"/v1/api360/directory/org/{self._org()}/users/{urllib.parse.quote(self._id(email))}/aliases"
        try:
            self._call(self.cloud_api, "DELETE", f"{base}/{urllib.parse.quote(local, safe='')}")
        except NotFound:
            # формат псевдонима в документации не уточнён: пробуем и полный адрес
            self._call(self.cloud_api, "DELETE", f"{base}/{urllib.parse.quote(norm_email(alias), safe='')}")
        return None

    # -- автоответ и пересылка ----------------------------------------------------------------
    def _rules_path(self, email: str, rule_id: Any = None) -> str:
        path = f"/admin/v1/org/{self._org()}/mail/users/{urllib.parse.quote(self._id(email))}/settings/user_rules"
        return f"{path}/{int(rule_id)}" if rule_id is not None else path

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        rules = self._rules(self._id(email))["autoreplies"]
        return {"enabled": bool(rules), "subject": "", "text": (rules[0].get("text") or "") if rules else "",
                "start": "", "end": ""}

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        if enabled and (start or end):
            raise NotSupported(f"{self.label}: автоответ на период API 360 не задаёт — только включить и выключить "
                               f"(уберите даты и выключите автоответ потом).")
        old = self._rules(self._id(email))["autoreplies"]
        new_id = None
        if enabled:
            body = f"{subject.strip()}\n\n{text}" if subject.strip() else text
            created = self._call(self.legacy, "POST", self._rules_path(email),
                                 body={"autoreply": {"ruleName": RULE_AUTOREPLY, "text": body}})
            new_id = created.get("ruleId")
        for rule in old:
            if rule.get("ruleId") is not None and rule.get("ruleId") != new_id:
                self._call(self.legacy, "DELETE", self._rules_path(email, rule["ruleId"]))
        if not enabled:
            return "автоответ выключен" if old else "автоответа и так не было"
        return "автоответ включён" + ("; тема — первой строкой текста (тему ответа API не задаёт)"
                                      if subject.strip() else "")

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        rules = self._rules(self._id(email))["forwards"]
        return {"addresses": [norm_email(r.get("address")) for r in rules if r.get("address")],
                "keep_copy": all(r.get("withStore", True) for r in rules)}

    def set_forwarding(self, email: str, *, addresses, keep_copy: bool = True) -> str:
        wanted = [norm_email(a) for a in addresses]
        if wanted:
            own = self._domain_names()
            outside = [a for a in wanted if split_email(a)[1] not in own]
            if outside:
                raise NotSupported(f"{self.label}: пересылка только на адреса доменов организации "
                                   f"({', '.join(own) or '—'}) — {', '.join(outside)} не подходит.")
        old = self._rules(self._id(email))["forwards"]
        have = {(norm_email(r.get("address")), bool(r.get("withStore", True))) for r in old}
        for address in wanted:
            if (address, bool(keep_copy)) not in have:
                self._call(self.legacy, "POST", self._rules_path(email),
                           body={"forward": {"ruleName": RULE_FORWARD, "address": address,
                                             "withStore": bool(keep_copy)}})
        for rule in old:
            if (norm_email(rule.get("address")) not in wanted or bool(rule.get("withStore", True)) != bool(keep_copy)) \
                    and rule.get("ruleId") is not None:
                self._call(self.legacy, "DELETE", self._rules_path(email, rule["ruleId"]))
        if not wanted:
            return "пересылка выключена" if old else "пересылки и так не было"
        return f"пересылка на {', '.join(wanted)}" + ("" if keep_copy else "; в ящике писем не остаётся")

    # -- IMAP и состояние ----------------------------------------------------------------------
    def imap_admin_problem(self) -> str:
        if not self.secret("client_id") or not self.secret("client_secret"):
            return ("не задано сервисное приложение (ClientID и Client secret) — ящики копируются только с паролями "
                    "сотрудников")
        return ""

    def imap_auth(self, email: str) -> ImapAuth:
        email = norm_email(email)
        return ImapAuth(mech="xoauth2", user=email, secret=self.user_token(email), host=self.imap_server()[0])

    def user_token(self, email: str) -> str:
        """Временный (1 час) токен сотрудника от сервисного приложения — обмен токена по адресу."""
        client_id, client_secret = self.secret("client_id"), self.secret("client_secret")
        if not client_id or not client_secret:
            raise ServerAuthError(f"{self.label}: сервисное приложение не задано — войти в ящик без пароля "
                                  f"сотрудника нечем.", hint="Укажите в подключении ClientID и Client secret "
                                                             "сервисного приложения (см. шаги настройки).")
        key = ("yandex360", hashlib.sha256(f"{client_id}:{client_secret}".encode("utf-8")).hexdigest()[:16],
               self.OAUTH_URL, email)
        cached = TOKENS.get(key)
        if cached:
            if self.api is not None:
                self.api.secret(cached)
            return cached
        form = {"grant_type": TOKEN_EXCHANGE, "client_id": client_id, "client_secret": client_secret,
                "subject_token": email, "subject_token_type": SUBJECT_EMAIL}
        oauth = HttpClient(self.OAUTH_URL, api=self.api, server_label=self.label)
        try:
            body = token_request(oauth, "/token", form, self.label)
        except ServerAuthError as exc:
            raise ServerAuthError(
                f"{exc.message} (токен сотрудника {email})",
                hint="Проверьте ClientID и Client secret сервисного приложения, что владелец организации включил "
                     "сервисные приложения и зарегистрировал это приложение с правом mail:imap_full, и что тариф "
                     "это позволяет.") from exc
        token = body["access_token"]
        if self.api is not None:
            self.api.secret(token)
        TOKENS.put(key, token, body.get("expires_in") or 3600)
        return token

    def test(self) -> Status:
        st = Status(version="API 360")
        org_id = self._org()
        try:
            orgs = self._call(self.legacy, "GET", "/directory/v1/org", params={"pageSize": 100})
        except ServerAuthError as exc:
            if "(401)" in exc.message:
                raise ServerAuthError(f"{self.label}: OAuth-токен администратора не принят — {exc.message}",
                                      hint="Получите новый токен (шаги настройки подключения).") from exc
            orgs = None
            st.problems.append(("org", "warning", "сведения об организации не прочитаны (право "
                                                  "directory:read_organization)"))
        if orgs is not None:
            items = orgs.get("organizations") or []
            org = next((o for o in items if str(o.get("id")) == str(org_id)), None)
            if org is None:
                known = ", ".join(f"{o.get('id')} — {o.get('name')}" for o in items[:10]) or "ни одной"
                raise ServerError(f"{self.label}: организации {org_id} нет среди доступных токену ({known}).",
                                  hint="Исправьте идентификатор организации или получите токен администратора "
                                       "этой организации.")
            st.details.append(f"Организация: {org.get('name') or org_id} (№{org_id})")
            if org.get("subscriptionPlan"):
                st.details.append(f"Тариф: {org['subscriptionPlan']}")
        try:
            domains = self.domains()
        except ServerAuthError:
            domains = []
            st.problems.append(("domains", "warning", "домены не прочитаны (право directory:read_domains) — "
                                                      "пересылку и создание учётных записей не проверить"))
        if domains:
            names = [f"{d.get('name')}{' (основной)' if d.get('master') else ''}" for d in domains]
            st.details.append("Домены: " + ", ".join(names[:10]))
            for d in domains:
                if not d.get("verified"):
                    st.problems.append((f"domain_{d.get('name')}", "warning", f"домен {d.get('name')} не подтверждён"))
                elif d.get("mx") is False:
                    st.problems.append((f"mx_{d.get('name')}", "warning",
                                        f"MX-запись домена {d.get('name')} не указывает на mx.yandex.net — письма "
                                        f"могут не доходить"))
        sample: List[Dict] = []
        try:
            body = self._call(self.legacy, "GET", self._users_path(), params={"page": 1, "perPage": 20})
            sample = body.get("users") or []
            st.details.append(f"Сотрудников: {body.get('total', len(sample))}")
        except ServerAuthError as exc:
            st.problems.append(("users", "error", f"список сотрудников не прочитан: {exc.message}"))
        try:
            mail = self._call(self.cloud_api, "GET", f"/v1/admin/org/{org_id}/mail-settings")
            if mail.get("enable_imap") is False:
                st.problems.append(("imap_off", "warning", "в настройках почты организации доступ по IMAP выключен "
                                                           "— у новых сотрудников ящики копироваться не будут"))
        except ServerError as exc:
            self.api.note(f"настройки почты организации не прочитаны: {exc.message}")
        self._test_service_app(st, org_id, sample)
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st

    def _test_service_app(self, st: Status, org_id: int, sample: List[Dict]) -> None:
        client_id = self.secret("client_id")
        if not client_id or not self.secret("client_secret"):
            st.problems.append(("service_app", "warning", "сервисное приложение не задано — ящики без паролей "
                                                          "сотрудников копироваться не будут"))
            return
        try:
            apps = self._call(self.legacy, "GET", f"/security/v1/org/{org_id}/service_applications")
        except ServerError as exc:
            apps = None
            self.api.note(f"список сервисных приложений не прочитан (его видит только владелец организации): "
                          f"{exc.message}")
        if apps is not None:
            app = next((a for a in apps.get("applications") or [] if str(a.get("id")) == client_id), None)
            if app is None:
                st.problems.append(("service_app", "error", "приложение с указанным ClientID не зарегистрировано "
                                                            "как сервисное — копирование ящиков не пойдёт"))
                return
            scopes = set(app.get("scopes") or [])
            if IMAP_FULL not in scopes and IMAP_RO not in scopes:
                st.problems.append(("service_app", "error", "у сервисного приложения нет права mail:imap_full — "
                                                            "копирование ящиков не пойдёт"))
                return
            if IMAP_FULL not in scopes:
                st.problems.append(("service_app", "warning", "у сервисного приложения только mail:imap_ro: "
                                                              "копирование пойдёт, действия с письмами на сервере — "
                                                              "нет"))
            st.details.append("Сервисное приложение зарегистрировано: " + ", ".join(sorted(scopes)))
        probe = next((norm_email(u.get("email")) for u in sample
                      if u.get("email") and u.get("isEnabled", True) and not u.get("isRobot")
                      and not u.get("isDismissed")), "")
        if probe:
            try:
                self.user_token(probe)
                st.details.append(f"Токен сотрудника для IMAP получен ({probe})")
            except ServerError as exc:
                st.problems.append(("imap_token", "warning", f"токен сотрудника для IMAP не получен — копирование "
                                                             f"ящиков без паролей не пойдёт: {exc.message}"))


DRIVER = Yandex360Driver
