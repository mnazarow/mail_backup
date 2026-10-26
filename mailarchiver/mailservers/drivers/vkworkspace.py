"""
VK WorkSpace (бывшая «Почта Mail.ru для бизнеса», biz.mail.ru) — через API biz.mail.ru v1.

Документация: https://biz.mail.ru/developer/ (разделы «API», «OAuth 2.0 авторизация», «Пользователи»; часть
страниц уже перенаправляет на workspace.vk.ru/docs/saas/api). Что описано и используется здесь:

* адрес API ``https://biz.mail.ru/api/v1/``, токен — в заголовке ``Authorization: Bearer``; списки — страницами
  (``offset``/``limit`` ≤ 100, ответ ``{"paging": {...}, "data": [...]}``); ошибки клиента — HTTP 400
  ``{"code", "message", "data"}``; не больше 200 запросов в минуту с одного адреса;
* OAuth 2.0 на o2.mail.ru: администратор один раз получает код (``/login``, область ``biz.api``) и обменивает его
  на refresh token (``/token``); MailArchiver по refresh token сам получает токен доступа (действует час). Клиента
  API регистрирует служба поддержки;
* пользователи домена: список, одна учётная запись, создание, изменение (имя и фамилия — только вместе, пароль,
  ``status``: active / blocked / soft-blocked), псевдонимы (список, добавить, удалить, не больше 5). Удаление в API
  есть (пометка на удаление), но MailArchiver его не выполняет: удаляет он только после сверки всего ящика с
  архивом по IMAP без пароля сотрудника, а такого входа у VK WorkSpace нет.

Номер домена — из списка доменов (``GET /domains``: пример запроса есть в разделе «API», страница «Домены»
сейчас перенаправляет на новый сайт, поэтому ответ разбирается осторожно: элементы с ``id`` и ``name``) или из
настройки подключения.

Чего API не умеет: войти в ящик без пароля сотрудника (ящики копируются по IMAP imap.mail.ru с паролем
приложения каждого сотрудника — поэтому и удаления через MailArchiver нет), квота и занятое место, последний вход,
автоответ, пересылка, требование сменить пароль, отдел и должность.
"""
from __future__ import annotations

import hashlib
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, NotFound, ServerAuthError, ServerError, Status, norm_email,
                    split_email)
from ..http import TOKENS, HttpClient, token_request

#: страниц списка — не больше (защита от бесконечного цикла)
MAX_PAGES = 1000
PAGE = 100
#: статусы пользователя (раздел «Пользователи»)
BLOCKED = ("blocked", "soft-blocked")
GONE = ("purging", "deleted")
#: коды ошибок API → понятный текст (тексты — из документации)
CODES = {
    1: "домен не подтверждён", 2: "в данный момент услуга недоступна", 134: "нельзя удалить ящик по умолчанию — "
    "смените его в настройках", 135: "нельзя удалить ящик администратора домена", 140: "невозможно создать "
    "пользователя", 141: "администратор с неподтверждённым телефоном может добавить не больше 5 пользователей",
    142: "превышен предел числа пользователей домена — обратитесь в службу поддержки", 143: "превышен предел "
    "псевдонимов у пользователя (не больше 5)", 144: "невозможно добавить такой псевдоним", 145: "невозможно "
    "сменить пароль пользователя", 147: "невозможно изменить пользователя", 192: "нельзя восстановить "
    "удалённого (терминированного) пользователя",
}
#: номера учётных записей по адресу (между сессиями: чтобы перечитать одну учётную запись без полного списка)
_IDS: Dict[Tuple[str, str], Dict[str, str]] = {}
_IDS_LOCK = threading.RLock()


