"""
Снимки базы данных: согласованная копия ``mailarchiver.db`` на момент снятия.

В базе — индекс писем (без него архив нельзя ни просматривать, ни выгружать),
ящики с зашифрованными паролями, расписания, настройки и история. Снимок
делается «на ходу» средствами SQLite (backup API): служба и задания не
останавливаются, а в копию попадает целостное состояние на одну точку времени.

Файлы: ``<каталог данных>/snapshots/mailarchiver-ГГГГММДДTЧЧММССZ.db.gz`` — сжатые
gzip; если включено шифрование копии, снимок дополнительно шифруется тем же
ключом (``….db.gz.enc``) — чтобы при выносе на другой сервер по нему нельзя
было прочитать темы писем и адреса. Хранятся последние N снимков
(``replica.db_snapshot_keep``), копия вне сервера получает те же файлы.

База на PostgreSQL (1.24.0): снимок — выгрузка схемы службы программой ``pg_dump``
(SQL, ``mailarchiver-….sql.gz[.enc]``), тоже согласованная на момент начала;
восстанавливается командой ``mailarchiver restore-snapshot`` (нужен ``psql``). Перед ``psql`` выгрузка
проверяется (:class:`_DumpGuard`, 1.25.0): команды psql и операторы, которых pg_dump не пишет, — отказ.
"""
from __future__ import annotations

import gzip
import itertools
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..errors import StorageError
from ..logging_setup import get_logger
from ..util import ensure_dir, disk_free_bytes

log = get_logger("snapshots")

SNAP_DIRNAME = "snapshots"
_NAME_RE = re.compile(r"^mailarchiver-(\d{8}T\d{6}Z)(?:-(\d{1,4}))?\.(db|sql)\.gz(\.enc)?$")
#: Последняя строка выгрузки pg_dump: без неё выгрузка оборвана.
_PG_DUMP_END = b"PostgreSQL database dump complete"
_LOCK = threading.Lock()
_COPY_CHUNK = 1024 * 1024


def snapshot_dir(cfg) -> str:
    return os.path.join(cfg.data_dir, SNAP_DIRNAME)


def list_snapshots(cfg) -> List[Dict]:
    """Снимки, самые свежие первыми: имя, путь, размер, время (UTC), зашифрован ли."""
    folder = snapshot_dir(cfg)
    out = []
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return out
    for name in names:
        match = _NAME_RE.match(name)
        if not match:
            continue
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        out.append({"name": name, "path": path, "size": size, "created_at": created.isoformat(),
                    "encrypted": bool(match.group(4)),
                    "kind": "postgresql" if match.group(3) == "sql" else "sqlite", "_order": (match.group(1), int(match.group(2) or 0))})
    out.sort(key=lambda item: item["_order"], reverse=True)
    for item in out:
        del item["_order"]
    return out


def newest_age_s(cfg) -> Optional[float]:
    snaps = list_snapshots(cfg)
    if not snaps:
        return None
    created = datetime.fromisoformat(snaps[0]["created_at"])
    return (datetime.now(timezone.utc) - created).total_seconds()


def prune(cfg, keep: int) -> int:
    """Оставить ``keep`` самых свежих снимков (не меньше одного)."""
    keep = max(1, int(keep))
    removed = 0
    for item in list_snapshots(cfg)[keep:]:
        try:
            os.unlink(item["path"])
            removed += 1
        except OSError as exc:
            log.warning("Не удалось удалить старый снимок базы %s: %s", item["path"], exc)
    return removed


def _clean_leftovers(folder: str) -> None:
    for name in os.listdir(folder):
        if name.startswith(".tmp-"):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass


def pg_tool(name: str) -> str:
    """Путь к ``pg_dump``/``psql`` или понятная ошибка."""
    found = shutil.which(name)
    if not found:
        raise StorageError(f"Не найдена программа {name}: без неё снимок базы PostgreSQL не снять и не развернуть.",
                           hint="Установите клиент PostgreSQL той же или более новой версии, что и сервер базы "
                                "(Debian/Ubuntu: apt install postgresql-client-16).")
    return found


