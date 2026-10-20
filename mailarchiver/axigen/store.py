"""
Таблицы базы для работы с Axigen.

* ``axigen_accounts`` — снимок учётных записей сервера с последней сверки:
  адрес, псевдонимы, ФИО, квота и занятое место, службы (вход разрешён или
  заблокирован). Учётная запись, которой в очередной сверке не оказалось,
  не удаляется, а помечается ``deleted_at`` — по ней видно, что ящик удалили
  на сервере, а архив остался.
* ``axigen_ftp_files`` — файлы писем, уже взятых через FTP Backup: имя файла в
  папке на сервере → письмо архива (UIDVALIDITY и UID в индексе писем). По ним
  следующее копирование скачивает только новые файлы.
* ``axigen_ftp_paths`` — где на FTP Backup нашёлся каталог ящика (чтобы не
  искать его каждый раз).
* ``axigen_log`` — журнал сессий CLI (последние :data:`LOG_KEEP`): что
  выполнялось на сервере, кем и с каким ответом; пароли замаскированы.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..util import utcnow_iso

#: UIDVALIDITY писем, взятых через FTP Backup Axigen (у IMAP-сервера он всегда
#: положительный; -1 и -2 заняты возвращёнными из прежних копий и импортом PST).
FTP_UIDVALIDITY = -3
#: Сколько сессий CLI хранить в журнале — по видам: изменения на сервере
#: хранятся дольше всего, плановые проверки состояния (раз в 15 минут) — меньше
#: всего, чтобы не вытеснять из журнала то, что делали люди.
LOG_KEEP = {"write": 5000, "console": 1000, "monitor": 200}
LOG_KEEP_OTHER = 500
#: Предел текста одной сессии в журнале.
LOG_TEXT_LIMIT = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS axigen_accounts (
    email          TEXT PRIMARY KEY,         -- адрес в нижнем регистре
    domain         TEXT NOT NULL,
    name           TEXT NOT NULL,            -- имя учётной записи (до «@»)
    aliases        TEXT DEFAULT '[]',
    first_name     TEXT DEFAULT '',
    last_name      TEXT DEFAULT '',
    quota_kb       INTEGER,                  -- предел объёма, КБ (NULL — не задан или неизвестен)
    used_bytes     INTEGER,                  -- занято на сервере, байт (NULL — неизвестно)
    messages       INTEGER,                  -- писем на сервере (NULL — неизвестно)
    folders        INTEGER,
    services       TEXT,                     -- JSON-список служб (NULL — неизвестен)
    login_blocked  INTEGER,                  -- 1 — вход закрыт, 0 — открыт, NULL — неизвестно
    saved_services TEXT DEFAULT '',          -- службы до блокировки из MailArchiver (для разблокировки)
    blocked_at     TEXT DEFAULT '',
    blocked_by     TEXT DEFAULT '',
    last_login     TEXT DEFAULT '',
    info           TEXT DEFAULT '{}',        -- прочие поля сервера как есть (для карточки)
    errors         TEXT DEFAULT '',          -- что не удалось прочитать при сверке
    first_seen     TEXT,
    last_seen      TEXT,
    deleted_at     TEXT DEFAULT ''           -- в последней сверке учётной записи не было
);
CREATE INDEX IF NOT EXISTS idx_axigen_accounts_domain ON axigen_accounts(domain);

CREATE TABLE IF NOT EXISTS axigen_ftp_files (
    account_id  INTEGER NOT NULL,
    folder      TEXT NOT NULL,               -- папка архива
    name        TEXT NOT NULL,               -- имя файла на FTP (как прислал сервер)
    size        INTEGER NOT NULL DEFAULT -1,
    mtime       TEXT DEFAULT '',
    uidvalidity INTEGER NOT NULL DEFAULT 0,  -- письмо в индексе архива (0 — не письмо)
    uid         INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL DEFAULT 'mail',  -- mail | other (служебный файл) | tar (пакет messages.tar)
    seen_at     TEXT,
    PRIMARY KEY(account_id, folder, name),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS axigen_ftp_paths (
    account_id  INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    found_at    TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS axigen_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    username  TEXT DEFAULT '',
    kind      TEXT DEFAULT 'read',           -- read | write | console | check | monitor | error
    summary   TEXT DEFAULT '',
    ok        INTEGER NOT NULL DEFAULT 1,
    commands  INTEGER NOT NULL DEFAULT 0,
    transcript TEXT DEFAULT ''
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
#  Журнал сессий CLI
# ---------------------------------------------------------------------------
def add_log(db, *, username: str, kind: str, summary: str, ok: bool, commands: int, transcript: str) -> int:
    text = transcript or ""
    if len(text) > LOG_TEXT_LIMIT:
        text = text[:LOG_TEXT_LIMIT] + "\n… (обрезано)"
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO axigen_log(ts, username, kind, summary, ok, commands, transcript) VALUES(?,?,?,?,?,?,?)",
            (utcnow_iso(), username or "", kind, (summary or "")[:500], 1 if ok else 0, int(commands), text))
        log_id = int(cur.lastrowid)
        keep = LOG_KEEP.get(kind, LOG_KEEP_OTHER)
        if kind in LOG_KEEP:
            where, args = "kind=?", (kind,)
        else:
            marks = ",".join("?" * len(LOG_KEEP))
            where, args = f"kind NOT IN ({marks})", tuple(LOG_KEEP)
        edge = conn.execute(f"SELECT id FROM axigen_log WHERE {where} ORDER BY id DESC LIMIT 1 OFFSET ?",
                            (*args, keep)).fetchone()
        if edge is not None:
            conn.execute(f"DELETE FROM axigen_log WHERE {where} AND id <= ?", (*args, int(edge[0])))
    return log_id


def list_log(db, limit: int = 100, *, kind: str = "", before: int = 0) -> List[Dict[str, Any]]:
    """Сессии журнала, новые сначала. ``kind`` — только этого вида (``people`` — всё, кроме проверок)."""
    sql = "SELECT id, ts, username, kind, summary, ok, commands FROM axigen_log WHERE 1=1"
    args: List[Any] = []
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
    row = db.query_one("SELECT * FROM axigen_log WHERE id=?", (int(log_id),))
    return dict(row) if row else None


# ---------------------------------------------------------------------------
#  Снимок учётных записей
# ---------------------------------------------------------------------------
_COLUMNS = ("domain", "name", "aliases", "first_name", "last_name", "quota_kb", "used_bytes", "messages",
            "folders", "services", "login_blocked", "last_login", "info", "errors")


def _row_dict(row) -> Dict[str, Any]:
    out = dict(row)
    for key, default in (("aliases", []), ("info", {})):
        try:
            out[key] = json.loads(out.get(key) or "") if out.get(key) else default
        except (TypeError, ValueError):
            out[key] = default
    try:
        out["services"] = json.loads(out["services"]) if out.get("services") else None
    except (TypeError, ValueError):
        out["services"] = None
    try:
        out["saved_services"] = json.loads(out["saved_services"]) if out.get("saved_services") else None
    except (TypeError, ValueError):
        out["saved_services"] = None
    if out.get("login_blocked") is not None:
        out["login_blocked"] = bool(out["login_blocked"])
    return out


def list_accounts(db, *, include_deleted: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM axigen_accounts"
    if not include_deleted:
        sql += " WHERE COALESCE(deleted_at, '')=''"
    return [_row_dict(r) for r in db.query(sql + " ORDER BY domain, name")]


def get_account(db, email: str) -> Optional[Dict[str, Any]]:
    row = db.query_one("SELECT * FROM axigen_accounts WHERE email=?", ((email or "").strip().lower(),))
    return _row_dict(row) if row else None


def save_snapshot(db, accounts: Sequence[Dict[str, Any]], listed_domains: Iterable[str]) -> Dict[str, List[str]]:
    """Записать итог сверки. Возвращает изменения: new, gone, back (вернулись), changed.

    Учётные записи доменов из ``listed_domains`` (их список прочитан целиком),
    которых нет в ``accounts``, помечаются удалёнными. Домены, список которых не
    прочитался, не трогаются: сбой чтения не должен «удалять» учётные записи.
    """
    now = utcnow_iso()
    listed = {d.strip().lower() for d in listed_domains}
    seen = set()
    changes: Dict[str, List[str]] = {"new": [], "gone": [], "back": [], "changed": []}
    existing = {r["email"]: r for r in db.query("SELECT * FROM axigen_accounts")}
    with db.transaction() as conn:
        for acc in accounts:
            email = acc["email"].strip().lower()
            seen.add(email)
            values = []
            for col in _COLUMNS:
                value = acc.get(col)
                if col in ("aliases", "info"):
                    value = json.dumps(value or ([] if col == "aliases" else {}), ensure_ascii=False)
                elif col == "services":
                    value = None if value is None else json.dumps(list(value), ensure_ascii=False)
                elif col == "login_blocked":
                    value = None if value is None else (1 if value else 0)
                values.append(value)
            old = existing.get(email)
            if old is not None and old["blocked_at"] and acc.get("login_blocked") is None \
                    and acc.get("services") is not None and not list(acc.get("services")):
                # пустой список служб у учётной записи, которую закрыл MailArchiver, — вход закрыт
                values[_COLUMNS.index("login_blocked")] = 1
            if old is None:
                conn.execute(
                    f"INSERT INTO axigen_accounts(email, {', '.join(_COLUMNS)}, first_seen, last_seen, deleted_at) "
                    f"VALUES(?, {', '.join('?' * len(_COLUMNS))}, ?, ?, '')",
                    (email, *values, now, now))
                changes["new"].append(email)
                continue
            if old["deleted_at"]:
                changes["back"].append(email)
            elif any((old[col] if col != "login_blocked" else old[col]) != values[i]
                     for i, col in enumerate(_COLUMNS) if col in ("services", "login_blocked", "aliases")):
                changes["changed"].append(email)
            conn.execute(
                f"UPDATE axigen_accounts SET {', '.join(f'{c}=?' for c in _COLUMNS)}, last_seen=?, deleted_at='' "
                f"WHERE email=?", (*values, now, email))
        for email, old in existing.items():
            if email in seen or old["deleted_at"] or old["domain"].lower() not in listed:
                continue
            conn.execute("UPDATE axigen_accounts SET deleted_at=? WHERE email=?", (now, email))
            changes["gone"].append(email)
    return changes


def note_blocked(db, email: str, *, saved_services: Optional[List[str]], user: str, services: List[str]) -> None:
    db.execute("UPDATE axigen_accounts SET saved_services=?, blocked_at=?, blocked_by=?, services=?, login_blocked=1 "
               "WHERE email=?",
               (json.dumps(saved_services, ensure_ascii=False) if saved_services is not None else "",
                utcnow_iso(), user or "", json.dumps(services, ensure_ascii=False), email.lower()))


def note_unblocked(db, email: str, services: List[str]) -> None:
    db.execute("UPDATE axigen_accounts SET saved_services='', blocked_at='', blocked_by='', services=?, "
               "login_blocked=0 WHERE email=?", (json.dumps(services, ensure_ascii=False), email.lower()))


def note_services(db, email: str, services: List[str], blocked: Optional[bool]) -> None:
    db.execute("UPDATE axigen_accounts SET services=?, login_blocked=? WHERE email=?",
               (json.dumps(services, ensure_ascii=False), None if blocked is None else (1 if blocked else 0),
                email.lower()))


#: Поля снимка, которые групповые действия обновляют после изменения на сервере.
_UPDATABLE = ("quota_kb", "aliases", "first_name", "last_name", "services", "login_blocked")


def update_fields(db, email: str, **fields: Any) -> None:
    """Обновить в снимке поля учётной записи, которые только что изменили на сервере.

    Так раздел показывает новое значение сразу, не дожидаясь следующей сверки.
    """
    sets, args = [], []
    for key, value in fields.items():
        if key not in _UPDATABLE:
            raise ValueError(f"поле {key} так не обновляется")
        if key == "aliases":
            value = json.dumps(list(value or []), ensure_ascii=False)
        elif key == "services":
            value = None if value is None else json.dumps(list(value), ensure_ascii=False)
        elif key == "login_blocked":
            value = None if value is None else (1 if value else 0)
        sets.append(f"{key}=?")
        args.append(value)
    if not sets:
        return
    db.execute(f"UPDATE axigen_accounts SET {', '.join(sets)} WHERE email=?", (*args, (email or "").lower()))


def merge_info(db, email: str, values: Dict[str, Any]) -> None:
    """Дописать в поле ``info`` (сведения для карточки) прочитанные с сервера значения."""
    row = db.query_one("SELECT info FROM axigen_accounts WHERE email=?", ((email or "").lower(),))
    if row is None:
        return
    try:
        info = json.loads(row["info"] or "{}")
    except (TypeError, ValueError):
        info = {}
    if not isinstance(info, dict):
        info = {}
    info.update(values)
    db.execute("UPDATE axigen_accounts SET info=? WHERE email=?",
               (json.dumps(info, ensure_ascii=False)[:20000], (email or "").lower()))


def note_deleted(db, email: str) -> None:
    db.execute("UPDATE axigen_accounts SET deleted_at=? WHERE email=?", (utcnow_iso(), email.lower()))


def upsert_created(db, *, email: str, domain: str, name: str, first_name: str = "", last_name: str = "",
                   services: Optional[List[str]] = None) -> None:
    now = utcnow_iso()
    db.execute(
        "INSERT INTO axigen_accounts(email, domain, name, first_name, last_name, services, login_blocked, "
        "first_seen, last_seen, deleted_at) VALUES(?,?,?,?,?,?,?,?,?, '') "
        "ON CONFLICT(email) DO UPDATE SET deleted_at='', last_seen=excluded.last_seen, "
        "first_name=excluded.first_name, last_name=excluded.last_name",
        (email.lower(), domain.lower(), name, first_name, last_name,
         json.dumps(services, ensure_ascii=False) if services is not None else None,
         None, now, now))


# ---------------------------------------------------------------------------
#  Файлы FTP Backup
# ---------------------------------------------------------------------------
def ftp_files(db, account_id: int, folder: str) -> Dict[str, sqlite3.Row]:
    rows = db.query("SELECT * FROM axigen_ftp_files WHERE account_id=? AND folder=?", (int(account_id), folder))
    return {r["name"]: r for r in rows}


def ftp_present_keys(db, account_id: int, folder: str) -> set:
    """(UIDVALIDITY, UID) писем папки, которые есть в архиве или вычищены по сроку хранения."""
    rows = db.query(
        "SELECT uidvalidity, uid FROM messages WHERE account_id=? AND folder=? "
        "UNION SELECT uidvalidity, uid FROM retired_uids WHERE account_id=? AND folder=?",
        (int(account_id), folder, int(account_id), folder))
    return {(int(r["uidvalidity"]), int(r["uid"])) for r in rows}


def ftp_save(db, rows: Sequence[Tuple]) -> None:
    """Записать файлы: (account_id, folder, name, size, mtime, uidvalidity, uid, kind)."""
    if not rows:
        return
    now = utcnow_iso()
    db.executemany(
        "INSERT INTO axigen_ftp_files(account_id, folder, name, size, mtime, uidvalidity, uid, kind, seen_at) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id, folder, name) DO UPDATE SET size=excluded.size, "
        "mtime=excluded.mtime, uidvalidity=excluded.uidvalidity, uid=excluded.uid, kind=excluded.kind, "
        "seen_at=excluded.seen_at", [(*row, now) for row in rows])


def ftp_forget(db, account_id: int, folder: str, names: Sequence[str]) -> int:
    """Забыть файлы, которых на сервере больше нет (письма в архиве остаются)."""
    done = 0
    names = list(names)
    for start in range(0, len(names), 500):
        chunk = names[start:start + 500]
        marks = ",".join("?" * len(chunk))
        cur = db.execute(f"DELETE FROM axigen_ftp_files WHERE account_id=? AND folder=? AND name IN ({marks})",
                         (int(account_id), folder, *chunk))
        done += int(cur.rowcount or 0)
    return done


def ftp_forget_retired(db, account_id: int, folder: str, uids: Sequence[int]) -> None:
    """Вычищенные по сроку письма, файлов которых на сервере больше нет, помнить незачем."""
    uids = [int(u) for u in uids]
    for start in range(0, len(uids), 500):
        chunk = uids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        db.execute(f"DELETE FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity=? AND uid IN ({marks})",
                   (int(account_id), folder, FTP_UIDVALIDITY, *chunk))


def ftp_folders(db, account_id: int) -> List[str]:
    return [r["folder"] for r in db.query("SELECT DISTINCT folder FROM axigen_ftp_files WHERE account_id=?",
                                          (int(account_id),))]


def ftp_max_uid(db, account_id: int, folder: str) -> int:
    a = int(db.scalar("SELECT MAX(uid) FROM messages WHERE account_id=? AND folder=? AND uidvalidity=?",
                      (int(account_id), folder, FTP_UIDVALIDITY)) or 0)
    b = int(db.scalar("SELECT MAX(uid) FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity=?",
                      (int(account_id), folder, FTP_UIDVALIDITY)) or 0)
    c = int(db.scalar("SELECT MAX(uid) FROM axigen_ftp_files WHERE account_id=? AND folder=? AND uidvalidity=?",
                      (int(account_id), folder, FTP_UIDVALIDITY)) or 0)
    return max(a, b, c)


def ftp_path(db, account_id: int) -> str:
    return str(db.scalar("SELECT path FROM axigen_ftp_paths WHERE account_id=?", (int(account_id),)) or "")


def set_ftp_path(db, account_id: int, path: str) -> None:
    db.execute("INSERT INTO axigen_ftp_paths(account_id, path, found_at) VALUES(?,?,?) "
               "ON CONFLICT(account_id) DO UPDATE SET path=excluded.path, found_at=excluded.found_at",
               (int(account_id), path, utcnow_iso()))


def forget_ftp_path(db, account_id: int) -> None:
    db.execute("DELETE FROM axigen_ftp_paths WHERE account_id=?", (int(account_id),))


def ftp_stats(db, account_id: int) -> Dict[str, int]:
    row = db.query_one("SELECT COUNT(*) AS files, SUM(CASE WHEN kind='mail' THEN 1 ELSE 0 END) AS mail "
                       "FROM axigen_ftp_files WHERE account_id=?", (int(account_id),))
    return {"files": int(row["files"] or 0), "mail": int(row["mail"] or 0)} if row else {"files": 0, "mail": 0}
