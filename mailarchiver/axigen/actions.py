"""
Изменения на сервере Axigen через CLI: только по команде администратора.

Каждое действие:

* начинается из корня CLI и в корень возвращается (``BACK`` из всех
  контекстов), что бы ни случилось;
* прежде чем менять, читает текущее состояние (службы учётной записи,
  разрешённые к очистке папки домена) и после изменения проверяет, что
  сервер его принял;
* не выполняется вовсе, если в «Настройки → Сервер Axigen» не разрешены
  изменения на сервере (:func:`require_changes_allowed`).

Пароль, который отправляется серверу, в журнал команд не попадает.
"""
from __future__ import annotations

import secrets
import string
from typing import Dict, List, Optional, Tuple

from ..errors import ValidationError
from . import parse
from .cli import AxigenCLI, has_control, quote, quote_list
from .common import (AxigenError, AxigenSettings, blocked_services, services_blocked,
                     split_email)
from .directory import (account_folders, enter_account, enter_domain, read_services, valid_account_name,
                        valid_domain)

#: Знаки случайного пароля: без кавычек, пробелов и «\» (их нельзя передать в CLI).
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 20


def require_changes_allowed(st: AxigenSettings) -> None:
    if not st.allow_changes:
        raise ValidationError(
            "Изменения на сервере Axigen не разрешены.",
            hint="Включите «Разрешить изменения на сервере» в «Настройки → Сервер Axigen» (только для тех, кто "
                 "отвечает за почтовый сервер).")


def random_password(length: int = PASSWORD_LENGTH) -> str:
    while True:
        value = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in value) and any(c.islower() for c in value) and any(c.isupper() for c in value):
            return value


def check_password(password: str) -> str:
    """Пароль, который можно передать в CLI. Пустой и с недопустимыми знаками — отказ."""
    value = password or ""
    if len(value) < 6:
        raise ValidationError("Пароль короче 6 знаков.")
    if len(value) > 64:
        raise ValidationError("Пароль длиннее 64 знаков.")
    if any(c in value for c in "\"\\") or has_control(value) or any(c.isspace() for c in value):
        raise ValidationError("В пароле для Axigen нельзя использовать пробелы, кавычки, «\\» и управляющие "
                              "знаки.")
    return value


def parse_email(email: str) -> Tuple[str, str]:
    local, domain = split_email(email)
    if not domain or not valid_domain(domain) or not valid_account_name(local):
        raise ValidationError(f"«{email}» — не адрес учётной записи Axigen.")
    return local, domain


def _commit_account(cli: AxigenCLI) -> None:
    cli.run("COMMIT", strict=True)
    cli.to_root()


# ---------------------------------------------------------------------------
#  Службы: блокировка и разблокировка входа
# ---------------------------------------------------------------------------
def get_services(cli: AxigenCLI, email: str) -> Optional[List[str]]:
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        return read_services(cli)
    finally:
        cli.to_root()


def set_services(cli: AxigenCLI, email: str, services: List[str]) -> List[str]:
    """Задать службы учётной записи и вернуть то, что сервер показывает после изменения."""
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        cli.run("CONFIG Limits")
        cli.run(f"SET services {quote_list(services, 'название службы')}", strict=True)
        cli.run("DONE")
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    after = get_services(cli, email)
    if after is None:
        raise AxigenError(f"{email}: после изменения сервер не показал список служб — проверьте учётную запись "
                          f"в WebAdmin.")
    if {s.lower() for s in after} != {s.lower() for s in services}:
        raise AxigenError(f"{email}: сервер не применил список служб (просили: {', '.join(services) or 'пусто'}; "
                          f"стало: {', '.join(after) or 'пусто'}).")
    return after


def block(cli: AxigenCLI, email: str, *, keep_receiving: bool = True,
          blocked_by_us: bool = False) -> Tuple[List[str], List[str]]:
    """Закрыть вход (POP3, IMAP, WebMail…). Возвращает (службы до, службы после)."""
    before = get_services(cli, email)
    if before is None:
        raise AxigenError(f"{email}: сервер не показал службы учётной записи — блокировать вслепую не будем.")
    state = services_blocked(before, blocked_by_us=blocked_by_us)
    if state is None:
        raise AxigenError(f"{email}: по списку служб ({', '.join(before) or 'пусто'}) не понять, открыт ли вход "
                          f"(службы могут наследоваться от домена) — блокировать вслепую не будем.",
                          hint="Задайте учётной записи службы явно в WebAdmin Axigen или закройте вход там.")
    target = blocked_services(before, keep_receiving)
    if state and {s.lower() for s in before} == {s.lower() for s in target}:
        return before, before
    after = set_services(cli, email, target)
    return before, after