def _pg_dump(svc, gz_path: str) -> None:
    """Выгрузить схему службы в ``gz_path`` (SQL, gzip)."""
    from .. import pgcompat
    conninfo, schema = pgcompat.split_url(svc.cfg.db_url)
    dbname, env = pgcompat.libpq_env(conninfo)
    cmd = [pg_tool("pg_dump"), "--format=plain", "--no-owner", "--no-privileges", "--quote-all-identifiers",
           f"--schema={schema}", f"--dbname={dbname}"]
    tail = b""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, env=env)
        try:
            with gzip.open(gz_path, "wb", compresslevel=6) as out:
                while True:
                    chunk = proc.stdout.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    tail = (tail + chunk)[-4096:]
        finally:
            proc.stdout.close()
            code = proc.wait()
        err.seek(0)
        message = err.read().decode("utf-8", "replace").strip()
    if code != 0:
        hint = "Проверьте адрес базы и права пользователя базы."
        if "version mismatch" in message:
            hint = "Версия pg_dump старше сервера базы: установите клиент PostgreSQL той же версии, что сервер."
        raise StorageError(f"pg_dump завершился с ошибкой ({code}): {message[-500:]}", hint=hint)
    if _PG_DUMP_END not in tail:
        raise StorageError("Выгрузка базы PostgreSQL оборвалась (нет последней строки pg_dump).",
                           hint="Повторите снимок; если ошибка повторяется — проверьте журнал сервера базы.")


