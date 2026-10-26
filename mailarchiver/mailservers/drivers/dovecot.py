"""
Postfix + Dovecot (и сборки на их основе: iRedMail, PostfixAdmin и т. п.) — через doveadm HTTP API.

doveadm HTTP API (``POST /doveadm/v1``, тело — JSON-массив ``[команда, параметры, метка]``) включает
администратор Dovecot: ``doveadm_api_key`` (заголовок ``X-Dovecot-API``) или ``doveadm_password`` (Basic с
именем ``doveadm``) и ``inet_listener http`` у службы doveadm. Ответ — ``[["doveadmResponse", строки, метка]]``
или ``[["error", {"type": …, "exitCode": N}, метка]]`` с кодом HTTP 200; все значения в строках — строки.

Что можно через doveadm:

* список учётных записей — обходом всех пользователей (``mailboxStatus`` с ``allUsers``: у каждой строки
  есть ``username``); ``user`` с маской по HTTP список не отдаёт;
* занятое место и квота — ``quotaGet`` (модуль quota; объём — в килобайтах), без него — сумма ``vsize`` папок;
* автоответ и пересылка — сценарием Sieve «mailarchiver» (команды ``sieve*`` модуля doveadm в Pigeonhole):
  активным становится он, а прежний активный сценарий пользователя подключается в нём через ``include``;
* копирование ящиков — по IMAP мастер-пользователем (SASL PLAIN с authzid или «ящик*мастер»).

Чего doveadm не умеет: база пользователей (SQL, LDAP, passwd-файлы, PostfixAdmin, iRedMail) лежит вне
Dovecot и только читается — закрыть вход, сменить пароль, создать и удалить учётную запись, квоту и
псевдонимы здесь не изменить.

Здесь же — общие для mailcow и Dovecot помощники: разбор адреса API и сценарий Sieve автоответа и
пересылки (mailcow держит его своим фильтром, Dovecot — сценарием пользователя).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...errors import ValidationError
from ..base import (EMAIL_RE, Cap, Driver, Field, ImapAuth, NotFound, NotSupported, ServerAuthError, ServerError,
                    Status, check_email, imap_fields, norm_email, tls_fields)
from ..http import HttpClient

#: Имя сценария Sieve, который ведёт MailArchiver.
SIEVE_NAME = "mailarchiver"
#: Строка сценария с его состоянием (JSON) — чтобы прочитать автоответ и пересылку обратно без разбора Sieve.
STATE_MARK = "# mailarchiver-state: "
SIEVE_COMMANDS = ("sieveList", "sieveGet", "sievePut", "sieveActivate", "sieveDeactivate", "sieveDelete")
#: Команды только для чтения: их можно повторить после 502/503/504 (изменения сценариев — нет).
READ_COMMANDS = frozenset({"mailboxStatus", "quotaGet", "sieveList", "sieveGet", "user", "who"})
#: Обход всех пользователей на большом сервере идёт долго.
LONG_TIMEOUT_S = 600.0
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Коды завершения doveadm (sysexits) → что это значит.
EXIT_TEXT = {
    2: "ящик менялся во время команды",
    64: "неверные параметры команды",
    65: "ошибка в данных",
    67: "такого пользователя нет",
    68: "не найдено",
    73: "квота переполнена",
    75: "временная ошибка",
    77: "нет прав",
    78: "ошибка в настройках Dovecot",
    1003: "срок действия истёк",
}


# ---------------------------------------------------------------------------
#  Общее для своих серверов: адрес API
# ---------------------------------------------------------------------------
def parse_base_url(raw: Any, label: str, strip: Sequence[str] = ()) -> str:
    """Адрес из настройки → ``схема://адрес[:порт][/путь]`` (без схемы — https, без хвостов из ``strip``)."""
    text = str(raw or "").strip()
    if not text:
        raise ValidationError(f"Заполните «{label}».")
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    try:
        _port = parsed.port                 # неверный порт — ValueError
    except ValueError as exc:
        raise ValidationError(f"«{label}»: неверный порт в адресе «{raw}».") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment or any(ch.isspace() for ch in text):
        raise ValidationError(f"«{label}»: адрес указан неверно: «{raw}».",
                              hint="Нужен адрес вида https://mail.company.ru или http://10.0.0.5:8080.")
    path = parsed.path.rstrip("/")
    for suffix in strip:
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def allow_plain_field(help_text: str) -> Field:
    return Field("allow_plain", "Разрешить без шифрования (http)", "bool", default=False, advanced=True,
                 help=help_text)


# ---------------------------------------------------------------------------
#  Общее: сценарий Sieve автоответа и пересылки
# ---------------------------------------------------------------------------
def sieve_string(value: Any) -> str:
    """Строка Sieve в кавычках (RFC 5228: экранируются только кавычка и обратная косая черта)."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def autoreply_state(*, subject: str, text: str, start: str = "", end: str = "",
                    addresses: Sequence[str] = ()) -> Dict[str, Any]:
    """Проверенные поля автоответа (для :func:`sieve_script`)."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not body.strip():
        raise ValidationError("Текст автоответа пуст.")
    for value in (start, end):
        if value:
            if not _DATE.match(value):
                raise ValidationError(f"Дата автоответа указана неверно: «{value}» (нужно ГГГГ-ММ-ДД).")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(f"Такой даты нет: «{value}».") from exc
    if start and end and start > end:
        raise ValidationError("Автоответ: дата окончания раньше даты начала.")
    own: List[str] = []
    for a in addresses:
        a = norm_email(a)
        if a and EMAIL_RE.match(a) and a not in own:
            own.append(a)
    return {"subject": " ".join((subject or "").split())[:250], "text": body[:10000], "start": start or "",
            "end": end or "", "addresses": own}


