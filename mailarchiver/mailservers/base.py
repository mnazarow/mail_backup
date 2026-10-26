"""
Подключения к почтовым серверам: общее описание драйвера.

Драйвер — класс, который знает API одного вида сервера (Microsoft 365,
Google Workspace, Яндекс 360, Zimbra, …) и приводит его к общему виду:
учётные записи — словарями одного формата (:data:`ACCOUNT_KEYS`), действия —
методами с одинаковыми именами. Что сервер умеет, драйвер объявляет набором
возможностей (:class:`Cap`): раздел «Ящики на сервере» показывает действие и
выполняет его только для учётных записей тех подключений, у которых нужная
возможность есть, а остальные пропускает с причиной.

Драйвер не хранит состояния между вызовами, кроме кэша токена доступа: каждое
действие получает свежий объект драйвера через
:func:`mailarchiver.mailservers.service.driver`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from ..errors import MailArchiverError, ValidationError


class Cap:
    """Возможности драйвера."""

    READ = "read"                  # список учётных записей (сверка) — есть у всех
    USAGE = "usage"                # занятое место (и квота) в сверке
    LAST_LOGIN = "last_login"      # последний вход в сверке
    BLOCK = "block"                # закрыть и открыть вход
    PASSWORD = "password"          # задать пароль
    MUST_CHANGE = "must_change"    # потребовать смену пароля при следующем входе
    QUOTA = "quota"                # задать квоту ящика
    ALIASES = "aliases"            # добавить и удалить псевдонимы
    CONTACT = "contact"            # ФИО, отдел, должность, телефон
    CREATE = "create"              # создать учётную запись
    DELETE = "delete"              # удалить учётную запись
    AUTOREPLY = "autoreply"        # автоответ
    FORWARD = "forward"            # пересылка
    IMAP_ADMIN = "imap_admin"      # вход в ящики по IMAP без паролей сотрудников
    STATUS = "status"              # состояние сервера (версия, лицензии, очередь) для мониторинга

    LABELS = {
        READ: "сверка учётных записей", USAGE: "занятое место и квота", LAST_LOGIN: "последний вход",
        BLOCK: "закрыть и открыть вход", PASSWORD: "пароли", MUST_CHANGE: "смена пароля при входе",
        QUOTA: "квота ящика", ALIASES: "псевдонимы", CONTACT: "ФИО, отдел, должность", CREATE: "создание",
        DELETE: "удаление", AUTOREPLY: "автоответ", FORWARD: "пересылка",
        IMAP_ADMIN: "копирование без паролей сотрудников", STATUS: "состояние сервера",
    }


#: Поля учётной записи, которые драйвер отдаёт в ``list_accounts`` / ``get_account``
#: (неизвестное — None или отсутствует).
ACCOUNT_KEYS = (
    "email",          # основной адрес (будет приведён к нижнему регистру)
    "native_id",      # идентификатор на сервере (GUID, id), если адреса мало для запросов
    "first_name", "last_name", "display_name",
    "aliases",        # список адресов
    "login_blocked",  # True — вход закрыт, False — открыт, None — неизвестно
    "quota_bytes",    # предел объёма ящика (None — нет или неизвестен)
    "used_bytes",     # занято (None — неизвестно)
    "messages",       # писем (None — неизвестно)
    "last_login",     # ISO-время последнего входа
    "department", "title", "phone", "mobile", "company", "office",
    "must_change",    # требуется смена пароля при входе
    "autoreply",      # автоответ включён
    "forward_to",     # список адресов пересылки
    "kind",           # user | shared | resource | admin … (как у сервера)
    "info",           # прочие поля сервера как есть (для карточки)
)

#: Поля «ФИО, отдел, должность», которые может задать раздел (драйвер объявляет свои).
CONTACT_FIELDS = (("first_name", "Имя"), ("last_name", "Фамилия"), ("display_name", "Отображаемое имя"),
                  ("department", "Отдел"), ("title", "Должность"), ("phone", "Телефон"),
                  ("mobile", "Мобильный"), ("company", "Организация"), ("office", "Офис"))


class ServerError(MailArchiverError):
    """Ошибка почтового сервера (ответ API) — с понятным текстом и подсказкой."""

    code = "mail_server"


class ServerAuthError(ServerError):
    """Сервер не принял учётные данные подключения (или прав не хватает)."""

    code = "mail_server_auth"


class ServerUnreachable(ServerError):
    """Нет связи с сервером (сеть, DNS, TLS, тайм-аут)."""

    code = "mail_server_unreachable"


class NotFound(ServerError):
    """Учётной записи (или другого объекта) на сервере нет."""

    code = "mail_server_not_found"


class AlreadyExists(ServerError):
    """Такая учётная запись (или адрес) на сервере уже есть."""

    code = "mail_server_exists"


class NotSupported(ServerError):
    """Сервер этого вида такого не умеет (или его API этого не позволяет)."""

    code = "mail_server_not_supported"


@dataclass
class Field:
    """Поле настройки подключения (форма «Подключить сервер»)."""

    key: str
    label: str
    kind: str = "text"            # text | password | textarea | secret_text | select | bool | int | url
    required: bool = False
    default: Any = None
    help: str = ""
    placeholder: str = ""
    options: Sequence[Tuple[str, str]] = ()
    #: секрет: хранится зашифрованным, в ответах API не отдаётся (только «задан»)
    secret: bool = False
    #: показывать в «Дополнительно»
    advanced: bool = False
    #: показывать, только если другое поле имеет одно из значений
    show_if: Optional[Dict[str, Sequence[Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind, "required": self.required,
                "default": self.default, "help": self.help, "placeholder": self.placeholder,
                "options": [{"value": v, "label": t} for v, t in self.options], "secret": self.secret,
                "advanced": self.advanced, "show_if": dict(self.show_if) if self.show_if else None}

    def coerce(self, value: Any) -> Any:
        if self.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "да")
            return bool(value) if value is not None else bool(self.default)
        if self.kind == "int":
            if value in (None, ""):
                return self.default
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"«{self.label}»: нужно целое число.") from exc
        text = "" if value is None else str(value)
        if self.kind not in ("textarea", "secret_text"):
            text = text.strip()
        if self.kind == "select" and self.options and text and text not in {v for v, _t in self.options}:
            raise ValidationError(f"«{self.label}»: недопустимое значение «{text}».")
        return text


@dataclass
class ImapAuth:
    """Как войти в ящик по IMAP без пароля сотрудника (возвращает драйвер с возможностью IMAP_ADMIN)."""

    #: xoauth2 — токен (user — адрес ящика); plain_authzid — SASL PLAIN: user/secret администратора,
    #: authzid — адрес ящика; separator — LOGIN «ящик<sep>администратор» с паролем администратора
    mech: str
    user: str
    secret: str
    authzid: str = ""
    separator: str = "*"
    #: на какой сервер можно отправить этот секрет (сверяется с сервером ящика)
    host: str = ""


@dataclass
class Status:
    """Состояние сервера для мониторинга и карточки подключения."""

    ok: bool = True
    version: str = ""
    details: List[str] = field(default_factory=list)       # строки «что видно» (организация, домены…)
    #: проблемы: (ключ, уровень warning|error, текст)
    problems: List[Tuple[str, str, str]] = field(default_factory=list)
    #: лицензии: использовано / всего (None — неизвестно)
    seats_used: Optional[int] = None
    seats_total: Optional[int] = None
    queue: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "version": self.version, "details": list(self.details),
                "problems": [list(p) for p in self.problems], "seats_used": self.seats_used,
                "seats_total": self.seats_total, "queue": self.queue, "extra": dict(self.extra)}


EMAIL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+$")


def norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def check_email(value: str, what: str = "Адрес") -> str:
    email = norm_email(value)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValidationError(f"{what} указан неверно: «{value}».")
    return email


def split_email(email: str) -> Tuple[str, str]:
    local, _sep, domain = norm_email(email).partition("@")
    return local, domain


def normalize_account(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Учётная запись от драйвера → общий вид (адреса — в нижнем регистре, псевдонимы без основного)."""
    out: Dict[str, Any] = {k: raw.get(k) for k in ACCOUNT_KEYS if k in raw}
    out["email"] = norm_email(raw.get("email"))
    if "aliases" in raw:                            # не прислал — в снимке остаются прежние
        aliases = []
        for a in raw.get("aliases") or []:
            a = norm_email(a)
            if a and a != out["email"] and a not in aliases:
                aliases.append(a)
        out["aliases"] = aliases
    fwd = raw.get("forward_to")
    if fwd is not None:
        out["forward_to"] = [norm_email(a) for a in (fwd if isinstance(fwd, (list, tuple)) else [fwd]) if a]
    for key in ("quota_bytes", "used_bytes", "messages"):
        value = raw.get(key)
        if value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
            out[key] = value if value is None or value >= 0 else None
    if out.get("quota_bytes") == 0:
        out["quota_bytes"] = None                   # 0 — «без предела» у большинства серверов
    for key in ("login_blocked", "must_change", "autoreply"):
        if raw.get(key) is not None:
            out[key] = bool(raw.get(key))
    info = raw.get("info")
    out["info"] = dict(info) if isinstance(info, dict) else {}
    return out