def unblock(cli: AxigenCLI, email: str, services: List[str]) -> List[str]:
    if not services:
        raise AxigenError(f"{email}: неизвестно, какие службы были у учётной записи до блокировки.")
    return set_services(cli, email, services)


# ---------------------------------------------------------------------------
#  Пароль, создание, удаление
# ---------------------------------------------------------------------------
def set_password(cli: AxigenCLI, email: str, password: str) -> None:
    local, domain = parse_email(email)
    check_password(password)
    enter_account(cli, domain, local)
    try:
        cli.run(f"SET password {quote(password, 'пароль')}", secrets=[password], strict=True)
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()


def account_exists(cli: AxigenCLI, email: str) -> bool:
    local, domain = parse_email(email)
    enter_domain(cli, domain)
    try:
        reply = cli.run(f"UPDATE Account name {quote(local, 'имя учётной записи')}", check=False)
        return reply.ok is not False
    finally:
        cli.to_root()


def create_account(cli: AxigenCLI, email: str, password: str, *, first_name: str = "", last_name: str = "") -> None:
    local, domain = parse_email(email)
    check_password(password)
    if account_exists(cli, email):
        raise ValidationError(f"Учётная запись {email} на сервере уже есть.")
    enter_domain(cli, domain)
    try:
        cli.run(f"ADD Account name {quote(local, 'имя учётной записи')} password {quote(password, 'пароль')}",
                secrets=[password], strict=True)
        if first_name or last_name:
            cli.run("CONFIG ContactInfo")
            if first_name:
                cli.run(f"SET firstName {quote(first_name, 'имя')}")
            if last_name:
                cli.run(f"SET lastName {quote(last_name, 'фамилия')}")
            cli.run("DONE")
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    if not account_exists(cli, email):
        raise AxigenError(f"Сервер ответил, что учётная запись {email} создана, но открыть её не удаётся.")


def remove_account(cli: AxigenCLI, email: str) -> None:
    local, domain = parse_email(email)
    enter_domain(cli, domain)
    try:
        cli.run(f"REMOVE Account name {quote(local, 'имя учётной записи')}", strict=True)
        # в части версий удаление в контексте домена нужно подтвердить; отказ «нечего
        # подтверждать» — не ошибка: удалилось ли, проверяется ниже
        cli.run("COMMIT", check=False)
    finally:
        cli.to_root()
    if account_exists(cli, email):
        raise AxigenError(f"Учётная запись {email} после удаления всё ещё открывается — удаление не применилось.")


# ---------------------------------------------------------------------------
#  Очистка папок на сервере
# ---------------------------------------------------------------------------
def purge_allowed_folders(cli: AxigenCLI, domain: str) -> Optional[List[str]]:
    """Папки, которые домену разрешено очищать (``CONFIG adminLimits`` → purgeFolders)."""
    enter_domain(cli, domain)
    try:
        cli.run("CONFIG adminLimits")
        values = parse.parse_show(cli.run("SHOW").lines)
        cli.run("BACK", check=False)
    finally:
        cli.to_root()
    raw = parse.get(values, "purgeFolders", "purge folders")
    if not raw:
        return None
    return parse.parse_list_value(raw)


def server_folders(cli: AxigenCLI, email: str) -> List[str]:
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        folders, _table = account_folders(cli)
    finally:
        cli.to_root()
    return [f.name for f in folders]


def purge_folder(cli: AxigenCLI, email: str, folder: str, days: int) -> Tuple[Optional[int], Optional[int]]:
    """``PURGE account folders <папка> BEFORE <N>d`` — удалить на сервере письма папки старше N дней.

    Возвращает число писем папки до и после (None — сервер его не показал).
    """
    local, domain = parse_email(email)
    days = int(days)
    if days < 1:
        raise ValidationError("Срок — не меньше одного дня.")
    if "," in folder:
        raise ValidationError(f"Папку с запятой в имени («{folder}») очистить командой Axigen нельзя.")

    def count() -> Optional[int]:
        try:
            folders, _table = account_folders(cli)
        except AxigenError:
            return None
        return next((f.messages for f in folders if f.name.lower() == folder.lower()), None)

    enter_account(cli, domain, local)
    try:
        before = count()
        cli.run(f"PURGE account folders {quote(folder, 'имя папки')} BEFORE {days}d", strict=True)
        after = count()
    finally:
        cli.to_root()
    return before, after


