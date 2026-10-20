"""
Ход задания раздела по ящикам — для «Повторить для неудачных».

Итог задания (``jobs.result``) пишется только в самом конце и обрезается до 5000
строк: после перезапуска службы или сбоя посреди задания его нет вовсе, а у
большой операции видна лишь часть ящиков. Поэтому задание отмечает каждый ящик
по ходу работы: перед изменением — «идёт», после — итог. По этим отметкам
«Повторить» знает точно, какие ящики сделаны (их не трогаем), какие — с ошибкой
или не начаты (их предлагаем), а на каком задание прервалось посреди работы
(предлагаем с предупреждением: часть изменений могла выполниться).

Отметки операций, которых уже нет в истории, удаляет служебное обслуживание.
"""
from __future__ import annotations

from typing import Dict, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_op_items (
    op_id  INTEGER NOT NULL,           -- операция в истории раздела (bulk_ops.id)
    key    TEXT NOT NULL,              -- ящик (ax:<адрес>, mb:<номер>, адрес — при создании); '' — задание началось
    status TEXT NOT NULL,              -- running | success | skipped | failed
    next   TEXT NOT NULL DEFAULT '',   -- что сделать дальше (например, ax_forward — передать адрес)
    PRIMARY KEY (op_id, key)
);
"""
#: Отметка «задание началось»: без неё по пустой таблице не отличить «ничего не сделано» от старого задания.
STARTED = ""
RUNNING, SUCCESS, SKIPPED, FAILED = "running", "success", "skipped", "failed"


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def mark(db, op_id: Optional[int], key: str, status: str, next_action: str = "") -> None:
    if not op_id:
        return
    db.execute("INSERT OR REPLACE INTO server_op_items(op_id, key, status, next) VALUES(?,?,?,?)",
               (int(op_id), key, status, next_action or ""))


def started(db, op_id: Optional[int]) -> None:
    mark(db, op_id, STARTED, RUNNING)


def items(db, op_id: int) -> Optional[Dict[str, Dict[str, str]]]:
    """Отметки операции: ключ → {status, next}. None — задание операции отметок не вело (до 1.13)."""
    rows = db.query("SELECT key, status, next FROM server_op_items WHERE op_id=?", (int(op_id),))
    out = {r["key"]: {"status": r["status"], "next": r["next"] or ""} for r in rows}
    if STARTED not in out:
        return None
    out.pop(STARTED)
    return out


def purge_orphans(db) -> int:
    cur = db.execute("DELETE FROM server_op_items WHERE op_id NOT IN (SELECT id FROM bulk_ops)")
    return cur.rowcount or 0
