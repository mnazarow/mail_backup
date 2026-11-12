"""
«Дела» — юридическое удержание и выгрузка писем для проверок и споров (eDiscovery, 1.20.0).

Дело объединяет ящики (сотрудников, чья переписка нужна), период и поисковый запрос.
Пока дело открыто и в нём включено удержание, архив его ящиков удерживается бессрочно:
очистка по сроку хранения, удаление ящика и архива, копия «с нуля» для них не работают —
так же, как при ручном удержании или увольнении. Закрытие дела возвращает ящику то
удержание, которое было до дела (если ящик не держит другое открытое дело).

Чтобы ни одно из мест, которые ставят и снимают удержание (увольнение, групповые действия,
удаление учётной записи на сервере), не сняло удержание дела, ``Database.set_account_hold``
для ящика из ``case_holds`` не трогает сам ящик, а запоминает новое значение — оно и
вернётся после закрытия дела.

Выгрузка дела — задание очереди ``case_export``: ZIP с письмами ``.eml`` как они лежат
в архиве (без изменений), опись ``manifest.csv`` с SHA-256 каждого письма, отчёт
``report.txt`` (кто, когда, по какому запросу, сколько писем) и ``manifest.sha256`` —
контрольная сумма описи. Сумма всего архива записывается в базу и показывается рядом
с выгрузкой: по ней получатель проверяет, что файл не меняли.

Всё — только администратору; каждое действие с делом — в аудите (``case=<номер>``).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import ValidationError
from .logging_setup import get_logger
from .util import safe_filename, utcnow_iso

log = get_logger("cases")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',   -- open | closed
    hold        INTEGER NOT NULL DEFAULT 1,     -- удерживать архив ящиков дела
    hold_scope  TEXT NOT NULL DEFAULT 'all',    -- all — весь архив ящиков; matching — письма по условиям (1.21.0)
    query       TEXT NOT NULL DEFAULT '',
    date_from   TEXT NOT NULL DEFAULT '',
    date_to     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    closed_at   TEXT NOT NULL DEFAULT '',
    closed_by   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS case_custodians (
    case_id    INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    added_at   TEXT NOT NULL DEFAULT '',
    added_by   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(case_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_case_custodians_acc ON case_custodians(account_id);
-- ящики, удерживаемые делами, и удержание, которое было у них до дела
CREATE TABLE IF NOT EXISTS case_holds (
    account_id  INTEGER PRIMARY KEY,
    prev_until  TEXT NOT NULL DEFAULT '',
    prev_reason TEXT NOT NULL DEFAULT '',
    since       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS case_exports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL,
    job_id     INTEGER,
    status     TEXT NOT NULL DEFAULT 'pending',
    path       TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    count      INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    params     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    format     TEXT NOT NULL DEFAULT 'eml',    -- eml | pst (1.21.0)
    protected  INTEGER NOT NULL DEFAULT 0      -- архив под паролем (1.21.0)
);
"""

