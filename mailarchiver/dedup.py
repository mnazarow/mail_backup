"""
Отчёт «Одинаковые вложения»: сколько места освободилось бы, если бы одно и то же
вложение хранилось в архиве один раз.

Одинаковые вложения в почте — обычное дело: рассылка с прайсом всем
сотрудникам (580 копий одного PDF), логотип в подписи каждого письма, договор,
пересланный по цепочке десять раз. Сейчас каждая копия лежит в своём файле
письма. Прежде чем менять формат хранения, стоит посчитать выигрыш на своих
письмах — это и делает отчёт. Формат хранения НЕ меняется.

Как считается. Каждое письмо читается потоково (``mimestream``): вложения
раскодируются порциями и хешируются (SHA-256), память — порядка одной строки
письма при любом его размере; зашифрованные и сжатые копии читаются так же, как
при просмотре. Для каждого вложения в базе (таблица ``attach_hashes``)
запоминаются отпечаток содержимого, размер файла и сколько вложение занимает
внутри письма (в base64 — примерно на треть больше самого файла). Одинаковыми
считаются вложения с одинаковым СОДЕРЖИМЫМ: имя файла может отличаться.

Экономия = место всех копий минус по одной копии каждого уникального вложения.
Подсчёт инкрементальный: следующий запуск читает только новые письма, а письма,
удалённые из архива (срок хранения, удаление ящика), выпадают из таблицы сами —
триггером. Большой архив читается частями: между ними очередь пропускает
копирования ящиков.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .errors import JobCancelled
from .logging_setup import get_logger
from .util import human_size

log = get_logger("dedup")

TABLE = "attach_hashes"
_META_WATERMARK = "dedup_last_id"
#: «Поколение» подсчёта: «Посчитать заново» его меняет, и проход, начатый до
#: этого, не запишет свою порцию поверх очищенной таблицы.
_META_GEN = "dedup_gen"
_META_ERRORS = "dedup_errors"
#: Где хранится готовый отчёт (как и результат глубокого анализа — в settings).
REPORT_KEY = "analytics.dedup"
#: Сколько писем читать за один проход (одна транзакция).
BATCH = 100
#: Вложения меньше этого не учитываются: экономия на них копеечная, а ссылка
#: на общую копию стоила бы столько же, сколько сама копия.
MIN_SIZE = 1024
#: Имя файла и тип в таблице обрезаются — они нужны только для отчёта.
_NAME_CAP = 100
_CTYPE_CAP = 127         # длинные типы Office (…wordprocessingml.document) — больше 70 символов
#: Строк в списках отчёта.
TOP_N = 20

#: Понятные названия частых типов вложений.
_TYPE_LABELS = {
    "application/pdf": "PDF",
    "image/jpeg": "Фото JPEG", "image/jpg": "Фото JPEG", "image/pjpeg": "Фото JPEG",
    "image/png": "Картинки PNG", "image/gif": "Картинки GIF", "image/bmp": "Картинки BMP",
    "image/tiff": "Сканы TIFF", "image/webp": "Картинки WebP", "image/heic": "Фото HEIC",
    "application/msword": "Word (.doc)",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (.docx)",
    "application/vnd.ms-excel": "Excel (.xls)",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel (.xlsx)",
    "application/vnd.ms-powerpoint": "PowerPoint (.ppt)",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint (.pptx)",
    "application/zip": "Архивы ZIP", "application/x-zip-compressed": "Архивы ZIP",
    "application/x-rar-compressed": "Архивы RAR", "application/vnd.rar": "Архивы RAR",
    "application/x-7z-compressed": "Архивы 7z",
    "message/rfc822": "Вложенные письма",
    "application/octet-stream": "Без типа (octet-stream)",
    "text/calendar": "Приглашения (ics)",
    "video/mp4": "Видео MP4", "audio/mpeg": "Аудио MP3",
}


# ---------------------------------------------------------------------------
#  Схема (вызывается из Database.init_schema)
# ---------------------------------------------------------------------------
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    hash       BLOB NOT NULL,
    message_id INTEGER NOT NULL,
    part       INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    enc_size   INTEGER NOT NULL,
    ctype      TEXT NOT NULL DEFAULT '',
    filename   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (hash, message_id, part)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_attach_hashes_msg ON {TABLE}(message_id);
CREATE TRIGGER IF NOT EXISTS messages_attach_ad AFTER DELETE ON messages BEGIN
    DELETE FROM {TABLE} WHERE message_id = old.id;
END;
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
#  Разбор одного письма
# ---------------------------------------------------------------------------
def scan_message(fh, *, min_size: int = MIN_SIZE,
                 cancelled: Optional[Callable[[], bool]] = None) -> List[Tuple]:
    """Вложения письма: ``(номер, отпечаток16, размер, место_в_письме, тип, имя)``.

    Номер — порядковый среди вложений письма (как в просмотрщике); вложения
    меньше ``min_size`` в результат не попадают, но номер на них расходуется.
    """
    from . import mimestream
    from .mailview import _is_attachment

    out: List[Tuple] = []
    idx = 0
    parts = 0
    cur = None          # [leaf, номер, хеш, декодер, размер, место в письме]
    chunks = 0
    events = mimestream.scan(fh)
    try:
        for kind, leaf, payload in events:
            if kind == "top":
                continue
            if kind == "leaf":
                parts += 1
                if parts > mimestream.MAX_PARTS:
                    break
                if _is_attachment(leaf.ctype, leaf.disp, leaf.filename):
                    leaf.want = True
                    cur = [leaf, idx, hashlib.sha256(), mimestream.decoder_for(leaf.cte), 0, 0]
                    idx += 1
                continue
            if cur is None or cur[0] is not leaf:
                continue
            if kind == "data":
                cur[5] += len(payload)
                data = cur[3].feed(payload)
                if data:
                    cur[2].update(data)
                    cur[4] += len(data)
                chunks += 1
                if cancelled is not None and chunks % 64 == 0 and cancelled():
                    raise JobCancelled("Подсчёт прерван.")
            elif kind == "end":
                tail = cur[3].close()
                if tail:
                    cur[2].update(tail)
                    cur[4] += len(tail)
                if cur[4] >= min_size:
                    out.append((cur[1], cur[2].digest()[:16], cur[4], cur[5],
                                (leaf.ctype or "")[:_CTYPE_CAP], (leaf.filename or "")[:_NAME_CAP]))
                cur = None
    finally:
        events.close()
    return out


# ---------------------------------------------------------------------------
#  Состояние подсчёта
# ---------------------------------------------------------------------------
def pending_count(db) -> int:
    last = int(db.get_meta(_META_WATERMARK) or 0)
    return int(db.scalar("SELECT COUNT(*) FROM messages WHERE id > ?", (last,)) or 0)


def status(svc) -> Dict:
    db = svc.db
    total = db.count_messages()
    pending = pending_count(db)
    return {"total": total, "scanned": max(0, total - pending), "pending": pending,
            "errors": int(db.get_meta(_META_ERRORS) or 0),
            "attachments": int(db.scalar(f"SELECT COUNT(*) FROM {TABLE}") or 0)}


def reset(db) -> None:
    """Забыть все отпечатки — следующий проход прочитает архив заново."""
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {TABLE}")
        for key, value in ((_META_WATERMARK, "0"), (_META_ERRORS, "0")):
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value", (key, value))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
                     "value=CAST(COALESCE(value, '0') AS INTEGER) + 1", (_META_GEN,))


def process_pending(svc, *, progress=None, cancelled=None, event=None,
                    min_seconds: Optional[float] = None, should_yield: Optional[Callable[[], bool]] = None,
                    max_seconds: Optional[float] = None) -> Dict:
    """Прочитать письма, появившиеся после прошлого прохода, и запомнить отпечатки вложений.

    Проход заканчивается, когда писем не осталось, по отмене, по истечении
    ``max_seconds`` — или, если прошло ``min_seconds``, когда ``should_yield()``
    говорит, что слот очереди ждут другие задания (копирования ящиков).
    Прочитанное сохраняется порциями — прерванный проход продолжится с места.
    """
    db = svc.db
    total_pending = pending_count(db)
    done = errors = found = 0
    started = time.time()
    gen = db.get_meta(_META_GEN) or "0"
    last = int(db.get_meta(_META_WATERMARK) or 0)
    stopped = ""
    while True:
        if cancelled is not None and cancelled():
            stopped = "cancelled"
            break
        spent = time.time() - started
        if done and max_seconds is not None and spent > max_seconds:
            stopped = "slice"
            break
        if done and min_seconds is not None and spent > min_seconds and should_yield is not None \
                and should_yield():
            stopped = "slice"            # хоть порцию, но прочитали — уступаем слот
            break
        rows = db.query("SELECT id, account_id, stored_path FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                        (last, BATCH))
        if not rows:
            break
        batch: List[Tuple] = []
        batch_rows = batch_errors = 0
        interrupted = False
        for row in rows:
            rel = row["stored_path"] or ""
            if rel:
                try:
                    fh = svc.store.open_message(row["account_id"], rel)
                    try:
                        items = scan_message(fh, cancelled=cancelled)
                    finally:
                        try:
                            fh.close()
                        except Exception:  # noqa: BLE001
                            pass
                    for part, digest, size, enc, ctype, name in items:
                        batch.append((digest, int(row["id"]), part, int(row["account_id"]), size, enc, ctype, name))
                except JobCancelled:
                    interrupted = True          # это письмо дочитаем в следующий раз
                    break
                except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа, битое письмо
                    batch_errors += 1
                    if errors + batch_errors <= 20 and event is not None:
                        event("WARNING", f"Письмо #{row['id']} не прочитано: "
                                         f"{getattr(exc, 'message', None) or exc}")
            last = int(row["id"])
            batch_rows += 1
        if batch_rows:
            with db.transaction() as conn:
                now_gen = conn.execute("SELECT value FROM meta WHERE key=?", (_META_GEN,)).fetchone()
                if (now_gen[0] if now_gen else "0") != gen:
                    stopped = "reset"           # подсчёт начали заново — эта порция устарела
                    break
                conn.executemany(f"INSERT OR REPLACE INTO {TABLE}(hash, message_id, part, account_id, size, "
                                 f"enc_size, ctype, filename) VALUES(?,?,?,?,?,?,?,?)", batch)
                conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                             "value=excluded.value", (_META_WATERMARK, str(last)))
                if batch_errors:
                    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                                 "value=CAST(COALESCE(value, '0') AS INTEGER) + ?",
                                 (_META_ERRORS, str(batch_errors), batch_errors))
            done += batch_rows
            errors += batch_errors
            found += len(batch)
            if progress is not None:
                progress(done, max(total_pending, done),
                         f"Прочитано писем: {done} из {total_pending}, вложений учтено: {found}")
        if interrupted:
            stopped = "cancelled"
            break
    return {"processed": done, "attachments": found, "errors": errors, "pending": pending_count(db),
            "stopped": stopped}


# ---------------------------------------------------------------------------
#  Отчёт
# ---------------------------------------------------------------------------
def _type_label(ctype: str) -> str:
    ctype = (ctype or "").lower()
    if ctype in _TYPE_LABELS:
        return _TYPE_LABELS[ctype]
    if ctype.startswith("image/"):
        return "Картинки " + ctype.split("/", 1)[1].upper()[:10]
    return ctype or "(тип не указан)"


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def build_report(svc) -> Dict:
    """Сводка по таблице отпечатков (только чтение базы — всё считает SQLite)."""
    db = svc.db
    st = status(svc)
    last = int(db.get_meta(_META_WATERMARK) or 0)
    archive_bytes = int(db.scalar("SELECT COALESCE(SUM(size), 0) FROM messages WHERE id <= ?", (last,)) or 0)
    row = db.query_one(f"SELECT COUNT(*) AS c, COALESCE(SUM(enc_size), 0) AS enc, COALESCE(SUM(size), 0) AS dec "
                       f"FROM {TABLE}")
    att_count, att_enc, att_dec = int(row["c"]), int(row["enc"]), int(row["dec"])
    row = db.query_one(
        f"SELECT COUNT(*) AS groups, COALESCE(SUM(keep_enc), 0) AS keep_enc, COALESCE(SUM(keep_dec), 0) AS keep_dec, "
        f"COALESCE(SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END), 0) AS dup_groups, "
        f"COALESCE(SUM(CASE WHEN c > 1 THEN c - 1 ELSE 0 END), 0) AS extra "
        f"FROM (SELECT COUNT(*) AS c, MAX(enc_size) AS keep_enc, MAX(size) AS keep_dec FROM {TABLE} GROUP BY hash)")
    unique_count, keep_enc, keep_dec = int(row["groups"]), int(row["keep_enc"]), int(row["keep_dec"])
    dup_groups, extra_copies = int(row["dup_groups"]), int(row["extra"])
    savings = max(0, att_enc - keep_enc)
    within = int(db.scalar(
        f"SELECT COALESCE(SUM(s), 0) FROM (SELECT SUM(enc_size) - MAX(enc_size) AS s FROM {TABLE} "
        f"GROUP BY hash, account_id HAVING COUNT(*) > 1)") or 0)
    # Если хранить вложения отдельно и раскодированными, уходит и «лишняя треть» base64.
    separate_store = max(0, att_enc - keep_dec)

    top = []
    for r in db.query(
            f"SELECT hash, COUNT(*) AS copies, COUNT(DISTINCT account_id) AS boxes, MAX(size) AS size, "
            f"SUM(enc_size) - MAX(enc_size) AS wasted FROM {TABLE} GROUP BY hash HAVING COUNT(*) > 1 "
            f"ORDER BY wasted DESC LIMIT ?", (TOP_N,)):
        ex = db.query_one(
            f"SELECT a.filename, a.ctype, a.message_id, a.account_id, m.folder FROM {TABLE} a "
            f"LEFT JOIN messages m ON m.id = a.message_id WHERE a.hash = ? ORDER BY a.message_id LIMIT 1",
            (r["hash"],))
        span = db.query_one(
            f"SELECT MIN(m.internaldate) AS first, MAX(m.internaldate) AS last, "
            f"COUNT(DISTINCT a.filename) AS names FROM {TABLE} a "
            f"JOIN messages m ON m.id = a.message_id WHERE a.hash = ?", (r["hash"],))
        top.append({
            "filename": (ex["filename"] if ex else "") or "(без имени)",
            "names": int(span["names"] or 1) if span else 1,
            "ctype": ex["ctype"] if ex else "", "type_label": _type_label(ex["ctype"] if ex else ""),
            "size": int(r["size"]), "size_h": human_size(int(r["size"])),
            "copies": int(r["copies"]), "mailboxes": int(r["boxes"]),
            "wasted": int(r["wasted"]), "wasted_h": human_size(int(r["wasted"])),
            "first": (span["first"] or "") if span else "", "last": (span["last"] or "") if span else "",
            "example": {"message_id": int(ex["message_id"]), "account_id": int(ex["account_id"]),
                        "folder": ex["folder"] or ""} if ex else None,
        })

    by_type: Dict[str, Dict] = {}
    for r in db.query(
            f"SELECT ctype, SUM(w) AS wasted, SUM(c - 1) AS copies FROM ("
            f"SELECT MAX(ctype) AS ctype, COUNT(*) AS c, SUM(enc_size) - MAX(enc_size) AS w FROM {TABLE} "
            f"GROUP BY hash HAVING COUNT(*) > 1) GROUP BY ctype"):
        label = _type_label(r["ctype"])
        item = by_type.setdefault(label, {"label": label, "value": 0, "copies": 0})
        item["value"] += int(r["wasted"] or 0)
        item["copies"] += int(r["copies"] or 0)
    types = sorted(by_type.values(), key=lambda x: -x["value"])[:10]
    for item in types:
        item["value_h"] = human_size(item["value"])

    names = {a.id: a.name for a in db.list_accounts()}
    boxes = []
    for r in db.query(
            f"SELECT account_id, COUNT(*) AS c, SUM(enc_size) AS b FROM {TABLE} WHERE hash IN "
            f"(SELECT hash FROM {TABLE} GROUP BY hash HAVING COUNT(*) > 1) "
            f"GROUP BY account_id ORDER BY b DESC LIMIT 10"):
        boxes.append({"account_id": int(r["account_id"]),
                      "label": names.get(int(r["account_id"]), f"#{r['account_id']}"),
                      "value": int(r["b"] or 0), "value_h": human_size(int(r["b"] or 0)), "count": int(r["c"])})

    # Письма-двойники целиком (одно и то же письмо в двух папках и т.п.) — по
    # отпечатку из индекса писем, без чтения файлов. Их вложения уже учтены выше.
    row = db.query_one(
        "SELECT COALESCE(SUM(c - 1), 0) AS copies, COALESCE(SUM((c - 1) * sz), 0) AS bytes FROM ("
        "SELECT COUNT(*) AS c, MAX(size) AS sz FROM messages WHERE id <= ? AND sha256 IS NOT NULL "
        "AND sha256 <> '' GROUP BY sha256 HAVING COUNT(*) > 1)", (last,))
    whole = {"copies": int(row["copies"] or 0), "bytes": int(row["bytes"] or 0),
             "bytes_h": human_size(int(row["bytes"] or 0))}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {"total": st["total"], "scanned": st["scanned"], "pending": st["pending"],
                     "errors": st["errors"], "complete": st["pending"] == 0,
                     "percent": _pct(st["scanned"], st["total"]) if st["total"] else 100.0},
        "min_size": MIN_SIZE, "min_size_h": human_size(MIN_SIZE),
        "archive_bytes": archive_bytes, "archive_h": human_size(archive_bytes),
        "attachments": {"count": att_count, "bytes": att_enc, "bytes_h": human_size(att_enc),
                        "decoded_bytes": att_dec, "decoded_h": human_size(att_dec),
                        "percent_of_archive": _pct(att_enc, archive_bytes)},
        "unique": {"count": unique_count, "bytes": keep_enc, "bytes_h": human_size(keep_enc)},
        "duplicates": {"copies": extra_copies, "groups": dup_groups},
        "savings": {"bytes": savings, "bytes_h": human_size(savings),
                    "percent_of_archive": _pct(savings, archive_bytes),
                    "percent_of_attachments": _pct(savings, att_enc),
                    "within_mailbox": within, "within_mailbox_h": human_size(within),
                    "separate_store": separate_store, "separate_store_h": human_size(separate_store),
                    "separate_store_percent": _pct(separate_store, archive_bytes)},
        "compressed": bool(getattr(svc.store, "compress", False)),
        "encrypted": bool(getattr(svc.store, "encrypt", False)),
        "top": top, "by_type": types, "by_account": boxes, "whole_messages": whole,
    }


def load_report(svc) -> Optional[Dict]:
    return svc.db.get_setting(REPORT_KEY, None)


def save_report(svc, report: Dict) -> None:
    svc.db.set_setting(REPORT_KEY, report)


def summary_text(report: Dict) -> str:
    s, cov = report["savings"], report["coverage"]
    text = (f"Одинаковых вложений: {report['duplicates']['copies']} лишних копий "
            f"({report['duplicates']['groups']} разных файлов). Хранение одной копии освободило бы "
            f"{s['bytes_h']} — {s['percent_of_archive']}% архива.")
    if not cov["complete"]:
        text += f" Прочитано {cov['percent']}% писем — подсчёт продолжится."
    return text
