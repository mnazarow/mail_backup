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
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..errors import StorageError
from ..logging_setup import get_logger
from ..util import ensure_dir, disk_free_bytes

log = get_logger("snapshots")

SNAP_DIRNAME = "snapshots"
_NAME_RE = re.compile(r"^mailarchiver-(\d{8}T\d{6}Z)(?:-(\d{1,4}))?\.db\.gz(\.enc)?$")
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
                    "encrypted": bool(match.group(3)), "_order": (match.group(1), int(match.group(2) or 0))})
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


def make_snapshot(svc, *, keep: Optional[int] = None) -> Dict:
    """Снять снимок базы. Возвращает описание снимка.

    Порядок: backup API → проверка целостности копии (``PRAGMA quick_check``) →
    gzip → (шифрование) → атомарное переименование. Бракованный снимок не
    сохраняется: если проверка нашла повреждения, это повод немедленно
    разобраться с самой базой, а не хранить копию повреждённых данных.
    """
    cfg = svc.cfg
    folder = ensure_dir(snapshot_dir(cfg), 0o700)
    with _LOCK:
        _clean_leftovers(folder)
        db_size = 0
        for suffix in ("", "-wal"):
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
        raw_tmp = os.path.join(folder, f".tmp-{stamp}.db")
        gz_tmp = raw_tmp + ".gz"
        enc_tmp = gz_tmp + ".enc"
        started = time.time()
        try:
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
            cipher = svc.store.cipher if svc.store.encrypt else None
            if cipher is not None:
                size = os.path.getsize(gz_tmp)
                with open(gz_tmp, "rb") as fin, open(enc_tmp, "wb") as fout:
                    cipher.encrypt_stream(fin, fout, size)
                    fout.flush()
                    os.fsync(fout.fileno())
                os.unlink(gz_tmp)
                final_tmp, final_name = enc_tmp, f"mailarchiver-{stamp}.db.gz.enc"
            else:
                with open(gz_tmp, "rb") as fh:
                    os.fsync(fh.fileno())
                final_tmp, final_name = gz_tmp, f"mailarchiver-{stamp}.db.gz"
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


def restore_snapshot(snapshot_path: str, dest_db: str, cipher=None) -> int:
    """Развернуть снимок в файл базы ``dest_db`` (через временный файл).

    Зашифрованный снимок (``.enc``) требует ключ шифрования копии.
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
        for suffix in ("-wal", "-shm"):
            if os.path.exists(dest_db + suffix):
                os.unlink(dest_db + suffix)
        os.replace(tmp, dest_db)
        os.chmod(dest_db, 0o600)
        return os.path.getsize(dest_db)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise StorageError(f"Снимок не читается: {exc}") from exc
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
