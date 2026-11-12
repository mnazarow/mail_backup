"""
Поиск по письмам архива: тема, отправитель и получатели, имена вложений и
текст письма — во всех ящиках или в одном.

Индекс — полнотекстовая таблица SQLite FTS5 ``mail_fts`` (rowid = id письма в
``messages``). Новые письма индексируются фоновым заданием «Индексация поиска»
(планировщик запускает его, когда появились непроиндексированные письма);
удалённые из архива письма удаляются из индекса триггером. Первое
индексирование большого архива идёт долго (читается каждое письмо) — поиск
работает сразу, по уже проиндексированной части.

Текст письма в индексе хранится открытым — поэтому при включённом шифровании
копии текст НЕ индексируется (если явно не разрешено настройкой): иначе база
раскрывала бы то, что шифрование должно прятать. Тема и адреса и так лежат в
базе открыто (в индексе писем).

Если SQLite собран без FTS5, поиск работает «по-простому»: по теме и
отправителю из индекса писем (медленнее, без текста писем).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from email.utils import getaddresses
from typing import Dict, Iterable, List, Optional, Tuple

from .logging_setup import get_logger
from .util import decode_mime_header

log = get_logger("search")

FTS_TABLE = "mail_fts"
_META_WATERMARK = "search_last_id"
#: «Поколение» индекса: перестройка его меняет, и проход, начатый до неё, не
#: запишет свою порцию поверх очищенного индекса.
_META_GEN = "search_gen"
#: Сколько писем индексировать за один проход (одна транзакция).
BATCH = 200
#: Сколько закодированного тела письма читать ради текста (дальше — не нужно).
TEXT_CAP_BYTES = 256 * 1024
#: Поле «кому/копия» в индексе ограничено: рассылки на тысячи адресов раздували бы индекс.
ADDRS_CAP = 4000
#: Сколько результатов отдавать за раз.
PAGE = 50

_FIELD_ALIASES = {
    "тема": "subject", "subject": "subject",
    "от": "addrs", "from": "addrs", "кому": "addrs", "to": "addrs", "адрес": "addrs",
    "вложение": "attach", "attach": "attach", "файл": "attach",
    "текст": "body", "body": "body",
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_QUOTE_LINE_RE = re.compile(r"^\s*>")
_REPLY_MARKERS = re.compile(
    r"^(?:-{2,}\s*(?:original message|исходное сообщение|пересылаемое сообщение|forwarded message)\s*-{2,}"
    r"|.{0,200}\b(?:wrote|написал(?:\(а\))?|пишет)\s*:\s*$)", re.IGNORECASE)
_HEADER_BLOCK_RE = re.compile(r"^(?:от|from)\s*:.*$", re.IGNORECASE)
_HEADER_FOLLOW_RE = re.compile(r"^(?:отправлено|дата|sent|date)\s*:", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_DROP_BLOCKS_RE = re.compile(r"<(script|style|head)[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h\d)\s*/?\s*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
#  Схема
# ---------------------------------------------------------------------------
def ensure_schema(conn) -> bool:
    """Создать таблицу FTS5 и триггер удаления. False — FTS5 в этой сборке SQLite нет."""
    try:
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
                     "subject, addrs, attach, body, tokenize='unicode61 remove_diacritics 2')")
    except Exception as exc:  # noqa: BLE001 - sqlite3.OperationalError: no such module: fts5
        log.warning("Полнотекстовый поиск недоступен (SQLite без FTS5): %s. Поиск будет только по теме "
                    "и отправителю.", exc)
        return False
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
                 f"DELETE FROM {FTS_TABLE} WHERE rowid = old.id; END")
    return True


def fts_available(db) -> bool:
    return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)))


# ---------------------------------------------------------------------------
#  Извлечение текста
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """Ё → Е: иначе «счет» не находил «счёт» (токенизатор SQLite ё не сводит)."""
    return (text or "").replace("ё", "е").replace("Ё", "Е")


def html_to_text(value: str) -> str:
    value = _DROP_BLOCKS_RE.sub(" ", value or "")
    value = _BREAK_RE.sub("\n", value)
    value = _TAG_RE.sub(" ", value)
    return html_mod.unescape(value)


def strip_quotes(text: str) -> str:
    """Убрать цитаты предыдущих писем: в переписке они повторяются десятки раз
    и раздували бы индекс, ничего не добавляя к поиску."""
    out: List[str] = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_LINE_RE.match(line):
            continue
        stripped = line.strip()
        if _REPLY_MARKERS.match(stripped):
            break
        if _HEADER_BLOCK_RE.match(stripped) and any(_HEADER_FOLLOW_RE.match(n.strip())
                                                    for n in lines[i + 1:i + 4]):
            break                              # блок «От: … Отправлено: … Тема: …» (Outlook)
        out.append(line)
    return "\n".join(out)


def _addresses(*values: str) -> str:
    parts: List[str] = []
    try:
        pairs = getaddresses([decode_mime_header(v) for v in values if v])
    except Exception:  # noqa: BLE001
        pairs = []
    for name, addr in pairs:
        if name:
            parts.append(name)
        if addr:
            parts.append(addr)
    return " ".join(parts)[:ADDRS_CAP]


def extract(open_fn, *, with_body: bool, body_cap_chars: int) -> Dict[str, str]:
    """Поля для индекса из файла письма (потоковым разбором — крупные письма не читаются в память)."""
    from . import mimestream
    from .mailview import _is_attachment, decode_text_bytes
    with open_fn() as fh:
        res = mimestream.summarize(fh, _is_attachment, TEXT_CAP_BYTES if with_body else 0)
    top = res["headers"]

    def hdr(name):
        try:
            value = top[name] if top is not None else None
            return str(value) if value is not None else ""
        except Exception:  # noqa: BLE001
            return ""
    fields = {"subject": decode_mime_header(hdr("Subject")),
              "addrs": _addresses(hdr("From"), hdr("To"), hdr("Cc"), hdr("Reply-To")),
              "attach": " ".join(decode_mime_header(a.get("filename") or "") for a in res["attachments"])[:2000],
              "body": ""}
    if with_body:
        texts = {}
        for ctype, buf in res["captured"].items():
            leaf = res["capture_leaf"][ctype]
            dec = mimestream.decoder_for(leaf.cte)
            data = dec.feed(bytes(buf)) + dec.close()
            texts[ctype] = decode_text_bytes(data, leaf.charset)
        text = texts.get("text/plain") or html_to_text(texts.get("text/html", ""))
        text = strip_quotes(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        fields["body"] = text[:body_cap_chars]
    return {k: normalize(v) for k, v in fields.items()}


# ---------------------------------------------------------------------------
#  Индексирование
# ---------------------------------------------------------------------------
def bodies_enabled(svc) -> bool:
    if not bool(svc.rt("search", "index_bodies")):
        return False
    if svc.store.encrypt and not bool(svc.rt("search", "index_bodies_encrypted")):
        return False
    return True


def pending_count(db) -> int:
    last = int(db.get_meta(_META_WATERMARK) or 0)
    return int(db.scalar("SELECT COUNT(*) FROM messages WHERE id > ?", (last,)) or 0)


def status(svc) -> Dict:
    db = svc.db
    available = fts_available(db)
    last = int(db.get_meta(_META_WATERMARK) or 0)
    total = db.count_messages()
    pending = pending_count(db) if available else 0
    return {"enabled": bool(svc.rt("search", "enabled")), "available": available,
            "indexed": max(0, total - pending), "total": total, "pending": pending, "last_id": last,
            "bodies": bodies_enabled(svc), "attachments": attachments_enabled(svc),
            "attach_backfill": attach_backfill_pending(db) if available else 0,
            "ocr": bool(svc.rt("search", "ocr")), "ocr_tools": _ocr_tools(),
            "attach_messages": int(db.scalar("SELECT COUNT(*) FROM messages WHERE has_attach=1") or 0),
            "attach_text_kb": _attach_cap(svc) // 1024,
            "db_size": _db_size(db),
            "bodies_blocked_by_encryption": bool(svc.rt("search", "index_bodies")) and svc.store.encrypt
            and not bool(svc.rt("search", "index_bodies_encrypted"))}


def _ocr_tools() -> Dict[str, bool]:
    from .attachtext import ocr_available
    return ocr_available()


def _db_size(db) -> int:
    try:
        return int(db.scalar("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()") or 0)
    except Exception:  # noqa: BLE001
        return 0


def reset_index(db) -> None:
    """Очистить индекс — следующий проход проиндексирует архив заново."""
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {FTS_TABLE}")
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '0') ON CONFLICT(key) DO UPDATE SET value='0'",
                     (_META_WATERMARK,))
        # новый проход прочитает и вложения — дочитывать их отдельно не нужно
        top = str(int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] or 0))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_META_ATTACH, top))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
                     "value=CAST(COALESCE(value, '0') AS INTEGER) + 1", (_META_GEN,))


def _body_cap(svc) -> int:
    try:
        cap_kb = int(svc.rt("search", "body_max_kb") or 16)
    except (TypeError, ValueError):
        cap_kb = 16
    return max(1, cap_kb) * 1024


#: Письма крупнее этого не разбираются ради текста вложений (письмо читается в память целиком).
ATTACH_MESSAGE_MAX = 60 * 1024 * 1024


def attachments_enabled(svc) -> bool:
    """Индексировать ли текст вложений: как текст писем (не при шифровании) и если не выключено."""
    return bodies_enabled(svc) and bool(svc.rt("search", "index_attachments"))


def _attach_cap(svc) -> int:
    """Сколько текста вложений одного письма индексировать (0 — не индексировать)."""
    if not attachments_enabled(svc):
        return 0
    try:
        return max(1, int(svc.rt("search", "attachment_text_kb") or 64)) * 1024
    except (TypeError, ValueError):
        return 64 * 1024


def ocr_settings(svc) -> Optional[Dict]:
    """Распознавание сканов и картинок (``search.ocr``, 1.21.0) — если включено и есть tesseract."""
    if not bool(svc.rt("search", "ocr")):
        return None
    from .attachtext import ocr_available
    if not ocr_available()["tesseract"]:
        return None
    try:
        return {"lang": str(svc.rt("search", "ocr_lang") or "rus+eng"),
                "max_pages": int(svc.rt("search", "ocr_max_pages") or 5),
                "time_s": float(svc.rt("search", "ocr_time_s") or 60)}
    except (TypeError, ValueError):
        return {"lang": "rus+eng", "max_pages": 5, "time_s": 60.0}


def attachments_text(svc, open_fn, cap_chars: int) -> str:
    """Текст поддерживаемых вложений письма (PDF, Word, Excel…), не больше ``cap_chars``."""
    from email import message_from_binary_file
    from email.policy import compat32
    from . import attachtext
    from .mailview import _decoded_payload, _is_attachment, _iter_parts
    try:
        max_one = max(1, int(svc.rt("search", "attachment_max_kb") or 10240)) * 1024
    except (TypeError, ValueError):
        max_one = 10240 * 1024
    try:
        time_s = float(svc.rt("search", "attachment_time_s") or 10)
    except (TypeError, ValueError):
        time_s = 10.0
    ocr = ocr_settings(svc)
    if ocr:
        time_s = max(time_s, float(ocr["time_s"]))
    with open_fn() as fh:
        msg = message_from_binary_file(fh, policy=compat32)
    out: List[str] = []
    left = cap_chars
    for part in _iter_parts(msg):
        if left <= 0:
            break
        ctype = (part.get_content_type() or "").lower()
        try:
            filename = decode_mime_header(part.get_filename() or "")
        except Exception:  # noqa: BLE001
            filename = ""
        disp = part.get_content_disposition()
        if not _is_attachment(ctype, disp, filename) or not attachtext.kind_of(filename, ctype, ocr=bool(ocr)):
            continue
        if ctype == "message/rfc822":
            payload = part.as_bytes() if not part.is_multipart() else b""
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                try:
                    payload = inner[0].as_bytes()
                except Exception:  # noqa: BLE001
                    payload = b""
        else:
            payload = _decoded_payload(part) or b""
        if not payload or len(payload) > max_one:
            continue
        text = attachtext.extract(filename, ctype, payload, left, time_s, ocr=ocr)
        if text:
            out.append(text)
            left -= len(text)
    return "\n".join(out)[:cap_chars]


def _row_fields(svc, row, with_body: bool, body_cap: int, attach_cap: int = 0) -> Tuple[Dict[str, str], str]:
    """Поля индекса для письма ``row`` и текст ошибки ('' — файл прочитан).

    Файл не читается (его нет, нет ключа, письмо битое) — письмо попадает в
    индекс по теме и отправителю из индекса писем: лучше так, чем никак.
    """
    fields = {"subject": normalize(row["subject"] or ""), "addrs": normalize(row["from_addr"] or ""),
              "attach": "", "body": ""}
    if not (row["stored_path"] or ""):
        return fields, ""
    try:
        full = extract(lambda r=row: svc.store.open_message(r["account_id"], r["stored_path"]),
                       with_body=with_body, body_cap_chars=body_cap)
    except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа, битое письмо
        return fields, str(getattr(exc, "message", None) or exc)
    if not full["subject"]:
        full["subject"] = fields["subject"]
    if attach_cap and _row_has(row, "has_attach") and int(_row_has(row, "size") or 0) <= ATTACH_MESSAGE_MAX:
        try:
            text = attachments_text(svc, lambda r=row: svc.store.open_message(r["account_id"], r["stored_path"]),
                                    attach_cap)
        except Exception as exc:  # noqa: BLE001 - вложения не прочитались — письмо остаётся в индексе
            log.debug("Текст вложений письма #%s не извлечён: %s", row["id"], exc)
            text = ""
        if text:
            full["attach"] = (full["attach"] + "\n" + normalize(text)).strip()
    return full, ""


def _row_has(row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def index_pending(svc, *, progress=None, cancelled=None, event=None, max_seconds: Optional[float] = None) -> Dict:
    """Проиндексировать письма, появившиеся после последнего прохода."""
    db = svc.db
    if not fts_available(db):
        return {"indexed": 0, "errors": 0, "pending": 0, "available": False}
    with_body = bodies_enabled(svc)
    body_cap = _body_cap(svc)
    attach_cap = _attach_cap(svc)
    total_pending = pending_count(db)
    done = errors = 0
    started = time.time()
    gen = db.get_meta(_META_GEN) or "0"
    last = int(db.get_meta(_META_WATERMARK) or 0)
    while True:
        if cancelled is not None and cancelled():
            break
        if max_seconds is not None and time.time() - started > max_seconds:
            break
        rows = db.query("SELECT id, account_id, stored_path, subject, from_addr, size, has_attach FROM messages WHERE id > ? "
                        "ORDER BY id LIMIT ?", (last, BATCH))
        if not rows:
            break
        batch_start = last
        batch: List[Tuple] = []
        for row in rows:
            fields, error = _row_fields(svc, row, with_body, body_cap, attach_cap)
            if error:
                errors += 1
                if errors <= 20 and event is not None:
                    event("WARNING", f"Письмо #{row['id']} проиндексировано только по теме и отправителю: {error}")
            batch.append((row["id"], fields["subject"], fields["addrs"], fields["attach"], fields["body"]))
            last = int(row["id"])
        with db.transaction() as conn:
            now_gen = conn.execute("SELECT value FROM meta WHERE key=?", (_META_GEN,)).fetchone()
            if (now_gen[0] if now_gen else "0") != gen:
                break                          # индекс перестроили во время прохода — эта порция устарела
            conn.executemany(f"INSERT OR REPLACE INTO {FTS_TABLE}(rowid, subject, addrs, attach, body) "
                             f"VALUES(?,?,?,?,?)", batch)
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value", (_META_WATERMARK, str(last)))
            if attach_cap:
                # вложения этой порции прочитаны: если до неё всё дочитано — отметка идёт следом
                mark = conn.execute("SELECT value FROM meta WHERE key=?", (_META_ATTACH,)).fetchone()
                if int(mark[0] if mark else 0) >= batch_start:
                    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                                 "value=excluded.value", (_META_ATTACH, str(last)))
        done += len(batch)
        if progress is not None:
            progress(done, max(total_pending, done), f"Проиндексировано писем: {done} из {total_pending}")
    return {"indexed": done, "errors": errors, "pending": pending_count(db), "available": True,
            "bodies": with_body}


def reindex_accounts(svc, account_ids: Iterable[int], *, progress=None, cancelled=None, event=None,
                     on_account=None) -> Dict:
    """Проиндексировать заново письма выбранных ящиков; остальной индекс не трогается.

    Нужно, когда записи ящика в индексе устарели или неполны: текст писем
    начали индексировать уже после первого прохода, файлы не читались (не было
    ключа шифрования), поиск не находит письма конкретного ящика. Перестраивать
    ради этого индекс всего архива — часы работы.

    Переиндексируются письма, которые общий проход уже прошёл (номер не больше
    его отметки); более новые он проиндексирует сам. ``on_account(id, итог)``
    вызывается после каждого ящика. Индекс перестроили целиком во время работы —
    остановка (``stopped="reset"``): записывать старые порции поверх нового
    индекса нельзя, а новый индекс и так прочитает все письма.
    """
    db = svc.db
    ids = [int(i) for i in account_ids]
    if not fts_available(db):
        return {"available": False, "indexed": 0, "errors": 0, "total": 0, "accounts": {}, "stopped": ""}
    with_body = bodies_enabled(svc)
    body_cap = _body_cap(svc)
    attach_cap = _attach_cap(svc)
    gen = db.get_meta(_META_GEN) or "0"
    watermark = int(db.get_meta(_META_WATERMARK) or 0)
    total = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        total += int(db.scalar(f"SELECT COUNT(*) FROM messages WHERE account_id IN ({marks}) AND id <= ?",
                               (*chunk, watermark)) or 0)
    done = errors = 0
    per: Dict[int, Dict[str, int]] = {}
    stopped = ""
    for account_id in ids:
        stat = per.setdefault(account_id, {"indexed": 0, "errors": 0})
        last = 0
        while not stopped:
            if cancelled is not None and cancelled():
                stopped = "cancelled"
                break
            rows = db.query("SELECT id, account_id, stored_path, subject, from_addr, size, has_attach FROM messages "
                            "WHERE account_id=? AND id > ? AND id <= ? ORDER BY id LIMIT ?",
                            (account_id, last, watermark, BATCH))
            if not rows:
                break
            batch: List[Tuple] = []
            failed_ids: List[int] = []
            for row in rows:
                fields, error = _row_fields(svc, row, with_body, body_cap, attach_cap)
                if error:
                    stat["errors"] += 1
                    errors += 1
                    failed_ids.append(int(row["id"]))
                    if errors <= 20 and event is not None:
                        event("WARNING", f"Письмо #{row['id']} не перечитано (в индексе остаётся прежняя запись): "
                                         f"{error}")
                batch.append((row["id"], fields["subject"], fields["addrs"], fields["attach"], fields["body"]))
                last = int(row["id"])
            with db.transaction() as conn:
                now_gen = conn.execute("SELECT value FROM meta WHERE key=?", (_META_GEN,)).fetchone()
                if (now_gen[0] if now_gen else "0") != gen:
                    stopped = "reset"
                    break
                if failed_ids:
                    # Файл не прочитался (нет ключа, сбой диска) — прежнюю, полную
                    # запись индекса не заменяем записью «только тема и отправитель».
                    marks = ",".join("?" * len(failed_ids))
                    have = {int(r[0]) for r in conn.execute(
                        f"SELECT rowid FROM {FTS_TABLE} WHERE rowid IN ({marks})", tuple(failed_ids))}
                    batch = [b for b in batch if int(b[0]) not in have]
                conn.executemany(f"INSERT OR REPLACE INTO {FTS_TABLE}(rowid, subject, addrs, attach, body) "
                                 f"VALUES(?,?,?,?,?)", batch)
            stat["indexed"] += len(batch)
            done += len(batch)
            if progress is not None:
                progress(done, max(total, done), f"Переиндексировано писем: {done} из {total}")
        if stopped:
            break
        if on_account is not None:
            on_account(account_id, dict(stat))
    return {"available": True, "indexed": done, "errors": errors, "total": total, "accounts": per,
            "stopped": stopped, "bodies": with_body}


#: докуда дочитан текст вложений уже проиндексированных писем (1.21.0)
_META_ATTACH = "search_attach_backfill"


def attach_backfill_pending(db) -> int:
    """Сколько проиндексированных писем с вложениями ждут чтения текста вложений."""
    if not fts_available(db):
        return 0
    last = int(db.get_meta(_META_ATTACH) or 0)
    watermark = int(db.get_meta(_META_WATERMARK) or 0)
    return int(db.scalar("SELECT COUNT(*) FROM messages WHERE has_attach=1 AND id > ? AND id <= ?",
                         (last, watermark)) or 0)


def backfill_attachments(svc, *, progress=None, cancelled=None, event=None,
                         max_seconds: Optional[float] = None) -> Dict:
    """Дочитать текст вложений писем, проиндексированных без него (1.21.0).

    Архив, проиндексированный до 1.20.0 (или с выключенным ``search.index_attachments``), не нужно
    перестраивать целиком: перечитываются только письма с вложениями, по порядку, с отметкой — прерванное
    задание продолжит с того же места. Новые письма общий проход индексирует сам, вместе с вложениями.
    """
    db = svc.db
    if not fts_available(db):
        return {"available": False, "indexed": 0, "errors": 0, "pending": 0}
    if not attachments_enabled(svc):
        return {"available": True, "indexed": 0, "errors": 0, "pending": attach_backfill_pending(db),
                "disabled": True}
    with_body = bodies_enabled(svc)
    body_cap = _body_cap(svc)
    attach_cap = _attach_cap(svc)
    gen = db.get_meta(_META_GEN) or "0"
    last = int(db.get_meta(_META_ATTACH) or 0)
    total = attach_backfill_pending(db)
    done = errors = 0
    started = time.time()
    while True:
        if cancelled is not None and cancelled():
            break
        if max_seconds is not None and time.time() - started > max_seconds:
            break
        watermark = int(db.get_meta(_META_WATERMARK) or 0)
        rows = db.query("SELECT id, account_id, stored_path, subject, from_addr, size, has_attach FROM messages "
                        "WHERE has_attach=1 AND id > ? AND id <= ? ORDER BY id LIMIT ?", (last, watermark, BATCH))
        if not rows:
            break
        batch: List[Tuple] = []
        for row in rows:
            fields, error = _row_fields(svc, row, with_body, body_cap, attach_cap)
            last = int(row["id"])
            if error:
                errors += 1                       # файл не прочитан — прежнюю запись индекса не трогаем
                if errors <= 20 and event is not None:
                    event("WARNING", f"Письмо #{row['id']}: вложения не прочитаны: {error}")
                continue
            batch.append((row["id"], fields["subject"], fields["addrs"], fields["attach"], fields["body"]))
        with db.transaction() as conn:
            now_gen = conn.execute("SELECT value FROM meta WHERE key=?", (_META_GEN,)).fetchone()
            if (now_gen[0] if now_gen else "0") != gen:
                break                             # индекс перестроили — он и так прочитает вложения
            conn.executemany(f"INSERT OR REPLACE INTO {FTS_TABLE}(rowid, subject, addrs, attach, body) "
                             f"VALUES(?,?,?,?,?)", batch)
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value", (_META_ATTACH, str(last)))
        done += len(batch)
        if progress is not None:
            progress(done + errors, max(total, done + errors), f"Прочитаны вложения писем: {done} из {total}")
    return {"available": True, "indexed": done, "errors": errors, "pending": attach_backfill_pending(db)}


# ---------------------------------------------------------------------------
#  Поиск
# ---------------------------------------------------------------------------
def _terms(text: str) -> List[str]:
    return _WORD_RE.findall(normalize(text).lower())


def build_match(query: str) -> str:
    """Запрос пользователя → выражение FTS5 (безопасно: только слова, в кавычках).

    Слова ищутся по началу («счет» находит «счета»), все должны встретиться.
    «Фраза в кавычках» — слова подряд. Поля: тема:, от:/кому:, вложение:, текст:.
    Составное слово (адрес, номер через дефис) ищется фразой.
    """
    tokens = re.findall(r'"[^"]*"|\S+', query or "")
    parts: List[str] = []
    for tok in tokens:
        field = None
        m = re.match(r"^([^\W\d_]+):(.*)$", tok, re.UNICODE)
        if m and m.group(1).lower() in _FIELD_ALIASES:
            field = _FIELD_ALIASES[m.group(1).lower()]
            tok = m.group(2)
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            words = _terms(tok[1:-1])
            expr = '"' + " ".join(words) + '"' if words else ""
        else:
            words = _terms(tok)
            expr = ('"' + " ".join(words) + '" *') if words else ""
        if not expr:
            continue
        parts.append(f"{field} : {expr}" if field else expr)
    return " AND ".join(parts)


def search(svc, query: str, *, account_ids: Optional[Iterable[int]] = None, folder: str = "",
           date_from: str = "", date_to: str = "", with_attachments: bool = False,
           order: str = "date", offset: int = 0, limit: int = PAGE) -> Dict:
    db = svc.db
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    where: List[str] = []
    args: List = []
    ids = list(account_ids) if account_ids is not None else None
    if ids is not None:
        if not ids:
            return {"results": [], "more": False, "fts": fts_available(db)}
        where.append(f"m.account_id IN ({','.join('?' * len(ids))})")
        args.extend(int(i) for i in ids)
    if folder:
        where.append("m.folder = ?")
        args.append(folder)
    since, until = svc.day_bounds_utc(date_from or None, date_to or None)
    if since:
        where.append("m.internaldate >= ?")
        args.append(since)
    if until:
        where.append("m.internaldate < ?")
        args.append(until)
    if with_attachments:
        where.append("m.has_attach = 1")
    use_fts = fts_available(db)
    match = build_match(query)
    if not match:
        return {"results": [], "more": False, "fts": use_fts, "empty_query": True}
    cols = ("m.id, m.account_id, m.folder, m.internaldate, m.subject, m.from_addr, m.has_attach, m.size, "
            "a.name AS account_name")
    if use_fts:
        sql = (f"SELECT {cols}, snippet({FTS_TABLE}, -1, char(2), char(3), '…', 12) AS snip "
               f"FROM {FTS_TABLE} JOIN messages m ON m.id = {FTS_TABLE}.rowid "
               f"JOIN accounts a ON a.id = m.account_id WHERE {FTS_TABLE} MATCH ?")
        params = [match] + args
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY rank" if order == "rank" else f" ORDER BY {FTS_TABLE}.rowid DESC"
    else:
        # Без FTS5: каждое слово — в теме или отправителе (LIKE по нижнему регистру).
        conds, params = [], []
        for word in _terms(query):
            conds.append("(ma_lower(COALESCE(m.subject,'')) LIKE ? OR ma_lower(COALESCE(m.from_addr,'')) LIKE ?)")
            like = f"%{word}%"
            params += [like, like]
        sql = (f"SELECT {cols}, '' AS snip FROM messages m JOIN accounts a ON a.id = m.account_id WHERE "
               + " AND ".join(conds + where))
        params += args
        sql += " ORDER BY m.id DESC"
    sql += " LIMIT ? OFFSET ?"
    params += [limit + 1, offset]
    try:
        rows = db.query(sql, tuple(params))
    except Exception as exc:  # noqa: BLE001 - синтаксис FTS5, который не удалось собрать
        from .errors import ValidationError
        raise ValidationError(f"Не удалось выполнить поиск: {exc}",
                              hint="Упростите запрос: слова через пробел, фраза — в кавычках.") from exc
    results = []
    for row in rows[:limit]:
        results.append({"id": row["id"], "account_id": row["account_id"], "account_name": row["account_name"],
                        "folder": row["folder"], "date": row["internaldate"], "subject": row["subject"] or "",
                        "from": row["from_addr"] or "", "has_attach": bool(row["has_attach"]),
                        "size": int(row["size"] or 0), "snippet": row["snip"] or ""})
    return {"results": results, "more": len(rows) > limit, "fts": use_fts, "offset": offset}
