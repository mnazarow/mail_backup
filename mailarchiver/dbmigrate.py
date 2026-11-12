"""
Перенос базы службы из SQLite в PostgreSQL (``mailarchiver db-migrate``, 1.24.0).

Порядок:

1. Схема файла SQLite обновляется до текущей версии (как при запуске службы), на
   время переноса файл закрыт для записи (``BEGIN IMMEDIATE``) — копируется
   согласованное состояние.
2. В PostgreSQL создаётся схема службы; её таблицы должны быть пусты (иначе — отказ,
   ``--force`` очищает их).
3. Все таблицы копируются одной транзакцией (``COPY``), внешние ключи проверяются в её
   конце, триггеры на время копирования выключены — счётчики и журналы переносятся как
   есть, а не пересчитываются. Индекс поиска переносится вместе с текстом: перечитывать
   письма не нужно.
4. Счётчики номеров (identity) выставляются за наибольший номер каждой таблицы.
5. Проверка: число строк каждой таблицы, а у писем ещё сумма размеров и наибольший номер
   должны совпасть.

Файл SQLite не меняется (кроме обновления схемы) и остаётся на месте: вернуться
можно, убрав ``database.url`` из настроек.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Dict, List, Optional

from .errors import ConfigError

#: Таблицы SQLite, которые не переносятся: служебные и теневые таблицы FTS5.
_SKIP_PREFIXES = ("sqlite_", "mail_fts")
_INT_TYPES = ("bigint", "integer", "smallint")
_FLOAT_TYPES = ("double precision", "real", "numeric")


def _pg_columns(conn, schema: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for table, column, dtype in conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = %s "
            "ORDER BY table_name, ordinal_position", (schema,)).fetchall():
        out.setdefault(table, {})[column] = dtype
    return out


def _converter(dtype: str, fixed: List[int]):
    """Значение SQLite → значение для столбца PostgreSQL. SQLite хранит в столбце что угодно (в INTEGER —
    строку, в TEXT — число); то, что не переводится, становится NULL и считается в ``fixed``."""
    if dtype in _INT_TYPES:
        def conv(v):
            if v is None or isinstance(v, int):
                return v
            try:
                return int(float(v)) if not isinstance(v, (bytes, bytearray)) else None
            except (TypeError, ValueError):
                fixed[0] += 1
                return None
        return conv
    if dtype in _FLOAT_TYPES:
        def conv(v):
            if v is None or isinstance(v, (int, float)):
                return v
            try:
                return float(v)
            except (TypeError, ValueError):
                fixed[0] += 1
                return None
        return conv
    if dtype == "bytea":
        def conv(v):
            if v is None or isinstance(v, (bytes, bytearray, memoryview)):
                return v
            return str(v).encode("utf-8")
        return conv

    def conv(v):
        if v is None:
            return None
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8", "replace")
        v = v if isinstance(v, str) else str(v)
        return v.replace("\x00", "") if "\x00" in v else v
    return conv


def migrate(sqlite_path: str, url: str, *, force: bool = False,
            say: Optional[Callable[[str], None]] = None) -> Dict:
    """Перенести базу ``sqlite_path`` в PostgreSQL ``url``. Возвращает отчёт: таблицы, строки, проверка."""
    import os

    from . import pgcompat, search
    from .database import Database
    say = say or (lambda _msg: None)
    if not os.path.isfile(sqlite_path):
        raise ConfigError(f"Файл базы SQLite не найден: {sqlite_path}")
    if not pgcompat.is_url(url):
        raise ConfigError("Укажите адрес PostgreSQL: postgresql://пользователь:пароль@сервер:5432/база",
                          hint="Адрес — ключом --to, переменной MAILARCHIVER_DATABASE_URL или database.url в config.yaml.")
    started = time.time()
    say(f"Обновление схемы файла {sqlite_path} до текущей версии…")
    src = Database(sqlite_path, None)
    src.init_schema()
    src.close_all()
    say(f"Схема в PostgreSQL ({pgcompat.redact(url)})…")
    dst = Database(url, None)
    try:
        dst.init_schema()
        engine = dst._pg
        schema = engine.schema
        sconn = sqlite3.connect(f"file:{sqlite_path}", uri=True, timeout=60)
        try:
            sconn.execute("BEGIN IMMEDIATE")       # служба не допишет ничего, пока идёт перенос
            report = _copy(sconn, engine, schema, force=force, say=say)
            sconn.rollback()
        finally:
            sconn.close()
        report["verify"] = _verify(sqlite_path, dst, report["tables"])
        report["seconds"] = round(time.time() - started, 1)
        report["schema"] = schema
        report["fts_rows"] = report.get("fts_rows", 0)
        report["ok"] = not report["verify"]
        if search.FTS_TABLE not in report["tables"]:
            report["tables"][search.FTS_TABLE] = report["fts_rows"]
        return report
    finally:
        dst.close_all()


def _copy(sconn, engine, schema: str, *, force: bool, say) -> Dict:
    from . import search
    pool = engine.pool()
    tables_src = [r[0] for r in sconn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if not r[0].startswith(_SKIP_PREFIXES)]
    out: Dict = {"tables": {}, "skipped": [], "fixed": 0}
    with pool.connection() as conn:
        pg_cols = _pg_columns(conn, schema)
        busy = []
        for table in ("accounts", "messages", "users"):
            if table in pg_cols and conn.execute(f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1').fetchone():
                busy.append(table)
        if busy and not force:
            raise ConfigError(f"В схеме «{schema}» PostgreSQL уже есть данные (таблицы: {', '.join(busy)}).",
                              hint="Перенос идёт в пустую схему. Укажите другую схему (…?schema=имя) или "
                                   "повторите с --force — данные этой схемы будут удалены.")
        with conn.transaction():
            conn.execute(f'SET LOCAL search_path = "{schema}", public')
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            names = ", ".join(f'"{schema}"."{t}"' for t in pg_cols)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
            for table in pg_cols:
                conn.execute(f'ALTER TABLE "{schema}"."{table}" DISABLE TRIGGER USER')
            for table in tables_src:
                if table not in pg_cols:
                    out["skipped"].append(table)
                    continue
                have = [r[1] for r in sconn.execute(f'PRAGMA table_info("{table}")')]
                cols = [c for c in have if c in pg_cols[table]]
                fixed = [0]
                convs = [_converter(pg_cols[table][c], fixed) for c in cols]
                quoted = ", ".join(f'"{c}"' for c in cols)
                n = 0
                with conn.cursor().copy(f'COPY "{schema}"."{table}" ({quoted}) FROM STDIN') as cp:
                    for row in sconn.execute(f'SELECT {quoted} FROM "{table}"'):
                        cp.write_row([f(v) for f, v in zip(convs, row)])
                        n += 1
                out["tables"][table] = n
                out["fixed"] += fixed[0]
                say(f"  {table}: {n}")
            out["fts_rows"] = _copy_fts(sconn, conn, schema, say)
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")      # проверка внешних ключей — здесь, до включения триггеров
            for table in pg_cols:
                conn.execute(f'ALTER TABLE "{schema}"."{table}" ENABLE TRIGGER USER')
            # счётчики номеров: за наибольший номер — и за наибольший выданный в SQLite (sqlite_sequence помнит
            # номера удалённых последних строк: проверка целостности аудита на них опирается)
            try:
                seqs = dict(sconn.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
            except sqlite3.Error:
                seqs = {}
            for table, cols in pg_cols.items():
                if "id" not in cols:
                    continue
                seq_name = conn.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f'"{schema}"."{table}"',)).fetchone()[0]
                if not seq_name:
                    continue
                top = max(int(conn.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{schema}"."{table}"').fetchone()[0]),
                          int(seqs.get(table) or 0))
                if top > 0:
                    conn.execute("SELECT setval(%s, %s, true)", (seq_name, top))
    out["tables"].setdefault(search.FTS_TABLE, out.get("fts_rows", 0))
    return out


def _copy_fts(sconn, conn, schema: str, say) -> int:
    """Индекс поиска: текст из FTS5, слова — заново (tsvector)."""
    from . import search
    try:
        rows = sconn.execute(f"SELECT rowid, subject, addrs, attach, body FROM {search.FTS_TABLE}")
    except sqlite3.Error:
        return 0                                    # SQLite без FTS5: индекс построится заново
    conv = _converter("text", [0])
    n = 0
    with conn.cursor().copy(f'COPY "{schema}"."{search.FTS_TABLE}" (rowid, subject, addrs, attach, body, tsv) '
                            f"FROM STDIN") as cp:
        for rowid, *fields in rows:
            fields = [conv(v) or "" for v in fields]
            cp.write_row([rowid, *fields, search.tsvector_text(dict(zip(search._FIELDS, fields)))])
            n += 1
    say(f"  индекс поиска: {n}")
    return n


def _verify(sqlite_path: str, dst, tables: Dict[str, int]) -> List[str]:
    """Расхождения между файлом SQLite и PostgreSQL (пустой список — всё сошлось)."""
    from . import search
    problems: List[str] = []
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=60)
    try:
        for table, copied in tables.items():
            if table == search.FTS_TABLE:
                try:
                    want = int(sconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    continue
            else:
                want = int(sconn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            got = int(dst.scalar(f'SELECT COUNT(*) FROM "{table}"') or 0)
            if not want == got == copied:
                problems.append(f"{table}: в SQLite {want}, перенесено {copied}, в PostgreSQL {got}")
        want = sconn.execute("SELECT COALESCE(SUM(size), 0), COALESCE(MAX(id), 0) FROM messages").fetchone()
        got = dst.query_one("SELECT COALESCE(SUM(size), 0) AS s, COALESCE(MAX(id), 0) AS m FROM messages")
        if (int(want[0]), int(want[1])) != (int(got["s"]), int(got["m"])):
            problems.append(f"messages: сумма размеров/наибольший номер {tuple(want)} ≠ {(got['s'], got['m'])}")
    finally:
        sconn.close()
    return problems