def forward_state(addresses: Sequence[str], keep_copy: bool) -> Optional[Dict[str, Any]]:
    """Проверенные адреса пересылки (None — пересылки нет)."""
    out: List[str] = []
    for a in addresses or ():
        a = check_email(a, "Адрес пересылки")
        if a not in out:
            out.append(a)
    return {"addresses": out, "keep_copy": bool(keep_copy)} if out else None


def sieve_script(autoreply: Optional[Dict[str, Any]], forward: Optional[Dict[str, Any]], *,
                 include: str = "") -> str:
    """Сценарий Sieve: автоответ (RFC 5230, с датами — RFC 5260), пересылка (redirect, с копией — RFC 3894)
    и, если у пользователя был свой активный сценарий, — его подключение (include, RFC 6609)."""
    requires: List[str] = []
    body: List[str] = []
    if autoreply:
        requires.append("vacation")
        args = ":days 1"
        if autoreply.get("subject"):
            args += " :subject " + sieve_string(autoreply["subject"])
        if autoreply.get("addresses"):
            args += " :addresses [" + ", ".join(sieve_string(a) for a in autoreply["addresses"]) + "]"
        stmt = f"vacation {args} {sieve_string(autoreply.get('text') or '')};"
        tests = []
        if autoreply.get("start"):
            tests.append(f'currentdate :value "ge" "date" {sieve_string(autoreply["start"])}')
        if autoreply.get("end"):
            tests.append(f'currentdate :value "le" "date" {sieve_string(autoreply["end"])}')
        if tests:
            requires += ["date", "relational"]
            body += [f"if allof ({', '.join(tests)}) {{", "    " + stmt, "}"]
        else:
            body.append(stmt)
    keep = True
    if forward and forward.get("addresses"):
        keep = bool(forward.get("keep_copy", True))
        if keep:
            requires.append("copy")
        for a in forward["addresses"]:
            body.append(f"redirect {':copy ' if keep else ''}{sieve_string(a)};")
        if not keep:
            body.append("stop;")              # письмо не остаётся в ящике — дальше фильтровать нечего
    if include and keep:
        requires.append("include")
        body.append(f"include :personal {sieve_string(include)};")
    state = {"v": 1, "autoreply": autoreply or None, "forward": forward or None, "include": include or ""}
    lines = ["# MailArchiver: автоответ и пересылка. Сценарий ведёт MailArchiver — правки вручную будут перезаписаны.",
             STATE_MARK + json.dumps(state, ensure_ascii=False, separators=(",", ":"))]
    if requires:
        lines.append("require [" + ", ".join(sieve_string(r) for r in requires) + "];")
    lines += body
    return "\n".join(lines) + "\n"


def sieve_state(script: str) -> Dict[str, Any]:
    """Сценарий MailArchiver → {autoreply, forward, include, manual}. Без строки состояния (сценарий правили
    вручную) — по самому сценарию: есть ли vacation и куда redirect."""
    for line in (script or "").splitlines():
        if line.startswith(STATE_MARK):
            try:
                data = json.loads(line[len(STATE_MARK):])
            except ValueError:
                break
            if isinstance(data, dict):
                auto = data.get("autoreply") if isinstance(data.get("autoreply"), dict) else None
                fwd = data.get("forward") if isinstance(data.get("forward"), dict) else None
                if fwd is not None:
                    fwd = {"addresses": [norm_email(a) for a in fwd.get("addresses") or [] if a],
                           "keep_copy": bool(fwd.get("keep_copy", True))}
                    fwd = fwd if fwd["addresses"] else None
                return {"autoreply": auto, "forward": fwd, "include": str(data.get("include") or ""),
                        "manual": False}
            break
    code = "\n".join(line for line in (script or "").splitlines() if not line.lstrip().startswith("#"))
    auto = None
    if re.search(r"(^|[;{}\s])vacation\s", code, re.M):
        auto = {"subject": "", "text": "", "start": "", "end": "", "addresses": []}
    targets = re.findall(r'\bredirect\s+((?::copy\s+)?)"([^"\\]+)"', code)
    fwd = None
    if targets:
        fwd = {"addresses": [norm_email(t) for _c, t in targets], "keep_copy": all(c.strip() for c, _t in targets)}
    return {"autoreply": auto, "forward": fwd, "include": "", "manual": True}


