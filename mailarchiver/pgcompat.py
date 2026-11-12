"""
PostgreSQL вместо SQLite (1.24.0): слой совместимости.

Код архива написан под SQLite (``sqlite3``: параметры ``?``, ``INSERT OR IGNORE``, ``lastrowid``,
``PRAGMA table_info``, триггеры ``BEGIN … END``, регистронезависимый ``LIKE``). Этот модуль даёт объект, который
ведёт себя как соединение ``sqlite3`` (``execute``/``executemany``/``executescript``/``commit``/``rollback``,
строки с доступом по имени и номеру), и переводит запросы на диалект PostgreSQL:

* параметры — ``%s``, значения передаются литералами без типа (PostgreSQL сам приводит их к типу столбца,
  как SQLite);
* ``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``; ``INSERT OR REPLACE`` / ``REPLACE`` → ``ON CONFLICT
  (ключ) DO UPDATE`` по первичному или уникальному ключу таблицы; ``lastrowid`` — через ``RETURNING id``;
* ``LIKE`` → ``ILIKE`` (в SQLite он без учёта регистра), ``IS``/``IS NOT`` со значением → ``IS [NOT] DISTINCT
  FROM``, ``MAX(a, b)``/``MIN(a, b)`` → ``GREATEST``/``LEAST``, ``CAST(… AS INTEGER)`` → ``BIGINT``;
* схема: ``INTEGER PRIMARY KEY [AUTOINCREMENT]`` → ``BIGINT … IDENTITY``, ``INTEGER`` → ``BIGINT``, ``REAL`` →
  ``DOUBLE PRECISION``, ``BLOB`` → ``BYTEA``; триггеры SQLite → функции plpgsql; ``sqlite_master`` и
  ``PRAGMA table_info`` — из каталога PostgreSQL; ``COLLATE NOCASE`` — недетерминированное сравнение ICU;
* ошибки — классами ``sqlite3`` (``IntegrityError``, ``OperationalError``): код ловит их как раньше.

Соединения — из пула (``database.pool_size``); транзакция (``BEGIN IMMEDIATE``) держит одно соединение и
глобальную блокировку записи (``pg_advisory_xact_lock``) — как единственный писатель SQLite.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
import sqlite3
import threading
import urllib.parse
import weakref
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .logging_setup import get_logger

log = get_logger("db")

#: Блокировка записи для транзакций (аналог единственного писателя SQLite).
WRITE_LOCK_KEY = 7_406_150_023
#: Минимальная версия сервера: подзапросы во FROM без псевдонима — с PostgreSQL 16.
MIN_SERVER_VERSION = 160000


def is_url(target: str) -> bool:
    return str(target or "").startswith(("postgresql://", "postgres://"))


def libpq_env(conninfo: str) -> Tuple[str, Dict[str, str]]:
    """Строка подключения для ``pg_dump``/``psql`` без пароля и окружение с ``PGPASSWORD`` (пароль в
    аргументах команды виден в списке процессов)."""
    import os
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(conninfo)
    env = dict(os.environ)
    password = params.pop("password", None)
    if password:
        env["PGPASSWORD"] = str(password)
    return make_conninfo(**{k: str(v) for k, v in params.items() if v is not None}), env


def redact(url: str) -> str:
    """Адрес базы без пароля — для журналов и интерфейса."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.password:
        netloc = parts.netloc.replace(":" + parts.password + "@", ":***@")
        return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return url


