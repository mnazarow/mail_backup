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
            "bodies": bodies_enabled(svc),
            "bodies_blocked_by_encryption": bool(svc.rt("search", "index_bodies")) and svc.store.encrypt
            and not bool(svc.rt("search", "index_bodies_encrypted"))}


def reset_index(db) -> None:
    """Очистить индекс — следующий проход проиндексирует архив заново."""
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {FTS_TABLE}")
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '0') ON CONFLICT(key) DO UPDATE SET value='0'",
                     (_META_WATERMARK,))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
                     "value=CAST(COALESCE(value, '0') AS INTEGER) + 1", (_META_GEN,))


def index_pending(svc, *, progress=None, cancelled=None, event=None, max_seconds: Optional[float] = None) -> Dict:
    """Проиндексировать письма, появившиеся после последнего прохода."""
    db = svc.db
    if not fts_available(db):
        return {"indexed": 0, "errors": 0, "pending": 0, "available": False}
    with_body = bodies_enabled(svc)
    try:
        cap_kb = int(svc.rt("search", "body_max_kb") or 16)
    except (TypeError, ValueError):
        cap_kb = 16
    body_cap = max(1, cap_kb) * 1024
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
        rows = db.query("SELECT id, account_id, stored_path, subject, from_addr FROM messages WHERE id > ? "
                        "ORDER BY id LIMIT ?", (last, BATCH))
        if not rows:
            break
        batch: List[Tuple] = []
        for row in rows:
            fields = {"subject": normalize(row["subject"] or ""), "addrs": normalize(row["from_addr"] or ""),
                      "attach": "", "body": ""}
            rel = row["stored_path"] or ""
            if rel:
                try:
                    fields = extract(lambda r=row: svc.store.open_message(r["account_id"], r["stored_path"]),
                                     with_body=with_body, body_cap_chars=body_cap)
                    if not fields["subject"]:
                        fields["subject"] = normalize(row["subject"] or "")
                except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа, битое письмо
                    errors += 1
                    if errors <= 20 and event is not None:
                        event("WARNING", f"Письмо #{row['id']} проиндексировано только по теме и отправителю: "
                                         f"{getattr(exc, 'message', None) or exc}")
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
        done += len(batch)
        if progress is not None:
            progress(done, max(total_pending, done), f"Проиндексировано писем: {done} из {total_pending}")
    return {"indexed": done, "errors": errors, "pending": pending_count(db), "available": True,
            "bodies": with_body}


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
