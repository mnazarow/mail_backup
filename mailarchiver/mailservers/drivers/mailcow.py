"""
mailcow (mailcow-dockerized) — через REST API ``/api/v1`` с ключом ``X-API-Key``.

Запросы — ``/api/v1/<get|add|edit|delete>/<что>[/<кто>]``: чтение — GET, изменения — POST с JSON. Изменения
отвечают кодом 200 и списком итогов ``{"type": "success"|"danger"|"error", "msg": …}`` (отказ — тоже 200,
его видно по ``type``); пустой список mailcow отдаёт как ``{}``, а числа — то числами, то строками.

* Учётные записи — ящики (``get/mailbox/all``): квота (при записи — в МБ, при чтении — в байтах), занято,
  писем, последний вход по IMAP/POP3/SMTP/SSO, ``active``: 1 — работает, 0 — выключен, 2 — вход запрещён,
  но письма принимаются.
* Псевдонимы — отдельные объекты (адрес → ``goto``); псевдоним ящика — объект, ведущий только в него.
  Передача адреса удалённой учётной записи коллеге — новые псевдонимы с ``goto`` на коллегу.
* Автоответ и пересылка — фильтром Sieve mailcow (``add/edit/delete/filter``, prefilter: выполняется до
  личных фильтров пользователя). Включённым mailcow держит один prefilter на ящик — чужой включённый
  MailArchiver не выключает.
* Копирование ящиков — по IMAP мастер-пользователем Dovecot из mailcow.conf (``ящик*мастер@mailcow.local``).
"""
from __future__ import annotations

import math
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...errors import MailArchiverError, ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, NotSupported, ServerAuthError, ServerError,
                    Status, check_email, imap_fields, norm_email, split_email, tls_fields)
from ..http import HttpClient
from .dovecot import (allow_plain_field, as_int, autoreply_state, autoreply_text, autoreply_view, forward_state,
                      forwarding_text, forwarding_view, parse_base_url, sieve_script, sieve_state)

#: Описание фильтра Sieve, который ведёт MailArchiver (по нему фильтр и находится).
FILTER_DESC = "MailArchiver: автоответ и пересылка"
#: Домен, который mailcow дописывает к имени мастер-пользователя Dovecot.
MASTER_DOMAIN = "mailcow.local"
ACTIVE_TEXT = {0: "выключен", 1: "работает", 2: "вход запрещён, письма принимаются"}
PROTOCOLS = ("imap", "pop3", "smtp", "sieve", "sogo", "eas", "dav")
#: Без этих контейнеров почта не ходит.
CRITICAL_CONTAINERS = ("dovecot", "postfix", "mysql", "nginx", "php-fpm", "redis", "unbound", "rspamd")
#: Адрес в пути запроса: mailcow переносит путь в строку запроса PHP — «+», «&», «#», «?» и пробел не пройдут.
_PATH_SAFE = re.compile(r"^[A-Za-z0-9._@-]+$")
MIB = 1024 * 1024

#: Отказы mailcow (msg[0]) → текст; {0}, {1} — аргументы из msg.
_EXISTS = {
    "object_exists": "учётная запись {0} уже есть",
    "is_alias": "адрес {0} уже есть — это псевдоним",
    "is_alias_or_mailbox": "адрес {0} уже занят (ящик или псевдоним)",
    "is_spam_alias": "адрес {0} уже занят временным адресом (spam alias)",
}
_REFUSALS = {
    "password_complexity": "пароль не подходит политике паролей mailcow",
    "password_mismatch": "пароль и его повтор не совпали",
    "mailbox_quota_exceeded": "квота больше, чем можно ящику в этом домене (не больше {0} МБ)",
    "mailbox_quota_left_exceeded": "в квоте домена осталось только {0} МБ",
    "unlimited_quota_acl": "квоту «без предела» задавать нельзя",
    "domain_not_found": "домена {0} на сервере нет",
    "domain_invalid": "домен указан неверно",
    "mailbox_invalid": "имя ящика указано неверно",
    "max_mailbox_exceeded": "в домене уже предельное число ящиков ({0} из {1})",
    "max_alias_exceeded": "в домене уже предельное число псевдонимов",
    "alias_invalid": "псевдоним {0} указан неверно",
    "alias_empty": "не указан псевдоним",
    "goto_invalid": "адрес назначения {0} указан неверно",
    "goto_empty": "не указан адрес назначения",
    "external_goto_denied": "пересылка псевдонима на внешний адрес {0} запрещена",
    "sieve_error": "фильтр Sieve не принят: {0}",
    "script_empty": "пустой фильтр Sieve",
    "value_missing": "не заполнено обязательное поле",
    "filter_type": "неверный вид фильтра",
    "no_user_defined": "не указана учётная запись",
    "comment_too_long": "слишком длинный комментарий",
    "quarantine_category_invalid": "неверная категория карантина",
}