def split_url(url: str) -> Tuple[str, str]:
    """Адрес → (адрес для libpq, схема). Схема — параметр ``schema=`` (не libpq), по умолчанию ``mailarchiver``."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    schema = "mailarchiver"
    rest = []
    for k, v in query:
        if k == "schema":
            schema = v
        else:
            rest.append((k, v))
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", schema):
        raise ValueError(f"имя схемы PostgreSQL «{schema}»: только латиница в нижнем регистре, цифры и _")
    scheme = "postgresql"
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path, urllib.parse.urlencode(rest), "")), schema


# ===========================================================================
#  Строки и курсоры как в sqlite3
# ===========================================================================
class Row:
    """Строка результата: ``row[0]``, ``row["name"]`` (без учёта регистра), ``keys()``, ``dict(row)``."""
    __slots__ = ("_values", "_index")

    def __init__(self, values: Sequence[Any], index: Dict[str, int]) -> None:
        self._values = tuple(values)
        self._index = index

    def keys(self) -> List[str]:
        return [k for k, _ in sorted(((k, i) for k, i in self._index.items() if not k.startswith("\0")),
                                     key=lambda kv: kv[1])]

    def __getitem__(self, key):
        if isinstance(key, str):
            i = self._index.get(key)
            if i is None:
                i = self._index.get("\0" + key.lower())
            if i is None:
                raise IndexError("No item with that key")
            return self._values[i]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other) -> bool:
        if isinstance(other, Row):
            return self._values == other._values and self.keys() == other.keys()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"<Row {dict(zip(self.keys(), self._values))!r}>"


def _index_of(names: Sequence[str]) -> Dict[str, int]:
    idx: Dict[str, int] = {}
    for i, n in enumerate(names):
        idx.setdefault(n, i)
        idx.setdefault("\0" + n.lower(), i)
    return idx


def _value(v):
    """Значение из PostgreSQL → как вернул бы SQLite."""
    if v is None or isinstance(v, (str, int, float)):
        return v if not isinstance(v, bool) else int(v)
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, _dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    return v


class Cursor:
    """Результат запроса (всё уже прочитано): ``fetchone``/``fetchall``/итерация, ``rowcount``, ``lastrowid``."""

    def __init__(self, rows: List[Row], rowcount: int = -1, lastrowid: Optional[int] = None,
                 description=None) -> None:
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> List[Row]:
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def fetchmany(self, size: int = 1) -> List[Row]:
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self._rows = []


# ===========================================================================
#  Перевод запросов
# ===========================================================================
_MASK_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.S)
_SQLITE_MASTER = (
    "(SELECT c.relname::text AS name, CASE c.relkind WHEN 'i' THEN 'index' ELSE 'table' END AS type, "
    "COALESCE(t.relname, c.relname)::text AS tbl_name, '' AS sql FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_index x ON x.indexrelid = c.oid "
    "LEFT JOIN pg_class t ON t.oid = x.indrelid WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'i', 'p') "
    "UNION ALL SELECT tg.tgname::text, 'trigger', c2.relname::text, '' FROM pg_trigger tg "
    "JOIN pg_class c2 ON c2.oid = tg.tgrelid JOIN pg_namespace n2 ON n2.oid = c2.relnamespace "
    "WHERE n2.nspname = current_schema() AND NOT tg.tgisinternal) AS sqlite_master")


def _mask(sql: str) -> Tuple[str, List[str]]:
    saved: List[str] = []

    def keep(m):
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"
    return _MASK_RE.sub(keep, sql), saved


def _unmask(sql: str, saved: List[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], sql)


def strip_comments(sql: str) -> str:
    """Убрать комментарии ``--`` и ``/* */`` (строки и имена в кавычках не трогаются)."""
    masked, saved = _mask(sql)
    masked = re.sub(r"\x00(\d+)\x00",
                    lambda m: " " if saved[int(m.group(1))].startswith(("--", "/*")) else m.group(0), masked)
    return _unmask(masked, saved)


def _greatest(code: str) -> str:
    """``MAX(a, b)`` → ``GREATEST(a, b)``, ``MIN(a, b)`` → ``LEAST(a, b)`` (скалярные функции SQLite)."""
    out, i = [], 0
    for m in re.finditer(r"\b(MAX|MIN)\s*\(", code, re.I):
        if m.start() < i:
            continue
        depth, j, comma = 1, m.end(), False
        while j < len(code) and depth:
            ch = code[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 1:
                comma = True
            j += 1
        if comma:
            out.append(code[i:m.start()])
            out.append(("GREATEST(" if m.group(1).upper() == "MAX" else "LEAST("))
            i = m.end()
    out.append(code[i:])
    return "".join(out)


def _dml(code: str) -> str:
    """Замены в тексте запроса без строк (строки и имена в кавычках замаскированы)."""
    code = re.sub(r"\bNOT\s+LIKE\b", "NOT ILIKE", code, flags=re.I)
    code = re.sub(r"(?<!NOT )(?<!I)\bLIKE\b", "ILIKE", code, flags=re.I)
    # обратная косая черта в шаблоне LIKE у SQLite — обычный знак («%,\\Seen,%»), у PostgreSQL — экранирование
    code = re.sub(r"\b((?:NOT\s+)?ILIKE)\s+(\?|\x00\d+\x00)(?!\s*ESCAPE\b)", r"\1 \2 ESCAPE ''", code, flags=re.I)
    code = re.sub(r"\bIS\s+NOT\s+(?!NULL\b|DISTINCT\b|TRUE\b|FALSE\b)", "IS DISTINCT FROM ", code, flags=re.I)
    code = re.sub(r"\bIS\s+(?!NOT\b|NULL\b|DISTINCT\b|TRUE\b|FALSE\b)", "IS NOT DISTINCT FROM ", code, flags=re.I)
    code = re.sub(r"\bAS\s+INTEGER\b", "AS BIGINT", code, flags=re.I)
    code = re.sub(r"\bAS\s+REAL\b", "AS DOUBLE PRECISION", code, flags=re.I)
    code = re.sub(r"\bsqlite_master\b", _SQLITE_MASTER, code)
    code = re.sub(r"\bLIMIT\s+-1\b", "LIMIT ALL", code, flags=re.I)
    code = re.sub(r"\s+(INDEXED\s+BY\s+\w+|NOT\s+INDEXED)\b", "", code, flags=re.I)      # подсказки индекса SQLite
    code = re.sub(r"(?<![\w.])temp\.", "pg_temp.", code, flags=re.I)        # временные таблицы
    code = re.sub(r"\buser\b", '"user"', code, flags=re.I)      # столбец audit.user — слово PostgreSQL
    code = _greatest(code)
    return code


def _qualify_upsert(code: str, table: str) -> str:
    """``ON CONFLICT … DO UPDATE SET n = n + 1``: в PostgreSQL ``n`` справа неоднозначно (столбец таблицы или
    ``excluded``), в SQLite — столбец таблицы. Имена изменяемых столбцов справа получают имя таблицы."""
    m = re.search(r"\bDO\s+UPDATE\s+SET\b", code, re.I)
    if not m:
        return code
    head, rest = code[:m.end()], code[m.end():]
    tail = ""
    r = re.search(r"\bRETURNING\b", rest, re.I)
    if r:
        rest, tail = rest[:r.start()], rest[r.start():]
    where = ""
    w = re.search(r"\bWHERE\b", rest, re.I)
    if w:
        rest, where = rest[:w.start()], rest[w.start():]
    parts = _top_split(rest)
    names = []
    for part in parts:
        lhs = re.match(r"^\s*([A-Za-z_]\w*)\s*=", part)
        if lhs:
            names.append(lhs.group(1))
    if not names:
        return code
    rx = re.compile(r"(?<![\w.\"])(" + "|".join(map(re.escape, names)) + r")\b(?!\s*\()", re.I)
    out = []
    for part in parts:
        lhs = re.match(r"^(\s*[A-Za-z_]\w*\s*=)(.*)$", part, re.S)
        if lhs:
            part = lhs.group(1) + rx.sub(lambda mm: f"{table}.{mm.group(1)}", lhs.group(2))
        out.append(part)
    where = rx.sub(lambda mm: f"{table}.{mm.group(1)}", where) if where else ""
    return head + ",".join(out) + where + tail


class Translated:
    __slots__ = ("sql", "kind", "table", "conflict", "insert_cols", "returning")

    def __init__(self, sql, kind="", table="", conflict="", insert_cols=None, returning=False):
        self.sql, self.kind, self.table, self.conflict = sql, kind, table, conflict
        self.insert_cols, self.returning = insert_cols or [], returning


@lru_cache(maxsize=4096)
def _translate_static(sql: str) -> Translated:
    """Часть перевода, не зависящая от каталога базы."""
    stripped = sql.strip().rstrip(";").strip()
    masked, saved = _mask(stripped)
    head = masked.lstrip()[:40].upper()
    conflict = ""
    m = re.match(r"^\s*(INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO|REPLACE\s+INTO)\s+", masked, re.I)
    if m:
        conflict = "ignore" if (m.group(2) or "").upper() == "IGNORE" else "replace"
        masked = "INSERT INTO " + masked[m.end():]
    m = re.match(r"^\s*UPDATE\s+OR\s+IGNORE\s+", masked, re.I)
    if m:
        # обновление, которое нарушило бы уникальность, пропускается (Session.execute — через SAVEPOINT)
        conflict = "update_ignore"
        masked = "UPDATE " + masked[m.end():]
    kind = head.split(None, 1)[0] if head else ""
    table, cols = "", []
    if kind == "INSERT" or conflict:
        mt = re.match(r"^\s*INSERT\s+INTO\s+([A-Za-z_][\w]*)\s*(\(([^()]*)\))?", masked, re.I)
        if mt:
            table = mt.group(1)
            cols = [c.strip().strip('"').lower() for c in (mt.group(3) or "").split(",") if c.strip()]
        kind = "INSERT"
    code = _dml(masked)
    if kind == "INSERT" and table:
        code = _qualify_upsert(code, table)
    code = code.replace("?", "\x01")
    out = _unmask(code, saved)
    returning = bool(re.search(r"\bRETURNING\b", masked, re.I))
    out = out.replace("%", "%%").replace("\x01", "%s")
    return Translated(out, kind, table, conflict, cols, returning)


# ---- схема -----------------------------------------------------------------
def _lead(sql: str) -> str:
    """Оператор без комментариев и пробелов в начале."""
    return re.sub(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", sql, flags=re.S).strip()


def _split_script(script: str) -> List[str]:
    """Разбить скрипт на операторы; ``BEGIN … END`` триггера — один оператор."""
    masked, saved = _mask(script)
    out, buf, depth = [], [], 0
    for tok in re.split(r"(;|\bBEGIN\b|\bEND\b|\bCASE\b)", masked, flags=re.I):
        up = tok.upper()
        if up == "BEGIN" and re.search(r"\bTRIGGER\b", "".join(buf), re.I):
            depth += 1
        elif up == "CASE" and depth:
            depth += 1
        elif up == "END" and depth:
            depth -= 1
        if tok == ";" and depth == 0:
            stmt = _unmask("".join(buf), saved).strip()
            if stmt:
                out.append(stmt)
            buf = []
            continue
        buf.append(tok)
    stmt = _unmask("".join(buf), saved).strip()
    if stmt:
        out.append(stmt)
    return out


_TYPE_RULES = [
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I), "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\b", re.I), "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bAUTOINCREMENT\b", re.I), ""),
    (re.compile(r"\bINTEGER\b", re.I), "BIGINT"),
    (re.compile(r"\bREAL\b", re.I), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.I), "BYTEA"),
    (re.compile(r"\bDATETIME\b", re.I), "TEXT"),
    (re.compile(r"\bWITHOUT\s+ROWID\b", re.I), ""),
    (re.compile(r"\bDEFAULT\s+CURRENT_TIMESTAMP\b", re.I),
     "DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"),
]


def ddl_types(stmt: str) -> str:
    masked, saved = _mask(stmt)
    for rx, repl in _TYPE_RULES:
        masked = rx.sub(repl, masked)
    return _unmask(masked, saved)


def _top_split(text: str) -> List[str]:
    """Разбить по запятым верхнего уровня (в скобках запятые не считаются)."""
    out, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def split_foreign_keys(stmt: str) -> Tuple[str, List[Tuple[str, str, str]]]:
    """``CREATE TABLE`` без внешних ключей + список (таблица, столбцы, «REFERENCES …»).

    SQLite разрешает ссылки на таблицы, созданные позже (и взаимные: accounts ↔ employees), PostgreSQL —
    нет: ключи добавляются отдельно, когда все таблицы уже есть."""
    masked, saved = _mask(strip_comments(stmt))
    m = re.match(r"^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*)\((.*)\)(\s*)$", masked, re.I | re.S)
    if not m:
        return stmt, []
    head, table, body, tail = m.group(1), m.group(2), m.group(3), m.group(4)
    keep, fks = [], []
    for item in _top_split(body):
        fk = re.match(r"^\s*(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*(REFERENCES\b.*)$", item, re.I | re.S)
        if fk:
            fks.append((table, fk.group(1).strip(), _unmask(fk.group(2).strip(), saved)))
            continue
        col = re.match(r"^(\s*(\w+)\b.*?)\s+(REFERENCES\b.*)$", item, re.I | re.S)
        if col and not re.match(r"^\s*(PRIMARY|UNIQUE|CHECK|CONSTRAINT)\b", item, re.I):
            fks.append((table, col.group(2), _unmask(col.group(3).strip(), saved)))
            item = col.group(1)
        keep.append(item)
    return _unmask(head + "(" + ",".join(keep) + "\n)" + tail, saved), fks


_TRIGGER_RE = re.compile(
    r"^\s*CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+(BEFORE|AFTER)\s+(INSERT|DELETE|UPDATE(?:\s+OF\s+[\w\s,]+?)?)"
    r"\s+ON\s+(\w+)\s+(?:FOR\s+EACH\s+ROW\s+)?(?:WHEN\s+(.*?)\s+)?BEGIN\s+(.*)\bEND\s*$", re.I | re.S)


def trigger_ddl(stmt: str) -> List[str]:
    """Триггер SQLite → функция plpgsql и триггер PostgreSQL."""
    m = _TRIGGER_RE.match(stmt)
    if not m:
        raise sqlite3.OperationalError(f"не удалось перевести триггер для PostgreSQL: {stmt[:120]}")
    name, when, event, table, cond, body = m.groups()
    stmts = []
    for part in _split_script(body):
        tr = _translate_static(part)
        stmts.append(_conflict_clause(tr, None).replace("%%", "%") + ";")
    inner = "\n    ".join(stmts)
    if cond:
        cmasked, csaved = _mask(cond)
        inner = f"IF {_unmask(_dml(cmasked), csaved)} THEN\n    {inner}\n  END IF;"
    fn = f"ma_trg_{name}"
    ret = "NEW" if when.upper() == "BEFORE" and not event.upper().startswith("DELETE") else \
        ("OLD" if when.upper() == "BEFORE" else "NULL")
    event = re.sub(r"\s+", " ", event.strip())
    return [
        f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $ma$\nBEGIN\n  {inner}\n"
        f"  RETURN {ret};\nEND\n$ma$",
        f"DROP TRIGGER IF EXISTS {name} ON {table}",
        f"CREATE TRIGGER {name} {when} {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {fn}()",
    ]


def _conflict_clause(tr: Translated, catalog) -> str:
    """Добавить ``ON CONFLICT`` для ``INSERT OR IGNORE/REPLACE`` (и ``RETURNING``, если нужно)."""
    sql = tr.sql
    if tr.conflict == "ignore":
        sql += " ON CONFLICT DO NOTHING"
    elif tr.conflict == "replace":
        target = catalog.conflict_target(tr.table, tr.insert_cols) if catalog is not None else []
        if not target:
            raise sqlite3.OperationalError(f"INSERT OR REPLACE в {tr.table}: не найден ключ для ON CONFLICT")
        rest = [c for c in tr.insert_cols if c not in target]
        if rest:
            sql += (f" ON CONFLICT ({', '.join(target)}) DO UPDATE SET "
                    + ", ".join(f"{c} = EXCLUDED.{c}" for c in rest))
        else:
            sql += f" ON CONFLICT ({', '.join(target)}) DO NOTHING"
    return sql


# ===========================================================================
#  Соединение
# ===========================================================================
_SETUP_SQL = [
    # функции SQLite, которых нет в PostgreSQL
    "CREATE OR REPLACE FUNCTION public.ma_lower(text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE "
    "AS $$ SELECT lower($1 COLLATE \"und-x-icu\") $$",
    "CREATE OR REPLACE FUNCTION public.julianday(text) RETURNS double precision LANGUAGE sql IMMUTABLE "
    "AS $$ SELECT extract(epoch FROM $1::timestamptz) / 86400.0 + 2440587.5 $$",
    "CREATE OR REPLACE FUNCTION public.instr(text, text) RETURNS integer LANGUAGE sql IMMUTABLE "
    "AS $$ SELECT strpos($1, $2) $$",
    "CREATE OR REPLACE FUNCTION public.round(double precision, integer) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE AS $$ SELECT round($1::numeric, $2)::double precision $$",
    "CREATE OR REPLACE FUNCTION public.char(integer) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT chr($1) $$",
]


def _map_error(exc: Exception) -> Exception:
    import psycopg
    msg = str(exc).strip()
    if isinstance(exc, psycopg.errors.IntegrityError):
        return sqlite3.IntegrityError(msg)
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return sqlite3.OperationalError("interrupted: " + msg)
    if isinstance(exc, (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected)):
        return sqlite3.OperationalError("database is locked: " + msg)
    if isinstance(exc, psycopg.OperationalError):
        return sqlite3.OperationalError(msg)
    return sqlite3.OperationalError(msg)


def _param(v):
    if v is None or isinstance(v, (bytes, bytearray, memoryview)):
        return v
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        if not -(1 << 63) <= v < (1 << 63):
            raise OverflowError("Python int too large to convert to SQLite INTEGER")   # как sqlite3
        return str(v)
    v = v if isinstance(v, str) else str(v)
    # PostgreSQL не хранит символ NUL в тексте (SQLite хранит: бывает в темах битых писем)
    return v.replace("\x00", "") if "\x00" in v else v


#: Все открытые пулы процесса (тесты закрывают их после каждого теста).
_ENGINES: "weakref.WeakSet[Engine]" = weakref.WeakSet()


def engines() -> List["Engine"]:
    return list(_ENGINES)


class Engine:
    """Пул соединений с одной базой (и схемой) PostgreSQL и каталог её таблиц."""

    def __init__(self, url: str, pool_size: int = 20) -> None:
        self.url = url
        self.conninfo, self.schema = split_url(url)
        self.pool_size = max(2, int(pool_size))
        self._pool = None
        self._lock = threading.Lock()
        self._cols: Dict[str, List[str]] = {}
        self._keys: Dict[str, List[List[str]]] = {}
        self._identity: Dict[str, bool] = {}
        self._info: Dict[str, List[Row]] = {}
        import atexit
        atexit.register(self.close)
        _ENGINES.add(self)

    # -- пул -------------------------------------------------------------------
    def pool(self):
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._prepare()
                    from psycopg_pool import ConnectionPool
                    self._pool = ConnectionPool(self.conninfo, min_size=1, max_size=self.pool_size, open=True,
                                                kwargs=self._kwargs(), timeout=60, name="mailarchiver")
        return self._pool

    def _kwargs(self) -> Dict[str, Any]:
        import psycopg
        return {"autocommit": True, "cursor_factory": psycopg.ClientCursor,
                "options": f"-c search_path={self.schema},public -c TimeZone=UTC"}

    def _prepare(self) -> None:
        """Схема, функции и сравнение без учёта регистра — один раз на базу."""
        import psycopg
        try:
            with psycopg.connect(self.conninfo, autocommit=True) as conn:
                ver = conn.info.server_version
                if ver < MIN_SERVER_VERSION:
                    raise sqlite3.OperationalError(
                        f"нужен PostgreSQL 16 или новее, а сервер — {ver // 10000}.{ver % 10000 // 100}")
                conn.execute("SELECT pg_advisory_lock(%s)", (WRITE_LOCK_KEY + 1,))
                try:
                    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                    for stmt in _SETUP_SQL:
                        conn.execute(stmt)
                    have = conn.execute("SELECT 1 FROM pg_collation WHERE collname = 'nocase' "
                                        "AND collnamespace = 'public'::regnamespace").fetchone()
                    if not have:
                        conn.execute("CREATE COLLATION IF NOT EXISTS public.nocase (provider = icu, "
                                     "locale = 'und-u-ks-level2', deterministic = false)")
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (WRITE_LOCK_KEY + 1,))
        except psycopg.Error as exc:
            raise _map_error(exc) from None

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    # -- каталог ---------------------------------------------------------------
    def forget_catalog(self) -> None:
        self._info.clear()
        self._cols.clear()
        self._keys.clear()
        self._identity.clear()

    def _catalog_query(self, sql: str, params: tuple) -> List[tuple]:
        with self.pool().connection() as conn:
            return conn.execute(sql, params).fetchall()

    #: Столбцы таблицы схемы службы (pg_catalog: information_schema заметно медленнее).
    _COLS_SQL = ("SELECT a.attname, a.attidentity <> '' FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                 "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s AND a.attnum > 0 "
                 "AND NOT a.attisdropped ORDER BY a.attnum")

    def _load_columns(self, t: str) -> None:
        rows = self._catalog_query(self._COLS_SQL, (t,))
        self._cols[t] = [r[0] for r in rows]
        self._identity[t] = any(name == "id" and ident for name, ident in rows)

    def columns(self, table: str) -> List[str]:
        t = table.lower()
        if t not in self._cols:
            self._load_columns(t)
        return self._cols[t]

    def has_identity_id(self, table: str) -> bool:
        t = table.lower()
        if t not in self._identity:
            self._load_columns(t)
        return self._identity[t]

    def keys(self, table: str) -> List[List[str]]:
        """Первичный ключ, затем уникальные индексы таблицы (столбцы)."""
        t = table.lower()
        if t not in self._keys:
            rows = self._catalog_query(
                "SELECT i.indisprimary, array_agg(a.attname ORDER BY k.ord) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s AND i.indisunique "
                "AND i.indpred IS NULL GROUP BY i.indexrelid, i.indisprimary ORDER BY i.indisprimary DESC, "
                "i.indexrelid", (t,))
            self._keys[t] = [list(r[1]) for r in rows]
        return self._keys[t]

    def conflict_target(self, table: str, insert_cols: List[str]) -> List[str]:
        cols = insert_cols or [c.lower() for c in self.columns(table)]
        for key in self.keys(table):
            if all(k in cols for k in key):
                return key
        return []

    def table_info(self, table: str) -> List[Row]:
        t = table.lower()
        if t in self._info:
            return list(self._info[t])
        rows = self._catalog_query(
            "SELECT row_number() OVER (ORDER BY a.attnum) - 1, a.attname, upper(format_type(a.atttypid, a.atttypmod)), "
            "CASE WHEN a.attnotnull THEN 1 ELSE 0 END, pg_get_expr(d.adbin, d.adrelid), "
            "CASE WHEN EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary "
            "AND a.attnum = ANY(i.indkey)) THEN 1 ELSE 0 END "
            "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s ORDER BY a.attnum",
            (table.lower(),))
        idx = _index_of(["cid", "name", "type", "notnull", "dflt_value", "pk"])
        self._info[t] = [Row(tuple(_value(v) for v in r), idx) for r in rows]
        return list(self._info[t])

    # -- перевод с каталогом -------------------------------------------------------
    def translate(self, sql: str) -> Tuple[str, bool]:
        """→ (запрос для PostgreSQL, нужен ли id новой строки)."""
        tr = _translate_static(sql)
        out = _conflict_clause(tr, self) if tr.conflict in ("ignore", "replace") else tr.sql
        want_id = False
        if tr.kind == "INSERT" and tr.table and not tr.returning and self.has_identity_id(tr.table):
            out += " RETURNING id"
            want_id = True
        return out, want_id


class _SessionCursor:
    """``conn.cursor()`` как в sqlite3: строки — кортежи, если ``row_factory`` не задан."""

    def __init__(self, session: "Session") -> None:
        self._s = session
        self._res: Optional[Cursor] = None
        self.row_factory = None
        self.rowcount = -1
        self.lastrowid = None

    def _row(self, r):
        return tuple(r) if self.row_factory is None else r

    def execute(self, sql: str, params: Iterable = ()) -> "_SessionCursor":
        self._res = self._s.execute(sql, params)
        self.rowcount, self.lastrowid = self._res.rowcount, self._res.lastrowid
        return self

    def fetchone(self):
        r = self._res.fetchone() if self._res is not None else None
        return None if r is None else self._row(r)

    def fetchall(self) -> list:
        return [self._row(r) for r in self._res.fetchall()] if self._res is not None else []

    def __iter__(self):
        return iter(self.fetchall())

    def close(self) -> None:
        self._res = None


class Session:
    """«Соединение» одного потока: без транзакции каждый запрос берёт соединение из пула, в транзакции —
    держит одно."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._conn = None           # соединение пула, пока открыта транзакция
        self._ctx = None
        self.row_factory = None     # для совместимости с sqlite3
        self.dialect = "postgresql"
        self.total_changes = 0      # как sqlite3.Connection.total_changes: строк изменено этим «соединением»

    # -- совместимость с sqlite3 -----------------------------------------------
    @property
    def in_transaction(self) -> bool:
        return self._conn is not None

    def create_function(self, *a, **k) -> None:
        return None

    def cursor(self) -> _SessionCursor:
        return _SessionCursor(self)

    def close(self) -> None:
        if self._conn is not None:
            self.rollback()

    # -- транзакции ------------------------------------------------------------
    def _begin(self, immediate: bool) -> None:
        if self._conn is not None:
            return
        ctx = self.engine.pool().connection()
        conn = ctx.__enter__()
        try:
            conn.execute("BEGIN")
            if immediate:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
        except BaseException:
            ctx.__exit__(None, None, None)
            raise
        self._conn, self._ctx = conn, ctx

    def _end(self, sql: str) -> None:
        conn, ctx = self._conn, self._ctx
        self._conn = self._ctx = None
        if conn is None:
            return
        try:
            conn.execute(sql)
        finally:
            ctx.__exit__(None, None, None)

    def commit(self) -> None:
        if self._conn is not None:
            try:
                self._end("COMMIT")
            except Exception as exc:  # noqa: BLE001
                raise _map_error(exc) from None

    def rollback(self) -> None:
        if self._conn is not None:
            try:
                self._end("ROLLBACK")
            except Exception as exc:  # noqa: BLE001
                log.warning("Откат транзакции PostgreSQL не удался: %s", exc)

    # -- выполнение ------------------------------------------------------------
    def _run(self, fn):
        import psycopg
        try:
            if self._conn is not None:
                return fn(self._conn)
            with self.engine.pool().connection() as conn:
                return fn(conn)
        except psycopg.Error as exc:
            if self._conn is not None:
                # ошибка в транзакции: PostgreSQL отвергнет всё до ROLLBACK — как и SQLite после ошибки,
                # вызывающий код откатывает транзакцию сам
                pass
            raise _map_error(exc) from None

    def execute(self, sql: str, params: Iterable = ()) -> Cursor:
        special = self._special(sql)
        if special is not None:
            return special
        text, want_id = self.engine.translate(sql)
        args = tuple(_param(p) for p in (params or ()))
        ddl = bool(re.match(r"^\s*(CREATE|ALTER|DROP)\b", sql, re.I))
        skip_dup = _translate_static(sql).conflict == "update_ignore"

        def run(conn):
            if not skip_dup:
                return conn.execute(text, args)
            import psycopg
            if self._conn is None:
                try:
                    return conn.execute(text, args)
                except psycopg.errors.UniqueViolation:
                    return None
            conn.execute("SAVEPOINT ma_upd")
            try:
                cur = conn.execute(text, args)
            except psycopg.errors.UniqueViolation:
                conn.execute("ROLLBACK TO SAVEPOINT ma_upd")
                return None
            conn.execute("RELEASE SAVEPOINT ma_upd")
            return cur

        def go(conn):
            cur = run(conn)
            if cur is None:
                return Cursor([], 0, None, None)
            rows: List[Row] = []
            if cur.description is not None:
                idx = _index_of([d.name for d in cur.description])
                rows = [Row(tuple(_value(v) for v in r), idx) for r in cur.fetchall()]
            lastrowid = None
            if want_id and rows:
                lastrowid = rows[-1][0]
                rows = []
            return Cursor(rows, cur.rowcount, lastrowid, cur.description)
        res = self._run(go)
        if res.rowcount and res.rowcount > 0 and _translate_static(sql).kind in ("INSERT", "UPDATE", "DELETE"):
            self.total_changes += res.rowcount
        if ddl:
            self.engine.forget_catalog()
        return res

    def executemany(self, sql: str, seq_params) -> Cursor:
        total = 0
        last = None
        for params in seq_params:
            cur = self.execute(sql, params)
            total += max(cur.rowcount, 0)
            last = cur.lastrowid if cur.lastrowid is not None else last
        return Cursor([], total, last)

    def executescript(self, script: str) -> Cursor:
        """Скрипт схемы. SQLite разрешает ссылку на таблицу, которая создаётся ниже, — PostgreSQL нет: такие
        операторы откладываются и повторяются, пока что-то создаётся."""
        pending = [pg for stmt in _split_script(script) for pg in self.ddl(stmt)]
        while pending:
            failed: List[Tuple[str, Exception]] = []
            for pg in pending:
                try:
                    self.execute_raw(pg)
                except sqlite3.OperationalError as exc:
                    if "does not exist" not in str(exc) or self._conn is not None:
                        raise
                    failed.append((pg, exc))
            if len(failed) == len(pending):
                raise failed[0][1]
            pending = [pg for pg, _ in failed]
        self.engine.forget_catalog()
        return Cursor([])

    def execute_raw(self, sql: str, params: tuple = ()) -> Cursor:
        """Запрос уже на диалекте PostgreSQL (без перевода)."""
        def go(conn):
            cur = conn.execute(sql, params or None)
            rows = []
            if cur.description is not None:
                idx = _index_of([d.name for d in cur.description])
                rows = [Row(tuple(_value(v) for v in r), idx) for r in cur.fetchall()]
            return Cursor(rows, cur.rowcount, None, cur.description)
        return self._run(go)

    def ddl(self, stmt: str) -> List[str]:
        """Оператор схемы SQLite → операторы PostgreSQL."""
        s = _lead(stmt)
        up = s.upper()
        if not s or up.startswith("PRAGMA") or up.startswith("ANALYZE") or up.startswith("VACUUM"):
            return []
        if re.match(r"^CREATE\s+VIRTUAL\s+TABLE\b", up):
            return []
        if re.match(r"^CREATE\s+TRIGGER\b", up):
            return trigger_ddl(s)
        if re.match(r"^DROP\s+TRIGGER\b", up):
            return self._drop_trigger(s)
        if re.match(r"^(CREATE|ALTER)\b", up):
            s = ddl_types(s)
        extra: List[str] = []
        if re.match(r"^CREATE\s+TABLE\b", up):
            s, fks = split_foreign_keys(s)
            for table, cols, ref in fks:
                name = re.sub(r"\W+", "_", f"fk_{table}_{cols}").lower()[:63]
                extra.append(f"DO $ma$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}' "
                             f"AND connamespace = current_schema()::regnamespace) THEN ALTER TABLE {table} ADD "
                             f"CONSTRAINT {name} FOREIGN KEY ({cols}) {ref} DEFERRABLE; END IF; END $ma$")
        tr = _translate_static(s)
        return [_conflict_clause(tr, self.engine).replace("%%", "%")] + extra

    def _drop_trigger(self, stmt: str) -> List[str]:
        m = re.match(r"^\s*DROP\s+TRIGGER\s+(IF\s+EXISTS\s+)?(\w+)\s*$", stmt, re.I)
        if not m:
            return [stmt]
        rows = self.execute_raw(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND t.tgname = %s", (m.group(2),)).fetchall()
        return [f"DROP TRIGGER IF EXISTS {m.group(2)} ON {r[0]}" for r in rows]

    def _special(self, sql: str) -> Optional[Cursor]:
        s = _lead(sql).rstrip(";").strip()
        up = s.upper()
        if up in ("BEGIN", "BEGIN TRANSACTION", "BEGIN DEFERRED"):
            self._begin(False)
            return Cursor([])
        if up in ("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
            self._begin(True)
            return Cursor([])
        if up in ("COMMIT", "END", "COMMIT TRANSACTION"):
            self.commit()
            return Cursor([])
        if up == "ROLLBACK":
            self.rollback()
            return Cursor([])
        if up.startswith("PRAGMA"):
            m = re.match(r"^PRAGMA\s+table_info\s*\(\s*\"?(\w+)\"?\s*\)$", s, re.I)
            if m:
                return Cursor(self.engine.table_info(m.group(1)))
            if re.match(r"^PRAGMA\s+(quick_check|integrity_check)", s, re.I):
                return Cursor([Row(("ok",), _index_of(["quick_check"]))])
            if re.match(r"^PRAGMA\s+foreign_key_check", s, re.I):
                return Cursor([])
            return Cursor([])
        if re.match(r"^(CREATE|DROP)\s+TRIGGER\b", up) or re.match(r"^CREATE\s+VIRTUAL\s+TABLE\b", up):
            for pg in self.ddl(s):
                self.execute_raw(pg)
            return Cursor([])
        m = re.match(r"^(CREATE|ALTER)\s+((TEMP|TEMPORARY)\s+)?(TABLE|INDEX|UNIQUE\s+INDEX)\b", up)
        if m:
            for pg in self.ddl(s):
                self.execute_raw(pg)
            if not m.group(2):              # временная таблица в каталог схемы не попадает
                self.engine.forget_catalog()
            return Cursor([])
        return None
