"""
Общие и делегированные ящики (1.22.0).

Администратор открывает сотруднику доступ к чужому ящику: секретарю — ящик руководителя, отделу — общий
ящик ``info@``. Сотрудник видит такой ящик рядом со своими (вошёл паролем любого своего ящика): архив,
поиск и «Почта». Права:

* ``read`` — только читать: письма, вложения, поиск; менять ящик на сервере, отправлять от его имени,
  выгружать архив и запускать копирование нельзя;
* ``full`` — как свой ящик: «Почта» целиком, в том числе письма от имени ящика, выгрузка, копирование.

Доступ действует, пока сотрудник числится работающим и не наступила дата «до» (если задана); снятие
доступа закрывает его сразу (список считается на каждом запросе). Все действия делегата в аудите — под
его логином, с названием ящика.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from .errors import ValidationError
from .util import utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_delegations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,               -- чей ящик открывается
    employee_id INTEGER NOT NULL,               -- кому
    rights      TEXT NOT NULL DEFAULT 'read',   -- read | full
    until       TEXT NOT NULL DEFAULT '',       -- ГГГГ-ММ-ДД включительно ('' — бессрочно)
    note        TEXT NOT NULL DEFAULT '',
    granted_by  TEXT NOT NULL DEFAULT '',
    granted_at  TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_mailbox_delegations_emp ON mailbox_delegations(employee_id);
"""

RIGHTS = ("read", "full")
RIGHTS_LABEL = {"read": "только чтение", "full": "полный доступ"}


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def delegated_for_employee(db, employee_id: Optional[int]) -> Dict[int, str]:
    """{ящик: права} — что открыто работающему сотруднику ``employee_id`` (ящики выключенные — тоже)."""
    if not employee_id:
        return {}
    try:
        rows = db.query("SELECT d.account_id, d.rights FROM mailbox_delegations d "
                        "JOIN employees e ON e.id = d.employee_id AND e.status = 'active' "
                        "JOIN accounts a ON a.id = d.account_id "
                        "WHERE d.employee_id=? AND (d.until='' OR d.until >= ?) ORDER BY a.name",
                        (int(employee_id), _today()))
    except Exception:  # noqa: BLE001 - таблицы ещё нет (очень старая база при первом запуске)
        return {}
    return {int(r["account_id"]): (r["rights"] if r["rights"] in RIGHTS else "read") for r in rows}


def list_for_account(svc, account_id: int) -> List[Dict]:
    rows = svc.db.query("SELECT d.*, e.full_name, e.email, e.status FROM mailbox_delegations d "
                        "LEFT JOIN employees e ON e.id = d.employee_id WHERE d.account_id=? ORDER BY e.full_name",
                        (int(account_id),))
    today = _today()
    return [{"id": int(r["id"]), "employee_id": int(r["employee_id"]), "employee": r["full_name"] or "—",
             "email": r["email"] or "", "rights": r["rights"], "rights_label": RIGHTS_LABEL.get(r["rights"], ""),
             "until": r["until"], "note": r["note"], "granted_by": r["granted_by"], "granted_at": r["granted_at"],
             "active": (r["status"] == "active") and (not r["until"] or r["until"] >= today)} for r in rows]


def list_for_employee(svc, employee_id: int) -> List[Dict]:
    rows = svc.db.query("SELECT d.*, a.name, a.username FROM mailbox_delegations d JOIN accounts a "
                        "ON a.id = d.account_id WHERE d.employee_id=? ORDER BY a.name", (int(employee_id),))
    return [{"id": int(r["id"]), "account_id": int(r["account_id"]), "account": r["name"], "username": r["username"],
             "rights": r["rights"], "rights_label": RIGHTS_LABEL.get(r["rights"], ""), "until": r["until"]}
            for r in rows]