class VKWorkSpaceDriver(Driver):
    type = "vkworkspace"
    label = "VK WorkSpace"
    summary = ("API biz.mail.ru (OAuth o2.mail.ru): учётные записи домена, вход, пароли, имя и фамилия, псевдонимы, "
               "создание; ящики копируются по IMAP с паролями приложений сотрудников.")
    cloud = True
    #: адреса API (подменяются в тестах)
    API_URL = "https://biz.mail.ru/api/v1"
    OAUTH_URL = "https://o2.mail.ru"
    fields = (
        Field("domain", "Почтовый домен", required=True, placeholder="company.ru",
              help="Домен организации в VK WorkSpace — учётные записи этого домена попадут в сверку."),
        Field("client_id", "Идентификатор клиента API (client_id)", required=True,
              help="Выдаёт служба поддержки VK WorkSpace при регистрации клиента API."),
        Field("refresh_token", "Refresh token", "password", required=True, secret=True,
              help="Из ответа o2.mail.ru/token после входа администратора домена (см. шаги настройки). Токен "
                   "доступа MailArchiver получает по нему сам."),
        Field("domain_id", "Номер домена в API", "int", advanced=True,
              help="Пусто — найти по названию домена (список доменов, доступных токену)."),
        Field("read_aliases", "Читать псевдонимы при сверке", "bool", default=True, advanced=True,
              help="Псевдонимы — отдельный запрос на каждую учётную запись, а сервер принимает не больше 200 "
                   "запросов в минуту: в больших доменах сверка идёт дольше."),
    )
    #: удаления нет: MailArchiver удаляет учётную запись только после сверки ящика с архивом по IMAP без пароля
    #: сотрудника, а у VK WorkSpace такого входа нет
    caps = frozenset({Cap.READ, Cap.BLOCK, Cap.PASSWORD, Cap.CONTACT, Cap.CREATE, Cap.ALIASES, Cap.STATUS})
    contact_fields = ("first_name", "last_name")
    password_rule = ("от 6 до 40 знаков латиницей, хотя бы один спецсимвол, не только цифры; без имени, логина, "
                     "телефона и названия домена")
    imap_default = ("imap.mail.ru", 993, "ssl")
    limits = (
        "Войти в ящик без пароля сотрудника API VK WorkSpace не позволяет («войти как пользователь» есть только в "
        "веб-панели администратора): ящики копируются по IMAP (imap.mail.ru, 993, SSL) с паролем приложения "
        "каждого сотрудника — заведите их в «Почтовых ящиках».",
        "Поэтому удалить учётную запись через MailArchiver нельзя: удаляет он только после сверки всего ящика с "
        "архивом по IMAP без пароля сотрудника, а такого входа нет. Удаляйте в панели администратора (30 дней "
        "учётную запись можно восстановить); после сверки она будет помечена удалённой, и её адрес можно передать "
        "коллеге («Передать адрес коллеге»).",
        "Квоту, занятое место, последний вход, автоответ и пересылку API не показывает и не задаёт.",
        "Требовать смену пароля при входе, задавать отдел, должность и отображаемое имя API не умеет; имя и "
        "фамилия меняются только вместе.",
        "Закрыть вход: «мягкая» блокировка — вход запрещён, письма продолжают приходить; если письма принимать не "
        "нужно — полная блокировка.",
        "Псевдонимов у учётной записи — не больше 5, только в её домене.",
        "Сервер принимает не больше 200 запросов в минуту: в больших доменах сверка с псевдонимами идёт дольше.",
    )
    setup = (
        "Напишите в службу поддержки VK WorkSpace: зарегистрировать клиента API biz.mail.ru для вашего домена. "
        "Получите client_id, client_secret и согласуйте адрес возврата (redirect_uri).",
        "Администратор домена открывает в браузере https://o2.mail.ru/login?client_id=<client_id>&response_type=code"
        "&scope=biz.api&redirect_uri=<адрес возврата>&state=<любая строка> и разрешает доступ. Из адреса, на "
        "который его вернёт, возьмите code — он действует 5 минут.",
        "Обменяйте код на токены: curl -u <client_id>:<client_secret> -d grant_type=authorization_code -d "
        "code=<code> -d redirect_uri=<адрес возврата> https://o2.mail.ru/token. В подключение вставьте client_id "
        "и refresh_token из ответа.",
        "Ящики в архив заводятся вручную («Почтовые ящики»): сервер imap.mail.ru, порт 993, SSL, логин — адрес, "
        "пароль — пароль приложения сотрудника. Пароли приложений администратор раздаёт массово (панель "
        "администратора → «Пользователи» → CSV), или сотрудник создаёт сам: «Безопасность» → «Пароли для внешних "
        "приложений», полный доступ к почте.",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._client: Optional[HttpClient] = None
        self._fresh = False
        self._domain: Optional[str] = None
        self._domain_info: Dict[str, Any] = {}
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки, токен, запросы -------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        domain = str(settings.get("domain") or "").strip().lower()
        if "." not in domain or any(c in domain for c in "@/: "):
            raise ValidationError("Почтовый домен указан неверно — нужно имя вида company.ru.")
        return []

    def _domain_name(self) -> str:
        return str(self.setting("domain") or "").strip().lower()

    def _token_key(self) -> Tuple:
        material = hashlib.sha256(self.secret("refresh_token").encode("utf-8")).hexdigest()[:16]
        return ("vkworkspace", self.OAUTH_URL, str(self.setting("client_id") or ""), material)

    def token(self) -> str:
        """Токен доступа по refresh token (кэш — до истечения)."""
        client_id = str(self.setting("client_id") or "").strip()
        refresh = self.secret("refresh_token")
        if not client_id or not refresh:
            raise ServerAuthError(f"{self.label}: не заданы client_id и refresh token.",
                                  hint="Откройте подключение и заполните их (см. шаги настройки).")
        key = self._token_key()
        cached = TOKENS.get(key)
        if cached:
            if self.api is not None:
                self.api.secret(cached)
            return cached
        oauth = HttpClient(self.OAUTH_URL, api=self.api, server_label=self.label)
        try:
            body = token_request(oauth, "/token", {"client_id": client_id, "grant_type": "refresh_token",
                                                   "refresh_token": refresh}, self.label)
        except ServerAuthError as exc:
            hint = "Проверьте client_id и refresh token."
            if "token not found" in exc.message.lower():
                hint = ("Refresh token больше не действует — администратор домена должен заново войти через "
                        "o2.mail.ru и получить новый (шаги настройки).")
            raise ServerAuthError(exc.message, hint=hint) from exc
        token = body["access_token"]
        if self.api is not None:
            self.api.secret(token)
        TOKENS.put(key, token, body.get("expires_in") or 3600)
        self._fresh = True
        return token

    @property
    def client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient(self.API_URL, api=self.api, server_label=self.label,
                                      headers={"Authorization": f"Bearer {self.token()}"})
        return self._client

    def _call(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, body: Any = None,
              expect: Sequence[int] = (200, 201, 204)) -> Any:
        """Запрос к API → JSON. Ошибка клиента (400) — текстом сервера; токен отозван — один повтор с новым."""
        for attempt in (1, 2):
            client = self.client                    # отказ сервера токенов — сразу, без повтора
            try:
                if body is None:
                    resp = client.request(method, path, params=params, expect=tuple(expect) + (400,))
                else:
                    resp = client.request(method, path, params=params, json_body=body, expect=tuple(expect) + (400,))
            except ServerAuthError:
                if attempt == 1 and not self._fresh:
                    # токен из кэша могли отозвать раньше срока — берём новый и повторяем
                    TOKENS.drop(self._token_key())
                    self._client = None
                    continue
                raise
            if resp.status == 400:
                raise self._client_error(resp.json() if resp.body else {})
            return resp.json() if resp.body else {}
        raise ServerError(f"{self.label}: запрос не выполнен.")  # pragma: no cover — цикл выше всегда выходит

    def _client_error(self, body: Any) -> ServerError:
        body = body if isinstance(body, dict) else {}
        code = body.get("code")
        text = CODES.get(code) or str(body.get("message") or "ошибка в данных запроса").rstrip(".")
        details = []
        data = body.get("data")
        if isinstance(data, dict):
            for field, problems in data.items():
                problems = problems if isinstance(problems, list) else [problems]
                details.append(f"{field}: {'; '.join(str(p) for p in problems)}")
        elif data:
            details.append(str(data))
        message = f"{self.label}: сервер отклонил запрос ({text}" + (f" — {', '.join(details)}" if details else "") \
            + ")"
        low = " ".join(details).lower()
        if code == 0 and ("существ" in low or "занят" in low or "already" in low or "exists" in low):
            return AlreadyExists(message)
        return ServerError(message)

    # -- домен и список ------------------------------------------------------------------------
    @staticmethod
    def _items(body: Any) -> List[Dict]:
        if isinstance(body, list):
            return [x for x in body if isinstance(x, dict)]
        if isinstance(body, dict):
            return [x for x in body.get("data") or [] if isinstance(x, dict)]
        return []

    def _pages(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """Все страницы списка: offset/limit, пока в ``paging`` есть ``next``."""
        out: List[Dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = self._call("GET", path, params=dict(params or {}, limit=PAGE, offset=offset))
            items = self._items(body)
            out.extend(items)
            paging = body.get("paging") if isinstance(body, dict) else None
            if not items or not isinstance(paging, dict) or not paging.get("next"):
                return out
            offset += len(items)
        self.incomplete = True
        return out

    def domains(self) -> List[Dict]:
        return self._pages("/domains")

    def domain_id(self) -> str:
        if self._domain is not None:
            return self._domain
        fixed = self.setting("domain_id")
        if fixed not in (None, ""):
            self._domain = str(int(fixed))
            return self._domain
        name = self._domain_name()
        found = self.domains()
        for d in found:
            if str(d.get("name") or "").strip().lower() == name and d.get("id") is not None:
                self._domain = str(d["id"])
                self._domain_info = d
                return self._domain
        known = ", ".join(str(d.get("name")) for d in found[:10] if d.get("name")) or "ни одного"
        raise NotFound(f"{self.label}: домена {name} нет среди доступных токену ({known}).",
                       hint="Проверьте домен в подключении и что токен получен администратором этого домена.")

    def _users_path(self, uid: str = "") -> str:
        path = f"/domains/{self.domain_id()}/users"
        return f"{path}/{urllib.parse.quote(str(uid), safe='')}" if uid else path

    def _ids(self) -> Dict[str, str]:
        with _IDS_LOCK:
            return _IDS.setdefault((self.API_URL, self.domain_id()), {})

    def _remember(self, users: List[Dict]) -> None:
        ids = self._ids()
        with _IDS_LOCK:
            # удалённые — первыми: если с тем же адресом есть и действующая учётная запись, запомнится она
            for u in sorted(users, key=lambda u: str(u.get("status") or "") not in GONE):
                email = norm_email(u.get("email"))
                if email and u.get("id") is not None:
                    ids[email] = str(u["id"])
            while len(ids) > 200000:
                ids.pop(next(iter(ids)))

    def _all_users(self) -> List[Dict]:
        users = self._pages(self._users_path())
        self._remember(users)
        return users

    def _account(self, user: Dict) -> Optional[Dict[str, Any]]:
        email = norm_email(user.get("email"))
        status = str(user.get("status") or "")
        if not email or status in GONE:
            return None
        groups = [str(g.get("name")) for g in user.get("groups") or [] if isinstance(g, dict) and g.get("name")]
        return {"email": email, "native_id": str(user.get("id") or ""), "first_name": user.get("firstname") or "",
                "last_name": user.get("lastname") or "", "display_name": user.get("nick") or "",
                "login_blocked": True if status in BLOCKED else (False if status == "active" else None),
                "kind": "user",
                "info": {"status": status, "status_at": user.get("status_at") or "",
                         "created": user.get("created_at") or "", "username": user.get("username") or "",
                         "groups": groups, "spammer": bool(user.get("is_spammer"))}}

    def aliases(self, uid: str) -> List[str]:
        body = self._call("GET", f"{self._users_path(uid)}/aliases")
        out = []
        for a in self._items(body):
            email = norm_email(a.get("email")) or (f"{str(a.get('username')).lower()}@{self._domain_name()}"
                                                   if a.get("username") else "")
            if email:
                out.append(email)
        return out

    def list_accounts(self) -> List[Dict[str, Any]]:
        out = []
        read_aliases = bool(self.setting("read_aliases", True))
        alias_error = ""
        for user in self._all_users():
            acc = self._account(user)
            if acc is None:
                continue
            if read_aliases and not alias_error:
                try:
                    acc["aliases"] = self.aliases(acc["native_id"])
                except ServerError as exc:
                    alias_error = exc.message
            out.append(acc)
        if alias_error:
            self.sync_problems.append(f"псевдонимы прочитаны не у всех учётных записей: {alias_error}")
        return out

    def _user(self, email: str) -> Optional[Dict]:
        """Учётная запись по адресу (любого статуса, действующая — в первую очередь): по известному номеру, иначе —
        по полному списку. Номер, запомненный в прошлых сессиях, мог остаться от удалённой учётной записи, а с тем же
        адресом уже заведена новая — тогда ищется по списку."""
        email = norm_email(email)
        uid = self._ids().get(email)
        stale: Optional[Dict] = None
        if uid:
            try:
                user = self._call("GET", self._users_path(uid))
            except NotFound:
                user = None
            if isinstance(user, dict) and norm_email(user.get("email")) == email:
                if str(user.get("status") or "") not in GONE:
                    return user
                stale = user
            with _IDS_LOCK:
                self._ids().pop(email, None)
        matches = [u for u in self._all_users() if norm_email(u.get("email")) == email]
        live = next((u for u in matches if str(u.get("status") or "") not in GONE), None)
        if live is not None:
            return live
        return matches[0] if matches else stale

    def _id(self, email: str) -> str:
        user = self._user(email)
        if user is None or str(user.get("status") or "") in GONE:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        return str(user["id"])

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._user(email)
        acc = self._account(user) if user else None
        if acc is None:
            return None
        acc["aliases"] = self.aliases(acc["native_id"])
        return acc

    # -- изменения -----------------------------------------------------------------------------
    def _patch(self, email: str, body: Dict[str, Any]) -> Dict:
        return self._call("PATCH", self._users_path(self._id(email)), body=body)

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        if enabled:
            self._patch(email, {"status": "active"})
            return "вход открыт"
        if keep_receiving:
            self._patch(email, {"status": "soft-blocked"})
            return "вход закрыт («мягкая» блокировка: письма продолжают приходить)"
        self._patch(email, {"status": "blocked"})
        return "вход закрыт (полная блокировка)"

    def check_password(self, password: str) -> None:
        if not password or not 6 <= len(password) <= 40:
            raise ValidationError(f"Пароль не подходит VK WorkSpace: {self.password_rule}.")
        if any(c.isspace() for c in password):
            raise ValidationError("Пароль не должен содержать пробелов.")
        if any(not 33 <= ord(c) <= 126 for c in password):
            raise ValidationError(f"Пароль не подходит VK WorkSpace: только латиница, цифры и спецсимволы "
                                  f"({self.password_rule}).")
        if password.isdigit() or all(c.isalnum() for c in password):
            raise ValidationError(f"Пароль не подходит VK WorkSpace: {self.password_rule}.")
        name = self._domain_name().split(".")[0]
        if len(name) >= 4 and name in password.lower():
            raise ValidationError("Пароль не должен содержать название домена.")

    def random_password(self) -> str:
        """Случайный пароль, который примет VK WorkSpace (20 знаков, со спецсимволом, без названия домена)."""
        from ..base import random_password
        while True:
            value = random_password()
            try:
                self.check_password(value)
                return value
            except ValidationError:
                continue

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        local = split_email(email)[0]
        if len(local) >= 4 and local in password.lower():
            raise ValidationError("Пароль не должен содержать логин учётной записи.")
        self._patch(email, {"password": password})
        return "пароль сменён"

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        if "first_name" not in values and "last_name" not in values:
            return ""
        user = self._user(email)
        if user is None:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        # имя и фамилия меняются только вместе
        body = {"firstname": values.get("first_name", user.get("firstname") or ""),
                "lastname": values.get("last_name", user.get("lastname") or "")}
        self._call("PATCH", self._users_path(str(user["id"])), body=body)
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        email = norm_email(email)
        local, domain = split_email(email)
        if domain != self._domain_name():
            raise ValidationError(f"{self.label}: подключение работает с доменом {self._domain_name()}, а адрес "
                                  f"{email} — из другого.")
        if bool(first_name) != bool(last_name):
            raise ValidationError(f"{self.label}: имя и фамилия задаются только вместе — укажите оба или ни одного.")
        existing = self._user(email)
        if existing is not None:
            note = " (помечена на удаление — восстановите её в панели администратора)" \
                if str(existing.get("status") or "") in GONE else ""
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть{note}.")
        body: Dict[str, Any] = {"username": local, "password": password}
        if first_name and last_name:
            body.update({"firstname": first_name, "lastname": last_name})
        user = self._call("POST", self._users_path(), body=body, expect=(200, 201))
        if not isinstance(user, dict):
            user = {}
        user.setdefault("email", email)
        self._remember([user])
        return self._account(user) or {"email": email, "first_name": first_name, "last_name": last_name}

    # delete_account — нет (см. caps): удаляют в панели администратора

    def _alias_local(self, email: str, alias: str) -> str:
        local, domain = split_email(alias)
        own = split_email(email)[1]
        if domain and domain != own:
            raise ValidationError(f"{self.label}: псевдоним — только в домене учётной записи ({own}), а не {domain}.")
        return local

    def add_alias(self, email: str, alias: str) -> Optional[List[str]]:
        local = self._alias_local(email, alias)
        uid = self._id(email)
        try:
            self._call("POST", f"{self._users_path(uid)}/aliases", body={"username": local}, expect=(200, 201))
        except AlreadyExists as exc:
            raise AlreadyExists(f"{self.label}: адрес {norm_email(alias)} уже занят ({exc.message}).") from exc
        return self.aliases(uid)

    def remove_alias(self, email: str, alias: str) -> Optional[List[str]]:
        local = self._alias_local(email, alias)
        uid = self._id(email)
        body = self._call("GET", f"{self._users_path(uid)}/aliases")
        target = norm_email(alias) if "@" in alias else f"{local}@{split_email(email)[1]}"
        found = next((a for a in self._items(body) if norm_email(a.get("email")) == target
                      or str(a.get("username") or "").lower() == local), None)
        if found is None or found.get("id") is None:
            raise NotFound(f"{self.label}: псевдонима {target} у {norm_email(email)} нет.")
        self._call("DELETE", f"{self._users_path(uid)}/aliases/{urllib.parse.quote(str(found['id']), safe='')}")
        return self.aliases(uid)

    # -- состояние -------------------------------------------------------------------------------
    def test(self) -> Status:
        st = Status(version="biz.mail.ru API v1")
        try:
            domain_id = self.domain_id()
        except ServerAuthError as exc:
            raise ServerAuthError(f"{self.label}: доступ к API не получен — {exc.message}",
                                  hint=exc.hint or "Проверьте client_id и refresh token.") from exc
        line = f"Домен: {self._domain_name()} (№{domain_id})"
        if self._domain_info.get("status"):
            line += f", состояние: {self._domain_info['status']}"
        st.details.append(line)
        body = self._call("GET", self._users_path(), params={"limit": 1, "offset": 0})
        paging = body.get("paging") if isinstance(body, dict) else None
        if isinstance(paging, dict) and paging.get("results_count") is not None:
            st.details.append(f"Учётных записей: {paging['results_count']}")
        st.details.append("Ящики копируются по IMAP (imap.mail.ru) с паролями приложений сотрудников.")
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = VKWorkSpaceDriver