def _fmt(text: str, args: Sequence[str]) -> str:
    try:
        return text.format(*(list(args) + ["?"] * 3))
    except (IndexError, KeyError, ValueError):
        return text


def _gotos(value: Any) -> List[str]:
    return [norm_email(g) for g in str(value or "").split(",") if g.strip()]


def _iso(epoch: Any) -> str:
    ts = as_int(epoch) or 0
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _split_name(full: Any) -> Tuple[str, str]:
    """«Полное имя» mailcow → (имя, фамилия): MailArchiver пишет «Имя Фамилия», делим по первому пробелу."""
    words = str(full or "").split()
    if not words:
        return "", ""
    return words[0], " ".join(words[1:])


class MailcowDriver(Driver):
    type = "mailcow"
    label = "mailcow"
    summary = ("mailcow через REST API: ящики, вход (с приёмом писем и без), пароли, квота, псевдонимы, полное "
               "имя, создание и удаление, автоответ и пересылка фильтром Sieve; копирование ящиков по IMAP "
               "мастер-пользователем Dovecot.")
    cloud = False
    fields = (
        Field("host", "Адрес mailcow", required=True, placeholder="mail.company.ru",
              help="Адрес веб-интерфейса mailcow — как в браузере; можно с портом: mail.company.ru:8443."),
        Field("api_key", "Ключ API", "password", required=True, secret=True,
              help="mailcow → Система → Настройки → Доступ → «Изменить данные администратора» → API. Для изменений "
                   "нужен ключ «чтение и запись»; ключа «только чтение» хватит для сверки."),
        Field("master_user", "Мастер-пользователь Dovecot (DOVECOT_MASTER_USER)", placeholder="archmaster",
              help="Из mailcow.conf; «@mailcow.local» MailArchiver допишет сам. Пусто — ящики без паролей "
                   "сотрудников не копируются."),
        Field("master_password", "Пароль мастер-пользователя (DOVECOT_MASTER_PASS)", "password", secret=True),
        allow_plain_field("mailcow обычно открыт по https. Включайте, только если сеть до сервера доверенная: ключ "
                          "API уйдёт открытым текстом."),
        *imap_fields("как адрес mailcow"),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.MUST_CHANGE, Cap.QUOTA,
                      Cap.ALIASES, Cap.CONTACT, Cap.CREATE, Cap.DELETE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN,
                      Cap.STATUS})
    contact_fields = ("first_name", "last_name")
    password_rule = "от 8 до 64 знаков без пробелов (и требования политики паролей mailcow)"
    imap_default = None
    limits = (
        "Закрыть вход с приёмом писем — состояние ящика «вход запрещён, письма принимаются» (active = 2); без "
        "приёма — ящик выключается (active = 0). В выключенный ящик mailcow не пускает и мастер-пользователя: "
        "последняя копия и сверка с архивом перед удалением не пройдут, пока вход не закроют с приёмом писем.",
        "Пароль меняется только у ящиков, которые входят паролем mailcow (не LDAP, Keycloak или OIDC).",
        "ФИО в mailcow — одно поле «Полное имя»: MailArchiver пишет туда «Имя Фамилия», а при сверке делит его по "
        "первому пробелу. Отдел, должность и телефон mailcow не хранит.",
        "Псевдоним ящика — объект mailcow, который ведёт только в этот ящик. Псевдонимы на несколько ящиков, "
        "общие (catch-all) и адреса доменов-псевдонимов в его псевдонимах не показываются.",
        "Автоответ и пересылка — фильтром «MailArchiver: автоответ и пересылка» (prefilter, выполняется до личных "
        "фильтров пользователя из SOGo). Включённым mailcow держит только один prefilter на ящик: если у ящика уже "
        "включён другой, MailArchiver его не выключает и действие не выполняет.",
        "Удалённый ящик ещё несколько дней (MAILDIR_GC_TIME, обычно 5 суток) лежит на сервере в "
        "/var/vmail/_garbage; восстановить его через API нельзя. Псевдонимы, которые вели только в него, "
        "удаляются вместе с ним; если убрать их не удалось, удаление всё равно засчитывается, а что осталось — "
        "в итоге действия.",
        "Пароль мастер-пользователя Dovecot API mailcow не отдаёт: нужен постоянный мастер-пользователь из "
        "mailcow.conf (по умолчанию mailcow создаёт случайного при каждом перезапуске контейнера dovecot).",
        "Запросы с неверным ключом API netfilter-mailcow считает подбором и блокирует адрес MailArchiver.",
    )
    setup = (
        "mailcow → Система → Настройки → Доступ → «Изменить данные администратора» → API: включите ключ «чтение "
        "и запись» (для одной сверки хватит «только чтение»), в «Разрешённые IP» укажите адрес MailArchiver. "
        "Скопируйте ключ в подключение.",
        "Для копирования ящиков без паролей задайте постоянного мастер-пользователя Dovecot: в mailcow.conf — "
        "DOVECOT_MASTER_USER=archmaster и DOVECOT_MASTER_PASS=<пароль без спецзнаков>, затем docker compose up -d. "
        "В подключении — имя archmaster (без @mailcow.local) и этот пароль.",
        "IMAP-сервер для копирования — адрес mailcow, порт 993 (SSL) или 143 (STARTTLS); если IMAP открыт на "
        "другом адресе, укажите его в «Дополнительно».",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._http: Optional[HttpClient] = None
        self._alias_cache: Optional[List[Dict[str, Any]]] = None
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки и связь -----------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        parse_base_url(settings.get("host"), "Адрес mailcow", ("/api/v1", "/api"))
        key = secrets.get("api_key") or ""
        if not key:
            raise ValidationError("Укажите ключ API mailcow.")
        if not re.fullmatch(r"[A-Za-z0-9-]+", key):
            raise ValidationError("Ключ API mailcow — только латинские буквы, цифры и дефис (так его выдаёт mailcow).")
        master = str(settings.get("master_user") or "").strip()
        if master and not secrets.get("master_password"):
            raise ValidationError("Укажите пароль мастер-пользователя (DOVECOT_MASTER_PASS) — или оставьте пустым и "
                                  "имя.")
        if secrets.get("master_password") and not master:
            raise ValidationError("Укажите имя мастер-пользователя (DOVECOT_MASTER_USER).")
        if master and (any(ch.isspace() for ch in master) or "*" in master):
            raise ValidationError("Имя мастер-пользователя — без пробелов и звёздочки.")
        return []

    def _base(self) -> str:
        return parse_base_url(self.setting("host"), "Адрес mailcow", ("/api/v1", "/api")) + "/api/v1"

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            key = self.secret("api_key")
            if not key:
                raise ServerAuthError(f"{self.label}: в подключении не задан ключ API.")
            self._http = HttpClient(self._base(), api=self.api, verify_tls=bool(self.setting("verify_tls", True)),
                                    cert_sha256=str(self.setting("cert_sha256") or ""),
                                    allow_plain=bool(self.setting("allow_plain")), server_label=self.label,
                                    headers={"X-API-Key": key})
        return self._http

    @property
    def seen_fingerprint(self) -> str:
        return self._http.seen_fingerprint if self._http is not None else ""

    def _note(self, text: str) -> None:
        if self.api is not None:
            self.api.note(text)

    def imap_server(self) -> Tuple[str, int, str]:
        host = str(self.setting("imap_host") or "").strip()
        if not host:
            try:
                host = urllib.parse.urlsplit(self._base()).hostname or ""
            except ValidationError:
                host = ""
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")

    def _call(self, method: str, path: str, **kw):
        try:
            return self.http.request(method, path, **kw)
        except ServerAuthError as exc:
            text = exc.message.lower()
            if "read/write access denied" in text:
                raise ServerAuthError(f"{self.label}: ключ API — только для чтения, изменений им не сделать.",
                                      hint="Создайте в mailcow ключ «чтение и запись» (Система → Настройки → Доступ → "
                                           "API) и укажите его в подключении.") from exc
            if "for ip" in text:
                raise ServerAuthError(f"{self.label}: mailcow не принимает запросы к API с этого адреса "
                                      f"({exc.message}).",
                                      hint="Добавьте IP-адрес MailArchiver в «Разрешённые IP» ключа API (или в "
                                           "API_ALLOW_FROM в mailcow.conf).") from exc
            if "authentication failed" in text:
                raise ServerAuthError(f"{self.label}: mailcow не принял ключ API (authentication failed).",
                                      hint="Проверьте ключ и что он включён. Не повторяйте проверку много раз подряд: "
                                           "после неудачных попыток netfilter-mailcow блокирует адрес.") from exc
            raise

    @staticmethod
    def _msg(item: Dict[str, Any]) -> Tuple[str, List[str]]:
        msg = item.get("msg")
        if isinstance(msg, list):
            return (str(msg[0]) if msg else ""), [str(a) for a in msg[1:]]
        return str(msg or ""), []

    def _refusal(self, item: Dict[str, Any]) -> ServerError:
        """Отказ mailcow ({"type": "danger"|"error", "msg": …}) → исключение с понятным текстом."""
        key, args = self._msg(item)
        if key in _EXISTS:
            return AlreadyExists(f"{self.label}: {_fmt(_EXISTS[key], args)}.")
        if key == "username_invalid":
            return NotFound(f"{self.label}: учётной записи {args[0] if args else ''} нет.")
        if key == "access_denied":
            return ServerAuthError(f"{self.label}: mailcow отказал в доступе (access_denied).",
                                   hint="Нужен ключ API администратора с правом записи.")
        if key in _REFUSALS:
            return ServerError(f"{self.label}: {_fmt(_REFUSALS[key], args)}.")
        detail = key + (": " + ", ".join(args) if args else "")
        return ServerError(f"{self.label}: сервер отказал ({detail or 'без пояснений'}).")

    def _get(self, path: str) -> Any:
        body = self._call("GET", path).json()
        if body is None:
            raise ServerAuthError(f"{self.label}: mailcow ответил пустотой — так он отвечает на запрос без принятого "
                                  f"ключа API.", hint="Проверьте ключ API в подключении.")
        if isinstance(body, dict) and str(body.get("type") or "").lower() in ("error", "danger") and "msg" in body:
            raise self._refusal(body)
        return body

    def _list(self, path: str) -> List[Dict[str, Any]]:
        """Список mailcow: массив, а пустой — ``{}``."""
        body = self._get(path)
        if isinstance(body, list):
            return [x for x in body if isinstance(x, dict)]
        if isinstance(body, dict):
            if not body:
                return []
            if any(k in body for k in ("username", "address", "domain_name", "script_data")):
                return [body]
            return [v for v in body.values() if isinstance(v, dict)]
        raise ServerError(f"{self.label}: неожиданный ответ на {path}: {str(body)[:200]}")

    def _post(self, path: str, body: Any, *, log_body: bool = True) -> List[Dict[str, Any]]:
        """Изменение: итоги {type, msg}; хоть один отказ или ни одного «success» — исключение."""
        data = self._call("POST", path, json_body=body, log_body=log_body).json()
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        items = [i for i in items if isinstance(i, dict)]
        bad = [i for i in items if str(i.get("type") or "").lower() in ("danger", "error")]
        if bad:
            raise self._refusal(bad[0])
        if any(str(i.get("type") or "").lower() == "success" for i in items):
            return items
        if not items:
            raise ServerError(f"{self.label}: сервер не ответил на изменение ({path}).")
        key, args = self._msg(items[0])
        detail = key + (": " + ", ".join(args) if args else "")
        raise ServerError(f"{self.label}: сервер не подтвердил изменение ({detail}).")

    # -- чтение ---------------------------------------------------------------------------------
    def _mailbox(self, email: str) -> Optional[Dict[str, Any]]:
        email = norm_email(email)
        domain = split_email(email)[1]
        if _PATH_SAFE.match(email):
            body = self._get(f"/get/mailbox/{urllib.parse.quote(email, safe='@')}")
            rows = body if isinstance(body, list) else [body] if isinstance(body, dict) and body else []
        elif _PATH_SAFE.match(domain):
            rows = self._list(f"/get/mailbox/all/{domain}")
        else:
            rows = self._list("/get/mailbox/all")
        for m in rows:
            if isinstance(m, dict) and norm_email(m.get("username")) == email:
                return m
        return None

    def _require(self, email: str) -> Dict[str, Any]:
        m = self._mailbox(email)
        if m is None:
            raise NotFound(f"{self.label}: учётной записи {norm_email(email)} нет.")
        return m

    def _alias_objects(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._alias_cache is None or refresh:
            self._alias_cache = self._list("/get/alias/all")
        return self._alias_cache

    @staticmethod
    def _alias_index(objs: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[str]]]:
        """Ящик → {own: свои псевдонимы, off: из них выключенные, shared: псевдонимы на несколько ящиков}."""
        out: Dict[str, Dict[str, List[str]]] = {}
        for a in objs:
            address = norm_email(a.get("address"))
            if not address or address.startswith("@") or "@" not in address:
                continue
            gotos = _gotos(a.get("goto"))
            if len(gotos) == 1 and gotos[0] != address:
                slot = out.setdefault(gotos[0], {"own": [], "off": [], "shared": []})
                slot["own"].append(address)
                if as_int(a.get("active")) == 0:
                    slot["off"].append(address)
            elif len(gotos) > 1:
                for g in gotos:
                    out.setdefault(g, {"own": [], "off": [], "shared": []})["shared"].append(address)
        return out

    def _own_aliases(self, email: str, refresh: bool = True) -> List[str]:
        return list(self._alias_index(self._alias_objects(refresh)).get(norm_email(email), {}).get("own") or [])

    def _filters_of(self, email: str) -> List[Dict[str, Any]]:
        email = norm_email(email)
        try:
            if _PATH_SAFE.match(email):
                rows = self._list(f"/get/filters/{urllib.parse.quote(email, safe='@')}")
            else:
                rows = self._list("/get/filters/all")
        except NotFound as exc:
            raise NotSupported(f"{self.label}: в этой версии mailcow нет API фильтров Sieve ({exc.message}).") from exc
        return [f for f in rows if norm_email(f.get("username")) == email]

    @staticmethod
    def _ours(f: Dict[str, Any]) -> bool:
        return str(f.get("script_desc") or "").startswith("MailArchiver")

    def _state(self, filters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Состояние включённого фильтра MailArchiver (None — его нет или он выключен)."""
        mine = [f for f in filters if self._ours(f) and as_int(f.get("active")) == 1]
        return sieve_state(str(mine[0].get("script_data") or "")) if mine else None

    def _account(self, m: Dict[str, Any], index: Dict[str, Dict[str, List[str]]],
                 filters: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        email = norm_email(m.get("username"))
        attrs = m.get("attributes") if isinstance(m.get("attributes"), dict) else {}
        active = as_int(m.get("active"))
        if active is None:
            active = as_int(m.get("active_int"))
        name = " ".join(str(m.get("name") or "").split())
        first, last = _split_name(name)
        logins = [as_int(m.get(k)) or 0 for k in ("last_imap_login", "last_pop3_login", "last_smtp_login",
                                                   "last_sso_login")]
        slot = index.get(email) or {}
        protocols = [p for p in PROTOCOLS if str(attrs.get(f"{p}_access", "1")) != "0"]
        info: Dict[str, Any] = {
            "domain": m.get("domain") or split_email(email)[1], "authsource": m.get("authsource") or "mailcow",
            "state": ACTIVE_TEXT.get(active, str(m.get("active"))) if active is not None else "",
            "protocols": ", ".join(protocols), "created": m.get("created") or "", "modified": m.get("modified") or "",
        }
        if len(protocols) < len(PROTOCOLS):
            info["protocols_off"] = ", ".join(p for p in PROTOCOLS if p not in protocols)
        if slot.get("off"):
            info["aliases_off"] = list(slot["off"])
        if slot.get("shared"):
            info["shared_aliases"] = list(slot["shared"])[:20]
        if m.get("tags"):
            info["tags"] = list(m.get("tags") or [])
        acc: Dict[str, Any] = {
            "email": email, "native_id": str(m.get("username") or email), "display_name": name, "first_name": first,
            "last_name": last, "aliases": list(slot.get("own") or []),
            "login_blocked": None if active is None else active != 1,
            "quota_bytes": as_int(m.get("quota")), "used_bytes": as_int(m.get("quota_used")),
            "messages": as_int(m.get("messages")), "last_login": _iso(max(logins)),
            "must_change": str(attrs.get("force_pw_update") or "0") == "1", "kind": "user", "info": info,
        }
        if filters is not None:
            state = self._state(filters)
            acc["autoreply"] = bool(state and state.get("autoreply"))
            acc["forward_to"] = list(((state or {}).get("forward") or {}).get("addresses") or [])
            others = [f for f in filters if not self._ours(f) and as_int(f.get("active")) == 1]
            if others:
                info["filters"] = ", ".join(str(f.get("script_desc") or f.get("id")) for f in others)[:300]
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        mailboxes = self._list("/get/mailbox/all")
        index = self._alias_index(self._alias_objects(refresh=True))
        by_user: Optional[Dict[str, List[Dict[str, Any]]]] = {}
        try:
            for f in self._list("/get/filters/all"):
                by_user.setdefault(norm_email(f.get("username")), []).append(f)
        except ServerAuthError:
            raise
        except ServerError as exc:
            by_user = None
            self._note(f"фильтры Sieve не прочитаны — автоответ и пересылка в сверке неизвестны: {exc.message}")
        out = []
        for m in mailboxes:
            if not norm_email(m.get("username")):
                continue
            out.append(self._account(m, index, None if by_user is None else by_user.get(norm_email(m["username"]), [])))
        if not out:
            self.incomplete = True
            self.sync_problems.append("mailcow не показал ни одного ящика — прежние учётные записи удалёнными не "
                                      "считаются; проверьте ключ API и что ящики есть")
        return out

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        m = self._mailbox(email)
        if m is None:
            return None
        index = self._alias_index(self._alias_objects(refresh=True))
        try:
            filters: Optional[List[Dict[str, Any]]] = self._filters_of(email)
        except ServerAuthError:
            raise
        except ServerError as exc:
            filters = None
            self._note(f"фильтры Sieve не прочитаны: {exc.message}")
        return self._account(m, index, filters)

    # -- изменения ------------------------------------------------------------------------------
    def _edit(self, email: str, attr: Dict[str, Any], *, log_body: bool = True) -> None:
        self._post("/edit/mailbox", {"items": [norm_email(email)], "attr": attr}, log_body=log_body)

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        if enabled:
            self._edit(email, {"active": "1"})
            return "вход открыт (ящик работает)"
        if keep_receiving:
            self._edit(email, {"active": "2"})
            return "вход закрыт, письма продолжают приниматься"
        self._edit(email, {"active": "0"})
        return ("вход закрыт, ящик выключен — письма не принимаются; в выключенный ящик mailcow не пускает и "
                "мастер-пользователя (копия и удаление через MailArchiver не пройдут)")

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        m = self._require(email)
        source = str(m.get("authsource") or "mailcow")
        if source != "mailcow":
            raise NotSupported(f"{self.label}: пароль {norm_email(email)} хранится не в mailcow (вход через {source}) "
                               f"— меняйте его там.")
        if self.api is not None:
            self.api.secret(password)
        self._edit(email, {"password": password, "password2": password, "force_pw_update": "1" if must_change else "0"},
                   log_body=False)
        return "пароль сменён" + ("; при следующем входе — смена пароля" if must_change else "")

    def set_must_change(self, email: str, value: bool) -> str:
        self._edit(email, {"force_pw_update": "1" if value else "0"})
        return "при следующем входе потребуется сменить пароль" if value else "требование снято"

    @staticmethod
    def _mib(quota_bytes: Optional[int]) -> int:
        return max(1, math.ceil(int(quota_bytes) / MIB)) if quota_bytes else 0

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        mib = self._mib(quota_bytes)
        self._edit(email, {"quota": str(mib)})
        return "" if mib else "квота снята (без предела)"

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        m = self._require(email)
        first, last = _split_name(m.get("name"))
        first = str(values.get("first_name", first) or "").strip()
        last = str(values.get("last_name", last) or "").strip()
        name = " ".join(x for x in (first, last) if x)
        if not name:
            return "полное имя не изменено (mailcow не принимает пустое)"
        self._edit(email, {"name": name})
        return f"полное имя: {name}"

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.check_password(password)
        email = check_email(email)
        local, domain = split_email(email)
        name = " ".join(x for x in ((first_name or "").strip(), (last_name or "").strip()) if x) or local
        body = {"active": "1", "domain": domain, "local_part": local, "name": name, "authsource": "mailcow",
                "password": password, "password2": password, "force_pw_update": "1" if must_change else "0"}
        if quota_bytes:
            body["quota"] = str(self._mib(quota_bytes))
        if self.api is not None:
            self.api.secret(password)
        self._post("/add/mailbox", body, log_body=False)
        return self.get_account(email) or {"email": email, "first_name": first_name, "last_name": last_name,
                                           "display_name": name}

    def delete_account(self, email: str) -> str:
        email = norm_email(email)
        self._post("/delete/mailbox", [email])
        # ящик удалён — необратимо; дальше ничто не превращает удаление в «ошибку»: что не вышло с псевдонимами,
        # уходит в итог действия (иначе удалённая учётная запись числилась бы на сервере, а архив — без удержания)
        text = ("учётная запись удалена; папка ящика ещё несколько дней (MAILDIR_GC_TIME) лежит на сервере в "
                "/var/vmail/_garbage — через API её не восстановить")
        try:
            text += self._drop_aliases_of(email)
        except Exception as exc:  # noqa: BLE001 — удаление уже состоялось
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            self._note(f"псевдонимы удалённого ящика {email} не проверены: {message}")
            text += f"; псевдонимы, которые вели в удалённый ящик, не проверены ({message}) — проверьте в mailcow"
        finally:
            self._alias_cache = None
        return text

    def _drop_aliases_of(self, email: str) -> str:
        """Псевдонимы, что ещё ведут в удалённый ящик: свои — удалить, общие — без него. → дополнение к итогу."""
        # mailcow обычно сам убирает адрес из псевдонимов, но это не документировано: что осталось — убираем
        removed: List[str] = []
        trimmed: List[str] = []
        left: List[str] = []
        failed: List[str] = []
        drop: List[Tuple[str, str]] = []
        try:
            objs = self._alias_objects(refresh=True)
        except MailArchiverError as exc:
            self._note(f"псевдонимы удалённого ящика {email} не прочитаны: {exc.message}")
            return f"; псевдонимы, которые вели в удалённый ящик, не проверены ({exc.message}) — проверьте в mailcow"
        for a in objs:
            gotos = _gotos(a.get("goto"))
            if email not in gotos:
                continue
            address = norm_email(a.get("address"))
            rest = [g for g in gotos if g != email]
            if rest:
                try:
                    self._post("/edit/alias", {"items": [str(a.get("id"))], "attr": {"goto": ",".join(rest)}})
                    trimmed.append(address)
                except MailArchiverError as exc:
                    failed.append(f"{address} ({exc.message})")
            elif address.startswith("@"):
                left.append(address)
            else:
                drop.append((str(a.get("id")), address))
        if drop:
            try:
                self._post("/delete/alias", [i for i, _a in drop])
                removed = [a for _i, a in drop]
            except MailArchiverError as exc:
                failed.append(f"{', '.join(a for _i, a in drop)} ({exc.message})")
        text = ""
        if removed:
            text += f"; удалены её псевдонимы: {', '.join(removed)}"
        if trimmed:
            text += f"; адрес убран из псевдонимов на несколько ящиков: {', '.join(trimmed)}"
        if left:
            text += f"; общий адрес {', '.join(left)} ведёт в удалённый ящик — исправьте его в mailcow"
        if failed:
            text += f"; псевдонимы, которые ведут в удалённый ящик, не исправлены: {'; '.join(failed)} — исправьте их " \
                    f"в mailcow"
        return text

    def add_alias(self, email: str, alias: str) -> List[str]:
        email = norm_email(email)
        alias = check_email(alias, "Псевдоним")
        for a in self._alias_objects(refresh=True):
            if norm_email(a.get("address")) != alias:
                continue
            gotos = _gotos(a.get("goto"))
            if gotos == [email]:
                if as_int(a.get("active")) == 0:
                    self._post("/edit/alias", {"items": [str(a.get("id"))], "attr": {"active": "1"}})
                return self._own_aliases(email)
            if email in gotos:
                raise AlreadyExists(f"{self.label}: адрес {alias} уже ведёт в несколько ящиков ({', '.join(gotos)}) — "
                                    f"это не псевдоним одного ящика.")
            raise AlreadyExists(f"{self.label}: адрес {alias} уже занят — это псевдоним, письма с него идут на "
                                f"{', '.join(gotos) or '—'}.")
        self._post("/add/alias", {"address": alias, "goto": email, "active": "1"})
        return self._own_aliases(email)

    def remove_alias(self, email: str, alias: str) -> List[str]:
        email = norm_email(email)
        alias = norm_email(alias)
        for a in self._alias_objects(refresh=True):
            if norm_email(a.get("address")) != alias:
                continue
            gotos = _gotos(a.get("goto"))
            if email not in gotos:
                raise ServerError(f"{self.label}: псевдоним {alias} ведёт не в {email}, а на "
                                  f"{', '.join(gotos) or '—'}.")
            rest = [g for g in gotos if g != email]
            if rest:
                self._post("/edit/alias", {"items": [str(a.get("id"))], "attr": {"goto": ",".join(rest)}})
            else:
                self._post("/delete/alias", [str(a.get("id"))])
            return self._own_aliases(email)
        raise NotFound(f"{self.label}: псевдонима {alias} нет.")

    # -- автоответ и пересылка (фильтр Sieve) ------------------------------------------------------
    def _update_filter(self, email: str, change: Callable[[Optional[Dict[str, Any]]], Tuple[Any, Any]]
                       ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        email = norm_email(email)
        self._require(email)
        filters = self._filters_of(email)
        ours = [f for f in filters if self._ours(f)]
        autoreply, forward = change(self._state(filters))
        if not autoreply and not forward:
            if ours:
                self._post("/delete/filter", [str(f.get("id")) for f in ours])
            return None, None
        foreign = [f for f in filters if not self._ours(f) and as_int(f.get("active")) == 1
                   and str(f.get("filter_type") or "prefilter") == "prefilter"]
        if foreign:
            names = ", ".join(f"«{f.get('script_desc') or f.get('id')}»" for f in foreign)
            raise ServerError(f"{self.label}: у {email} уже включён фильтр {names} (prefilter) — включённым mailcow "
                              f"держит только один, а чужой MailArchiver не выключает.",
                              hint="Добавьте автоответ или пересылку в этот фильтр вручную (mailcow → ящик → Фильтры) "
                                   "или выключите его и повторите.")
        attr = {"script_desc": FILTER_DESC, "script_data": sieve_script(autoreply, forward), "filter_type": "prefilter",
                "active": "1"}
        if ours:
            self._post("/edit/filter", {"items": [str(ours[0].get("id"))], "attr": attr})
            if len(ours) > 1:
                self._post("/delete/filter", [str(f.get("id")) for f in ours[1:]])
        else:
            self._post("/add/filter", dict(attr, username=email))
        return autoreply, forward

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        return autoreply_view(self._state(self._filters_of(email)))

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        email = norm_email(email)
        auto = None
        if enabled:
            auto = autoreply_state(subject=subject, text=text, start=start, end=end,
                                   addresses=[email] + self._own_aliases(email))
        self._update_filter(email, lambda state: (auto, (state or {}).get("forward")))
        return autoreply_text(enabled, auto) + (f" (фильтр «{FILTER_DESC}»)" if enabled else "")

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        return forwarding_view(self._state(self._filters_of(email)))

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        fwd = forward_state(addresses, keep_copy)
        self._update_filter(email, lambda state: ((state or {}).get("autoreply"), fwd))
        return forwarding_text(fwd) + (f" (фильтр «{FILTER_DESC}»)" if fwd else "")

    # -- IMAP и состояние ------------------------------------------------------------------------
    def _master_login(self) -> str:
        master = str(self.setting("master_user") or "").strip()
        if master and "@" not in master:
            master = f"{master}@{MASTER_DOMAIN}"
        return master

    def imap_admin_problem(self) -> str:
        if not self._master_login() or not self.secret("master_password"):
            return "не задан мастер-пользователь Dovecot (DOVECOT_MASTER_USER и DOVECOT_MASTER_PASS)"
        return ""

    def imap_auth(self, email: str) -> ImapAuth:
        master = self._master_login()
        password = self.secret("master_password")
        if not master or not password:
            raise ServerAuthError(f"{self.label}: мастер-пользователь Dovecot в подключении не задан — войти в ящик "
                                  f"без пароля сотрудника нечем.",
                                  hint="Задайте DOVECOT_MASTER_USER и DOVECOT_MASTER_PASS в mailcow.conf и укажите их "
                                       "в подключении.")
        return ImapAuth(mech="separator", user=master, secret=password, authzid=norm_email(email), separator="*",
                        host=self.imap_server()[0])

    def test(self) -> Status:
        st = Status()
        ver = self._get("/get/status/version")
        version = str(ver.get("version") or "") if isinstance(ver, dict) else ""
        st.version = f"mailcow {version}".strip()
        domains = self._list("/get/domain/all")
        names = [str(d.get("domain_name")) for d in domains if d.get("domain_name")]
        if names:
            st.details.append("Домены: " + ", ".join(names[:10]) + (f" и ещё {len(names) - 10}" if len(names) > 10
                                                                    else ""))
            st.details.append(f"Ящиков: {sum(as_int(d.get('mboxes_in_domain')) or 0 for d in domains)}")
        else:
            st.problems.append(("domains", "warning", "в mailcow нет ни одного домена"))
        try:
            raw = self._get("/get/status/containers")
            items = [c for c in (raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else [])
                     if isinstance(c, dict)]
            running = 0
            for c in items:
                name = str(c.get("container") or "?")
                state = str(c.get("state") or "").lower()
                if state == "running":
                    running += 1
                    continue
                short = name[:-len("-mailcow")] if name.endswith("-mailcow") else name
                level = "error" if short in CRITICAL_CONTAINERS else "warning"
                st.problems.append((f"container_{short}", level,
                                    f"контейнер {name}: {state or 'состояние неизвестно'}"))
            if items:
                st.details.append(f"Контейнеры: работают {running} из {len(items)}")
        except ServerAuthError:
            raise
        except ServerError as exc:
            st.problems.append(("containers", "warning", f"состояние контейнеров не прочитано: {exc.message}"))
        try:
            vmail = self._get("/get/status/vmail")
            if isinstance(vmail, dict) and vmail.get("used_percent"):
                pct = as_int(str(vmail.get("used_percent")).strip().rstrip("%"))
                st.details.append(f"Диск с почтой: занято {vmail.get('used')} из {vmail.get('total')} "
                                  f"({vmail.get('used_percent')})")
                if pct is not None and pct >= 90:
                    st.problems.append(("vmail_disk", "error" if pct >= 97 else "warning",
                                        f"диск с почтой заполнен на {pct} %"))
        except ServerAuthError:
            raise
        except ServerError as exc:
            self._note(f"место на диске не прочитано: {exc.message}")
        try:
            queue = self._get("/get/mailq/all")
            st.queue = len(queue) if isinstance(queue, list) else 0
            st.details.append(f"Очередь Postfix: {st.queue}")
        except ServerAuthError:
            raise
        except ServerError as exc:
            self._note(f"очередь не прочитана: {exc.message}")
        if getattr(self.conn, "allow_changes", False):
            # пустое изменение (без ящиков) ничего не меняет, а ключу «только чтение» mailcow отвечает 403
            try:
                self._call("POST", "/edit/mailbox", json_body={"items": [], "attr": {}})
            except ServerAuthError as exc:
                st.problems.append(("api_read_only", "warning", exc.message))
            except ServerError as exc:
                self._note(f"право записи не проверено: {exc.message}")
        master = self._master_login()
        if not master or not self.secret("master_password"):
            st.problems.append(("imap_master", "warning", "мастер-пользователь Dovecot не задан — ящики без паролей "
                                                          "сотрудников не скопировать"))
        else:
            host, port, _sec = self.imap_server()
            st.details.append(f"Копирование ящиков: мастер-пользователь {master}, IMAP {host}:{port}")
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = MailcowDriver
