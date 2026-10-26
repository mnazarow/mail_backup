"""
Таблицы базы для подключений к почтовым серверам.

* ``mail_servers`` — подключения: вид сервера, название, настройки (JSON) и
  секреты (JSON, зашифрован тем же ключом, что пароли ящиков), включено ли,
  разрешены ли изменения на сервере, расписание сверки, итог последней сверки и
  проверки состояния.
* ``server_accounts`` — снимок учётных записей каждого подключения с последней
  сверки (в том же виде, что у Axigen: адрес, псевдонимы, ФИО, квота, занятое
  место, вход закрыт или открыт). Учётная запись, которой не оказалось в
  очередной сверке, не удаляется, а помечается ``deleted_at``.
* ``server_log`` — журнал сессий работы с API (последние :data:`LOG_KEEP` на
  подключение): кто, что, вызовы и ответы сервера; секреты замаскированы.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..util import utcnow_iso

LOG_KEEP = 2000
LOG_TEXT_LIMIT = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_servers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,            -- m365 | google | yandex360 | vkworkspace | zimbra | communigate | ...
    name           TEXT NOT NULL,
    settings       TEXT NOT NULL DEFAULT '{}',
    secrets_enc    TEXT NOT NULL DEFAULT '',  -- JSON с секретами, зашифрован
    enabled        INTEGER NOT NULL DEFAULT 1,
    allow_changes  INTEGER NOT NULL DEFAULT 0,
    sync_enabled   INTEGER NOT NULL DEFAULT 1,
    sync_cron      TEXT NOT NULL DEFAULT '50 4 * * *',
    sync_add_new   INTEGER NOT NULL DEFAULT 0,  -- заводить ящики в архиве для новых учётных записей
    monitor        INTEGER NOT NULL DEFAULT 1,
    last_sync_at   TEXT DEFAULT '',
    last_sync_status TEXT DEFAULT '',
    last_sync_summary TEXT DEFAULT '',
    last_check_at  TEXT DEFAULT '',
    last_check_ok  INTEGER,
    status         TEXT DEFAULT '{}',           -- последнее состояние (версия, лицензии, проблемы)
    fails          INTEGER NOT NULL DEFAULT 0,  -- проверок подряд без связи
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS server_accounts (
    server_id      INTEGER NOT NULL,
    email          TEXT NOT NULL,            -- адрес в нижнем регистре
    native_id      TEXT DEFAULT '',
    domain         TEXT DEFAULT '',
    first_name     TEXT DEFAULT '',
    last_name      TEXT DEFAULT '',
    display_name   TEXT DEFAULT '',
    aliases        TEXT DEFAULT '[]',
    quota_kb       INTEGER,
    used_bytes     INTEGER,
    messages       INTEGER,
    login_blocked  INTEGER,                  -- 1 — вход закрыт, 0 — открыт, NULL — неизвестно
    blocked_at     TEXT DEFAULT '',          -- закрыт из MailArchiver: когда и кем
    blocked_by     TEXT DEFAULT '',
    last_login     TEXT DEFAULT '',
    department     TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    phone          TEXT DEFAULT '',
    mobile         TEXT DEFAULT '',
    company        TEXT DEFAULT '',
    office         TEXT DEFAULT '',
    must_change    INTEGER,
    autoreply      INTEGER,
    forward_to     TEXT DEFAULT '[]',
    kind           TEXT DEFAULT '',
    info           TEXT DEFAULT '{}',
    first_seen     TEXT,
    last_seen      TEXT,
    deleted_at     TEXT DEFAULT '',
    PRIMARY KEY (server_id, email)
);

CREATE TABLE IF NOT EXISTS server_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id  INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    username   TEXT DEFAULT '',
    kind       TEXT DEFAULT 'read',          -- read | write | check | monitor | sync
    summary    TEXT DEFAULT '',
    ok         INTEGER NOT NULL DEFAULT 1,
    calls      INTEGER NOT NULL DEFAULT 0,
    transcript TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_server_log_server ON server_log(server_id, id);
"""