def random_password(length: int = 20) -> str:
    """Случайный пароль со всеми видами знаков (требования сложности Microsoft 365, Google, Zimbra и др.)."""
    import secrets
    import string
    lower, upper, digits, marks = string.ascii_lowercase, string.ascii_uppercase, string.digits, "-_.!#%+="
    pool = lower + upper + digits + marks
    while True:
        chars = [secrets.choice(lower), secrets.choice(upper), secrets.choice(digits), secrets.choice(marks)]
        chars += [secrets.choice(pool) for _ in range(max(8, length) - 4)]
        secrets.SystemRandom().shuffle(chars)
        value = "".join(chars)
        if value[0] not in marks:                   # знак в начале путает часть консолей
            return value


class Driver:
    """Базовый драйвер. Методы, которых вид сервера не умеет, бросают :class:`NotSupported`."""

    #: вид сервера (ключ в настройках подключения)
    type = ""
    #: название вида для людей
    label = ""
    #: одна строка: как MailArchiver с ним работает
    summary = ""
    #: облачный сервис (адрес API известен заранее) или свой сервер
    cloud = False
    #: поля настройки подключения
    fields: Tuple[Field, ...] = ()
    #: возможности
    caps: FrozenSet[str] = frozenset({Cap.READ})
    #: какие поля «ФИО, отдел, должность» сервер позволяет задать
    contact_fields: Tuple[str, ...] = ()
    #: требования к паролю — для подсказки (проверка — check_password)
    password_rule = "от 8 до 64 знаков"
    #: IMAP-сервер по умолчанию для копирования ящиков (host, port, security) — облачные сервисы
    imap_default: Optional[Tuple[str, int, str]] = None
    #: заметки о том, чего сервер не умеет (показываются в форме подключения и документации)
    limits: Tuple[str, ...] = ()
    #: что настроить на стороне сервера (шаги для формы подключения)
    setup: Tuple[str, ...] = ()

    def __init__(self, conn, api) -> None:
        #: подключение (:class:`mailarchiver.mailservers.store.Connection`)
        self.conn = conn
        #: журнал вызовов API этой сессии (:class:`mailarchiver.mailservers.http.ApiLog`)
        self.api = api

    # -- описание ----------------------------------------------------------------
    @classmethod
    def describe(cls) -> Dict[str, Any]:
        return {"type": cls.type, "label": cls.label, "summary": cls.summary, "cloud": cls.cloud,
                "fields": [f.to_dict() for f in cls.fields], "caps": sorted(cls.caps),
                "caps_labels": {c: Cap.LABELS.get(c, c) for c in sorted(cls.caps)},
                "contact_fields": list(cls.contact_fields), "password_rule": cls.password_rule,
                "imap_default": list(cls.imap_default) if cls.imap_default else None,
                "limits": list(cls.limits), "setup": list(cls.setup)}

    @classmethod
    def can(cls, cap: str) -> bool:
        return cap in cls.caps

    # -- настройки ---------------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        value = self.conn.settings.get(key)
        if value in (None, ""):
            for f in self.fields:
                if f.key == key and f.default is not None:
                    return f.default
            return default
        return value

    def secret(self, key: str) -> str:
        return str(self.conn.secrets.get(key) or "")

    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        """Дополнительная проверка настроек (поверх обязательных полей). → предупреждения."""
        return []

    def check_password(self, password: str) -> None:
        if not password or len(password) < 8 or len(password) > 64:
            raise ValidationError(f"Пароль не подходит серверу «{self.label}»: {self.password_rule}.")
        if any(ch.isspace() for ch in password):
            raise ValidationError("Пароль не должен содержать пробелов.")

    def random_password(self) -> str:
        """Случайный пароль, который примет сервер: 20 знаков — заглавные, строчные, цифры и знак."""
        return random_password()

    # -- обязательное --------------------------------------------------------------
    def test(self) -> Status:
        """Проверка подключения и прав: версия, организация, что не так."""
        raise NotImplementedError

    def list_accounts(self) -> List[Dict[str, Any]]:
        """Все учётные записи (см. :data:`ACCOUNT_KEYS`)."""
        raise NotImplementedError

    # -- по возможностям ------------------------------------------------------------
    def _no(self, what: str):
        raise NotSupported(f"{self.label}: {what} — сервер этого вида так не умеет (или его API этого не "
                           f"позволяет).")

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        """Одна учётная запись (по умолчанию — из полного списка)."""
        email = norm_email(email)
        for row in self.list_accounts():
            if norm_email(row.get("email")) == email:
                return row
        return None

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        self._no("закрыть или открыть вход")

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self._no("задать пароль")

    def set_must_change(self, email: str, value: bool) -> str:
        self._no("потребовать смену пароля")

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        self._no("задать квоту")

    def add_alias(self, email: str, alias: str) -> List[str]:
        self._no("добавить псевдоним")

    def remove_alias(self, email: str, alias: str) -> List[str]:
        self._no("удалить псевдоним")

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        self._no("изменить ФИО и контактные данные")

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._no("создать учётную запись")

    def delete_account(self, email: str) -> str:
        self._no("удалить учётную запись")

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        self._no("прочитать автоответ")

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        self._no("задать автоответ")

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        self._no("прочитать пересылку")

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        self._no("задать пересылку")

    def imap_auth(self, email: str) -> ImapAuth:
        self._no("войти в ящик без пароля сотрудника")

    def imap_admin_problem(self) -> str:
        """Почему вход в ящики без паролей в этом подключении не настроен (пусто — настроен). Без обращения к
        серверу: например, не задан мастер-пароль или пароль администратора почты."""
        return ""

    def status(self) -> Status:
        """Состояние для мониторинга (по умолчанию — то же, что проверка подключения)."""
        return self.test()

    def imap_server(self) -> Tuple[str, int, str]:
        """IMAP-сервер для копирования ящиков этого подключения: (адрес, порт, ssl|starttls|plain)."""
        host = str(self.setting("imap_host") or "").strip()
        if not host and self.imap_default:
            return self.imap_default
        if not host:
            host = str(self.setting("host") or "").strip()
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")