#: Причина удержания, которую ставит дело (``accounts.hold_reason``).
HOLD_REASON = "case"
PAGE = 50
NAME_MAX = 200
DESC_MAX = 4000
QUERY_MAX = 1000
#: Сколько писем выбирать за один запрос при выгрузке.
BATCH = 500


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    if "hold_scope" not in {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}:
        conn.execute("ALTER TABLE cases ADD COLUMN hold_scope TEXT NOT NULL DEFAULT 'all'")
    have = {r[1] for r in conn.execute("PRAGMA table_info(case_exports)").fetchall()}
    for col, decl in (("format", "TEXT NOT NULL DEFAULT 'eml'"), ("protected", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:                                   # база 1.20.0
            conn.execute(f"ALTER TABLE case_exports ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
#  Дела и ящики
# ---------------------------------------------------------------------------
def _clean(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _check_dates(svc, date_from: str, date_to: str) -> Tuple[str, str]:
    from .util import parse_day
    out = []
    for value in (date_from, date_to):
        value = (value or "").strip()
        out.append(parse_day(value).isoformat() if value else "")
    svc.day_bounds_utc(out[0] or None, out[1] or None)        # «с» позже «по» — ошибка
    return out[0], out[1]


def get_case(svc, case_id: int):
    row = svc.db.query_one("SELECT * FROM cases WHERE id=?", (int(case_id),))
    if row is None:
        raise ValidationError("Дело не найдено.", code="not_found")
    return row


def custodian_ids(svc, case_id: int) -> List[int]:
    return [int(r["account_id"]) for r in svc.db.query(
        "SELECT account_id FROM case_custodians WHERE case_id=? ORDER BY account_id", (int(case_id),))]


def _existing_accounts(svc, ids: Iterable[int]) -> List[int]:
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    out: List[int] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        out += [int(r["id"]) for r in svc.db.query(
            f"SELECT id FROM accounts WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))]
    return out


def create_case(svc, actor: str, *, name: str, description: str = "", query: str = "", date_from: str = "",
                date_to: str = "", hold: bool = True, account_ids: Iterable[int] = (), hold_scope: str = "all") -> int:
    name = _clean(name, NAME_MAX)
    hold_scope = _scope(hold_scope)
    if not name:
        raise ValidationError("Укажите название дела.")
    date_from, date_to = _check_dates(svc, date_from, date_to)
    query = _clean(query, QUERY_MAX)
    _check_query(query)
    ids = _existing_accounts(svc, account_ids)
    now = utcnow_iso()
    with svc.db.transaction() as conn:
        cur = conn.execute("INSERT INTO cases(name, description, status, hold, hold_scope, query, date_from, "
                           "date_to, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (name, _clean(description, DESC_MAX), "open", 1 if hold else 0, hold_scope, query,
                            date_from, date_to, now, actor))
        case_id = int(cur.lastrowid)
        conn.executemany("INSERT OR IGNORE INTO case_custodians(case_id, account_id, added_at, added_by) "
                         "VALUES(?,?,?,?)", [(case_id, i, now, actor) for i in ids])
    sync_holds(svc)
    svc.db.add_audit(actor, "case_create", f"case={case_id} «{name}», ящиков: {len(ids)}"
                     + ((", удержание" + (" писем по условиям" if hold_scope == "matching" else "")) if hold else ""))
    return case_id


HOLD_SCOPES = ("all", "matching")


def _scope(value: str) -> str:
    value = (value or "all").strip().lower()
    if value not in HOLD_SCOPES:
        raise ValidationError("Удержание — «весь архив ящиков» или «письма по условиям дела».")
    return value


def update_case(svc, actor: str, case_id: int, fields: Dict) -> None:
    row = get_case(svc, case_id)
    if row["status"] != "open":
        raise ValidationError("Дело закрыто — изменить его нельзя.", hint="Откройте дело заново.")
    sets: Dict[str, object] = {}
    if "name" in fields:
        sets["name"] = _clean(fields["name"], NAME_MAX)
        if not sets["name"]:
            raise ValidationError("Укажите название дела.")
    if "description" in fields:
        sets["description"] = _clean(fields["description"], DESC_MAX)
    if "query" in fields:
        sets["query"] = _clean(fields["query"], QUERY_MAX)
        _check_query(str(sets["query"]))
    if "date_from" in fields or "date_to" in fields:
        sets["date_from"], sets["date_to"] = _check_dates(
            svc, fields.get("date_from", row["date_from"]), fields.get("date_to", row["date_to"]))
    if "hold" in fields:
        sets["hold"] = 1 if fields["hold"] else 0
    if "hold_scope" in fields:
        sets["hold_scope"] = _scope(fields["hold_scope"])
    if not sets:
        return
    svc.db.execute(f"UPDATE cases SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                   (*sets.values(), int(case_id)))
    if "hold" in sets:
        sync_holds(svc)
    changed = ", ".join(sorted(sets))
    svc.db.add_audit(actor, "case_update", f"case={int(case_id)} «{row['name']}»: {changed}")


def set_custodians(svc, actor: str, case_id: int, add: Iterable[int] = (), remove: Iterable[int] = ()) -> Dict:
    row = get_case(svc, case_id)
    if row["status"] != "open":
        raise ValidationError("Дело закрыто — состав ящиков не меняется.", hint="Откройте дело заново.")
    add_ids = _existing_accounts(svc, add)
    remove_ids = sorted({int(i) for i in remove})
    now = utcnow_iso()
    with svc.db.transaction() as conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO case_custodians(case_id, account_id, added_at, added_by) "
                         "VALUES(?,?,?,?)", [(int(case_id), i, now, actor) for i in add_ids])
        added = conn.total_changes - before
        before = conn.total_changes
        conn.executemany("DELETE FROM case_custodians WHERE case_id=? AND account_id=?",
                         [(int(case_id), i) for i in remove_ids])
        removed = conn.total_changes - before
    sync_holds(svc)
    if added or removed:
        svc.db.add_audit(actor, "case_custodians", f"case={int(case_id)} «{row['name']}»: +{added} −{removed}")
    return {"added": added, "removed": removed}


def close_case(svc, actor: str, case_id: int, reopen: bool = False) -> None:
    row = get_case(svc, case_id)
    if reopen:
        if row["status"] == "open":
            return
        svc.db.execute("UPDATE cases SET status='open', closed_at='', closed_by='' WHERE id=?", (int(case_id),))
    else:
        if row["status"] != "open":
            return
        svc.db.execute("UPDATE cases SET status='closed', closed_at=?, closed_by=? WHERE id=?",
                       (utcnow_iso(), actor, int(case_id)))
    sync_holds(svc)
    svc.db.add_audit(actor, "case_reopen" if reopen else "case_close", f"case={int(case_id)} «{row['name']}»")


def delete_case(svc, actor: str, case_id: int) -> None:
    row = get_case(svc, case_id)
    if row["status"] == "open":
        raise ValidationError("Удалить можно только закрытое дело.", hint="Сначала закройте дело.")
    for exp in svc.db.query("SELECT path FROM case_exports WHERE case_id=?", (int(case_id),)):
        _unlink(exp["path"])
    with svc.db.transaction() as conn:
        conn.execute("DELETE FROM case_exports WHERE case_id=?", (int(case_id),))
        conn.execute("DELETE FROM case_custodians WHERE case_id=?", (int(case_id),))
        conn.execute("DELETE FROM cases WHERE id=?", (int(case_id),))
    sync_holds(svc)
    svc.db.add_audit(actor, "case_delete", f"case={int(case_id)} «{row['name']}»")


def purge_old_exports(svc) -> int:
    """Выгрузки дел старше ``cases.export_keep_days`` дней — удалить файл и запись (1.21.0)."""
    try:
        days = int(svc.rt("cases", "export_keep_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return 0
    from datetime import timedelta
    before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    removed = 0
    for exp in svc.db.query("SELECT id, case_id, path FROM case_exports WHERE created_at < ? "
                            "AND status NOT IN ('pending', 'running')", (before,)):
        _unlink(exp["path"])
        svc.db.execute("DELETE FROM case_exports WHERE id=?", (exp["id"],))
        removed += 1
    if removed:
        svc.db.add_audit("system", "case_export_cleanup", f"удалено выгрузок дел старше {days} дн.: {removed}")
    return removed


def _unlink(path: str) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  Удержание
# ---------------------------------------------------------------------------
def held_accounts(svc) -> List[int]:
    """Ящики, которые должны удерживаться: в открытых делах с удержанием."""
    return [int(r["account_id"]) for r in svc.db.query(
        "SELECT DISTINCT cc.account_id FROM case_custodians cc JOIN cases c ON c.id = cc.case_id "
        "JOIN accounts a ON a.id = cc.account_id WHERE c.status='open' AND c.hold=1")]


def sync_holds(svc) -> Dict[str, int]:
    """Привести удержание ящиков в соответствие с открытыми делами.

    Ящик, которого не держит больше ни одно дело, получает назад удержание,
    которое было у него до дела (или то, что поставили за время дела).
    """
    from .employees import HOLD_FOREVER
    held = released = 0
    now = utcnow_iso()
    with svc.db.transaction() as conn:
        # чтение — внутри транзакции с блокировкой записи: другой процесс не изменит удержание между
        # чтением и записью (1.21.0)
        want = set(held_accounts(svc))
        have = {int(r["account_id"]): r for r in conn.execute("SELECT * FROM case_holds").fetchall()}
        for account_id in sorted(want - set(have)):
            acc = conn.execute("SELECT hold_until, hold_reason FROM accounts WHERE id=?", (account_id,)).fetchone()
            if acc is None:
                continue
            conn.execute("INSERT OR REPLACE INTO case_holds(account_id, prev_until, prev_reason, since) "
                         "VALUES(?,?,?,?)", (account_id, acc[0] or "", acc[1] or "", now))
            conn.execute("UPDATE accounts SET hold_until=?, hold_reason=?, updated_at=? WHERE id=?",
                         (HOLD_FOREVER, HOLD_REASON, now, account_id))
            held += 1
        for account_id in sorted(set(have) - want):
            prev = have[account_id]
            until, reason = prev["prev_until"] or "", (prev["prev_reason"] or "manual") if prev["prev_until"] else ""
            if reason == "dismissed":
                # сотрудника вернули на работу за время дела — удержание «уволен» не возвращается
                acc = conn.execute("SELECT dismissed_at FROM accounts WHERE id=?", (account_id,)).fetchone()
                if acc is not None and not acc[0]:
                    until, reason = "", ""
            conn.execute("UPDATE accounts SET hold_until=?, hold_reason=?, updated_at=? WHERE id=?",
                         (until, reason, now, account_id))
            conn.execute("DELETE FROM case_holds WHERE account_id=?", (account_id,))
            released += 1
    return {"held": held, "released": released}


def purge_protection(svc, account_id: int) -> Tuple[bool, List]:
    """Как дела удерживают архив ящика при очистке по сроку: (весь архив, [дела «письма по условиям»]).

    (True, []) — открытое дело удерживает весь архив; (False, [дела]) — удерживаются только письма по
    условиям этих дел, остальные чистятся по сроку хранения (1.21.0); (False, []) — дела не удерживают.
    """
    rows = svc.db.query("SELECT c.* FROM cases c JOIN case_custodians cc ON cc.case_id = c.id WHERE "
                        "cc.account_id=? AND c.status='open' AND c.hold=1", (int(account_id),))
    if not rows:
        return False, []
    if any(((r["hold_scope"] if "hold_scope" in r.keys() else "") or "all") != "matching" for r in rows):
        return True, []
    return False, list(rows)


def protected_ids(svc, case_rows: List, account_id: int, ids: List[int]) -> set:
    """Какие из писем ``ids`` ящика подходят под условия дел ``case_rows`` — их очистка не трогает."""
    out: set = set()
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    if any((r["query"] or "").strip() for r in case_rows):
        # запрос ищется по индексу: письма, до которых индексация ещё не дошла, — тоже под удержанием
        from .search import _META_WATERMARK
        watermark = int(svc.db.get_meta(_META_WATERMARK) or 0)
        out.update(int(i) for i in ids if int(i) > watermark)
    for row in case_rows:
        found = _select(svc, row, [int(account_id)], "m.id", f"AND m.id IN ({marks})", tuple(int(i) for i in ids))
        out.update(int(r["id"]) for r in found)
    return out


def cases_of_account(svc, account_id: int) -> List[Dict]:
    return [{"id": int(r["id"]), "name": r["name"], "status": r["status"], "hold": bool(r["hold"])}
            for r in svc.db.query("SELECT c.id, c.name, c.status, c.hold FROM cases c JOIN case_custodians cc "
                                  "ON cc.case_id = c.id WHERE cc.account_id=? ORDER BY c.id", (int(account_id),))]


def hold_cases_text(svc, account_id: int) -> str:
    """«дело «Иванов против…»» — какие открытые дела держат ящик (для сообщений об отказе)."""
    names = [f"«{c['name']}»" for c in cases_of_account(svc, account_id) if c["status"] == "open" and c["hold"]]
    if not names:
        return ""
    return ("делом " if len(names) == 1 else "делами ") + ", ".join(names[:3]) + (" …" if len(names) > 3 else "")


# ---------------------------------------------------------------------------
#  Письма дела
# ---------------------------------------------------------------------------
def _check_query(query: str) -> None:
    from .search import build_match
    if query and not build_match(query):
        raise ValidationError("В запросе нет ни одного слова для поиска.",
                              hint="Оставьте запрос пустым, чтобы в дело попали все письма ящиков за период.")


def _where(svc, row, ids: List[int]) -> Tuple[str, List, str]:
    """Условия отбора писем дела: (where, args, match)."""
    from .search import build_match, fts_available
    where = [f"m.account_id IN ({','.join('?' * len(ids))})"]
    args: List = list(ids)
    since, until = svc.day_bounds_utc(row["date_from"] or None, row["date_to"] or None)
    if since:
        where.append("m.internaldate >= ?")
        args.append(since)
    if until:
        where.append("m.internaldate < ?")
        args.append(until)
    match = build_match(row["query"] or "") if (row["query"] or "").strip() else ""
    if match and not fts_available(svc.db):
        from .search import _terms
        for word in _terms(row["query"]):
            where.append("(ma_lower(COALESCE(m.subject,'')) LIKE ? OR ma_lower(COALESCE(m.from_addr,'')) LIKE ?)")
            args += [f"%{word}%", f"%{word}%"]
        match = ""
    return " AND ".join(where), args, match


def _select(svc, row, ids: List[int], cols: str, tail: str, extra_args: Tuple = ()) -> List:
    from .search import FTS_TABLE
    where, args, match = _where(svc, row, ids)
    if match:
        sql = (f"SELECT {cols} FROM {FTS_TABLE} JOIN messages m ON m.id = {FTS_TABLE}.rowid "
               f"JOIN accounts a ON a.id = m.account_id WHERE {FTS_TABLE} MATCH ? AND {where} {tail}")
        params = [match] + args + list(extra_args)
    else:
        sql = f"SELECT {cols} FROM messages m JOIN accounts a ON a.id = m.account_id WHERE {where} {tail}"
        params = args + list(extra_args)
    try:
        return svc.db.query(sql, tuple(params))
    except Exception as exc:  # noqa: BLE001 - синтаксис FTS5
        raise ValidationError(f"Не удалось выполнить запрос дела: {exc}",
                              hint="Упростите запрос: слова через пробел, фраза — в кавычках.") from exc


def count_messages(svc, row, ids: Optional[List[int]] = None) -> Dict[str, int]:
    ids = custodian_ids(svc, row["id"]) if ids is None else ids
    if not ids:
        return {"count": 0, "size": 0}
    res = _select(svc, row, ids, "COUNT(*) AS c, COALESCE(SUM(m.size), 0) AS s", "")
    return {"count": int(res[0]["c"] or 0), "size": int(res[0]["s"] or 0)} if res else {"count": 0, "size": 0}


def list_messages(svc, case_id: int, offset: int = 0, limit: int = PAGE) -> Dict:
    from .search import FTS_TABLE
    row = get_case(svc, case_id)
    ids = custodian_ids(svc, case_id)
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    if not ids:
        return {"results": [], "more": False, "offset": offset}
    _, _, match = _where(svc, row, ids)
    snip = f"snippet({FTS_TABLE}, -1, char(2), char(3), '…', 12)" if match else "''"
    cols = (f"m.id, m.account_id, m.folder, m.internaldate, m.subject, m.from_addr, m.has_attach, m.size, m.gone, "
            f"a.name AS account_name, {snip} AS snip")
    rows = _select(svc, row, ids, cols, "ORDER BY m.internaldate DESC, m.id DESC LIMIT ? OFFSET ?",
                   (limit + 1, offset))
    results = [{"id": r["id"], "account_id": r["account_id"], "account_name": r["account_name"],
                "folder": r["folder"], "date": r["internaldate"], "subject": r["subject"] or "",
                "from": r["from_addr"] or "", "has_attach": bool(r["has_attach"]), "size": int(r["size"] or 0),
                "gone": bool(r["gone"]), "snippet": r["snip"] or ""} for r in rows[:limit]]
    return {"results": results, "more": len(rows) > limit, "offset": offset}


def case_dict(svc, row, *, full: bool = False) -> Dict:
    ids = custodian_ids(svc, row["id"])
    out = {"id": int(row["id"]), "name": row["name"], "description": row["description"], "status": row["status"],
           "hold": bool(row["hold"]), "hold_scope": (row["hold_scope"] if "hold_scope" in row.keys() else "") or "all",
           "query": row["query"], "date_from": row["date_from"],
           "date_to": row["date_to"], "created_at": row["created_at"], "created_by": row["created_by"],
           "closed_at": row["closed_at"], "closed_by": row["closed_by"], "custodians": len(ids),
           "exports": int(svc.db.scalar("SELECT COUNT(*) FROM case_exports WHERE case_id=?", (row["id"],)) or 0)}
    if full:
        from .accountops import hold_label
        from .search import pending_count, fts_available
        accs = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            accs += svc.db.query("SELECT a.id, a.name, a.username, a.hold_until, a.hold_reason, a.dismissed_at, "
                                 "e.full_name AS employee FROM accounts a LEFT JOIN employees e ON "
                                 "e.id = a.employee_id WHERE a.id IN (" + ",".join("?" * len(chunk)) + ")",
                                 tuple(chunk))
        out["accounts"] = [{"id": int(a["id"]), "name": a["name"], "username": a["username"],
                            "employee": a["employee"] or "", "hold": hold_label(a["hold_until"] or ""),
                            "hold_reason": a["hold_reason"] or "", "dismissed": bool(a["dismissed_at"]),
                            "messages": int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?",
                                                          (a["id"],)) or 0)}
                           for a in sorted(accs, key=lambda r: (r["name"] or "").lower())]
        out.update(count_messages(svc, row, ids))
        out["index_pending"] = pending_count(svc.db) if (row["query"] or "").strip() and fts_available(svc.db) else 0
        out["fts"] = fts_available(svc.db)
        out["export_list"] = [export_dict(e) for e in svc.db.query(
            "SELECT * FROM case_exports WHERE case_id=? ORDER BY id DESC LIMIT 50", (row["id"],))]
        marker = f"case={int(row['id'])} "
        out["history"] = [{"ts": r["ts"], "user": r["user"], "action": r["action"], "detail": r["detail"]}
                          for r in svc.db.query("SELECT ts, user, action, detail FROM audit WHERE action LIKE "
                                                "'case_%' AND detail LIKE ? ORDER BY id DESC LIMIT 100",
                                                (marker + "%",))]
    return out


def list_cases(svc) -> List[Dict]:
    return [case_dict(svc, r) for r in svc.db.query(
        "SELECT * FROM cases ORDER BY (status='open') DESC, id DESC")]


def export_dict(row) -> Dict:
    return {"id": int(row["id"]), "case_id": int(row["case_id"]), "job_id": row["job_id"], "status": row["status"],
            "size": int(row["size"] or 0), "count": int(row["count"] or 0), "sha256": row["sha256"],
            "error": row["error"], "created_at": row["created_at"], "created_by": row["created_by"],
            "file": os.path.basename(row["path"] or ""), "ready": bool(row["path"]) and os.path.exists(row["path"]),
            "format": (row["format"] if "format" in row.keys() else "") or "eml",
            "protected": bool(row["protected"]) if "protected" in row.keys() else False}


# ---------------------------------------------------------------------------
#  Выгрузка
# ---------------------------------------------------------------------------
def start_export(svc, actor: str, case_id: int, *, fmt: str = "eml", password: str = "") -> Dict:
    from .models import JobType
    row = get_case(svc, case_id)
    ids = custodian_ids(svc, case_id)
    if not ids:
        raise ValidationError("В деле нет ни одного ящика.", hint="Добавьте ящики сотрудников, чья переписка нужна.")
    if not count_messages(svc, row, ids)["count"]:
        raise ValidationError("По условиям дела не найдено ни одного письма.")
    busy = svc.db.query_one("SELECT job_id FROM case_exports WHERE case_id=? AND status IN ('pending','running')",
                            (int(case_id),))
    if busy is not None:
        raise ValidationError(f"Выгрузка этого дела уже идёт (задание #{busy['job_id']}).")
    fmt = (fmt or "eml").lower()
    if fmt not in EXPORT_FORMATS:
        raise ValidationError("Формат выгрузки — EML или PST.")
    if fmt == "pst":
        from .export.registry import resolve_engine
        resolve_engine("auto", "pst")                     # движок PST недоступен — ошибка сразу, а не в задании
    password = password or ""
    if password and len(password) < 8:
        raise ValidationError("Пароль архива — не короче 8 знаков.")
    params: Dict = {"case_id": int(case_id), "format": fmt}
    if password:
        params["password_enc"] = svc.db.secret.encrypt(password)    # в задании — зашифрованным ключом сервиса
    job_id = svc.queue.enqueue(JobType.CASE_EXPORT, None, params, priority=4, created_by=actor)
    svc.db.execute("INSERT INTO case_exports(case_id, job_id, status, created_at, created_by, params, format, "
                   "protected) VALUES(?,?,?,?,?,?,?,?)",
                   (int(case_id), int(job_id), "pending", utcnow_iso(), actor,
                    json.dumps({"query": row["query"], "date_from": row["date_from"], "date_to": row["date_to"],
                                "accounts": ids}, ensure_ascii=False), fmt, 1 if password else 0))
    svc.db.add_audit(actor, "case_export", f"case={int(case_id)} «{row['name']}»: задание #{job_id}, {fmt}"
                     f"{', под паролем' if password else ''}")
    return {"job_id": int(job_id)}


def _arc_name(folder: str) -> str:
    parts = [safe_filename(p, 60, "folder") for p in (folder or "INBOX").replace("\\", "/").split("/") if p]
    return "/".join(parts) or "INBOX"


class _HashingReader:
    """Читает поток и одновременно считает SHA-256 и размер прочитанного."""

    def __init__(self, raw) -> None:
        self.raw = raw
        self.hash = hashlib.sha256()
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n)
        self.hash.update(data)
        self.size += len(data)
        return data


class _PlainZip:
    """Обычный ZIP (deflate, ZIP64) с тем же набором методов, что у :class:`AesZipWriter`."""

    def __init__(self, path: str) -> None:
        self.zf = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)

    def write_stream(self, name: str, src, *, size_hint: int = -1, mtime=None) -> int:
        total = 0
        with self.zf.open(name, "w", force_zip64=True) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                total += len(chunk)
        return total

    def writestr(self, name: str, data: bytes, mtime=None) -> int:
        self.zf.writestr(name, data)
        return len(data)

    def write_file(self, name: str, path: str) -> int:
        self.zf.write(path, name)
        return os.path.getsize(path)

    def close(self) -> None:
        self.zf.close()

    def abort(self) -> None:
        try:
            self.zf.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  Подпись описи (Ed25519, 1.21.0)
# ---------------------------------------------------------------------------
SIGN_KEY_META = "case_sign_key"


def signing_key(svc):
    """Ключ подписи выгрузок дел (Ed25519): создаётся при первой выгрузке, хранится в базе зашифрованным."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    row = svc.db.query_one("SELECT value FROM meta WHERE key=?", (SIGN_KEY_META,))
    if row is not None and row["value"]:
        try:
            pem = svc.db.secret.decrypt(row["value"]).encode("ascii")
            return serialization.load_pem_private_key(pem, password=None)
        except Exception as exc:  # noqa: BLE001 - ключ сервиса заменили: подпись прежним ключом не восстановить
            log.warning("Ключ подписи выгрузок дел не прочитан (%s) — создаётся новый", exc)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("ascii")
    svc.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (SIGN_KEY_META, svc.db.secret.encrypt(pem)))
    svc.db.add_audit("system", "case_sign_key", f"создан ключ подписи выгрузок дел {public_key_info_of(key)['fingerprint']}")
    return key


def public_key_info_of(key) -> Dict:
    from cryptography.hazmat.primitives import serialization
    pub = key.public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fp = hashlib.sha256(raw).hexdigest()
    return {"pem": pub.public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii"),
            "fingerprint": " ".join(fp[i:i + 4] for i in range(0, 32, 4)).upper(), "sha256": fp}


def public_key_info(svc) -> Dict:
    return public_key_info_of(signing_key(svc))


EXPORT_FORMATS = ("eml", "pst")


def _epoch(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def handle_export(ctx) -> Dict:
    """Задание ``case_export``: письма дела в ZIP (или PST в ZIP) с описью, подписью и отчётом."""
    from .models import JobStatus
    from .util import human_size
    svc = ctx.services
    case_id = int(ctx.params.get("case_id") or 0)
    fmt = str(ctx.params.get("format") or "eml").lower()
    if fmt not in EXPORT_FORMATS:
        fmt = "eml"
    password = ""
    if ctx.params.get("password_enc"):
        password = svc.db.secret.decrypt(str(ctx.params["password_enc"]))
    row = get_case(svc, case_id)
    exp = svc.db.query_one("SELECT id FROM case_exports WHERE job_id=?", (ctx.job_id,))
    if exp is None:
        cur = svc.db.execute("INSERT INTO case_exports(case_id, job_id, status, created_at) VALUES(?,?,?,?)",
                             (case_id, ctx.job_id, "pending", utcnow_iso()))
        export_id = int(cur.lastrowid)
    else:
        export_id = int(exp["id"])
    svc.db.execute("UPDATE case_exports SET status='running', format=?, protected=? WHERE id=?",
                   (fmt, 1 if password else 0, export_id))
    ids = custodian_ids(svc, case_id)
    total = count_messages(svc, row, ids)["count"] if ids else 0
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"case_{case_id}_{safe_filename(row['name'], 60, 'case')}_{ts}_{ctx.job_id}"
    os.makedirs(svc.cfg.exports_dir, exist_ok=True)
    final_path = os.path.join(svc.cfg.exports_dir, base + ".zip")
    tmp_path = final_path + ".part"
    man_path = final_path + ".manifest.part"
    work_dir = ""
    ctx.event("INFO", f"Выгрузка дела «{row['name']}»: ящиков {len(ids)}, писем по условиям — {total}, формат "
                      f"{fmt.upper()}{', архив под паролем' if password else ''}.")
    done = errors = 0
    used: set = set()
    problems: List[str] = []
    ok = False
    sign = bool(svc.rt("cases", "sign_exports")) if svc.rt("cases", "sign_exports") is not None else True
    from .aeszip import AesZipWriter
    try:
        # опись — сразу на диск: у дела могут быть сотни тысяч писем (1.21.0)
        with open(man_path, "w", encoding="utf-8-sig", newline="") as man:
            mw = csv.writer(man)
            mw.writerow(["file", "sha256", "size", "account", "folder", "date_utc", "from", "subject", "message_id",
                         "archive_id", "archive_sha256", "gone_from_server", "journal_source"])
            zw = AesZipWriter(tmp_path, password) if password else _PlainZip(tmp_path)
            try:
                if fmt == "pst":
                    work_dir = tempfile.mkdtemp(prefix="case_pst_", dir=svc.cfg.tmp_dir)
                    done, errors = _export_pst(ctx, svc, row, ids, base, work_dir, zw, mw, problems, total)
                else:
                    done, errors = _export_eml(ctx, svc, row, ids, base, zw, mw, used, problems, total)
                man.flush()
                man.close()
                zw.write_file(f"{base}/manifest.csv", man_path)
                man_sha = _file_sha256(man_path)
                zw.writestr(f"{base}/manifest.sha256", f"{man_sha}  manifest.csv\n".encode())
                key_info = None
                if sign:
                    key = signing_key(svc)
                    with open(man_path, "rb") as fh:
                        signature = key.sign(fh.read())
                    key_info = public_key_info_of(key)
                    zw.writestr(f"{base}/manifest.sig", signature)
                    zw.writestr(f"{base}/signing_key.pem", key_info["pem"].encode("ascii"))
                zw.writestr(f"{base}/report.txt", _report(svc, row, ids, ctx, done, errors, problems, fmt=fmt,
                                                          protected=bool(password), key_info=key_info).encode("utf-8"))
                zw.close()
            except BaseException:
                zw.abort()
                raise
        os.replace(tmp_path, final_path)
        ok = True
    except Exception as exc:
        from .errors import JobCancelled
        status = JobStatus.CANCELLED if isinstance(exc, JobCancelled) else JobStatus.FAILED
        svc.db.execute("UPDATE case_exports SET status=?, error=? WHERE id=?",
                       (status, str(getattr(exc, "message", None) or exc)[:500], export_id))
        raise
    finally:
        if not ok:
            _unlink(tmp_path)
        _unlink(man_path)
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
    digest = _file_sha256(final_path)
    size = os.path.getsize(final_path)
    status = JobStatus.PARTIAL if errors else JobStatus.SUCCESS
    svc.db.execute("UPDATE case_exports SET status=?, path=?, size=?, count=?, sha256=?, error=? WHERE id=?",
                   (status, final_path, size, done, digest, "; ".join(problems[:5])[:1000], export_id))
    summary = (f"Выгружено писем: {done} ({human_size(size)}), не выгружено: {errors}. "
               f"SHA-256 архива: {digest}.")
    ctx.event("INFO" if not errors else "WARNING", summary)
    svc.db.add_audit("system", "case_export_done", f"case={case_id} «{row['name']}»: {done} писем, {fmt}"
                     f"{', под паролем' if password else ''}, sha256={digest}")
    return {"final_status": status, "summary": summary, "path": final_path, "count": done, "errors": errors,
            "sha256": digest}


_BATCH_COLS = ("m.id, m.account_id, m.folder, m.internaldate, m.subject, m.from_addr, m.message_id, m.stored_path, "
               "m.sha256, m.gone, m.flags, a.name AS account_name")


def _batches(ctx, svc, row, ids):
    last_id = 0
    while ids:
        if ctx.is_cancelled():
            from .errors import JobCancelled
            raise JobCancelled("Выгрузка дела отменена.")
        batch = _select(svc, row, ids, _BATCH_COLS, "AND m.id > ? ORDER BY m.id LIMIT ?", (last_id, BATCH))
        if not batch:
            return
        last_id = int(batch[-1]["id"])
        yield batch


def _journal_source(svc, msg_id: int) -> str:
    """Откуда письмо журнала пришло в архив: «2026-…; ip=…; helo=…; auth=…; tls» (пусто — не из журнала)."""
    from .journal import receipt
    rec = receipt(svc, int(msg_id))
    if not rec:
        return ""
    parts = [rec["received_at"], f"ip={rec['ip']}", f"helo={rec['helo']}"]
    if rec["auth_user"]:
        parts.append(f"auth={rec['auth_user']}")
    if rec["client_cert"]:
        parts.append(f"cert={rec['client_cert']}")
    if rec["tls"]:
        parts.append("tls")
    return "; ".join(parts)


def _manifest_row(mw, name: str, sha: str, size: int, msg, svc=None) -> None:
    mw.writerow([name, sha, size, msg["account_name"], msg["folder"], msg["internaldate"] or "",
                 msg["from_addr"] or "", msg["subject"] or "", msg["message_id"] or "", msg["id"],
                 msg["sha256"] or "", "yes" if msg["gone"] else "",
                 _journal_source(svc, msg["id"]) if svc is not None else ""])


def _problem(ctx, problems: List[str], msg_id, exc, errors: int) -> None:
    text = str(getattr(exc, "message", None) or exc)
    problems.append(f"#{msg_id}: {text}")
    if errors <= 20:
        ctx.event("ERROR", f"Письмо #{msg_id} не выгружено: {text}")


def _export_eml(ctx, svc, row, ids, base, zw, mw, used, problems, total) -> Tuple[int, int]:
    done = errors = 0
    for batch in _batches(ctx, svc, row, ids):
        for msg in batch:
            acc_dir = safe_filename(msg["account_name"] or f"account_{msg['account_id']}", 80, "account")
            day = (msg["internaldate"] or "")[:10] or "nodate"
            name = f"{base}/{acc_dir}/{_arc_name(msg['folder'])}/{day}_{msg['id']}.eml"
            if name in used:
                name = name[:-4] + f"_{len(used)}.eml"
            used.add(name)
            if not msg["stored_path"]:
                errors += 1
                problems.append(f"#{msg['id']}: нет файла копии")
                continue
            try:
                with svc.store.open_message(msg["account_id"], msg["stored_path"]) as src:
                    hr = _HashingReader(src)
                    zw.write_stream(name, hr, size_hint=-1, mtime=_epoch(msg["internaldate"]))
            except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа шифрования
                errors += 1
                _problem(ctx, problems, msg["id"], exc, errors)
                continue
            _manifest_row(mw, name[len(base) + 1:], hr.hash.hexdigest(), hr.size, msg, svc)
            done += 1
        ctx.progress(done + errors, max(total, done + errors), f"Выгружено писем: {done} из {total}")
    return done, errors


def _export_pst(ctx, svc, row, ids, base, work_dir, zw, mw, problems, total) -> Tuple[int, int]:
    """Письма дела — в файл Outlook (.pst): папки «Ящик/Папка»; опись — с суммами исходных писем."""
    from .export.base import MailItem
    from .export.registry import resolve_engine
    engine = resolve_engine("auto", "pst")
    counters = {"done": 0, "errors": 0}

    def items():
        for batch in _batches(ctx, svc, row, ids):
            for msg in batch:
                try:
                    raw = svc.store.read_message(msg["account_id"], msg["stored_path"]) if msg["stored_path"] else None
                    if raw is None:
                        raise ValidationError("нет файла копии")
                except Exception as exc:  # noqa: BLE001
                    counters["errors"] += 1
                    _problem(ctx, problems, msg["id"], exc, counters["errors"])
                    continue
                acc = safe_filename(msg["account_name"] or f"account_{msg['account_id']}", 80, "account")
                _manifest_row(mw, "mailbox.pst", hashlib.sha256(raw).hexdigest(), len(raw), msg, svc)
                counters["done"] += 1
                yield MailItem(folder=f"{acc}/{msg['folder'] or 'INBOX'}", raw=raw,
                               flags=[f for f in (msg["flags"] or "").split() if f],
                               internaldate=_epoch(msg["internaldate"]), message_id=msg["message_id"] or "",
                               size=len(raw), delimiter="/")
    out = os.path.join(work_dir, "mailbox.pst")
    options = {"pst_format": svc.rt("export", "pst_format"), "aspose_license_path": svc.rt("export",
               "aspose_license_path"), "tmp_dir": svc.cfg.tmp_dir,
               "pst_split_size_mb": svc.rt("export", "pst_split_size_mb"), "pst_display_name": row["name"]}
    res = engine.export(items(), out, options=options, progress_cb=ctx.progress, cancel_cb=ctx.is_cancelled,
                        total_hint=total)
    if ctx.is_cancelled():
        from .errors import JobCancelled
        raise JobCancelled("Выгрузка дела отменена.")
    for part in (res.parts if len(res.parts or []) > 1 else [out]):
        zw.write_file(f"{base}/{os.path.basename(part)}", part)
    return counters["done"], counters["errors"]


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _report(svc, row, ids: List[int], ctx, done: int, errors: int, problems: List[str], *, fmt: str = "eml",
            protected: bool = False, key_info: Optional[Dict] = None) -> str:
    from .search import pending_count
    from .version import __version__
    job = svc.db.get_job(ctx.job_id)
    who = (job["created_by"] if job is not None else "") or "—"
    names = [r["name"] for r in svc.db.query(
        f"SELECT name FROM accounts WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", tuple(ids))] if ids else []
    lines = [
        f"Выгрузка дела №{row['id']} «{row['name']}»",
        f"Сформирована: {utcnow_iso()} (UTC), MailArchiver {__version__}, задание #{ctx.job_id}, запустил: {who}",
        "",
        f"Описание: {row['description'] or '—'}",
        f"Запрос: {row['query'] or '(все письма ящиков)'}",
        f"Период: {row['date_from'] or 'с начала'} — {row['date_to'] or 'по сегодня'} (по местному времени сервера)",
        f"Удержание архива: {'включено' if row['hold'] else 'нет'}; состояние дела: "
        f"{'открыто' if row['status'] == 'open' else 'закрыто'}",
        f"Ящики ({len(names)}): " + ", ".join(names),
        "",
        f"Выгружено писем: {done}; не выгружено: {errors}.",
    ]
    if (row["query"] or "").strip():
        pending = pending_count(svc.db)
        if pending:
            lines.append(f"Внимание: на момент выгрузки индекс поиска не охватывал {pending} новейших писем "
                         f"архива — письма, подходящие под запрос, среди них могли не попасть в выгрузку.")
    if fmt == "pst":
        lines += ["",
                  "Письма — в файле Outlook mailbox.pst (папки «Ящик/Папка»; большой файл разбит на части).",
                  "manifest.csv — опись: SHA-256 и размер каждого письма в том виде, в каком оно хранится в архиве,",
                  "ящик, папка, дата, отправитель, тема, Message-ID. Письма в .pst преобразованы для Outlook, поэтому",
                  "их суммы с описью не совпадут: для проверки подлинности запросите выгрузку в формате EML."]
    else:
        lines += ["",
                  "Письма лежат в виде, в котором они сохранены в архиве (файлы .eml без изменений).",
                  "manifest.csv — опись: путь, SHA-256 и размер каждого файла, ящик, папка, дата, отправитель, тема,",
                  "Message-ID; archive_sha256 — сумма, записанная архивом при сохранении письма;",
                  "journal_source — для писем из журналирования: когда и откуда (IP, HELO, логин) оно принято."]
    lines.append("manifest.sha256 — SHA-256 самой описи. Проверка: sha256sum -c manifest.sha256")
    if key_info:
        lines += ["",
                  "manifest.sig — подпись описи ключом сервера архива (Ed25519), signing_key.pem — его открытый ключ.",
                  f"Отпечаток ключа: {key_info['fingerprint']} — сверьте с отпечатком, который сообщила организация",
                  "(MailArchiver → «Дела» → «Ключ подписи»): иначе подпись не доказывает, откуда выгрузка.",
                  "Проверка (OpenSSL 3): openssl pkeyutl -verify -pubin -inkey signing_key.pem -rawin "
                  "-in manifest.csv -sigfile manifest.sig"]
    if protected:
        lines += ["", "Архив зашифрован паролем (AES-256, WinZip AES): открывается 7-Zip, WinRAR, WinZip, Keka.",
                  "Пароль в архиве и в базе архива не хранится — его передают получателю отдельно."]
    if problems:
        lines += ["", "Не выгружены:"] + [f"  {p}" for p in problems[:200]]
    return "\n".join(lines) + "\n"


def export_path(svc, case_id: int, export_id: int) -> Tuple[str, str]:
    row = svc.db.query_one("SELECT * FROM case_exports WHERE id=? AND case_id=?", (int(export_id), int(case_id)))
    if row is None or not row["path"] or not os.path.exists(row["path"]):
        raise ValidationError("Файл выгрузки не найден.", code="not_found")
    real = os.path.realpath(row["path"])
    root = os.path.realpath(svc.cfg.exports_dir)
    if not real.startswith(root + os.sep):
        raise ValidationError("Файл выгрузки вне каталога выгрузок.", code="not_found")
    return real, os.path.basename(real)
