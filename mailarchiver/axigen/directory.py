"""
Учётные записи сервера Axigen: чтение через CLI и сверка с ящиками архива.

Сверка читает по каждому домену список учётных записей, а по каждой записи —
псевдонимы, ФИО, квоту, занятое место (по списку папок), службы (по ним видно,
заблокирован ли вход) и регистрационные сведения (последний вход, если сервер
их отдаёт). Каждое чтение — отдельная команда: если сервер не знает одну из
них или ответил непривычно, теряется только это поле, а не вся сверка.

Итог сверки — таблица ``axigen_accounts`` (см. :mod:`.store`), которую
показывает раздел «Сервер Axigen», а сравнение с ящиками архива — кто есть
на сервере, но не копируется; чьи ящики удалены на сервере; кто заблокирован.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..logging_setup import get_logger
from ..models import Account, AuthType
from . import parse
from . import store as axstore
from .cli import AxigenCLI, quote
from .common import AxigenCommandError, AxigenSettings, services_blocked

log = get_logger("axigen.directory")

_DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}"
                        r"[A-Za-z0-9])?)+")
#: Имя учётной записи (часть адреса до «@»). Без «/»: имя становится частью
#: пути на FTP Backup, «../другой» не должно уводить в чужой каталог.
_NAME_RE = re.compile(r"[A-Za-z0-9._+\-=#$%&'*?^`{|}~!]{1,128}")


def valid_domain(name: str) -> bool:
    return bool(_DOMAIN_RE.fullmatch(name or ""))


def valid_account_name(name: str) -> bool:
    text = name or ""
    return bool(_NAME_RE.fullmatch(text)) and text not in (".", "..") and not text.startswith(".")


# ---------------------------------------------------------------------------
#  Чтение через CLI
# ---------------------------------------------------------------------------
def _first_token(value: str) -> str:
    return (value or "").strip().split()[0] if (value or "").strip() else ""


def list_domains(cli: AxigenCLI) -> List[str]:
    reply = cli.run("LIST Domains")
    table = parse.parse_table(reply.lines)
    col = table.column("name", "domain") or (table.columns[0] if table.columns else "name")
    out = []
    for row in table.rows:
        name = _first_token(row.get(col, "")).lower()
        if valid_domain(name) and name not in out:
            out.append(name)
    return out


def enter_domain(cli: AxigenCLI, domain: str) -> None:
    cli.to_root()
    cli.run(f"UPDATE Domain name {quote(domain, 'имя домена')}")


def enter_account(cli: AxigenCLI, domain: str, name: str) -> None:
    enter_domain(cli, domain)
    cli.run(f"UPDATE Account name {quote(name, 'имя учётной записи')}")


def list_account_names(cli: AxigenCLI, domain: str) -> List[str]:
    enter_domain(cli, domain)
    try:
        reply = cli.run("LIST Accounts")
    finally:
        cli.to_root()
    table = parse.parse_table(reply.lines)
    col = table.column("name", "account") or (table.columns[0] if table.columns else "name")
    out = []
    for row in table.rows:
        name = _first_token(row.get(col, ""))
        if "@" in name:
            local, _, dom = name.rpartition("@")
            if dom.lower() == domain.lower():
                name = local
        if valid_account_name(name) and name not in out:
            out.append(name)
    return out


def _config_show(cli: AxigenCLI, section: str) -> Dict[str, str]:
    """``CONFIG <section>`` → ``SHOW`` → ``BACK``: поля вложенного контекста учётной записи."""
    cli.run(f"CONFIG {section}")
    try:
        return parse.parse_show(cli.run("SHOW").lines)
    finally:
        cli.run("BACK", check=False)


@dataclass
class FolderInfo:
    name: str
    messages: Optional[int] = None
    size: Optional[int] = None


def account_folders(cli: AxigenCLI) -> Tuple[List[FolderInfo], parse.Table]:
    """Папки учётной записи (в её контексте): имя, писем, размер — что отдал сервер."""
    reply = cli.run("LIST Folders")
    table = parse.parse_table(reply.lines)
    name_col = table.column("name", "folder") or (table.columns[0] if table.columns else "name")
    size_col = table.column("size", "bytes", "used")
    count_col = table.column("messages", "count", "total", "msgs")
    out = []
    for row in table.rows:
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        size = parse.parse_size(row.get(size_col, "")) if size_col else None
        count = parse.parse_int(row.get(count_col, "")) if count_col else None
        out.append(FolderInfo(name=name, messages=count, size=size))
    return out, table


def read_services(cli: AxigenCLI) -> Optional[List[str]]:
    """Службы учётной записи (в её контексте) — из ``CONFIG Limits`` → ``SHOW``."""
    values = _config_show(cli, "Limits")
    raw = parse.get(values, "services")
    if not raw:
        return None
    return parse.parse_list_value(raw)


def account_details(cli: AxigenCLI, domain: str, name: str) -> Dict:
    """Всё, что удалось прочитать об учётной записи. Ошибки отдельных полей — в ``errors``."""
    email = f"{name}@{domain}".lower()
    info: Dict = {"email": email, "domain": domain.lower(), "name": name, "aliases": [], "first_name": "",
                  "last_name": "", "quota_kb": None, "used_bytes": None, "messages": None, "folders": None,
                  "services": None, "login_blocked": None, "last_login": "", "info": {}, "errors": ""}
    problems: List[str] = []
    enter_account(cli, domain, name)
    try:
        steps: Sequence[Tuple[str, Callable[[], None]]] = (
            ("псевдонимы", lambda: _read_aliases(cli, info, domain)),
            ("ФИО", lambda: _read_contact(cli, info)),
            ("квота", lambda: _read_quota(cli, info)),
            ("службы", lambda: _read_services_into(cli, info)),
            ("папки", lambda: _read_folders(cli, info)),
            ("регистрационные сведения", lambda: _read_registry(cli, info)),
        )
        for what, step in steps:
            try:
                step()
            except AxigenCommandError as exc:
                # отказ не меняет контекст: вложенный контекст закрывается в _config_show
                problems.append(f"{what}: {exc.reply or exc.message}")
    finally:
        cli.to_root()
    info["errors"] = "; ".join(problems)[:1000]
    return info


def _read_aliases(cli: AxigenCLI, info: Dict, domain: str) -> None:
    table = parse.parse_table(cli.run("LIST Aliases").lines)
    col = table.column("alias", "name") or (table.columns[0] if table.columns else "name")
    out = []
    for row in table.rows:
        alias = _first_token(row.get(col, ""))
        if not alias:
            continue
        if "@" not in alias:
            alias = f"{alias}@{domain}"
        alias = alias.lower()
        if alias != info["email"] and alias not in out:
            out.append(alias)
    info["aliases"] = out


def _read_contact(cli: AxigenCLI, info: Dict) -> None:
    values = _config_show(cli, "ContactInfo")
    info["first_name"] = parse.get(values, "firstName", "first name", "givenName")[:200]
    info["last_name"] = parse.get(values, "lastName", "last name", "surname")[:200]
    display = parse.get(values, "displayName", "nickName")
    if display:
        info["info"]["display_name"] = display[:200]


def _read_quota(cli: AxigenCLI, info: Dict) -> None:
    values = _config_show(cli, "Quotas")
    raw = parse.get(values, "totalMessageSize", "messageSizeQuota", "totalMessageSizeQuota")
    number = parse.parse_int(raw)
    if number is not None and number > 0:
        info["quota_kb"] = number
    elif raw:
        info["info"]["quota"] = raw[:60]


def _read_services_into(cli: AxigenCLI, info: Dict) -> None:
    services = read_services(cli)
    info["services"] = services
    info["login_blocked"] = services_blocked(services)


def _read_folders(cli: AxigenCLI, info: Dict) -> None:
    folders, table = account_folders(cli)
    info["folders"] = len(folders)
    sizes = [f.size for f in folders if f.size is not None]
    counts = [f.messages for f in folders if f.messages is not None]
    if sizes:
        info["used_bytes"] = sum(sizes)
    if counts:
        info["messages"] = sum(counts)
    if table.columns and len(table.columns) > 1:
        info["info"]["folder_columns"] = table.titles[:12]


def _read_registry(cli: AxigenCLI, info: Dict) -> None:
    values = parse.parse_show(cli.run("SHOW RegistryInformation").lines)
    keep = {}
    last = None
    for key, value in values.items():
        if len(keep) < 40:
            keep[key[:60]] = value[:200]
        low = key.lower()
        if "login" in low and ("date" in low or "time" in low or "last" in low):
            when = parse.parse_epoch_or_date(value)
            if when is not None and (last is None or when > last):
                last = when
    if keep:
        info["info"]["registry"] = keep
    if last is not None:
        info["last_login"] = last.isoformat()


@dataclass
class SyncResult:
    domains: List[str] = field(default_factory=list)          # прочитанные целиком
    failed_domains: Dict[str, str] = field(default_factory=dict)
    accounts: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    changes: Dict[str, List[str]] = field(default_factory=dict)
    commands: int = 0


def fetch(cli: AxigenCLI, st: AxigenSettings, *, progress: Optional[Callable[[int, int, str], None]] = None,
          cancelled: Optional[Callable[[], bool]] = None, details: bool = True) -> SyncResult:
    """Прочитать учётные записи всех доменов (или доменов из настроек)."""
    res = SyncResult()
    all_domains = list_domains(cli)
    domains = [d for d in all_domains if st.domain_allowed(d)]
    missing = [d for d in st.domains if d.strip() and d.strip().lower() not in all_domains]
    for d in missing:
        res.failed_domains[d.lower()] = "домена нет на сервере (или он неактивен)"
    names: List[Tuple[str, str]] = []
    for domain in domains:
        try:
            for name in list_account_names(cli, domain):
                names.append((domain, name))
            res.domains.append(domain)
        except AxigenCommandError as exc:
            res.failed_domains[domain] = exc.reply or exc.message
            cli.to_root()
    total = len(names)
    for i, (domain, name) in enumerate(names, 1):
        if cancelled and cancelled():
            from ..errors import JobCancelled
            raise JobCancelled("Сверка с Axigen отменена.")
        if details:
            try:
                info = account_details(cli, domain, name)
            except AxigenCommandError as exc:
                info = {"email": f"{name}@{domain}".lower(), "domain": domain, "name": name,
                        "errors": f"учётная запись не открылась: {exc.reply or exc.message}"}
                res.errors.append(f"{name}@{domain}: {exc.reply or exc.message}")
                cli.to_root()
        else:
            info = {"email": f"{name}@{domain}".lower(), "domain": domain, "name": name}
        res.accounts.append(info)
        if progress and (i % 5 == 0 or i == total):
            progress(i, total, f"{name}@{domain}")
    res.commands = cli.commands
    return res


# ---------------------------------------------------------------------------
#  Сверка с ящиками архива
# ---------------------------------------------------------------------------
def is_axigen_mailbox(acc: Account, st: AxigenSettings) -> bool:
    """Ящик копируется с этого сервера Axigen (по FTP Backup или по IMAP с его адреса)."""
    if acc.auth_type == AuthType.AXIGEN_FTP:
        return True
    host = (st.imap_host or "").strip().lower()
    return bool(host) and (acc.host or "").strip().lower() == host


def match(server_rows: Sequence[Dict], accounts: Sequence[Account], st: AxigenSettings) -> Dict[str, Account]:
    """Учётная запись сервера (адрес) → ящик архива: по логину, затем по псевдонимам."""
    by_login: Dict[str, Account] = {}
    for acc in accounts:
        key = (acc.username or "").strip().lower()
        if not key:
            continue
        prev = by_login.get(key)
        # из двух ящиков с одним логином предпочитаем тот, что копируется с этого сервера
        if prev is None or (not is_axigen_mailbox(prev, st) and is_axigen_mailbox(acc, st)):
            by_login[key] = acc
    out: Dict[str, Account] = {}
    # Сначала — точное совпадение логина с адресом учётной записи (и удалённой тоже): ящик уволенного,
    # чей адрес после удаления передан коллеге псевдонимом, остаётся за своей (удалённой) учётной записью
    # и не «переезжает» к коллеге.
    own = {str(row["email"]).strip().lower() for row in server_rows}
    for row in server_rows:
        acc = by_login.get(row["email"])
        if acc is not None:
            out[row["email"]] = acc
    for row in server_rows:
        email = row["email"]
        if email in out or row.get("deleted_at"):
            continue
        for alias in row.get("aliases") or []:
            if str(alias).strip().lower() in own:
                continue                    # это адрес другой учётной записи (например, переданный коллеге)
            acc = by_login.get(alias)
            if acc is not None:
                out[email] = acc
                break
    return out


def overview(svc, st: AxigenSettings) -> Dict:
    """Строки для раздела «Сервер Axigen → Учётные записи»: сервер + архив."""
    rows = axstore.list_accounts(svc.db)
    accounts = svc.db.list_accounts()
    matched = match(rows, accounts, st)
    totals = svc.db.message_totals_by_account()
    linked = set()
    out = []
    for row in rows:
        acc = matched.get(row["email"])
        item = dict(row)
        item["mailbox"] = None
        if acc is not None:
            linked.add(acc.id)
            t = totals.get(acc.id) or {}
            item["mailbox"] = {"id": acc.id, "name": acc.name, "enabled": acc.enabled, "auth_type": acc.auth_type,
                               "host": acc.host, "same_server": is_axigen_mailbox(acc, st),
                               "last_backup_at": acc.last_backup_at, "last_backup_status": acc.last_backup_status,
                               "messages": int(t.get("messages", 0)), "bytes": int(t.get("bytes", 0)),
                               "hold_until": acc.hold_until, "dismissed_at": acc.dismissed_at}
        quota = row.get("quota_kb")
        used = row.get("used_bytes")
        item["quota_percent"] = (round(100.0 * used / (quota * 1024), 1)
                                 if quota and used is not None and quota > 0 else None)
        out.append(item)
    # ящики архива с этого сервера, которых на сервере нет вовсе (не было в сверках)
    known = {r["email"] for r in rows} | {a for r in rows for a in (r.get("aliases") or [])}
    orphans = [{"id": a.id, "name": a.name, "username": a.username, "enabled": a.enabled}
               for a in accounts if is_axigen_mailbox(a, st) and a.id not in linked
               and (a.username or "").strip().lower() not in known]
    return {"accounts": out, "orphans": orphans}


def new_mailbox(st: AxigenSettings, row: Dict, *, auth: str, template: Optional[Dict] = None) -> Account:
    """Ящик архива для учётной записи сервера (по шаблону ящиков сотрудников, если он есть)."""
    template = template or {}
    full = " ".join(x for x in (row.get("first_name") or "", row.get("last_name") or "") if x).strip()
    name = full or row["email"]
    host = st.imap_host or st.host
    return Account(name=name[:200], host=host, port=int(template.get("port") or 993),
                   username=row["email"], password="",
                   auth_type=AuthType.AXIGEN_FTP if auth == AuthType.AXIGEN_FTP else AuthType.PASSWORD,
                   security=template.get("security") or "ssl", enabled=True,
                   folder_include=list(template.get("folder_include") or []),
                   folder_exclude=list(template.get("folder_exclude") or []),
                   notes="Заведён по учётной записи сервера Axigen.",
                   retention_days=int(template.get("retention_days", -1)))