_ACCOUNT_COLUMNS = ("native_id", "domain", "first_name", "last_name", "display_name", "aliases", "quota_kb",
                    "used_bytes", "messages", "login_blocked", "last_login", "department", "title", "phone",
                    "mobile", "company", "office", "must_change", "autoreply", "forward_to", "kind", "info")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@dataclass
class Connection:
    id: int
    type: str
    name: str
    settings: Dict[str, Any] = field(default_factory=dict)
    secrets: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    allow_changes: bool = False
    sync_enabled: bool = True
    sync_cron: str = "50 4 * * *"
    sync_add_new: bool = False
    monitor: bool = True
    last_sync_at: str = ""
    last_sync_status: str = ""
    last_sync_summary: str = ""
    last_check_at: str = ""
    last_check_ok: Optional[bool] = None
    status: Dict[str, Any] = field(default_factory=dict)
    fails: int = 0
    secret_broken: bool = False

    def public(self) -> Dict[str, Any]:
        """Без секретов (для API): у секретов — только задан ли."""
        return {"id": self.id, "type": self.type, "name": self.name, "settings": dict(self.settings),
                "secrets_set": sorted(k for k, v in self.secrets.items() if v), "enabled": self.enabled,
                "allow_changes": self.allow_changes, "sync_enabled": self.sync_enabled, "sync_cron": self.sync_cron,
                "sync_add_new": self.sync_add_new, "monitor": self.monitor, "last_sync_at": self.last_sync_at,
                "last_sync_status": self.last_sync_status, "last_sync_summary": self.last_sync_summary,
                "last_check_at": self.last_check_at, "last_check_ok": self.last_check_ok, "status": self.status,
                "fails": self.fails, "secret_broken": self.secret_broken}


def _json(text: Any, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _conn_from_row(db, row) -> Connection:
    secrets: Dict[str, str] = {}
    broken = False
    if row["secrets_enc"]:
        try:
            secrets = json.loads(db.secret.decrypt(row["secrets_enc"]) or "{}")
        except Exception:  # noqa: BLE001 — ключ сменился: секреты нужно ввести заново
            broken = True
    return Connection(
        id=int(row["id"]), type=row["type"], name=row["name"], settings=_json(row["settings"], {}), secrets=secrets,
        enabled=bool(row["enabled"]), allow_changes=bool(row["allow_changes"]), sync_enabled=bool(row["sync_enabled"]),
        sync_cron=row["sync_cron"] or "50 4 * * *", sync_add_new=bool(row["sync_add_new"]),
        monitor=bool(row["monitor"]), last_sync_at=row["last_sync_at"] or "",
        last_sync_status=row["last_sync_status"] or "", last_sync_summary=row["last_sync_summary"] or "",
        last_check_at=row["last_check_at"] or "",
        last_check_ok=None if row["last_check_ok"] is None else bool(row["last_check_ok"]),
        status=_json(row["status"], {}), fails=int(row["fails"] or 0), secret_broken=broken)


# ---------------------------------------------------------------------------
#  Подключения
# ---------------------------------------------------------------------------
def list_servers(db, *, enabled_only: bool = False) -> List[Connection]:
    sql = "SELECT * FROM mail_servers"
    if enabled_only:
        sql += " WHERE enabled=1"
    try:
        rows = db.query(sql + " ORDER BY name COLLATE NOCASE, id")
    except sqlite3.OperationalError:
        return []                                   # база ещё не обновлена (старая схема)
    return [_conn_from_row(db, r) for r in rows]


def get_server(db, server_id: int) -> Optional[Connection]:
    try:
        row = db.query_one("SELECT * FROM mail_servers WHERE id=?", (int(server_id),))
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None
    return _conn_from_row(db, row) if row else None


def create_server(db, *, type: str, name: str, settings: Dict[str, Any], secrets: Dict[str, str],
                  enabled: bool = True, allow_changes: bool = False, sync_enabled: bool = True,
                  sync_cron: str = "50 4 * * *", sync_add_new: bool = False, monitor: bool = True) -> int:
    now = utcnow_iso()
    cur = db.execute(
        "INSERT INTO mail_servers(type, name, settings, secrets_enc, enabled, allow_changes, sync_enabled, sync_cron, "
        "sync_add_new, monitor, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (type, name, json.dumps(settings, ensure_ascii=False),
         db.secret.encrypt(json.dumps(secrets, ensure_ascii=False)) if secrets else "",
         int(enabled), int(allow_changes), int(sync_enabled), sync_cron, int(sync_add_new), int(monitor), now, now))
    return int(cur.lastrowid)