# ---------------------------------------------------------------------------
#  Квота, псевдонимы, контактные данные, признаки учётной записи
# ---------------------------------------------------------------------------
#: Наибольшая квота учётной записи по документации Axigen — 4 ТБ (в КБ).
MAX_QUOTA_KB = 4 * 1024 ** 3
#: Поля ``CONFIG ContactInfo``, которые меняют групповые действия (имя в CLI → подпись).
CONTACT_FIELDS = {"firstName": "имя", "lastName": "фамилия", "middleName": "отчество", "company": "организация",
                  "department": "отдел", "position": "должность", "office": "офис",
                  "businessPhone": "рабочий телефон", "mobilePhone": "мобильный телефон"}
#: Признаки учётной записи (``SET`` в её контексте) и допустимые значения.
ACCOUNT_FLAGS = {"mustChangePass": ("yes", "no"), "accountType": ("basic", "premium"),
                 "publishContactInfo": ("yes", "no")}


def read_quota_kb(cli: AxigenCLI, email: str) -> Optional[int]:
    """Квота объёма писем учётной записи, КБ (None — сервер не показал)."""
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        cli.run("CONFIG Quotas")
        try:
            values = parse.parse_show(cli.run("SHOW").lines)
        finally:
            cli.run("BACK", check=False)
    finally:
        cli.to_root()
    return parse.parse_int(parse.get(values, "totalMessageSize", "messageSizeQuota", "totalMessageSizeQuota"))


def set_quota(cli: AxigenCLI, email: str, quota_kb: int, *, threshold: Optional[int] = None) -> Optional[int]:
    """Задать квоту объёма писем (КБ) и, по желанию, порог предупреждения (%).

    Возвращает квоту, которую сервер показывает после изменения (None — не показал).
    """
    local, domain = parse_email(email)
    quota_kb = int(quota_kb)
    if quota_kb < 1024 or quota_kb > MAX_QUOTA_KB:
        raise ValidationError("Квота — от 1 МБ до 4 ТБ.")
    enter_account(cli, domain, local)
    try:
        cli.run("CONFIG Quotas")
        cli.run(f"SET totalMessageSize {quota_kb}", strict=True)
        if threshold is not None:
            threshold = int(threshold)
            if not 1 <= threshold <= 100:
                raise ValidationError("Порог предупреждения — от 1 до 100 %.")
            cli.run(f"SET quotaLimitThreshold {threshold}", strict=True)
            cli.run("SET quotaLimitNotificationEnabled yes", strict=True)
        cli.run("DONE")
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    return read_quota_kb(cli, email)


def list_aliases(cli: AxigenCLI, email: str) -> List[str]:
    """Псевдонимы учётной записи — полными адресами в нижнем регистре."""
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        table = parse.parse_table(cli.run("LIST Aliases").lines)
    finally:
        cli.to_root()
    col = table.column("alias", "name") or (table.columns[0] if table.columns else "name")
    out: List[str] = []
    for row in table.rows:
        alias = (row.get(col) or "").strip().split()
        if not alias:
            continue
        name = alias[0]
        if "@" not in name:
            name = f"{name}@{domain}"
        name = name.lower()
        if name != email.lower() and name not in out:
            out.append(name)
    return out


def alias_local(alias: str, domain: str) -> str:
    """Имя псевдонима без домена; псевдоним в чужом домене — отказ."""
    text = (alias or "").strip()
    local, at, dom = text.rpartition("@")
    if at:
        if dom.lower() != domain.lower():
            raise ValidationError(f"Псевдоним «{text}» — в другом домене: Axigen заводит псевдонимы только в домене "
                                  f"учётной записи ({domain}).")
        text = local
    if not valid_account_name(text):
        raise ValidationError(f"«{text}» — недопустимое имя псевдонима.")
    return text


def add_alias(cli: AxigenCLI, email: str, alias: str) -> List[str]:
    """Добавить псевдоним и вернуть псевдонимы после изменения (проверка, что сервер его принял)."""
    local, domain = parse_email(email)
    name = alias_local(alias, domain)
    enter_account(cli, domain, local)
    try:
        cli.run(f"ADD Alias {quote(name, 'имя псевдонима')}", strict=True)
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    after = list_aliases(cli, email)
    if f"{name}@{domain}".lower() not in after:
        raise AxigenError(f"{email}: сервер не показал новый псевдоним {name}@{domain} — проверьте в WebAdmin.")
    return after