def imap_fields(default_host_hint: str = "") -> Tuple[Field, ...]:
    """Поля «куда входить по IMAP для копирования» — для своих серверов."""
    return (
        Field("imap_host", "IMAP-сервер для копирования ящиков", placeholder=default_host_hint or "как адрес сервера",
              help="Куда входить по IMAP, чтобы копировать ящики. Пусто — тот же адрес, что выше.", advanced=True),
        Field("imap_port", "Порт IMAP", "int", default=993, advanced=True),
        Field("imap_security", "Шифрование IMAP", "select", default="ssl", advanced=True,
              options=(("ssl", "SSL/TLS (993)"), ("starttls", "STARTTLS (143)"), ("plain", "без шифрования"))),
    )


def tls_fields() -> Tuple[Field, ...]:
    """Проверка сертификата своего сервера: по умолчанию — обычная проверка; отпечаток — для самоподписанного."""
    return (
        Field("verify_tls", "Проверять сертификат сервера", "bool", default=True, advanced=True,
              help="Выключать не нужно: для самоподписанного сертификата «Проверить подключение» покажет его "
                   "отпечаток — нажмите «Доверять этому сертификату»."),
        Field("cert_sha256", "Отпечаток сертификата (SHA-256)", advanced=True,
              help="Сертификат сервера принимается, только если отпечаток совпадает (для самоподписанного)."),
    )