def make_snapshot(svc, *, keep: Optional[int] = None) -> Dict:
    """Снять снимок базы. Возвращает описание снимка.

    Порядок: backup API → проверка целостности копии (``PRAGMA quick_check``) →
    gzip → (шифрование) → атомарное переименование. Бракованный снимок не
    сохраняется: если проверка нашла повреждения, это повод немедленно
    разобраться с самой базой, а не хранить копию повреждённых данных.
    """
    cfg = svc.cfg
    folder = ensure_dir(snapshot_dir(cfg), 0o700)
    pg = bool(getattr(svc.db, "is_pg", False))
    with _LOCK:
        _clean_leftovers(folder)
        db_size = svc.db.size_bytes() if pg else 0
        for suffix in (() if pg else ("", "-wal")):
            try:
                db_size += os.path.getsize(cfg.db_path + suffix)
            except OSError:
                pass
        free = disk_free_bytes(folder)
        need = int(db_size * 1.6) + 64 * 1024 * 1024
        if free < need:
            raise StorageError(
                f"Недостаточно места для снимка базы: свободно {free // (1024 * 1024)} МБ, "
                f"нужно около {need // (1024 * 1024)} МБ.",
                hint="Освободите место в каталоге данных или уменьшите число хранимых снимков.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ext = "sql" if pg else "db"
        raw_tmp = os.path.join(folder, f".tmp-{stamp}.{ext}")
        gz_tmp = raw_tmp + ".gz"
        enc_tmp = gz_tmp + ".enc"
        started = time.time()
        try:
            if pg:
                _pg_dump(svc, gz_tmp)
            else:
                _sqlite_snapshot(cfg, raw_tmp, gz_tmp)
            cipher = svc.store.cipher if svc.store.encrypt else None
            if cipher is not None:
                size = os.path.getsize(gz_tmp)
                with open(gz_tmp, "rb") as fin, open(enc_tmp, "wb") as fout:
                    cipher.encrypt_stream(fin, fout, size)
                    fout.flush()
                    os.fsync(fout.fileno())
                os.unlink(gz_tmp)
                final_tmp, final_name = enc_tmp, f"mailarchiver-{stamp}.{ext}.gz.enc"
            else:
                with open(gz_tmp, "rb") as fh:
                    os.fsync(fh.fileno())
                final_tmp, final_name = gz_tmp, f"mailarchiver-{stamp}.{ext}.gz"
            final = os.path.join(folder, final_name)
            counter = 1
            while os.path.exists(final):
                # Два снимка в одну секунду (кнопка нажата дважды) — не затираем прежний.
                counter += 1
                final_name = final_name.replace(stamp, f"{stamp}-{counter}", 1) if counter == 2 \
                    else final_name.replace(f"{stamp}-{counter - 1}", f"{stamp}-{counter}", 1)
                final = os.path.join(folder, final_name)
            os.replace(final_tmp, final)
            os.chmod(final, 0o600)
        finally:
            for leftover in (raw_tmp, gz_tmp, enc_tmp):
                if os.path.exists(leftover):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
        info = {"name": final_name, "path": final, "size": os.path.getsize(final),
                "encrypted": cipher is not None, "seconds": round(time.time() - started, 1),
                "db_size": db_size}
        if keep is None:
            try:
                keep = int(svc.rt("replica", "db_snapshot_keep") or 3)
            except (TypeError, ValueError):
                keep = 3
        info["pruned"] = prune(cfg, keep)
        log.info("Снимок базы: %s (%.1f МБ из %.1f МБ, %.1f с).", final_name, info["size"] / 1048576,
                 db_size / 1048576, info["seconds"])
        return info


def _sqlite_snapshot(cfg, raw_tmp: str, gz_tmp: str) -> None:
    """Копия файла SQLite через backup API, проверка, gzip."""
    src = sqlite3.connect(cfg.db_path, timeout=60)
    dst = sqlite3.connect(raw_tmp)
    try:
        src.backup(dst)                     # одним шагом: согласованная копия на момент начала
        check = dst.execute("PRAGMA quick_check").fetchone()
        verdict = str(check[0]) if check else "?"
    finally:
        dst.close()
        src.close()
    if verdict.lower() != "ok":
        raise StorageError(
            f"Проверка целостности снимка базы нашла повреждения: {verdict[:300]}",
            hint="База данных службы повреждена. Остановите службу и проверьте её: "
                 "sqlite3 mailarchiver.db 'PRAGMA integrity_check'. Прежние снимки не удалены.")
    with open(raw_tmp, "rb") as fin, gzip.open(gz_tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, _COPY_CHUNK)
    os.unlink(raw_tmp)


def ensure_fresh(svc, max_age_s: float) -> Optional[Dict]:
    """Снять снимок, если самый свежий старше ``max_age_s`` (или снимков нет)."""
    age = newest_age_s(svc.cfg)
    if age is not None and age < max_age_s:
        return None
    return make_snapshot(svc)


def maybe_daily_snapshot(svc) -> Optional[Dict]:
    """Ежедневный снимок из часового обслуживания (если снимки включены)."""
    try:
        keep = int(svc.rt("replica", "db_snapshot_keep") or 0)
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return None
    return ensure_fresh(svc, 23 * 3600)


def restore_snapshot(snapshot_path: str, dest_db: str, cipher=None, *, keep_as: Optional[str] = None) -> int:
    """Развернуть снимок в файл базы ``dest_db`` (через временный файл).

    Зашифрованный снимок (``.enc``) требует ключ шифрования копии. Существующая база (с ``-wal``/``-shm``)
    переименовывается в ``keep_as`` (без него — заменяется) **только после** того, как новый файл
    распакован и проверен: битый снимок или неверный ключ оставляют прежнюю базу на месте (1.25.0).
    Возвращает размер получившейся базы.
    """
    if not os.path.isfile(snapshot_path):
        raise StorageError(f"Файл снимка не найден: {snapshot_path}")
    tmp = dest_db + ".restore-tmp"
    try:
        with open(snapshot_path, "rb") as raw:
            stream = raw
            reader = None
            if snapshot_path.endswith(".enc"):
                if cipher is None:
                    raise StorageError("Снимок зашифрован, а ключ шифрования не загружен.",
                                       hint="Укажите файл ключа (--key) — тот, которым шифровался архив.")
                reader = cipher.open_reader(raw)
                stream = reader
            try:
                with gzip.GzipFile(fileobj=stream, mode="rb") as gz, open(tmp, "wb") as out:
                    shutil.copyfileobj(gz, out, _COPY_CHUNK)
            finally:
                if reader is not None:
                    reader.close()
        conn = sqlite3.connect(tmp)
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if str(verdict).lower() != "ok":
            raise StorageError(f"Снимок повреждён: {verdict}")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(dest_db + suffix):
                if keep_as:
                    os.replace(dest_db + suffix, keep_as + suffix)
                elif suffix:
                    os.unlink(dest_db + suffix)
        os.replace(tmp, dest_db)
        os.chmod(dest_db, 0o600)
        return os.path.getsize(dest_db)
    except (OSError, EOFError, gzip.BadGzipFile, sqlite3.DatabaseError) as exc:
        raise StorageError(f"Снимок не читается: {exc}", hint="База не изменена.") from exc
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _DumpGuard:
    r"""Проверка выгрузки перед ``psql`` (1.25.0): ``psql`` выполняет не только SQL, но и свои команды
    (``\! команда`` — команда оболочки), а снимок мог подменить тот, кто может писать в копию вне сервера.

    Пропускается только то, что пишет ``pg_dump`` для схемы службы: команды SET, создание и изменение таблиц,
    индексов, функций на sql/plpgsql, триггеров, ``COPY … FROM stdin`` с данными, ``setval``. Обратная косая черта
    вне строк и данных COPY — отказ (строки ``\restrict``/``\unrestrict`` новых pg_dump убираются: старый psql
    их не знает). Строки и идентификаторы в кавычках, строки в долларах и комментарии разбираются как в psql.
    """

    _SET_OK = {"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout", "transaction_timeout",
               "client_encoding", "standard_conforming_strings", "check_function_bodies", "xmloption",
               "client_min_messages", "row_security", "default_tablespace", "default_table_access_method",
               "search_path", "default_with_oids"}
    _STMT_OK = re.compile(
        r"^(?:CREATE\s+(?:SCHEMA|TABLE|UNLOGGED\s+TABLE|SEQUENCE|INDEX|UNIQUE\s+INDEX|FUNCTION|OR\s+REPLACE\s+"
        r"FUNCTION|TRIGGER|OR\s+REPLACE\s+TRIGGER|CONSTRAINT\s+TRIGGER|VIEW|OR\s+REPLACE\s+VIEW|TYPE|DOMAIN)\b"
        r"|ALTER\s+(?:TABLE|SEQUENCE|SCHEMA|FUNCTION|INDEX|VIEW|TYPE|DOMAIN|TRIGGER)\b|COMMENT\s+ON\b)", re.I)
    _COPY = re.compile(r'^COPY\s+(?:"[^"]+"|\w+)(?:\.(?:"[^"]+"|\w+))?\s*(?:\([^()]*\))?\s+FROM\s+stdin$', re.I)
    _SETVAL = re.compile(r"^SELECT\s+pg_catalog\.setval\('[^']*',\s*\d+,\s*(?:true|false)\)$", re.I)
    _SET_CONFIG = re.compile(r"^SELECT\s+pg_catalog\.set_config\('search_path',\s*'[^']*',\s*false\)$", re.I)
    _SET = re.compile(r"^SET\s+(\w+)\s*(?:=|TO)\s*(.+)$", re.I | re.S)
    _LANG = re.compile(r'\bLANGUAGE\s+"?(\w+)"?', re.I)
    _RESTRICT = re.compile(rb"^\\(?:un)?restrict [A-Za-z0-9]{1,128}\r?\n?$")
    _DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")
    MAX_STMT = 1 << 20

    def __init__(self):
        self.copy = False
        self.quote = None          # None | "'" | "e'" | '"' | "$тег$"
        self.block = 0             # вложенность /* */
        self.raw: List[str] = []   # текст оператора целиком (для проверки SET и setval)
        self.code: List[str] = []  # текст оператора без строк и комментариев
        self.size = 0
        self.line_no = 0

    def _fail(self, what: str):
        raise StorageError(f"Снимок отклонён: {what} (строка {self.line_no}). Так pg_dump не пишет — файл мог "
                           f"быть изменён.", hint="База не изменена. Восстанавливайте снимки только из своей "
                                                  "копии; зашифрованные снимки (.enc) подделать без ключа нельзя.")

    def feed(self, line: bytes) -> bytes:
        """Строка выгрузки → что передать psql (b'' — убрать строку)."""
        self.line_no += 1
        if self.copy:
            if line.rstrip(b"\r\n") == b"\\.":
                self.copy = False
            return line
        if (self._RESTRICT.match(line) and self.quote is None and not self.block
                and not "".join(self.code).strip()):
            return b""
        self._lex(line.decode("utf-8", "replace"))
        return line

    def finish(self) -> None:
        if self.copy or self.quote or self.block or "".join(self.code).strip():
            raise StorageError("Снимок оборван: выгрузка закончилась посреди оператора или данных.",
                               hint="База не изменена.")

    def _end(self) -> None:
        code = re.sub(r"\s+", " ", "".join(self.code)).strip()
        raw = re.sub(r"\s+", " ", "".join(self.raw)).strip()
        self.code, self.raw, self.size = [], [], 0
        if not code:
            return
        head = code.split(" ", 1)[0].upper()
        if head == "SET":
            m = self._SET.match(raw)
            name = (m.group(1).lower() if m else "")
            if not m or name not in self._SET_OK:
                self._fail(f"команда «{raw[:80]}»")
            if name == "standard_conforming_strings" and m.group(2).strip().strip("'").lower() != "on":
                self._fail("standard_conforming_strings — только on")
            return
        if head == "SELECT":
            if not (self._SETVAL.match(raw) or self._SET_CONFIG.match(raw)):
                self._fail(f"запрос «{raw[:80]}»")
            return
        if head == "COPY":
            if not self._COPY.match(code):
                self._fail(f"COPY не из выгрузки («{code[:80]}»)")
            self.copy = True
            return
        if not self._STMT_OK.match(code):
            self._fail(f"оператор «{code[:80]}»")
        if re.match(r"^CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b", code, re.I):
            lang = self._LANG.search(code)
            if not lang or lang.group(1).lower() not in ("sql", "plpgsql"):
                self._fail("функция не на sql/plpgsql")

    def _add(self, raw: str, code: Optional[str] = None) -> None:
        """В текст оператора: ``raw`` — как есть, ``code`` — в текст без строк (по умолчанию то же)."""
        self.size += len(raw)
        if self.size > self.MAX_STMT:
            self._fail("слишком длинный оператор")
        self.raw.append(raw)
        self.code.append(raw if code is None else code)

    def _lex(self, text: str) -> None:
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            q = self.quote
            if self.block:                              # /* … */, могут быть вложенными
                if text.startswith("*/", i):
                    self.block -= 1
                    i += 2
                elif text.startswith("/*", i):
                    self.block += 1
                    i += 2
                else:
                    i += 1
                continue
            if q is not None:
                if q == "e'" and c == "\\":             # E'…': \' и \\ — внутри строки
                    self._add(text[i:i + 2], "")
                    i += 2
                    continue
                close = "'" if q == "e'" else q
                if text.startswith(close, i):
                    self.quote = None
                    self._add(close, close if q == '"' else "")
                    i += len(close)
                    continue
                self._add(c, c if q == '"' else "")
                i += 1
                continue
            if text.startswith("--", i):
                break                                   # комментарий до конца строки
            if text.startswith("/*", i):
                self.block = 1
                i += 2
                continue
            if c == "'":
                tail = "".join(self.code[-3:])[-2:]
                e_string = tail[-1:] in ("E", "e") and not (tail[:-1].isalnum() or tail[:-1] == "_")
                self.quote = "e'" if e_string else "'"
                self._add(c, "''")
                i += 1
                continue
            if c == '"':
                self.quote = '"'
                self._add(c)
                i += 1
                continue
            if c == "$":
                m = self._DOLLAR.match(text, i)
                prev = text[i - 1] if i else " "
                if m and not (prev.isalnum() or prev == "_"):
                    self.quote = m.group(0)
                    self._add(m.group(0), " $$ ")
                    i = m.end()
                    continue
            if c == "\\":
                self._fail("команда psql (обратная косая черта вне строки)")
            if c == ";":
                rest = text[i + 1:]
                self._end()
                if self.copy and rest.strip():
                    self._fail("после COPY … FROM stdin на той же строке что-то есть")
                i += 1
                continue
            self._add(c)
            i += 1


def restore_pg_snapshot(snapshot_path: str, url: str, cipher=None, *, keep_as: Optional[str] = None) -> int:
    """Развернуть выгрузку PostgreSQL (``….sql.gz[.enc]``) в схему службы из адреса ``url``.

    Всё — одной транзакцией ``psql``, вместе с тем, что делается с прежней схемой службы: она
    переименовывается в ``keep_as`` (если в ней есть таблицы — без ``keep_as`` отказ) или удаляется (пустая).
    Ошибка, оборванная или подозрительная выгрузка (см. :class:`_DumpGuard`) — ничего не меняется, прежняя
    схема на месте под своим именем (1.25.0). Снимок другой схемы разворачивается под именем схемы службы.
    Возвращает размер выгрузки (байт)."""
    from .. import pgcompat
    if not os.path.isfile(snapshot_path):
        raise StorageError(f"Файл снимка не найден: {snapshot_path}")
    psql = pg_tool("psql")
    conninfo, schema = pgcompat.split_url(url)
    engine = pgcompat.Engine(url, 2)
    try:
        engine.pool()                       # функции и правило сравнения, которые нужны таблицам службы
        session = pgcompat.Session(engine)
        tables = int(session.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname = ?", (schema,))
                     .fetchone()[0] or 0)
    finally:
        engine.close()
    if tables and not keep_as:
        raise StorageError(f"В схеме «{schema}» уже есть база службы.", hint="База не изменена.")
    if keep_as and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", keep_as):
        raise StorageError(f"Недопустимое имя схемы: {keep_as}")
    dbname, env = pgcompat.libpq_env(conninfo)
    total = 0
    guard = _DumpGuard()
    refused: Optional[StorageError] = None
    with open(snapshot_path, "rb") as raw:
        stream = raw
        reader = None
        if snapshot_path.endswith(".enc"):
            if cipher is None:
                raise StorageError("Снимок зашифрован, а ключ шифрования не загружен.",
                                   hint="Укажите файл ключа (--key) — тот, которым шифровался архив.")
            reader = cipher.open_reader(raw)
            stream = reader
        try:
            with gzip.GzipFile(fileobj=stream, mode="rb") as gz, tempfile.TemporaryFile() as err:
                head: List[bytes] = []
                source, seen = "", 0
                for line in gz:                         # схема выгрузки — в первых строках
                    head.append(line)
                    seen += len(line)
                    found = re.match(rb'CREATE SCHEMA "([a-z_][a-z0-9_]{0,62})";', line)
                    if found:
                        source = found.group(1).decode()
                        break
                    if seen > 256 * 1024:
                        break
                if not source:
                    raise StorageError("Это не снимок базы службы: в выгрузке нет схемы.", hint="База не изменена.")
                proc = subprocess.Popen([psql, "--quiet", "--no-psqlrc", "--single-transaction", "-v", "ON_ERROR_STOP=1",
                                         f"--dbname={dbname}"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=err, env=env)
                tail = b""
                try:
                    prelude = "SET standard_conforming_strings = on;\n"
                    if tables:
                        prelude += f'ALTER SCHEMA "{schema}" RENAME TO "{keep_as}";\n'
                    else:
                        prelude += f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;\n'
                    proc.stdin.write(prelude.encode())
                    try:
                        for line in itertools.chain(head, gz):
                            total += len(line)
                            tail = (tail + line)[-4096:]
                            out = guard.feed(line)
                            if out:
                                proc.stdin.write(out)
                        guard.finish()
                    except StorageError as exc:
                        refused = exc
                        proc.kill()                     # транзакция не завершена — откат
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass
                    if refused is None:
                        if _PG_DUMP_END not in tail:
                            proc.stdin.write(b"\nSELECT 'dump is truncated'::int;\n")   # ошибка → откат
                        elif source != schema:
                            proc.stdin.write(f'\nALTER SCHEMA "{source}" RENAME TO "{schema}";\n'.encode())
                        proc.stdin.close()
                except BrokenPipeError:
                    pass
                code = proc.wait()
                err.seek(0)
                message = err.read().decode("utf-8", "replace").strip()
        finally:
            if reader is not None:
                reader.close()
    if refused is not None:
        raise refused
    if _PG_DUMP_END not in tail and (code == 0 or "dump is truncated" in message):
        raise StorageError("Снимок оборван (нет последней строки pg_dump).", hint="База не изменена.")
    if code != 0:
        raise StorageError(f"psql завершился с ошибкой ({code}): {message[-500:]}",
                           hint="База не изменена (всё в одной транзакции). Если схема с именем из снимка уже "
                                "есть в базе — переименуйте или удалите её.")
    return total