def remove_alias(cli: AxigenCLI, email: str, alias: str) -> List[str]:
    local, domain = parse_email(email)
    name = alias_local(alias, domain)
    enter_account(cli, domain, local)
    try:
        cli.run(f"REMOVE Alias {quote(name, 'имя псевдонима')}", strict=True)
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    after = list_aliases(cli, email)
    if f"{name}@{domain}".lower() in after:
        raise AxigenError(f"{email}: псевдоним {name}@{domain} после удаления всё ещё есть.")
    return after


def read_contact(cli: AxigenCLI, email: str) -> Dict[str, str]:
    """Контактные данные учётной записи (``CONFIG ContactInfo`` → ``SHOW``): поле → значение."""
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        cli.run("CONFIG ContactInfo")
        try:
            values = parse.parse_show(cli.run("SHOW").lines)
        finally:
            cli.run("BACK", check=False)
    finally:
        cli.to_root()
    out: Dict[str, str] = {}
    for key in CONTACT_FIELDS:
        out[key] = parse.get(values, key)[:255]
    return out


def set_contact(cli: AxigenCLI, email: str, fields: Dict[str, str]) -> Dict[str, str]:
    """Записать поля контактных данных; вернуть, что сервер показывает после изменения."""
    local, domain = parse_email(email)
    for key in fields:
        if key not in CONTACT_FIELDS:
            raise ValidationError(f"Неизвестное поле контактных данных: {key}.")
    if not fields:
        return read_contact(cli, email)
    enter_account(cli, domain, local)
    try:
        cli.run("CONFIG ContactInfo")
        for key, value in fields.items():
            cli.run(f"SET {key} {quote(value, CONTACT_FIELDS[key])}", strict=True)
        cli.run("DONE")
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()
    after = read_contact(cli, email)
    wrong = [CONTACT_FIELDS[k] for k, v in fields.items() if (after.get(k) or "") and after.get(k) != v]
    if wrong:
        raise AxigenError(f"{email}: сервер показывает не то, что записано ({', '.join(wrong)}).")
    return after


def set_account_flag(cli: AxigenCLI, email: str, attr: str, value: str) -> None:
    """``SET <признак> <значение>`` учётной записи: смена пароля при входе, тип, публикация в контактах."""
    if attr not in ACCOUNT_FLAGS or value not in ACCOUNT_FLAGS[attr]:
        raise ValidationError(f"Недопустимый признак учётной записи: {attr}={value}.")
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        cli.run(f"SET {attr} {value}", strict=True)
        cli.run("COMMIT", strict=True)
    finally:
        cli.to_root()


def read_account_flags(cli: AxigenCLI, email: str) -> Dict[str, str]:
    """Признаки учётной записи из ``SHOW`` (что сервер показал; нет — пусто)."""
    local, domain = parse_email(email)
    enter_account(cli, domain, local)
    try:
        values = parse.parse_show(cli.run("SHOW").lines)
    finally:
        cli.to_root()
    return {attr: parse.get(values, attr).lower() for attr in ACCOUNT_FLAGS}


def domain_available_services(cli: AxigenCLI, domain: str) -> Optional[List[str]]:
    """Службы, разрешённые домену (``CONFIG adminLimits`` → availableServices), как их пишет сервер."""
    enter_domain(cli, domain)
    try:
        cli.run("CONFIG adminLimits")
        values = parse.parse_show(cli.run("SHOW").lines)
        cli.run("BACK", check=False)
    finally:
        cli.to_root()
    raw = parse.get(values, "availableServices", "available services")
    if not raw:
        return None
    return parse.parse_list_value(raw)


def match_server_folder(archive_folder: str, server: List[str]) -> Optional[str]:
    """Папка сервера для папки архива: то же имя без учёта регистра (INBOX ↔ Inbox)."""
    low = archive_folder.lower()
    for name in server:
        if name.lower() == low:
            return name
    return None


def describe_services(services: Optional[List[str]]) -> str:
    if services is None:
        return "неизвестно"
    return ", ".join(services) if services else "нет ни одной"


__all__ = ["block", "unblock", "set_password", "create_account", "remove_account", "purge_folder",
           "purge_allowed_folders", "server_folders", "random_password", "check_password",
           "require_changes_allowed", "match_server_folder", "describe_services", "get_services",
           "read_quota_kb", "set_quota", "list_aliases", "add_alias", "remove_alias", "alias_local",
           "read_contact", "set_contact", "set_account_flag", "read_account_flags", "domain_available_services",
           "set_services"]
