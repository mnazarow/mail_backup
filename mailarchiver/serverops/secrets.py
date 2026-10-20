"""
Новые пароли учётных записей — для одной выдачи администратору.

Когда групповое действие задаёт учётным записям случайные пароли и
администратору нужно раздать их сотрудникам (новые учётные записи, сброс
паролей), задание не может просто показать их: оно идёт в фоне. Пароли
записываются сюда — зашифрованными ключом приложения (тем же, что пароли
ящиков), — и скачиваются файлом **один раз**: после выдачи строки удаляются.
Невыданные удаляются через :data:`KEEP_HOURS` часов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..util import utcnow_iso

#: Сколько хранить невыданные пароли.
KEEP_HOURS = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_secrets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id      INTEGER NOT NULL,             -- групповая операция (bulk_ops.id)
    email      TEXT NOT NULL,
    name       TEXT DEFAULT '',
    secret     TEXT NOT NULL,                -- пароль, зашифрованный ключом приложения
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_server_secrets_op ON server_secrets(op_id);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)).isoformat()


def purge_old(db) -> int:
    cur = db.execute("DELETE FROM server_secrets WHERE created_at < ?", (_cutoff(),))
    return cur.rowcount or 0


def save(db, op_id: int, email: str, password: str, *, name: str = "", note: str = "") -> None:
    db.execute("INSERT INTO server_secrets(op_id, email, name, secret, note, created_at) VALUES(?,?,?,?,?,?)",
               (int(op_id), email, name[:200], db.secret.encrypt(password), note[:200], utcnow_iso()))


def forget(db, op_id: int, email: str) -> None:
    """Убрать пароль, который так и не был задан на сервере (сервер точно отказал)."""
    db.execute("DELETE FROM server_secrets WHERE op_id=? AND email=?", (int(op_id), email))


def set_note(db, op_id: int, email: str, note: str) -> None:
    """Пометка к паролю в файле для раздачи (например, «сервер не подтвердил смену»)."""
    db.execute("UPDATE server_secrets SET note=? WHERE op_id=? AND email=?", (note[:200], int(op_id), email))


def count(db, op_id: int) -> int:
    purge_old(db)
    row = db.query_one("SELECT COUNT(*) AS n FROM server_secrets WHERE op_id=?", (int(op_id),))
    return int(row["n"]) if row else 0


def take(db, op_id: int, build: Optional[Callable[[List[Dict[str, str]]], Any]] = None):
    """Выдать пароли операции и стереть их (второй раз не выдаются).

    ``build`` — сборка файла из паролей: строки стираются, только когда файл собран, и в той же транзакции
    (сбой сборки оставляет пароли на месте, а два одновременных скачивания не получат их оба).
    Без ``build`` возвращается список паролей.
    """
    purge_old(db)
    with db.transaction() as conn:
        rows = conn.execute("SELECT id, email, name, secret, note FROM server_secrets WHERE op_id=? ORDER BY id",
                            (int(op_id),)).fetchall()
        out = []
        for row in rows:
            try:
                password = db.secret.decrypt(row["secret"])
            except Exception:  # noqa: BLE001 — ключ сменился: пароль не восстановить
                password = ""
            out.append({"email": row["email"], "name": row["name"] or "", "password": password,
                        "note": row["note"] or ""})
        result = build(out) if build is not None else out
        if rows:
            conn.executemany("DELETE FROM server_secrets WHERE id=?", [(row["id"],) for row in rows])
    return result
