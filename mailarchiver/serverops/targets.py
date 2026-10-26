"""
Ящики на сервере — то, над чем выполняются групповые действия раздела.

Ящик на сервере бывает трёх видов:

* **учётная запись Axigen** (ключ ``ax:<адрес>``) — из последней сверки с
  сервером (таблица ``axigen_accounts``); с ней может быть связан ящик архива
  (по логину или псевдониму);
* **учётная запись подключённого почтового сервера** (ключ
  ``ms:<номер подключения>:<адрес>``) — Microsoft 365, Google Workspace,
  Яндекс 360, Zimbra и другие (раздел «Почтовые серверы», таблица
  ``server_accounts``); с ней тоже может быть связан ящик архива;
* **ящик архива на любом сервере** (ключ ``mb:<номер>``) — ящик MailArchiver,
  которому не нашлось учётной записи ни на Axigen, ни на подключённых серверах.
  С ним работают по IMAP — паролем ящика или входом администратора.

Ключ, а не номер строки: список строится заново при каждой проверке и в
задании, и ящик, который за это время удалили или пересоздали, не подменится
другим.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from ..models import Account, AuthType

KEY_AX = "ax:"
KEY_MB = "mb:"
KEY_MS = "ms:"
#: Сколько ящиков принимаем за одну операцию.
MAX_TARGETS = 20000


@dataclass
class Target:
    key: str
    email: str
    name: str
    host: str
    #: строка снимка учётной записи Axigen (None — ящик не на Axigen или Axigen не настроен)
    axigen: Optional[Dict] = None
    #: связанный ящик архива (None — в архиве ящика нет)
    mailbox: Optional[Account] = None
    #: строка снимка учётной записи подключённого почтового сервера (None — не такая учётная запись)
    server: Optional[Dict] = None
    #: подключение (:class:`mailarchiver.mailservers.store.Connection`) — для учётных записей подключений
    conn: Optional[object] = None

    @property
    def kind(self) -> str:
        if self.axigen is not None:
            return "axigen"
        return "server" if self.server is not None else "imap"

    @property
    def server_id(self) -> Optional[int]:
        return getattr(self.conn, "id", None) if self.server is not None else None

    @property
    def row(self) -> Optional[Dict]:
        """Снимок учётной записи на сервере (Axigen или подключения)."""
        return self.axigen if self.axigen is not None else self.server

    @property
    def deleted(self) -> bool:
        row = self.row
        return bool(row and row.get("deleted_at"))


def imap_problem(svc, acc: Optional[Account]) -> str:
    """Почему в ящик нельзя войти по IMAP (пусто — можно)."""
    if acc is None:
        return "ящика нет в архиве — войти по IMAP нечем (пароля нет)"
    if acc.secret_broken:
        return "сохранённый пароль ящика не расшифровывается (сменился secret.key) — введите пароль заново"
    if acc.auth_type == AuthType.PASSWORD:
        return "" if acc.password else "у ящика нет сохранённого пароля"
    if acc.auth_type == AuthType.MASTER:
        master = svc.master_credentials()
        if not master:
            return "вход администратора почты не настроен («Настройки → Вход через администратора почты»)"
        if (master.get("host") or "").strip().lower() != (acc.host or "").strip().lower():
            return f"вход администратора почты разрешён только для сервера {master.get('host') or '—'}"
        return ""
    if acc.auth_type == AuthType.OAUTH2:
        return "" if acc.oauth_refresh_token else "у ящика нет токена OAuth2 — войдите заново"
    if acc.auth_type == AuthType.AXIGEN_FTP:
        return "" if acc.password else ("ящик копируется через FTP Backup Axigen без пароля — по IMAP в него не "
                                        "войти; задайте пароль в карточке ящика")
    if acc.auth_type == AuthType.SERVER:
        from ..mailservers.service import imap_problem as server_problem
        return server_problem(svc, acc)
    return f"неизвестный способ входа: {acc.auth_type}"


def _axigen_settings(svc):
    from ..axigen.common import load_settings
    try:
        return load_settings(svc)
    except Exception:  # noqa: BLE001
        return None


def load(svc, *, include_deleted: bool = True) -> List[Target]:
    """Все ящики на сервере: учётные записи Axigen и ящики архива без учётной записи."""
    accounts = svc.db.list_accounts()
    st = _axigen_settings(svc)
    out: List[Target] = []
    linked = set()
    if st is not None and st.enabled:
        from ..axigen import directory
        from ..axigen import store as axstore
        rows = axstore.list_accounts(svc.db)
        # ящики, которые копируются через подключение к другому почтовому серверу, — не Axigen
        matched = directory.match(rows, [a for a in accounts if a.auth_type != AuthType.SERVER], st)
        host = st.imap_host or st.host
        for row in rows:
            acc = matched.get(row["email"])
            if acc is not None:
                linked.add(acc.id)
            if row.get("deleted_at") and not include_deleted:
                continue
            full = " ".join(x for x in (row.get("first_name") or "", row.get("last_name") or "") if x).strip()
            out.append(Target(key=KEY_AX + row["email"], email=row["email"], name=full or (acc.name if acc else "")
                              or row["email"], host=host or "", axigen=row, mailbox=acc))
    out.extend(_server_targets(svc, accounts, linked, include_deleted))
    for acc in accounts:
        if acc.id in linked:
            continue
        out.append(Target(key=f"{KEY_MB}{acc.id}", email=(acc.username or "").strip(), name=acc.name or acc.username,
                          host=acc.host or "", mailbox=acc))
    return out


def _server_targets(svc, accounts: Sequence[Account], linked: Set[int], include_deleted: bool) -> List[Target]:
    """Учётные записи подключённых почтовых серверов (``linked`` — занятые ящики архива, дополняется)."""
    from ..mailservers import match as msmatch
    from ..mailservers import store as msstore
    from ..mailservers.service import driver_class, target_key
    conns = [c for c in msstore.list_servers(svc.db) if c.enabled]
    if not conns:
        return []
    rows_by_server: Dict[int, List[Dict]] = {}
    hosts: Dict[int, str] = {}
    for conn in conns:
        rows_by_server[conn.id] = msstore.list_accounts(svc.db, conn.id)
        try:
            hosts[conn.id] = (driver_class(conn.type)(conn, None).imap_server()[0] or "").strip().lower()
        except Exception:  # noqa: BLE001
            hosts[conn.id] = ""
    matched = msmatch.match(rows_by_server, accounts, hosts, taken=linked)
    out: List[Target] = []
    for conn in conns:
        for row in rows_by_server[conn.id]:
            acc = matched.get((conn.id, row["email"]))
            if acc is not None:
                linked.add(acc.id)
            if row.get("deleted_at") and not include_deleted:
                continue
            full = (row.get("display_name") or " ".join(x for x in (row.get("first_name") or "",
                                                                     row.get("last_name") or "") if x)).strip()
            out.append(Target(key=target_key(conn.id, row["email"]), email=row["email"],
                              name=full or (acc.name if acc else "") or row["email"], host=hosts.get(conn.id, ""),
                              mailbox=acc, server=row, conn=conn))
    return out


def resolve(svc, keys: Sequence[str]) -> Dict[str, Target]:
    """Ящики по ключам (свежие данные). Неизвестных ключей в ответе нет."""
    wanted = set(keys)
    return {t.key: t for t in load(svc) if t.key in wanted}


def refresh(svc, t: Target) -> Optional[Target]:
    """Тот же ящик с данными, прочитанными заново (None — его больше нет)."""
    if t.key.startswith(KEY_AX):
        from ..axigen import store as axstore
        row = axstore.get_account(svc.db, t.email)
        if row is None:
            return None
        acc = svc.db.get_account(t.mailbox.id) if t.mailbox is not None else None
        return Target(key=t.key, email=t.email, name=t.name, host=t.host, axigen=row, mailbox=acc)
    if t.key.startswith(KEY_MS):
        from ..mailservers import store as msstore
        conn = msstore.get_server(svc.db, t.server_id) if t.server_id else None
        row = msstore.get_account(svc.db, conn.id, t.email) if conn is not None else None
        if row is None or not conn.enabled:
            return None
        acc = svc.db.get_account(t.mailbox.id) if t.mailbox is not None else None
        return Target(key=t.key, email=t.email, name=t.name, host=t.host, mailbox=acc, server=row, conn=conn)
    acc = svc.db.get_account(int(t.key[len(KEY_MB):]))
    if acc is None:
        return None
    return Target(key=t.key, email=(acc.username or "").strip(), name=acc.name or acc.username, host=acc.host or "",
                  mailbox=acc)


def clean_keys(keys: Optional[Sequence]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in keys or []:
        key = str(raw or "").strip()
        if not key or len(key) > 400 or key in seen:
            continue
        if not (key.startswith(KEY_AX) or key.startswith(KEY_MB) or key.startswith(KEY_MS)):
            continue
        if key.startswith(KEY_MB) and not key[len(KEY_MB):].isdigit():
            continue
        if key.startswith(KEY_MS):
            from ..mailservers.service import parse_key
            if parse_key(key) is None:
                continue
        seen.add(key)
        out.append(key)
    if len(out) > MAX_TARGETS:
        from ..errors import ValidationError
        raise ValidationError(f"Слишком много ящиков за одну операцию (больше {MAX_TARGETS}).")
    return out


AUTH_SHORT = {AuthType.PASSWORD: "по паролю", AuthType.MASTER: "вход администратора почты",
              AuthType.OAUTH2: "OAuth2", AuthType.AXIGEN_FTP: "FTP Backup Axigen",
              AuthType.SERVER: "через подключение"}


def dismissed_map(svc) -> Dict[str, str]:
    """Уволенные сотрудники: адрес (в нижнем регистре) → когда уволен."""
    out: Dict[str, str] = {}
    try:
        rows = svc.db.query("SELECT email, dismissed_at FROM employees WHERE dismissed_at IS NOT NULL "
                            "AND dismissed_at != '' AND email IS NOT NULL AND email != ''")
    except Exception:  # noqa: BLE001 — справочника сотрудников может не быть в старой базе
        return out
    for r in rows:
        out[str(r["email"]).strip().lower()] = r["dismissed_at"]
    return out


def own_addresses(items: Sequence[Target]) -> Set[str]:
    """Адреса учётных записей Axigen (и удалённых) и псевдонимы удалённых: у живой учётной записи такой
    псевдоним — чужой адрес, переданный коллеге."""
    from ..axigen.directory import inherited_addresses
    return inherited_addresses([t.row for t in items if t.row is not None])


def dismissed_at(t: Target, dismissed: Dict[str, str], own: Optional[Set[str]] = None) -> str:
    """Когда уволен сотрудник этого ящика: по карточке ящика архива или по «Сотрудникам» (адрес, псевдоним).

    Псевдоним, который сам — адрес другой учётной записи (адрес уволенного, переданный коллеге после
    удаления), коллегу уволенным не делает.
    """
    if t.mailbox is not None and t.mailbox.dismissed_at:
        return t.mailbox.dismissed_at
    own = own or set()
    for i, address in enumerate([t.email] + list((t.row or {}).get("aliases") or [])):
        address = str(address or "").strip().lower()
        if i and address in own:
            continue
        when = dismissed.get(address)
        if when:
            return when
    return ""


def row_dict(svc, t: Target, totals: Dict[int, Dict[str, int]], keeps: Dict[int, bool],
             dismissed: Optional[Dict[str, str]] = None, trends: Optional[Dict[str, Dict]] = None,
             own: Optional[Set[str]] = None, last: Optional[Dict[str, Dict]] = None) -> Dict:
    """Строка таблицы раздела."""
    ax = t.axigen or {}
    acc = t.mailbox
    item = {"key": t.key, "email": t.email, "name": t.name, "host": t.host, "kind": t.kind,
            "deleted": t.deleted, "dismissed_at": dismissed_at(t, dismissed or {}, own)}
    trend = (trends or {}).get(t.key)
    item["trend"] = ({"per_month": int(trend["per_month"]), "days_to_quota": trend["days_to_quota"],
                      "since": trend["since"], "span_days": trend["span_days"], "used": trend["used"],
                      "quota": trend["quota"]} if trend else None)
    # последний замер места (у ящиков не на Axigen — из «Отчёта»)
    item["usage"] = (last or {}).get(t.key)
    if t.server is not None:
        from ..mailservers.service import driver_class
        ax = t.server
        try:
            cls = driver_class(t.conn.type)
            label, caps = cls.label, sorted(cls.caps)
        except Exception:  # noqa: BLE001
            label, caps = t.conn.type, []
        item["server"] = {"id": t.conn.id, "name": t.conn.name, "type": t.conn.type, "label": label, "caps": caps,
                          "allow_changes": bool(t.conn.allow_changes)}
        item.update({"autoreply": ax.get("autoreply"), "forward_to": ax.get("forward_to") or [],
                     "department": ax.get("department") or "", "title": ax.get("title") or "",
                     "account_kind": ax.get("kind") or ""})
    if t.row is not None:
        quota = ax.get("quota_kb")
        used = ax.get("used_bytes")
        item.update({
            "login_blocked": ax.get("login_blocked"), "blocked_at": ax.get("blocked_at") or "",
            "saved_services": bool(ax.get("saved_services")), "services": ax.get("services"),
            "quota_kb": quota, "used_bytes": used, "messages_server": ax.get("messages"),
            "quota_percent": (round(100.0 * used / (quota * 1024), 1) if quota and used is not None and quota > 0
                              else None),
            "last_login": ax.get("last_login") or "", "aliases": ax.get("aliases") or [],
            "first_name": ax.get("first_name") or "", "last_name": ax.get("last_name") or "",
            "errors": ax.get("errors") or "", "deleted_at": ax.get("deleted_at") or ""})
    if acc is not None:
        tot = totals.get(acc.id) or {}
        problem = imap_problem(svc, acc)
        item["mailbox"] = {"id": acc.id, "name": acc.name, "enabled": acc.enabled, "auth_type": acc.auth_type,
                           "auth": AUTH_SHORT.get(acc.auth_type, acc.auth_type), "host": acc.host,
                           "imap": not problem, "imap_problem": problem,
                           "messages": int(tot.get("messages", 0)), "bytes": int(tot.get("bytes", 0)),
                           "last_backup_at": acc.last_backup_at, "last_backup_status": acc.last_backup_status,
                           "login_status": acc.login_status, "keeps_all": bool(keeps.get(acc.id)),
                           "hold_until": acc.hold_until, "dismissed_at": acc.dismissed_at}
    else:
        item["mailbox"] = None
    return item


def keeps_everything_map(svc, accounts: Sequence[Account]) -> Dict[int, bool]:
    """Хранит ли архив ящика все письма навсегда (для отбора и подсказок)."""
    from ..axigen.jobs import archive_keeps_everything
    out = {}
    for acc in accounts:
        try:
            out[acc.id] = archive_keeps_everything(svc, acc)[0]
        except Exception:  # noqa: BLE001
            out[acc.id] = False
    return out


def overview(svc) -> Dict:
    """Всё для раздела «Ящики на сервере»: строки таблицы и состояние Axigen."""
    import json as _json
    from ..models import JobType
    items = load(svc)
    accounts = [t.mailbox for t in items if t.mailbox is not None]
    totals = svc.db.message_totals_by_account()
    keeps = keeps_everything_map(svc, accounts)
    from .usage import trends as usage_trends
    dismissed = dismissed_map(svc)
    trends = usage_trends(svc.db)
    own = own_addresses(items)
    from .usage import latest
    last = latest(svc.db)
    rows = [row_dict(svc, t, totals, keeps, dismissed, trends, own, last) for t in items]
    st = _axigen_settings(svc)
    ax = {"enabled": bool(st and st.enabled), "configured": bool(st and st.configured),
          "allow_changes": bool(st and st.allow_changes), "host": (st.host if st else "") or "",
          "last_sync": None, "sync_running": False}
    if st is not None and st.enabled:
        last = svc.db.query_one(
            "SELECT id, status, finished_at, result FROM jobs WHERE type=? AND status IN ('success','partial','failed') "
            "ORDER BY id DESC LIMIT 1", (JobType.AXIGEN_SYNC,))
        if last is not None:
            try:
                summary = (_json.loads(last["result"] or "{}") or {}).get("summary", "")
            except (TypeError, ValueError):
                summary = ""
            ax["last_sync"] = {"job_id": last["id"], "status": last["status"], "finished_at": last["finished_at"],
                               "summary": summary}
        ax["sync_running"] = any(j["type"] == JobType.AXIGEN_SYNC for j in svc.db.active_jobs())
    running = [dict(id=j["id"], status=j["status"]) for j in svc.db.active_jobs() if j["type"] == JobType.SERVER_ACTION]
    return {"rows": rows, "axigen": ax, "running": running, "servers": servers_overview(svc),
            "master": svc.master_credentials() is not None}


def servers_overview(svc) -> List[Dict]:
    """Включённые подключения к почтовым серверам — для отбора и формы создания в разделе."""
    from ..errors import ValidationError
    from ..mailservers import store as msstore
    from ..mailservers.service import driver_class
    out = []
    for c in msstore.list_servers(svc.db, enabled_only=True):
        try:
            cls = driver_class(c.type)
        except ValidationError:
            continue
        out.append({"id": c.id, "name": c.name, "type": c.type, "label": cls.label, "caps": sorted(cls.caps),
                    "allow_changes": c.allow_changes, "password_rule": cls.password_rule,
                    "last_sync_at": c.last_sync_at, "last_sync_status": c.last_sync_status,
                    "last_sync_summary": c.last_sync_summary})
    return out