def grant(svc, actor: str, account_id: int, employee_id: int, rights: str = "read", until: str = "",
          note: str = "") -> Dict:
    rights = (rights or "read").strip().lower()
    if rights not in RIGHTS:
        raise ValidationError("Права — «только чтение» (read) или «полный доступ» (full).")
    acc = svc.db.get_account(int(account_id))
    emp = svc.db.query_one("SELECT id, full_name, status FROM employees WHERE id=?", (int(employee_id),))
    if acc is None or emp is None:
        raise ValidationError("Ящик или сотрудник не найден.", code="not_found")
    if acc.employee_id and int(acc.employee_id) == int(employee_id):
        raise ValidationError("Это ящик самого сотрудника — он и так ему открыт.")
    until = (until or "").strip()
    if until:
        try:
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            raise ValidationError("Дата «до» — в виде ГГГГ-ММ-ДД.") from None
    svc.db.execute("INSERT INTO mailbox_delegations(account_id, employee_id, rights, until, note, granted_by, granted_at) "
                   "VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id, employee_id) DO UPDATE SET rights=excluded.rights, "
                   "until=excluded.until, note=excluded.note, granted_by=excluded.granted_by, "
                   "granted_at=excluded.granted_at",
                   (int(account_id), int(employee_id), rights, until, (note or "").strip()[:300], actor, utcnow_iso()))
    svc.db.add_audit(actor, "mailbox_delegate", f"ящик «{acc.name}» открыт сотруднику «{emp['full_name']}»: "
                     f"{RIGHTS_LABEL[rights]}" + (f" до {until}" if until else ""))
    return {"ok": True, "items": list_for_account(svc, account_id)}


def revoke(svc, actor: str, account_id: int, employee_id: int) -> Dict:
    acc = svc.db.get_account(int(account_id))
    emp = svc.db.query_one("SELECT full_name FROM employees WHERE id=?", (int(employee_id),))
    cur = svc.db.execute("DELETE FROM mailbox_delegations WHERE account_id=? AND employee_id=?",
                         (int(account_id), int(employee_id)))
    if int(cur.rowcount or 0):
        svc.db.add_audit(actor, "mailbox_delegate", f"ящик «{acc.name if acc else account_id}»: доступ сотрудника "
                         f"«{emp['full_name'] if emp else employee_id}» снят")
    return {"ok": True, "items": list_for_account(svc, account_id)}


def unified_inbox(svc, account_ids: List[int], *, offset: int = 0, limit: int = 50) -> Dict:
    """«Все входящие»: «Входящие» нескольких ящиков одним списком, новые сверху (из архива)."""
    ids = [int(i) for i in account_ids][:200]
    if not ids:
        return {"items": [], "total": 0, "more": False}
    marks = ",".join("?" * len(ids))
    limit = max(1, min(200, int(limit)))
    where = f"m.account_id IN ({marks}) AND upper(m.folder)='INBOX' AND COALESCE(m.gone, 0)=0"
    total = int(svc.db.scalar(f"SELECT COUNT(*) FROM messages m WHERE {where}", tuple(ids)) or 0)
    rows = svc.db.query(f"SELECT m.id, m.account_id, m.folder, m.subject, m.from_addr, m.internaldate, m.size, "
                        f"m.has_attach, m.flags, a.name AS account_name FROM messages m JOIN accounts a "
                        f"ON a.id = m.account_id WHERE {where} ORDER BY m.internaldate DESC, m.id DESC LIMIT ? OFFSET ?",
                        (*ids, limit, max(0, int(offset))))
    items = [{"id": int(r["id"]), "account_id": int(r["account_id"]), "account": r["account_name"],
              "folder": r["folder"], "subject": r["subject"] or "", "from": r["from_addr"] or "",
              "date": r["internaldate"], "size": int(r["size"] or 0), "has_attach": bool(r["has_attach"]),
              "seen": "\\Seen" in (r["flags"] or "")} for r in rows]
    return {"items": items, "total": total, "more": offset + len(items) < total}