def autoreply_view(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    auto = (state or {}).get("autoreply") or None
    if not auto:
        return {"enabled": False, "subject": "", "text": "", "start": "", "end": ""}
    return {"enabled": True, "subject": auto.get("subject") or "", "text": auto.get("text") or "",
            "start": auto.get("start") or "", "end": auto.get("end") or ""}


def forwarding_view(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    fwd = (state or {}).get("forward") or None
    if not fwd:
        return {"addresses": [], "keep_copy": True}
    return {"addresses": list(fwd.get("addresses") or []), "keep_copy": bool(fwd.get("keep_copy", True))}


def autoreply_text(enabled: bool, auto: Optional[Dict[str, Any]]) -> str:
    if not enabled:
        return "автоответ выключен"
    period = ""
    if auto and (auto.get("start") or auto.get("end")):
        period = f" ({auto.get('start') or '…'} — {auto.get('end') or '…'})"
    return "автоответ включён" + period


def forwarding_text(fwd: Optional[Dict[str, Any]]) -> str:
    if not fwd:
        return "пересылка выключена"
    text = f"пересылка на {', '.join(fwd['addresses'])}"
    if not fwd.get("keep_copy", True):
        text += "; в ящике письма не остаются"
    return text


def as_int(value: Any) -> Optional[int]:
    """Число из ответа сервера (строка или число; «-», «error» и пусто — None)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


#: «Оставить как было» для автоответа или пересылки при записи сценария.
KEEP = object()


class DovecotDriver(Driver):
    type = "dovecot"
    label = "Postfix + Dovecot"
    summary = ("Свой сервер Postfix + Dovecot (и iRedMail, PostfixAdmin) через doveadm HTTP API: список ящиков, "
               "занятое место и квота, автоответ и пересылка сценарием Sieve; копирование ящиков по IMAP "
               "мастер-пользователем Dovecot.")
    cloud = False
    fields = (
        Field("base_url", "Адрес doveadm HTTP API", required=True, placeholder="http://10.0.0.5:8080",
              help="Схема, адрес и порт inet_listener http службы doveadm (без схемы — https://). Обычный http "
                   "на другой машине нужно разрешить ниже, в «Дополнительно»."),
        Field("auth", "Чем входить в doveadm HTTP API", "select", default="api_key",
              options=(("api_key", "ключ API (doveadm_api_key)"), ("password", "пароль doveadm (doveadm_password)"))),
        Field("api_key", "Ключ API (doveadm_api_key)", "password", secret=True, show_if={"auth": ["api_key"]}),
        Field("doveadm_password", "Пароль doveadm (doveadm_password)", "password", secret=True,
              show_if={"auth": ["password"]}, help="Вход с именем пользователя doveadm."),
        Field("master_user", "Мастер-пользователь Dovecot (для копирования ящиков)", placeholder="archmaster",
              help="Учётная запись из passdb с master = yes. В iRedMail — с несуществующим доменом, например "
                   "archmaster@not-exist.com. Пусто — ящики без паролей сотрудников не копируются."),
        Field("master_password", "Пароль мастер-пользователя", "password", secret=True),
        Field("master_mode", "Как входить мастер-пользователем", "select", default="plain_authzid",
              options=(("plain_authzid", "SASL PLAIN с authzid (RFC 4616)"),
                       ("separator", "логин «ящик*мастер» (auth_master_user_separator)"))),
        Field("master_separator", "Разделитель (auth_master_user_separator)", default="*",
              show_if={"master_mode": ["separator"]}),
        Field("domains", "Только эти домены", placeholder="company.ru, company.com", advanced=True,
              help="Через запятую. Пусто — все пользователи, которых знает Dovecot."),
        allow_plain_field("doveadm HTTP API часто слушает обычный http на внутреннем порту. Включайте, только если "
                          "сеть до сервера доверенная: ключ API уйдёт открытым текстом. На эту же машину "
                          "(127.0.0.1) можно и без этого."),
        *imap_fields("как адрес doveadm"),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ()
    imap_default = None
    limits = (
        "Учётные записи хранит не Dovecot, а его база пользователей (SQL, LDAP, файлы passwd; PostfixAdmin, "
        "iRedMail и т. п.) — doveadm её только читает. Закрыть вход, сменить пароль, создать или удалить учётную "
        "запись, задать квоту и псевдонимы через это подключение нельзя: это делается там, где ведутся "
        "пользователи.",
        "Команды doveadm «kick» (разорвать сеансы) и «authCacheFlush» (сбросить кэш входа) есть, но базу "
        "пользователей не меняют: пока вход не закрыт в самой базе, сотрудник войдёт снова.",
        "Последний вход doveadm не показывает (модуль last_login пишет его во внешний словарь), псевдонимы "
        "живут в Postfix — в сверке их нет.",
        "Список учётных записей — обходом всех пользователей (как doveadm -A): база пользователей должна уметь их "
        "перечислять (iterate_query в SQL, iterate_* в LDAP). Если на одном пользователе doveadm споткнётся, он не "
        "отдаёт ничего — сверка сообщит ошибку.",
        "Квота и занятое место — из модуля quota (doveadm quota get). Без него занятое место — сумма размеров "
        "писем во всех папках, а квота неизвестна.",
        "Автоответ и пересылка — сценарием Sieve «mailarchiver» через doveadm (нужен модуль doveadm в Pigeonhole). "
        "Активным бывает только один сценарий: MailArchiver делает активным свой, подключая в нём прежний "
        "(include), а при выключении возвращает прежний. Если почтовый клиент (Roundcube, SOGo) потом включит "
        "свой сценарий, автоответ и пересылка перестанут действовать — сверка это покажет.",
        "Очередь Postfix и версию Dovecot doveadm HTTP API не показывает.",
        "doveadm HTTP API — опасный интерфейс: открывайте его только для адреса MailArchiver, а в недоверенной "
        "сети — только с TLS (ssl = yes у inet_listener http).",
    )
    setup = (
        "Включите doveadm HTTP API: в настройках Dovecot (например, conf.d/10-doveadm.conf) задайте "
        "doveadm_api_key = <длинный случайный ключ> (или doveadm_password) и в service doveadm добавьте "
        "inet_listener http { port = 8080 } (ssl = yes — для https). Перезапустите Dovecot.",
        "Разрешите доступ к этому порту только с адреса MailArchiver (межсетевой экран).",
        "Мастер-пользователь для копирования ящиков: passdb с master = yes и result_success = continue "
        "(Dovecot 2.3: passdb { driver = passwd-file; args = /etc/dovecot/master-users; master = yes; "
        "result_success = continue }), в файле — строка «archmaster:{SHA512-CRYPT}…» (doveadm pw -s SHA512-CRYPT). "
        "Для входа «ящик*мастер» — ещё auth_master_user_separator = *.",
        "Если включён модуль acl, дайте мастер-пользователю доступ ко всем папкам: plugin { acl_user = %u } "
        "(или master_user = %u).",
        "iRedMail: мастер-пользователи уже включены (/etc/dovecot/dovecot-master-users); имя укажите с "
        "несуществующим доменом, например archmaster@not-exist.com.",
        "Для квоты в сверке — модуль quota в mail_plugins; для автоответа и пересылки — Pigeonhole (sieve в "
        "mail_plugins для lda/lmtp) с хранилищем сценариев file (sieve = file:~/sieve;active=~/.dovecot.sieve).",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._http: Optional[HttpClient] = None
        self._tag = 0
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки ----------------------------------------------------------------------------
    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        parse_base_url(settings.get("base_url"), "Адрес doveadm HTTP API", ("/doveadm/v1",))
        if (settings.get("auth") or "api_key") == "password":
            if not secrets.get("doveadm_password"):
                raise ValidationError("Укажите пароль doveadm (doveadm_password).")
        elif not secrets.get("api_key"):
            raise ValidationError("Укажите ключ API (doveadm_api_key).")
        master = str(settings.get("master_user") or "").strip()
        if master and not secrets.get("master_password"):
            raise ValidationError("Укажите пароль мастер-пользователя — или оставьте пустым и имя.")
        if secrets.get("master_password") and not master:
            raise ValidationError("Укажите имя мастер-пользователя.")
        if master and any(ch.isspace() for ch in master):
            raise ValidationError("Имя мастер-пользователя — без пробелов.")
        sep = str(settings.get("master_separator") or "*")
        if (settings.get("master_mode") or "plain_authzid") == "separator" and \
                (len(sep) > 3 or any(ch.isspace() or ch.isalnum() for ch in sep)):
            raise ValidationError("Разделитель — один-три знака, не буквы и не цифры (обычно «*»).")
        for d in cls._domain_list(settings.get("domains")):
            if "." not in d or "@" in d:
                raise ValidationError(f"«Только эти домены»: «{d}» — не домен.")
        return []

    @staticmethod
    def _domain_list(raw: Any) -> List[str]:
        return [d.strip().lower().lstrip("@") for d in re.split(r"[\s,;]+", str(raw or "")) if d.strip()]

    def _domain_ok(self, email: str) -> bool:
        wanted = self._domain_list(self.setting("domains"))
        return not wanted or email.partition("@")[2] in wanted

    def _base_url(self) -> str:
        return parse_base_url(self.setting("base_url"), "Адрес doveadm HTTP API", ("/doveadm/v1",))

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            if (self.setting("auth") or "api_key") == "password":
                password = self.secret("doveadm_password")
                if not password:
                    raise ServerAuthError(f"{self.label}: не задан пароль doveadm.")
                token = base64.b64encode(f"doveadm:{password}".encode("utf-8")).decode("ascii")
                header = f"Basic {token}"
            else:
                key = self.secret("api_key")
                if not key:
                    raise ServerAuthError(f"{self.label}: не задан ключ API doveadm.")
                token = base64.b64encode(key.encode("utf-8")).decode("ascii")
                header = f"X-Dovecot-API {token}"
            if self.api is not None:
                self.api.secret(token)
            self._http = HttpClient(self._base_url(), api=self.api, verify_tls=bool(self.setting("verify_tls", True)),
                                    cert_sha256=str(self.setting("cert_sha256") or ""),
                                    allow_plain=bool(self.setting("allow_plain")), server_label=self.label,
                                    headers={"Authorization": header})
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
                host = urllib.parse.urlsplit(self._base_url()).hostname or ""
            except ValidationError:
                host = ""
        try:
            port = int(self.setting("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        return host, port, str(self.setting("imap_security") or "ssl")

    # -- вызов doveadm --------------------------------------------------------------------------
    def _cmd(self, command: str, params: Dict[str, Any], *, timeout: Optional[float] = None) -> Any:
        """Одна команда за запрос (так советует документация doveadm). → результат (обычно список строк)."""
        for attempt in (1, 2):
            self._tag += 1
            tag = f"ma{self._tag}"
            resp = self.http.request("POST", "/doveadm/v1", json_body=[[command, params, tag]],
                                     expect=(200, 400, 401, 403, 404, 500), timeout=timeout,
                                     idempotent=command in READ_COMMANDS)
            if resp.status in (401, 403):
                raise ServerAuthError(
                    f"{self.label}: doveadm HTTP API не принял ключ API (или пароль doveadm) — {resp.status}.",
                    hint="Сверьте doveadm_api_key (doveadm_password) в настройках Dovecot с подключением.")
            if resp.status == 404:
                raise NotSupported(
                    f"{self.label}: doveadm ответил «не найдено» (404) на команду «{command}» — такой команды на "
                    f"сервере нет (или адрес API указан неверно).")
            if resp.status == 400:
                raise ServerError(f"{self.label}: doveadm не принял запрос «{command}»: "
                                  f"{resp.text()[:300].strip() or 'код 400'}")
            if resp.status == 500:
                raise ServerError(f"{self.label}: внутренняя ошибка doveadm (500) на команде «{command}».",
                                  hint="Если в настройках Dovecot не задан ни doveadm_api_key, ни doveadm_password, "
                                       "doveadm HTTP API отвечает так на всё. Подробности — в журнале Dovecot.",
                                  retryable=True)
            body = resp.json()
            if not isinstance(body, list) or not body:
                raise ServerError(f"{self.label}: ответ не похож на doveadm HTTP API: {resp.text()[:200]!r}",
                                  hint="Проверьте адрес и порт (inet_listener http службы doveadm).")
            item = next((x for x in body if isinstance(x, list) and len(x) >= 3 and x[2] == tag), None)
            if item is None:
                item = body[0] if isinstance(body[0], list) and len(body[0]) >= 2 else None
            if item is None:
                raise ServerError(f"{self.label}: ответ doveadm не разобран: {resp.text()[:200]!r}")
            kind, payload = item[0], item[1]
            if kind == "doveadmResponse":
                return payload
            if kind != "error":
                raise ServerError(f"{self.label}: неожиданный ответ doveadm «{kind}» на команду «{command}».")
            err = payload if isinstance(payload, dict) else {}
            if str(err.get("type") or "") == "exitCode" and as_int(err.get("exitCode")) == 2 and attempt == 1:
                continue                        # ящик менялся во время команды — повторяем один раз
            raise self._error(command, params, err)
        raise ServerError(f"{self.label}: команда «{command}» не выполнена: ящик всё время менялся.", retryable=True)

    def _error(self, command: str, params: Dict[str, Any], err: Dict[str, Any]) -> ServerError:
        kind = str(err.get("type") or "")
        code = as_int(err.get("exitCode")) or 0
        who = params.get("user") or ("все пользователи" if params.get("allUsers") else "")
        where = f" ({who})" if who and isinstance(who, str) else ""
        if kind == "unknownMethod":
            what = {"quotaGet": " (модуль quota не подключён)"}.get(command, "")
            if command in SIEVE_COMMANDS:
                what = " (не подключён модуль doveadm в Pigeonhole)"
            return NotSupported(f"{self.label}: на сервере нет команды doveadm «{command}»{what}.")
        if kind == "unAuthorized":
            return ServerAuthError(f"{self.label}: doveadm не разрешает команду «{command}» этому ключу.")
        if kind == "invalidRequest":
            return ServerError(f"{self.label}: doveadm не принял параметры команды «{command}»"
                               f" — версия Dovecot их не знает.")
        if kind == "internalError":
            return ServerError(f"{self.label}: внутренняя ошибка doveadm на команде «{command}»{where}.",
                               retryable=True)
        text = EXIT_TEXT.get(code, f"код {code}")
        message = f"{self.label}: doveadm «{command}»{where}: {text} (код {code})."
        if code == 67:
            return NotFound(message)
        if code == 68:
            return NotFound(message)
        if code == 77:
            return ServerAuthError(message)
        if code == 75:
            return ServerError(message, retryable=True)
        if params.get("allUsers"):
            return ServerError(message, hint="При обходе всех пользователей doveadm отдаёт ошибку, если споткнулся "
                                             "хотя бы на одном; кто именно — видно в журнале Dovecot. Обходу нужна "
                                             "итерация базы пользователей (iterate_query в SQL).")
        return ServerError(message)

    @staticmethod
    def _rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    # -- чтение ---------------------------------------------------------------------------------
    def _user_fields(self, email: str) -> Dict[str, Any]:
        """Поля userdb пользователя. Ответ бывает строками {field, value} (исходники 2.3.21/2.4) или объектом
        {пользователь: {поле: значение}} (пример документации 2.3) — разбираются оба."""
        payload = self._cmd("user", {"userMask": [email]})
        fields: Dict[str, Any] = {}
        if isinstance(payload, dict):
            inner = payload.get(email)
            if inner is None and len(payload) == 1:
                inner = next(iter(payload.values()))
            if isinstance(inner, dict):
                fields = {str(k): v for k, v in inner.items()}
            elif not any(isinstance(v, dict) for v in payload.values()):
                fields = {str(k): v for k, v in payload.items()}
        elif isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue
                if "field" in row and "value" in row:
                    fields[str(row["field"])] = row["value"]
                else:
                    fields.update({str(k): v for k, v in row.items() if not isinstance(v, (dict, list))})
        return fields

    @staticmethod
    def _sum_status(rows: List[Dict[str, Any]]) -> Dict[str, int]:
        out = {"messages": 0, "vsize": 0, "folders": 0}
        for row in rows:
            out["folders"] += 1
            out["messages"] += as_int(row.get("messages")) or 0
            out["vsize"] += as_int(row.get("vsize")) or 0
        return out

    @staticmethod
    def _pick_quota(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Строки quota get одного пользователя → {used, limit, messages, root} (объём — из КБ в байты)."""
        storage = [r for r in rows
                   if str(r.get("type") or "").upper() == "STORAGE" and as_int(r.get("value")) is not None]
        if not storage:
            return None
        limited = [r for r in storage if as_int(r.get("limit"))]
        row = min(limited, key=lambda r: as_int(r.get("limit")) or 0) if limited else storage[0]
        out = {"used": (as_int(row.get("value")) or 0) * 1024, "root": str(row.get("root") or "")}
        limit = as_int(row.get("limit"))
        out["limit"] = limit * 1024 if limit else None
        msg = [r for r in rows if str(r.get("type") or "").upper() == "MESSAGE" and as_int(r.get("value")) is not None]
        out["messages"] = as_int(msg[0].get("value")) if msg else None
        return out

    def _account(self, email: str, native: str, status: Optional[Dict[str, int]], quota: Optional[Dict[str, Any]],
                 sieve: Any = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        info: Dict[str, Any] = dict(extra or {})
        acc: Dict[str, Any] = {"email": email, "native_id": native, "aliases": [], "login_blocked": None,
                               "kind": "user"}
        if status is not None:
            acc["messages"] = status["messages"]
            acc["used_bytes"] = status["vsize"]
            info["folders"] = status["folders"]
            info["vsize"] = status["vsize"]
        if quota is not None:
            acc["used_bytes"] = quota["used"]
            acc["quota_bytes"] = quota["limit"]
            if acc.get("messages") is None and quota.get("messages") is not None:
                acc["messages"] = quota["messages"]
            if quota.get("root"):
                info["quota_root"] = quota["root"]
            info["usage_source"] = "quota"
        elif status is not None:
            info["usage_source"] = "vsize"
        if sieve is not KEEP:
            state = sieve if isinstance(sieve, dict) else None
            acc["autoreply"] = bool(state and state.get("autoreply"))
            acc["forward_to"] = list(((state or {}).get("forward") or {}).get("addresses") or [])
            if state and state.get("manual"):
                info["sieve_manual"] = True
        acc["info"] = info
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        rows = self._rows(self._cmd("mailboxStatus", {"allUsers": True, "field": ["messages", "vsize"],
                                                      "mailboxMask": ["*"]}, timeout=LONG_TIMEOUT_S))
        per: Dict[str, List[Dict[str, Any]]] = {}
        native: Dict[str, str] = {}
        no_domain: List[str] = []
        for row in rows:
            name = str(row.get("username") or "").strip()
            if not name:
                continue
            email = norm_email(name)
            if "@" not in email or not EMAIL_RE.match(email):
                if name not in no_domain:
                    no_domain.append(name)
                continue
            if not self._domain_ok(email):
                continue
            per.setdefault(email, []).append(row)
            native.setdefault(email, name)
        if rows and not per and not no_domain:
            raise ServerError(f"{self.label}: doveadm вернул папки без имён пользователей — список учётных записей "
                              f"не составить.", hint="Нужна версия Dovecot 2.3 или новее.")
        if no_domain:
            self.sync_problems.append(
                f"пропущены пользователи без домена в имени ({len(no_domain)}): {', '.join(no_domain[:5])}"
                f"{' …' if len(no_domain) > 5 else ''} — MailArchiver сверяет учётные записи вида имя@домен")
        if not per:
            self.incomplete = True
            self.sync_problems.append("doveadm не вернул ни одного пользователя — прежние учётные записи удалёнными не "
                                      "считаются; проверьте итерацию базы пользователей (doveadm user '*')")
        quota: Dict[str, Dict[str, Any]] = {}
        try:
            qrows = self._rows(self._cmd("quotaGet", {"allUsers": True}, timeout=LONG_TIMEOUT_S))
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for row in qrows:
                grouped.setdefault(norm_email(row.get("username")), []).append(row)
            for email, items in grouped.items():
                picked = self._pick_quota(items)
                if picked is not None:
                    quota[email] = picked
        except ServerError as exc:
            self._note(f"квота не прочитана — занятое место по размеру папок: {exc.message}")
        sieve = self._all_sieve(native)
        out = []
        for email in sorted(per):
            out.append(self._account(email, native[email], self._sum_status(per[email]), quota.get(email),
                                     sieve.get(email) if sieve is not None else KEEP))
        return out

    def _all_sieve(self, native: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Сценарии MailArchiver всех пользователей: адрес → состояние (None — сценария нет или он не активен).
        Не вышло (нет модуля, ошибка) — None: автоответ и пересылка в сверке неизвестны."""
        if not native:
            return {}
        try:
            rows = self._rows(self._cmd("sieveList", {"allUsers": True}, timeout=LONG_TIMEOUT_S))
        except ServerError as exc:
            self._note(f"сценарии Sieve не прочитаны — автоответ и пересылка в сверке неизвестны: {exc.message}")
            return None
        out: Dict[str, Any] = {email: None for email in native}
        active = [norm_email(r.get("username")) for r in rows
                  if str(r.get("script") or "") == SIEVE_NAME and self._is_active(r.get("active"))]
        for email in active:
            if email not in native:
                continue
            try:
                out[email] = sieve_state(self._sieve_get(native[email], SIEVE_NAME))
            except ServerError as exc:
                self._note(f"{email}: сценарий {SIEVE_NAME} не прочитан: {exc.message}")
                out.pop(email, None)
        return out

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        email = norm_email(email)
        if not self._domain_ok(email):
            return None
        extra: Dict[str, Any] = {}
        user = email
        try:
            fields = self._user_fields(email)
        except NotFound:
            return None
        except ServerAuthError:
            raise
        except ServerError as exc:
            self._note(f"userdb не прочитана: {exc.message}")
            fields = {}
        if fields.get("user"):
            user = str(fields["user"])
        for key in ("home", "mail", "quota_rule"):
            if fields.get(key):
                extra[key] = str(fields[key])
        try:
            rows = self._rows(self._cmd("mailboxStatus", {"user": user, "field": ["messages", "vsize"],
                                                          "mailboxMask": ["*"]}))
        except NotFound:
            return None
        quota = None
        try:
            quota = self._pick_quota(self._rows(self._cmd("quotaGet", {"user": user})))
        except NotFound:
            return None
        except ServerError as exc:
            self._note(f"квота не прочитана: {exc.message}")
        sieve: Any = KEEP
        try:
            _names, active, state = self._managed(user)
            sieve = state if active == SIEVE_NAME else None
        except ServerError as exc:
            self._note(f"сценарии Sieve не прочитаны: {exc.message}")
        return self._account(norm_email(user) if "@" in user else email, user, self._sum_status(rows), quota, sieve,
                             extra)

    # -- Sieve ----------------------------------------------------------------------------------
    @staticmethod
    def _is_active(value: Any) -> bool:
        return str(value or "").strip().lower() in ("active", "yes", "1", "true")

    def _sieve_get(self, user: str, name: str) -> str:
        rows = self._rows(self._cmd("sieveGet", {"user": user, "scriptname": name}))
        parts = []
        for row in rows:
            for key in ("sieve script", "sieve", "script"):
                if key in row:
                    parts.append(str(row[key] or ""))
                    break
            else:
                if len(row) == 1:
                    parts.append(str(next(iter(row.values())) or ""))
        return "".join(parts)

    def _managed(self, user: str) -> Tuple[List[str], str, Optional[Dict[str, Any]]]:
        """Сценарии пользователя: (имена, активный, состояние сценария MailArchiver или None)."""
        rows = self._rows(self._cmd("sieveList", {"user": user}))
        names: List[str] = []
        active = ""
        for row in rows:
            name = str(row.get("script") or "")
            if not name:
                continue
            names.append(name)
            if self._is_active(row.get("active")):
                active = name
        state = sieve_state(self._sieve_get(user, SIEVE_NAME)) if SIEVE_NAME in names else None
        return names, active, state

    def _update(self, email: str, change: Callable[[Optional[Dict[str, Any]]], Tuple[Any, Any]]
                ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Записать сценарий MailArchiver: ``change(текущее состояние) → (автоответ, пересылка)``; оба пусты —
        сценарий удаляется, а прежний сценарий пользователя снова становится активным."""
        user = norm_email(email)
        try:
            names, active, state = self._managed(user)
        except NotSupported as exc:
            raise NotSupported(f"{exc.message} Автоответ и пересылка через doveadm недоступны.") from exc
        current = state if active == SIEVE_NAME else None
        autoreply, forward = change(current)
        if not autoreply and not forward:
            if SIEVE_NAME in names:
                if active == SIEVE_NAME:
                    previous = (state or {}).get("include") or ""
                    if previous and previous in names and previous != SIEVE_NAME:
                        self._cmd("sieveActivate", {"user": user, "scriptname": previous})
                    else:
                        self._cmd("sieveDeactivate", {"user": user})
                self._cmd("sieveDelete", {"user": user, "scriptname": [SIEVE_NAME]})
            return None, None
        if active == SIEVE_NAME:
            include = (state or {}).get("include") or ""
            include = include if include in names and include != SIEVE_NAME else ""
        else:
            include = active
        script = sieve_script(autoreply, forward, include=include)
        self._cmd("sievePut", {"user": user, "scriptname": SIEVE_NAME, "activate": active != SIEVE_NAME,
                               "file": script})
        return autoreply, forward

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        _names, active, state = self._managed(norm_email(email))
        return autoreply_view(state if active == SIEVE_NAME else None)

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        email = norm_email(email)
        auto = autoreply_state(subject=subject, text=text, start=start, end=end) if enabled else None

        def change(state):
            return auto, (state or {}).get("forward")
        self._update(email, change)
        return autoreply_text(enabled, auto) + (f" (сценарий Sieve «{SIEVE_NAME}»)" if enabled else "")

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        _names, active, state = self._managed(norm_email(email))
        return forwarding_view(state if active == SIEVE_NAME else None)

    def set_forwarding(self, email: str, *, addresses: Sequence[str], keep_copy: bool = True) -> str:
        email = norm_email(email)
        fwd = forward_state(addresses, keep_copy)

        def change(state):
            return (state or {}).get("autoreply"), fwd
        self._update(email, change)
        return forwarding_text(fwd) + (f" (сценарий Sieve «{SIEVE_NAME}»)" if fwd else "")

    # -- IMAP и состояние ------------------------------------------------------------------------
    def imap_admin_problem(self) -> str:
        if not str(self.setting("master_user") or "").strip() or not self.secret("master_password"):
            return "не задан мастер-пользователь Dovecot и его пароль"
        return ""

    def imap_auth(self, email: str) -> ImapAuth:
        master = str(self.setting("master_user") or "").strip()
        password = self.secret("master_password")
        if not master or not password:
            raise ServerAuthError(f"{self.label}: мастер-пользователь Dovecot в подключении не задан — войти в ящик "
                                  f"без пароля сотрудника нечем.",
                                  hint="Заполните «Мастер-пользователь Dovecot» и его пароль в подключении.")
        host = self.imap_server()[0]
        if (self.setting("master_mode") or "plain_authzid") == "separator":
            return ImapAuth(mech="separator", user=master, secret=password, authzid=norm_email(email),
                            separator=str(self.setting("master_separator") or "*"), host=host)
        return ImapAuth(mech="plain_authzid", user=master, secret=password, authzid=norm_email(email), host=host)

    def test(self) -> Status:
        st = Status(version="Dovecot (doveadm HTTP API)")
        listing = self.http.request("GET", "/doveadm/v1", expect=(200, 401, 403, 404, 500))
        if listing.status in (401, 403):
            raise ServerAuthError(f"{self.label}: doveadm HTTP API не принял ключ API (или пароль doveadm) — "
                                  f"{listing.status}.",
                                  hint="Сверьте doveadm_api_key (doveadm_password) в настройках Dovecot с "
                                       "подключением.")
        if listing.status == 404:
            raise ServerError(f"{self.label}: по адресу {self._base_url()} нет doveadm HTTP API (404).",
                              hint="Укажите адрес и порт inet_listener http службы doveadm, без пути.")
        if listing.status == 500:
            raise ServerError(f"{self.label}: внутренняя ошибка doveadm (500).",
                              hint="Проверьте, что в настройках Dovecot задан doveadm_api_key или doveadm_password.")
        body = listing.json()
        if not isinstance(body, list):
            raise ServerError(f"{self.label}: ответ не похож на doveadm HTTP API.",
                              hint="Проверьте адрес и порт (inet_listener http службы doveadm).")
        names = {str(c.get("command")) for c in body if isinstance(c, dict) and c.get("command")}
        st.details.append(f"doveadm HTTP API: команд доступно {len(names)}")
        if "mailboxStatus" not in names:
            st.problems.append(("mailbox_status", "error", "нет команды mailboxStatus — сверка учётных записей "
                                                           "невозможна"))
        if "quotaGet" in names:
            st.details.append("Квоты: модуль quota подключён")
        else:
            st.problems.append(("quota", "warning", "модуль quota не подключён — квота в сверке не видна, занятое "
                                                    "место считается по размеру папок"))
        if all(c in names for c in SIEVE_COMMANDS):
            st.details.append("Sieve: команды doveadm Pigeonhole доступны (автоответ и пересылка)")
        else:
            st.problems.append(("sieve", "warning", "команд doveadm sieve нет (модуль doveadm в Pigeonhole не "
                                                    "подключён) — автоответ и пересылка не будут работать"))
        try:
            who = self._rows(self._cmd("who", {}))
            sessions = sum(as_int(r.get("connections")) or 0 for r in who)
            st.details.append(f"Сейчас подключено: пользователей {len(who)}, соединений {sessions}")
        except ServerError as exc:
            self._note(f"сеансы не прочитаны: {exc.message}")
        if self._domain_list(self.setting("domains")):
            st.details.append("Домены: " + ", ".join(self._domain_list(self.setting("domains"))))
        master = str(self.setting("master_user") or "").strip()
        if not master or not self.secret("master_password"):
            st.problems.append(("imap_master", "warning", "мастер-пользователь Dovecot не задан — ящики без паролей "
                                                          "сотрудников не скопировать"))
        else:
            mode = "SASL PLAIN с authzid" if (self.setting("master_mode") or "plain_authzid") != "separator" else \
                f"логин «ящик{self.setting('master_separator') or '*'}{master}»"
            st.details.append(f"Копирование ящиков: мастер-пользователь {master} ({mode}), IMAP "
                              f"{self.imap_server()[0]}:{self.imap_server()[1]}")
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = DovecotDriver