def update_server(db, server_id: int, *, name: str, settings: Dict[str, Any], secrets: Dict[str, str],
                  enabled: bool, allow_changes: bool, sync_enabled: bool, sync_cron: str, sync_add_new: bool,
                  monitor: bool) -> None:
    db.execute(
        "UPDATE mail_servers SET name=?, settings=?, secrets_enc=?, enabled=?, allow_changes=?, sync_enabled=?, "
        "sync_cron=?, sync_add_new=?, monitor=?, updated_at=? WHERE id=?",
        (name, json.dumps(settings, ensure_ascii=False),
         db.secret.encrypt(json.dumps(secrets, ensure_ascii=False)) if secrets else "",
         int(enabled), int(allow_changes), int(sync_enabled), sync_cron, int(sync_add_new), int(monitor),
         utcnow_iso(), int(server_id)))


def delete_server(db, server_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM server_accounts WHERE server_id=?", (int(server_id),))
        conn.execute("DELETE FROM server_log WHERE server_id=?", (int(server_id),))
        conn.execute("DELETE FROM mail_servers WHERE id=?", (int(server_id),))


def note_sync(db, server_id: int, status: str, summary: str) -> None:
    db.execute("UPDATE mail_servers SET last_sync_at=?, last_sync_status=?, last_sync_summary=? WHERE id=?",
               (utcnow_iso(), status, (summary or "")[:500], int(server_id)))


def note_check(db, server_id: int, ok: bool, status: Dict[str, Any]) -> None:
    db.execute("UPDATE mail_servers SET last_check_at=?, last_check_ok=?, status=?, "
               "fails=CASE WHEN ? THEN 0 ELSE fails + 1 END WHERE id=?",
               (utcnow_iso(), int(ok), json.dumps(status, ensure_ascii=False), int(ok), int(server_id)))


def set_setting(db, server_id: int, key: str, value: Any) -> None:
    """Одна настройка (например, отпечаток сертификата после «Доверять»)."""
    conn = get_server(db, server_id)
    if conn is None:
        return
    settings = dict(conn.settings)
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    db.execute("UPDATE mail_servers SET settings=?, updated_at=? WHERE id=?",
               (json.dumps(settings, ensure_ascii=False), utcnow_iso(), int(server_id)))


# ---------------------------------------------------------------------------
#  Снимок учётных записей
# ---------------------------------------------------------------------------
def _row_dict(row) -> Dict[str, Any]:
    out = dict(row)
    out["aliases"] = _json(out.get("aliases"), [])
    out["forward_to"] = _json(out.get("forward_to"), [])
    out["info"] = _json(out.get("info"), {})
    for key in ("login_blocked", "must_change", "autoreply"):
        if out.get(key) is not None:
            out[key] = bool(out[key])
    return out


def list_accounts(db, server_id: Optional[int] = None, *, include_deleted: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM server_accounts WHERE 1=1"
    args: List[Any] = []
    if server_id is not None:
        sql += " AND server_id=?"
        args.append(int(server_id))
    if not include_deleted:
        sql += " AND COALESCE(deleted_at, '')=''"
    try:
        rows = db.query(sql + " ORDER BY server_id, email", tuple(args))
    except sqlite3.OperationalError:
        return []
    return [_row_dict(r) for r in rows]


def get_account(db, server_id: int, email: str) -> Optional[Dict[str, Any]]:
    row = db.query_one("SELECT * FROM server_accounts WHERE server_id=? AND email=?",
                       (int(server_id), (email or "").strip().lower()))
    return _row_dict(row) if row else None


#: колонка снимка → поле учётной записи от драйвера (если имя другое)
_SOURCE = {"quota_kb": "quota_bytes"}


def _present(acc: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Только колонки, о которых драйвер что-то сообщил: чего он не прислал (место у одной учётной записи
    Microsoft 365, псевдонимы при ограничении частоты), остаётся как было в снимке."""
    return {k: v for k, v in values.items() if k == "domain" or _SOURCE.get(k, k) in acc}


def _values(acc: Dict[str, Any]) -> Dict[str, Any]:
    email = acc["email"]
    quota = acc.get("quota_bytes")
    return {
        "native_id": str(acc.get("native_id") or ""), "domain": email.partition("@")[2],
        "first_name": acc.get("first_name") or "", "last_name": acc.get("last_name") or "",
        "display_name": acc.get("display_name") or "",
        "aliases": json.dumps(acc.get("aliases") or [], ensure_ascii=False),
        "quota_kb": int(quota) // 1024 if quota else None, "used_bytes": acc.get("used_bytes"),
        "messages": acc.get("messages"),
        "login_blocked": None if acc.get("login_blocked") is None else int(bool(acc.get("login_blocked"))),
        "last_login": acc.get("last_login") or "", "department": acc.get("department") or "",
        "title": acc.get("title") or "", "phone": acc.get("phone") or "", "mobile": acc.get("mobile") or "",
        "company": acc.get("company") or "", "office": acc.get("office") or "",
        "must_change": None if acc.get("must_change") is None else int(bool(acc.get("must_change"))),
        "autoreply": None if acc.get("autoreply") is None else int(bool(acc.get("autoreply"))),
        "forward_to": json.dumps(acc.get("forward_to") or [], ensure_ascii=False), "kind": acc.get("kind") or "",
        "info": json.dumps(acc.get("info") or {}, ensure_ascii=False, default=str)[:20000],
    }


def save_snapshot(db, server_id: int, accounts: Iterable[Dict[str, Any]], *, complete: bool = True) -> Dict[str, List[str]]:
    """Записать сверку. ``complete`` — список полный: кого в нём нет, тот удалён на сервере.

    → {"new": [...], "gone": [...], "back": [...]} — адреса новых, пропавших и вернувшихся.
    """
    now = utcnow_iso()
    seen = set()
    changes: Dict[str, List[str]] = {"new": [], "gone": [], "back": []}
    with db.transaction() as conn:
        known = {r["email"]: r for r in conn.execute(
            "SELECT email, deleted_at FROM server_accounts WHERE server_id=?", (int(server_id),)).fetchall()}
        for acc in accounts:
            email = acc["email"]
            if not email or email in seen:
                continue
            seen.add(email)
            values = _values(acc)
            prev = known.get(email)
            if prev is None:
                changes["new"].append(email)
                cols = ["server_id", "email", *values.keys(), "first_seen", "last_seen", "deleted_at"]
                conn.execute(f"INSERT INTO server_accounts({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                             (int(server_id), email, *values.values(), now, now, ""))
            else:
                if prev["deleted_at"]:
                    changes["back"].append(email)
                values = _present(acc, values)
                sets = "".join(f"{k}=?, " for k in values)
                conn.execute(f"UPDATE server_accounts SET {sets}last_seen=?, deleted_at='' "
                             f"WHERE server_id=? AND email=?", (*values.values(), now, int(server_id), email))
        if complete:
            for email, prev in known.items():
                if email not in seen and not prev["deleted_at"]:
                    changes["gone"].append(email)
                    conn.execute("UPDATE server_accounts SET deleted_at=? WHERE server_id=? AND email=?",
                                 (now, int(server_id), email))
    return changes


def upsert_account(db, server_id: int, acc: Dict[str, Any]) -> None:
    """Одна учётная запись — сразу после изменения на сервере (не дожидаясь сверки)."""
    save_snapshot(db, server_id, [acc], complete=False)


def update_fields(db, server_id: int, email: str, **fields: Any) -> None:
    allowed = set(_ACCOUNT_COLUMNS) | {"blocked_at", "blocked_by", "deleted_at"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(key)
        if key in ("aliases", "forward_to", "info"):
            value = json.dumps(value or ([] if key != "info" else {}), ensure_ascii=False)
        elif key in ("login_blocked", "must_change", "autoreply") and value is not None:
            value = int(bool(value))
        sets.append(f"{key}=?")
        args.append(value)
    if sets:
        db.execute(f"UPDATE server_accounts SET {', '.join(sets)} WHERE server_id=? AND email=?",
                   (*args, int(server_id), (email or "").strip().lower()))


def note_deleted(db, server_id: int, email: str) -> None:
    update_fields(db, server_id, email, deleted_at=utcnow_iso())


def merge_info(db, server_id: int, email: str, extra: Dict[str, Any]) -> None:
    row = get_account(db, server_id, email)
    if row is None:
        return
    info = dict(row.get("info") or {})
    info.update(extra)
    update_fields(db, server_id, email, info=info)


def counts(db) -> Dict[int, Dict[str, int]]:
    out: Dict[int, Dict[str, int]] = {}
    try:
        rows = db.query("SELECT server_id, COUNT(*) AS n, SUM(CASE WHEN COALESCE(deleted_at,'')='' THEN 1 ELSE 0 END) "
                        "AS live, SUM(CASE WHEN login_blocked=1 AND COALESCE(deleted_at,'')='' THEN 1 ELSE 0 END) "
                        "AS blocked FROM server_accounts GROUP BY server_id")
    except sqlite3.OperationalError:
        return out
    for r in rows:
        out[int(r["server_id"])] = {"total": int(r["n"] or 0), "live": int(r["live"] or 0),
                                    "blocked": int(r["blocked"] or 0)}
    return out


# ---------------------------------------------------------------------------
#  Журнал
# ---------------------------------------------------------------------------
def add_log(db, server_id: int, *, username: str, kind: str, summary: str, ok: bool, calls: int,
            transcript: str) -> int:
    text = transcript or ""
    if len(text) > LOG_TEXT_LIMIT:
        text = text[:LOG_TEXT_LIMIT] + "\n… (обрезано)"
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO server_log(server_id, ts, username, kind, summary, ok, calls, transcript) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(server_id), utcnow_iso(), username or "", kind, (summary or "")[:500], int(ok), int(calls), text))
        log_id = int(cur.lastrowid)
        edge = conn.execute("SELECT id FROM server_log WHERE server_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                            (int(server_id), LOG_KEEP)).fetchone()
        if edge is not None:
            conn.execute("DELETE FROM server_log WHERE server_id=? AND id<=?", (int(server_id), int(edge[0])))
    return log_id


def list_log(db, server_id: int, limit: int = 100, *, before: int = 0, kind: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT id, server_id, ts, username, kind, summary, ok, calls FROM server_log WHERE server_id=?"
    args: List[Any] = [int(server_id)]
    if kind == "people":
        sql += " AND kind NOT IN ('monitor', 'check')"
    elif kind:
        sql += " AND kind=?"
        args.append(kind)
    if before:
        sql += " AND id<?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in db.query(sql, tuple(args))]


def get_log(db, log_id: int) -> Optional[Dict[str, Any]]:
    row = db.query_one("SELECT * FROM server_log WHERE id=?", (int(log_id),))
    return dict(row) if row else None


def delete_accounts_of(db, server_id: int, emails: Sequence[str]) -> None:
    for email in emails:
        db.execute("DELETE FROM server_accounts WHERE server_id=? AND email=?", (int(server_id), email))
