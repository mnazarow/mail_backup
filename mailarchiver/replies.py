# -*- coding: utf-8 -*-
"""
Аналитика ответов на письма — раздел «Ответы на письма» (1.16.0).

Как связываются письма и ответы
-------------------------------
Почтовые программы, отвечая на письмо, пишут в ответ заголовок ``In-Reply-To``
с ``Message-ID`` исходного письма (и цепочку ``References``). Фоновое задание
«Индекс ответов на письма» читает ЗАГОЛОВКИ каждого письма архива (тело не
читается) и кладёт в таблицу ``reply_msgs`` компактную запись:

* отпечатки (64 бита) своего ``Message-ID``, ``In-Reply-To`` («родитель») и
  первого из ``References`` («цепочка»);
* направление для ЭТОГО ящика: входящее, исходящее (папка «Отправленные» или
  отправитель — адрес самого ящика / его сотрудника) или «не в счёт»
  (черновики, спам, исходящие, дубли «Вся почта» Gmail, повтор письма в ящике);
* тип: личное, автоответ / недоставка, рассылка, уведомление робота;
* собеседник (отправитель входящего или первый получатель исходящего) и домены;
* где ящик в адресатах входящего: «Кому», «Копия» или не указан (скрытая копия,
  список рассылки);
* время письма (заголовок ``Date``, при явной ошибке — время получения сервером)
  и флаг ``\\Answered``.

После каждой порции связи пересчитываются только для затронутых писем:

* у входящего — первый ЛИЧНЫЙ ответ из этого же ящика (``reply_ts``) и первый
  ответ из другого ящика архива (``other_ts`` — ответил коллега);
* у исходящего — первый личный ответ, пришедший в этот ящик (``reply_ts``).

Автоответы («Нахожусь в отпуске») ответом не считаются. Ответ раньше исходного
письма больше чем на 10 минут (сбитые часы) — тоже.

Что считается
-------------
:func:`analyze` по индексу за период: сводка и сравнение с прошлым таким же
периодом, время ответа (медиана, среднее, 75/90-й процентили, в рабочих часах),
соблюдение срока ответа (SLA), динамика, распределения, тепловые карты
«день × час», сравнение ящиков и отделов, отправители и домены, исходящие без
ответа, цепочки переписки. :func:`list_messages` — сами письма (без ответа,
просроченные, отвеченные с опозданием, исходящие без ответа) постранично и в CSV.

Свои домены, исключённые отправители, срок ответа и рабочее время применяются
при расчёте — смена настроек индекс не перестраивает. Перестройка нужна, только
если поменялись адреса сотрудников и ящиков (направление писем считается при
индексации): кнопка «Пересобрать индекс».
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses, parsedate_to_datetime
from fnmatch import fnmatchcase
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import parse_work_days, parse_work_hours
from .logging_setup import get_logger
from .util import decode_mime_header

log = get_logger("replies")

TABLE = "reply_msgs"
#: Разобрано всё, что между «нижней» и «верхней» отметкой: новые письма (выше верхней) — сразу, давние (ниже
#: нижней) — от новых к старым, чтобы последние месяцы были готовы первыми.
_META_HIGH = "replies_last_id"
_META_LOW = "replies_low_id"
#: Отпечатки адресов каждого ящика (с адресами его сотрудника): от них зависит направление писем.
_META_OWN = "replies_own_fp"
#: Ящики, письма которых разбираются заново (адреса поменялись): {id ящика: последний разобранный id письма}.
_META_REDO = "replies_redo"
#: «Поколение» индекса: «Пересобрать» его меняет, и проход, начатый до этого, не запишет свою порцию.
_META_GEN = "replies_gen"
#: Писем за одну транзакцию.
BATCH = 300
#: Сколько байт заголовков читать (дальше — тело письма).
HEADER_CAP = 256 * 1024
#: Допуск на расхождение часов: ответ «раньше» письма не больше чем на 10 минут ещё ответ.
SKEW_S = 600
#: Сколько ящиков, отправителей и доменов отдавать в таблицах.
TOP_N = 25

# направление письма для ящика
SKIP, IN, OUT = 0, 1, 2
# тип письма
PERSONAL, AUTOREPLY, BULK, NOTIFY, EXCLUDED = 0, 1, 2, 3, 4
KIND_LABELS = {PERSONAL: "Личные", AUTOREPLY: "Автоответы и недоставка", BULK: "Рассылки",
               NOTIFY: "Уведомления роботов", EXCLUDED: "Исключены настройкой"}
# где ящик среди адресатов входящего
ADDR_OTHER, ADDR_TO, ADDR_CC = 0, 1, 2

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id          INTEGER PRIMARY KEY,           -- = messages.id
    account_id  INTEGER NOT NULL,
    mid         INTEGER NOT NULL,              -- отпечаток Message-ID (без него — минус id письма)
    parent      INTEGER,                       -- отпечаток In-Reply-To (или последнего из References)
    root        INTEGER,                       -- отпечаток первого письма цепочки
    dir         INTEGER NOT NULL,              -- 1 входящее, 2 исходящее, 0 не в счёт
    kind        INTEGER NOT NULL DEFAULT 0,    -- 0 личное, 1 автоответ, 2 рассылка, 3 уведомление
    addressed   INTEGER NOT NULL DEFAULT 0,    -- входящее: 1 ящик в «Кому», 2 в «Копии», 0 не указан
    peer        TEXT NOT NULL DEFAULT '',      -- отправитель входящего / первый получатель исходящего
    domains     TEXT NOT NULL DEFAULT '',      -- домен отправителя / домены получателей через запятую
    rcpt        INTEGER NOT NULL DEFAULT 0,    -- получателей в «Кому» и «Копии»
    ts          INTEGER NOT NULL,              -- время письма, UTC, секунды
    flag_ans    INTEGER NOT NULL DEFAULT 0,    -- флаг \\Answered
    reply_id    INTEGER,                       -- первый ответ (см. описание модуля)
    reply_ts    INTEGER,
    other_ts    INTEGER,                       -- входящее: первый ответ из другого ящика архива
    subj        INTEGER,                       -- отпечаток темы без «Re:», «Fwd:»… (для связи по теме)
    reply_how   INTEGER                        -- как найден ответ: 1 по заголовкам, 2 по теме и собеседнику
);
CREATE INDEX IF NOT EXISTS idx_reply_acc_ts ON {TABLE}(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_reply_ts ON {TABLE}(ts);
CREATE INDEX IF NOT EXISTS idx_reply_mid ON {TABLE}(mid, account_id);
CREATE INDEX IF NOT EXISTS idx_reply_parent ON {TABLE}(parent, account_id) WHERE parent IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS messages_reply_ad AFTER DELETE ON messages BEGIN
    DELETE FROM {TABLE} WHERE id = old.id;
END;
"""


#: Колонки, которых не было в первых сборках 1.16.0: при их добавлении индекс разбирается заново.
_LATE_COLUMNS = (("subj", "INTEGER"), ("reply_how", "INTEGER"))


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
    added = False
    for name, decl in _LATE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {decl}")
            added = True
    if added:
        conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (_META_HIGH, _META_LOW, _META_OWN))
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_reply_subj ON {TABLE}(account_id, subj, ts) WHERE subj IS NOT NULL")
    conn.commit()


# ===========================================================================
#  Разбор заголовков
# ===========================================================================
_HDR_END = re.compile(rb"\r?\n\r?\n")
_ID_RE = re.compile(r"<\s*([^<>\s]+?)\s*>")
_NOREPLY_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?|notifications?|notify|"
    r"alerts?|robot|daemon|auto[-_.]?(?:reply|mailer|notify|notification)?)(?:[-+._].*)?$")
_AUTO_SUBJECT = ("automatic reply", "auto reply", "autoreply", "auto-reply", "out of office", "автоматический ответ",
                 "автоответ", "вне офиса", "нет на месте", "undeliverable", "undelivered mail", "недоставлен",
                 "не доставлено", "delivery status notification", "delivery failure", "mail delivery failed",
                 "returned mail", "failure notice", "message not delivered")
#: Почтовые службы для всех: их домены не становятся «своими» сами (ящик сотрудника на gmail.com не делает
#: внутренними всех отправителей с gmail.com). Явно — настройкой replies.internal_domains.
PUBLIC_DOMAINS = frozenset((
    "gmail.com", "googlemail.com", "yandex.ru", "ya.ru", "yandex.com", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
    "internet.ru", "rambler.ru", "lenta.ru", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
    "me.com", "mac.com", "yahoo.com", "aol.com", "gmx.com", "gmx.de", "proton.me", "protonmail.com", "zoho.com",
    "tutanota.com", "qip.ru", "ukr.net"))


def read_headers(fh):
    """Заголовки письма из потока: читается только начало файла (до пустой строки)."""
    buf = bytearray()
    while len(buf) < HEADER_CAP:
        chunk = fh.read(32768)
        if not chunk:
            break
        start = max(0, len(buf) - 3)
        buf += chunk
        m = _HDR_END.search(buf, start)
        if m:
            del buf[m.start():]
            break
    else:
        del buf[HEADER_CAP:]
    return BytesHeaderParser(policy=compat32).parsebytes(bytes(buf) + b"\r\n\r\n")


def msg_ids(value) -> List[str]:
    """Идентификаторы писем из заголовка (Message-ID, In-Reply-To, References), в нижнем регистре."""
    if value is None:
        return []
    text = str(value)
    found = _ID_RE.findall(text)
    if not found:
        tok = text.strip().split()
        found = [tok[0]] if tok and "@" in tok[0] else []     # Message-ID без угловых скобок
    return [f.strip().lower() for f in found if f.strip()]


def fingerprint(value: str) -> int:
    """64-битный (положительный) отпечаток идентификатора письма."""
    raw = hashlib.blake2b(value.encode("utf-8", "replace"), digest_size=8).digest()
    return (int.from_bytes(raw, "big") & 0x7FFF_FFFF_FFFF_FFFF) or 1


def _emails(msg, *names: str) -> List[str]:
    values: List[str] = []
    for name in names:
        values.extend(str(v) for v in (msg.get_all(name) or []))
    out: List[str] = []
    try:
        pairs = getaddresses(values)
    except Exception:  # noqa: BLE001 - кривой заголовок
        pairs = []
    for _name, addr in pairs:
        a = (addr or "").strip().strip("<>").lower()
        if "@" in a and a not in out:
            out.append(a)
    return out


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1] if "@" in (addr or "") else ""


def _email_from_text(value: str) -> str:
    try:
        pairs = getaddresses([value or ""])
    except Exception:  # noqa: BLE001
        pairs = []
    for _n, addr in pairs:
        a = (addr or "").strip().lower()
        if "@" in a:
            return a
    return ""


def classify(msg, sender: str) -> int:
    """Тип письма по заголовкам: личное, автоответ/недоставка, рассылка или уведомление робота."""
    def h(name: str) -> str:
        v = msg.get(name)
        return str(v).strip().lower() if v is not None else ""
    auto = h("auto-submitted")
    prec = h("precedence")
    local = sender.split("@", 1)[0] if sender else ""
    subject = decode_mime_header(msg.get("Subject") or "").strip().lower()
    if (auto.startswith("auto-replied") or h("x-autoreply") or h("x-autorespond") or prec == "auto_reply"
            or "multipart/report" in h("content-type") or local in ("mailer-daemon", "postmaster")
            or subject.startswith(_AUTO_SUBJECT)):
        return AUTOREPLY
    if h("list-id") or h("list-unsubscribe") or prec in ("bulk", "list", "junk") or h("feedback-id"):
        return BULK
    if (auto and auto != "no") or _NOREPLY_RE.match(local):
        return NOTIFY
    return PERSONAL


def folder_kind(folder: str) -> str:
    """Вид папки: sent — отправленные, skip — не в счёт (черновики, спам, исходящие, дубли Gmail), other."""
    from .serverops.imap_ops import DRAFT_NAMES, JUNK_NAMES, OUTBOX_NAMES, SENT_NAMES
    low = (folder or "").strip().lower()
    leaf = re.split(r"[/\\.]", low)[-1].strip() if low else ""
    if low.startswith(("[gmail]", "[google mail]")) and leaf in (
            "all mail", "вся почта", "important", "важные", "starred", "помеченные", "chats", "чаты"):
        return "skip"
    if leaf in SENT_NAMES or low in SENT_NAMES:
        return "sent"
    if leaf in DRAFT_NAMES or leaf in OUTBOX_NAMES or leaf in JUNK_NAMES:
        return "skip"
    return "other"


def _parse_iso_ts(value) -> Optional[int]:
    if not value:
        return None
    s = str(value)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


_MIN_TS = 315532800          # 1980-01-01: раньше — явно сбитая дата


def message_ts(date_header, internaldate) -> int:
    """Время письма: заголовок Date, а если его нет, он сбит или из будущего — время получения сервером."""
    idt = _parse_iso_ts(internaldate)
    dt_ts = None
    if date_header:
        try:
            dt = parsedate_to_datetime(str(date_header))
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_ts = int(dt.timestamp())
        except (TypeError, ValueError, IndexError, OverflowError):
            dt_ts = None
    limit = (idt if idt is not None else int(time.time())) + 86400
    if dt_ts is not None and _MIN_TS <= dt_ts <= limit:
        return dt_ts
    return idt if idt is not None else (dt_ts or 0)


# ===========================================================================
#  Адреса своей организации
# ===========================================================================
class Context:
    """Адреса ящиков (и их сотрудников) и свои домены — для направления писем и «внутренней» переписки."""

    def __init__(self, svc):
        db = svc.db
        self.own: Dict[int, Set[str]] = defaultdict(set)
        emp_addrs: Dict[int, Set[str]] = defaultdict(set)
        for e in db.query("SELECT id, email, known_logins FROM employees"):
            s = emp_addrs[int(e["id"])]
            if "@" in (e["email"] or ""):
                s.add(e["email"].strip().lower())
            try:
                known = json.loads(e["known_logins"] or "[]")
            except (TypeError, ValueError):
                known = []
            for k in known if isinstance(known, list) else []:
                if "@" in str(k):
                    s.add(str(k).strip().lower())
        self.names: Dict[int, str] = {}
        for a in db.query("SELECT id, name, username, employee_id FROM accounts"):
            aid = int(a["id"])
            self.names[aid] = a["name"] or f"#{aid}"
            u = (a["username"] or "").strip().lower()
            if "@" in u:
                self.own[aid].add(u)
            if a["employee_id"] is not None:
                self.own[aid] |= emp_addrs.get(int(a["employee_id"]), set())
        auto = {_domain(x) for s in self.own.values() for x in s} - PUBLIC_DOMAINS
        self.org_domains: Set[str] = {d for d in auto if d} | {
            str(d).strip().lower().lstrip("@") for d in (svc.rt("replies", "internal_domains") or []) if str(d).strip()}

    def fingerprints(self) -> Dict[str, str]:
        """Отпечаток адресов каждого ящика: изменился — письма ящика надо разобрать заново."""
        return {str(aid): hashlib.blake2b("\n".join(sorted(addrs)).encode("utf-8"), digest_size=8).hexdigest()
                for aid, addrs in self.own.items()}


# ===========================================================================
#  Тема письма — для связи ответа, написанного без «Ответить»
# ===========================================================================
_SUBJ_PREFIX = re.compile(r"^\s*(?:\[[^\]]{1,40}\]\s*)?(?:re|fwd?|fw|aw|sv|tr|wg|rv|отв|ответ|пересл|переслать)"
                          r"\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*", re.IGNORECASE)
_SUBJ_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
_SUBJ_SPACE = re.compile(r"\s+")
#: Ответ по теме ищется не дальше, чем через столько секунд после письма.
SUBJECT_WINDOW_S = 14 * 86400


def subject_key(subject: str) -> Optional[int]:
    """Отпечаток темы без «Re:», «Fwd:», «Отв:», «[EXT]»… ``None`` — темы нет или она слишком короткая."""
    text = (subject or "").strip()
    for _ in range(10):
        new = _SUBJ_PREFIX.sub("", text, count=1)
        new = _SUBJ_TAG.sub("", new, count=1) if new == text else new
        if new == text:
            break
        text = new
    text = _SUBJ_SPACE.sub(" ", text).strip().lower()
    if len(text) < 4:
        return None
    return fingerprint("s:" + text)


# ===========================================================================
#  Индексация
# ===========================================================================
def build_record(svc, ctx: Context, row) -> Tuple[tuple, str]:
    """Запись индекса для письма ``row`` (строка messages) и текст ошибки ('' — файл прочитан).

    Файл не читается — письмо попадает в индекс по полям индекса писем (без связей с ответами).
    """
    aid = int(row["account_id"])
    msg = None
    err = ""
    if row["stored_path"]:
        try:
            with svc.store.open_message(aid, row["stored_path"]) as fh:
                msg = read_headers(fh)
        except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа, битое письмо
            err = str(getattr(exc, "message", None) or exc)
    ids = msg_ids(msg.get("Message-ID")) if msg is not None else []
    if not ids:
        ids = msg_ids(row["message_id"])
    mid = fingerprint(ids[0]) if ids else -int(row["id"])
    parent = root = None
    if msg is not None:
        irt = msg_ids(msg.get("In-Reply-To"))
        refs = msg_ids(" ".join(str(v) for v in (msg.get_all("References") or [])))
        parent_s = irt[0] if irt else (refs[-1] if refs else None)
        if parent_s and (not ids or parent_s != ids[0]):
            parent = fingerprint(parent_s)
        root_s = refs[0] if refs else parent_s
        root = fingerprint(root_s) if root_s and (not ids or root_s != ids[0]) else None
    if root is None:
        root = mid
    sender = (_emails(msg, "From") or [""])[0] if msg is not None else _email_from_text(row["from_addr"] or "")
    own = ctx.own.get(aid, set())
    fk = folder_kind(row["folder"])
    if fk == "skip":
        direction = SKIP
    elif fk == "sent" or (sender and sender in own):
        direction = OUT
    else:
        direction = IN
    to = _emails(msg, "To") if msg is not None else []
    cc = _emails(msg, "Cc") if msg is not None else []
    rcpts = to + [x for x in cc if x not in to]
    addressed = ADDR_OTHER
    if direction == OUT:
        others = [x for x in rcpts if x not in own] or rcpts
        peer = others[0] if others else ""
        doms: List[str] = []
        for x in others:
            d = _domain(x)
            if d and d not in doms:
                doms.append(d)
            if len(doms) >= 12:
                break
        domains = ",".join(doms)[:240]
    else:
        peer = sender
        domains = _domain(sender)
        if own & set(to):
            addressed = ADDR_TO
        elif own & set(cc):
            addressed = ADDR_CC
    kind = classify(msg, sender) if msg is not None else PERSONAL
    ts = message_ts(msg.get("Date") if msg is not None else None, row["internaldate"])
    flag_ans = 1 if "\\answered" in str(row["flags"] or "").lower() else 0
    subject = decode_mime_header(msg.get("Subject") or "") if msg is not None else (row["subject"] or "")
    rec = (int(row["id"]), aid, mid, parent, root, direction, kind, addressed, peer[:320], domains,
           len(rcpts), int(ts), flag_ans, subject_key(subject))
    return rec, err


_INSERT = (f"INSERT OR REPLACE INTO {TABLE}(id, account_id, mid, parent, root, dir, kind, addressed, peer, domains, "
           f"rcpt, ts, flag_ans, subj) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
_MSG_COLS = "id, account_id, folder, message_id, internaldate, flags, stored_path, from_addr, subject"

# Ответы по заголовкам (In-Reply-To): у входящего — первое личное исходящее этого ящика, у исходящего — первое
# личное входящее этого ящика; «ответил коллега» — личное исходящее другого ящика.
_LINK_SQL = f"""
UPDATE {TABLE} SET
    reply_id = CASE dir
        WHEN 1 THEN (SELECT r.id FROM {TABLE} r INDEXED BY idx_reply_parent WHERE r.parent = {TABLE}.mid AND r.account_id = {TABLE}.account_id
                     AND r.dir = 2 AND r.kind = 0 AND r.ts >= {TABLE}.ts - {SKEW_S} ORDER BY r.ts, r.id LIMIT 1)
        WHEN 2 THEN (SELECT r.id FROM {TABLE} r INDEXED BY idx_reply_parent WHERE r.parent = {TABLE}.mid AND r.account_id = {TABLE}.account_id
                     AND r.dir = 1 AND r.kind = 0 AND r.ts >= {TABLE}.ts - {SKEW_S} ORDER BY r.ts, r.id LIMIT 1)
    END,
    other_ts = CASE dir
        WHEN 1 THEN (SELECT MIN(r.ts) FROM {TABLE} r INDEXED BY idx_reply_parent WHERE r.parent = {TABLE}.mid AND r.account_id <> {TABLE}.account_id
                     AND r.dir = 2 AND r.kind = 0 AND r.ts >= {TABLE}.ts - {SKEW_S})
    END
WHERE mid IN (SELECT mid FROM temp._reply_aff) AND dir IN (1, 2)
"""
_LINK_HOW_SQL = (f"UPDATE {TABLE} SET reply_how = CASE WHEN reply_id IS NULL THEN NULL ELSE 1 END "
                 f"WHERE mid IN (SELECT mid FROM temp._reply_aff) AND dir IN (1, 2)")
# Ответ, написанный новым письмом (без In-Reply-To): та же тема без «Re:», тот же собеседник, в этом же ящике,
# в течение двух недель. Ищется только у писем без ответа по заголовкам. Пересчитываются лишь письма, на которые
# новое письмо может быть таким ответом (та же тема в двух неделях до него), — не все письма с частой темой.
_AFF_SUBJ_SQL = f"""
INSERT OR IGNORE INTO temp._reply_aff_ids(id)
SELECT r.id FROM temp._reply_aff_subj a JOIN {TABLE} r INDEXED BY idx_reply_subj
    ON r.account_id = a.account_id AND r.subj = a.subj AND r.ts >= a.ts - {SUBJECT_WINDOW_S} AND r.ts <= a.ts
WHERE r.dir IN (1, 2) AND r.kind = 0 AND r.peer <> ''
"""
_LINK_SUBJ_SQL = f"""
UPDATE {TABLE} SET
    reply_id = (SELECT r.id FROM {TABLE} r INDEXED BY idx_reply_subj WHERE r.account_id = {TABLE}.account_id
                AND r.subj = {TABLE}.subj AND r.ts > {TABLE}.ts AND r.ts <= {TABLE}.ts + {SUBJECT_WINDOW_S}
                AND r.dir = 3 - {TABLE}.dir AND r.kind = 0 AND r.parent IS NULL AND r.peer = {TABLE}.peer
                ORDER BY r.ts, r.id LIMIT 1),
    reply_how = 2
WHERE id IN (SELECT id FROM temp._reply_aff_ids) AND (reply_id IS NULL OR reply_how = 2)
"""
_LINK_TS_SQL = (f"UPDATE {TABLE} SET reply_ts = (SELECT r.ts FROM {TABLE} r WHERE r.id = {TABLE}.reply_id), "
                f"reply_how = CASE WHEN reply_id IS NULL THEN NULL ELSE reply_how END "
                f"WHERE (mid IN (SELECT mid FROM temp._reply_aff) OR id IN (SELECT id FROM temp._reply_aff_ids)) "
                f"AND dir IN (1, 2)")


def _link(conn, mids: Iterable[int], subjects: Iterable[Tuple[int, int, int]] = ()) -> None:
    """Пересчитать ответы: для писем с отпечатками ``mids`` (новые письма и те, на которые пришли ответы) — по
    заголовкам; по теме — для писем, на которые может отвечать письмо ``subjects`` (ящик, отпечаток темы, время)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _reply_aff(mid INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _reply_aff_subj(account_id INTEGER, subj INTEGER, ts INTEGER)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _reply_aff_ids(id INTEGER PRIMARY KEY)")
    for t in ("_reply_aff", "_reply_aff_subj", "_reply_aff_ids"):
        conn.execute(f"DELETE FROM temp.{t}")
    conn.executemany("INSERT OR IGNORE INTO temp._reply_aff(mid) VALUES(?)", [(int(m),) for m in mids if m])
    conn.execute(_LINK_SQL)
    conn.execute(_LINK_HOW_SQL)
    conn.executemany("INSERT INTO temp._reply_aff_subj(account_id, subj, ts) VALUES(?, ?, ?)",
                     [(int(a), int(t), int(ts)) for a, t, ts in subjects if t is not None])
    # письма, у которых по заголовкам ответа не нашлось, — тоже кандидаты на связь по теме
    conn.execute(f"INSERT INTO temp._reply_aff_subj(account_id, subj, ts) SELECT account_id, subj, ts FROM {TABLE} "
                 f"WHERE mid IN (SELECT mid FROM temp._reply_aff) AND subj IS NOT NULL AND reply_id IS NULL "
                 f"AND dir IN (1, 2)")
    conn.execute(_AFF_SUBJ_SQL)
    conn.execute(_LINK_SUBJ_SQL)
    conn.execute(_LINK_TS_SQL)
    for t in ("_reply_aff", "_reply_aff_subj", "_reply_aff_ids"):
        conn.execute(f"DELETE FROM temp.{t}")


def _marks(db) -> Tuple[Optional[int], Optional[int], Dict[str, int]]:
    """(верхняя отметка, нижняя отметка, ящики к переразбору). Верхней нет — индекс ещё не начинали."""
    high = db.get_meta(_META_HIGH)
    low = db.get_meta(_META_LOW)
    try:
        redo = {str(k): int(v) for k, v in json.loads(db.get_meta(_META_REDO) or "{}").items()}
    except (TypeError, ValueError, AttributeError):
        redo = {}
    return (int(high) if high not in (None, "") else None), (int(low) if low not in (None, "") else None), redo


def pending_count(db, account_ids: Optional[Sequence[int]] = None) -> int:
    """Сколько писем ещё не разобрано (новые, давние и письма ящиков, разбираемых заново)."""
    high, low, redo = _marks(db)
    ids = None if account_ids is None else [int(i) for i in account_ids]
    if ids is not None and not ids:
        return 0
    acc = "" if ids is None else f" AND account_id IN ({','.join('?' * len(ids))})"
    args = () if ids is None else tuple(ids)
    if high is None:
        return int(db.scalar(f"SELECT COUNT(*) FROM messages WHERE 1=1{acc}", args) or 0)
    low = high + 1 if low is None else low
    n = int(db.scalar(f"SELECT COUNT(*) FROM messages WHERE (id > ? OR id < ?){acc}", (high, low, *args)) or 0)
    for aid, cursor in redo.items():
        if ids is not None and int(aid) not in ids:
            continue
        n += int(db.scalar("SELECT COUNT(*) FROM messages WHERE account_id = ? AND id > ? AND id >= ? AND id <= ?",
                           (int(aid), cursor, low, high)) or 0)
    return n


def needs_pass(svc) -> bool:
    """Есть ли работа индексу: новые или давние письма, ящики к переразбору, поменявшиеся адреса ящиков."""
    db = svc.db
    if pending_count(db) > 0:
        return True
    try:
        stored = json.loads(db.get_meta(_META_OWN) or "{}")
    except (TypeError, ValueError):
        stored = {}
    current = Context(svc).fingerprints()
    return any(stored.get(k) not in (None, v) for k, v in current.items())


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def index_pending(svc, *, progress=None, cancelled=None, event=None, max_seconds: Optional[float] = None) -> Dict:
    """Разобрать письма: сначала новые (по возрастанию id), потом ящики, у которых поменялись адреса, потом
    давние — от новых к старым, чтобы раздел быстрее показывал последние месяцы."""
    db = svc.db
    ctx = Context(svc)
    gen = db.get_meta(_META_GEN) or "0"
    high, low, redo = _marks(db)
    if high is None:                                   # первый проход (или после «Пересобрать»)
        top = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages") or 0)
        high, low = top, top + 1
        with db.transaction() as conn:
            _set_meta(conn, _META_HIGH, str(high))
            _set_meta(conn, _META_LOW, str(low))
    elif low is None:
        low = 1
    # адреса ящика (или его сотрудника) поменялись — его уже разобранные письма разбираются заново
    fps = ctx.fingerprints()
    try:
        stored = json.loads(db.get_meta(_META_OWN) or "{}")
    except (TypeError, ValueError):
        stored = {}
    changed = [k for k, v in fps.items() if stored.get(k) not in (None, v)]
    for k in changed:
        redo[k] = 0
    if changed and event is not None:
        names = ", ".join(ctx.names.get(int(k), "#" + k) for k in changed[:10])
        event("INFO", f"Поменялись адреса ящиков ({names}{'…' if len(changed) > 10 else ''}) — их письма "
                      "разбираются заново.")
    with db.transaction() as conn:
        _set_meta(conn, _META_OWN, json.dumps(fps, sort_keys=True))
        _set_meta(conn, _META_REDO, json.dumps(redo, sort_keys=True))
    total_pending = pending_count(db)
    done = errors = 0
    started = time.time()
    stopped = ""
    while True:
        if cancelled is not None and cancelled():
            stopped = "cancelled"
            break
        if max_seconds is not None and time.time() - started > max_seconds:
            stopped = "slice"
            break
        phase, redo_acc = "new", None
        rows = db.query(f"SELECT {_MSG_COLS} FROM messages WHERE id > ? ORDER BY id LIMIT ?", (high, BATCH))
        if not rows and redo:
            redo_acc = next(iter(sorted(redo)))
            phase = "redo"
            rows = db.query(f"SELECT {_MSG_COLS} FROM messages WHERE account_id = ? AND id > ? AND id >= ? "
                            f"AND id <= ? ORDER BY id LIMIT ?", (int(redo_acc), redo[redo_acc], low, high, BATCH))
            if not rows:
                redo.pop(redo_acc)
                with db.transaction() as conn:
                    _set_meta(conn, _META_REDO, json.dumps(redo, sort_keys=True))
                continue
        if not rows and low > 1:
            phase = "old"
            rows = db.query(f"SELECT {_MSG_COLS} FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (low, BATCH))
            if not rows:
                low = 1
                with db.transaction() as conn:
                    _set_meta(conn, _META_LOW, "1")
                continue
        if not rows:
            break
        batch: List[list] = []
        for row in rows:
            rec, err = build_record(svc, ctx, row)
            if err:
                errors += 1
                if errors <= 20 and event is not None:
                    event("WARNING", f"Письмо #{row['id']}: заголовки не прочитаны ({err}) — в индексе без связей "
                                     "с ответами.")
            batch.append(list(rec))
        ids = [int(r["id"]) for r in rows]
        with db.transaction() as conn:
            now_gen = conn.execute("SELECT value FROM meta WHERE key=?", (_META_GEN,)).fetchone()
            if (now_gen[0] if now_gen else "0") != gen:
                stopped = "reset"
                break                          # индекс пересобрали во время прохода — порция устарела
            # То же письмо в другой папке ящика считается один раз — копия с меньшим id (сохранённая раньше),
            # в каком бы порядке ни шёл разбор (давние письма разбираются от новых к старым).
            seen: Dict[Tuple[int, int], list] = {}
            demote: List[int] = []
            for rec in batch:
                if rec[5] == SKIP or rec[2] < 0:
                    continue
                key = (rec[1], rec[2])
                other = seen.get(key)
                if other is not None:
                    keep, drop = (rec, other) if rec[0] < other[0] else (other, rec)
                    drop[5] = SKIP
                    seen[key] = keep
                    continue
                row = conn.execute(f"SELECT id FROM {TABLE} WHERE mid=? AND account_id=? AND dir<>0 AND id<>? "
                                   f"ORDER BY id LIMIT 1", (rec[2], rec[1], rec[0])).fetchone()
                if row is not None and int(row[0]) < rec[0]:
                    rec[5] = SKIP
                    continue
                if row is not None:
                    demote.append(int(row[0]))
                seen[key] = rec
            if demote:
                conn.executemany(f"UPDATE {TABLE} SET dir = 0 WHERE id = ?", [(i,) for i in demote])
            conn.executemany(_INSERT, [tuple(r) for r in batch])
            _link(conn, {r[2] for r in batch if r[5] != SKIP and r[2] > 0} | {r[3] for r in batch if r[3]},
                  {(r[1], r[13], r[11]) for r in batch if r[13] is not None and r[5] != SKIP})
            if phase == "new":
                high = max(ids)
                _set_meta(conn, _META_HIGH, str(high))
            elif phase == "redo":
                redo[redo_acc] = max(ids)
                _set_meta(conn, _META_REDO, json.dumps(redo, sort_keys=True))
            else:
                low = min(ids)
                _set_meta(conn, _META_LOW, str(low))
        done += len(batch)
        if progress is not None:
            progress(done, max(total_pending, done), f"Разобрано писем: {done} из {total_pending}")
    _cache_clear()
    return {"indexed": done, "errors": errors, "pending": pending_count(db), "stopped": stopped,
            "readdressed": len(changed)}


def reset_index(db) -> None:
    """Очистить индекс ответов — следующий проход разберёт архив заново (от новых писем к старым)."""
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {TABLE}")
        conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (_META_HIGH, _META_LOW, _META_REDO))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
                     "value=CAST(COALESCE(value, '0') AS INTEGER) + 1", (_META_GEN,))
    _cache_clear()


def indexed_since(db) -> Optional[str]:
    """С какой даты письма уже разобраны (пока давние письма ещё разбираются) — ISO или None."""
    high, low, _redo = _marks(db)
    if high is None or low is None or low <= 1:
        return None
    if db.scalar("SELECT 1 FROM messages WHERE id < ? LIMIT 1", (low,)) is None:
        return None
    return db.scalar("SELECT MIN(internaldate) FROM messages WHERE id >= ? AND id <= ?", (low, high))


def status(svc, account_ids: Optional[Sequence[int]] = None) -> Dict:
    """Состояние индекса: сколько писем разобрано и сколько ждёт (по всем или по своим ящикам)."""
    db = svc.db
    if account_ids is None:
        total = int(db.count_messages() or 0)
    else:
        ids = [int(i) for i in account_ids]
        total = sum(int(db.count_messages(i) or 0) for i in ids)
    pending = min(total, pending_count(db, account_ids))
    redo = _marks(db)[2]
    mine = None if account_ids is None else {int(i) for i in account_ids}
    readdress: List[str] = []
    for aid in sorted(int(a) for a in redo):
        acc = db.get_account(aid) if (mine is None or aid in mine) and pending else None
        if acc is not None:
            readdress.append(acc.name)
    from .models import JobType
    job = next((j for j in db.active_jobs() if j["type"] == JobType.REPLY_INDEX), None)
    return {"enabled": bool(svc.rt("replies", "enabled")), "total": total, "pending": pending,
            "indexed": max(0, total - pending), "ready": total > 0 and pending < total,
            "since": indexed_since(db) if pending else None, "readdress": readdress,
            "job_id": int(job["id"]) if job else None, "job_status": job["status"] if job else None}


# ===========================================================================
#  Настройки расчёта
# ===========================================================================
class Schedule:
    """Рабочее время и местное время писем (часовой пояс планировщика)."""

    def __init__(self, tz, days: frozenset, start_min: int, end_min: int):
        self.tz = tz
        self.days = days                   # 1 — понедельник … 7 — воскресенье
        self.start = start_min * 60
        self.end = end_min * 60
        self._off: Dict[int, int] = {}

    def offset(self, ts: int) -> int:
        key = ts // 3600
        v = self._off.get(key)
        if v is None:
            try:
                off = datetime.fromtimestamp(key * 3600, self.tz).utcoffset()
                v = int(off.total_seconds()) if off is not None else 0
            except (OverflowError, OSError, ValueError):
                v = 0
            self._off[key] = v
        return v

    def local(self, ts: int) -> int:
        return ts + self.offset(ts)

    @staticmethod
    def weekday(local: int) -> int:
        """0 — понедельник."""
        return (local // 86400 + 3) % 7

    def in_hours(self, ts: int) -> bool:
        loc = self.local(ts)
        sec = loc % 86400
        return (self.weekday(loc) + 1) in self.days and self.start <= sec < self.end

    def business_seconds(self, t0: int, t1: int) -> int:
        """Сколько рабочих секунд между t0 и t1."""
        if t1 <= t0:
            return 0
        off = self.offset(t0)
        l0, l1 = t0 + off, t1 + off
        d0, d1 = l0 // 86400, l1 // 86400
        if d1 - d0 > 400:                  # больше года — дальше не важно для времени ответа
            d1 = d0 + 400
            l1 = d1 * 86400 + 86399
        total = 0
        for d in range(d0, d1 + 1):
            if ((d + 3) % 7 + 1) not in self.days:
                continue
            ws, we = d * 86400 + self.start, d * 86400 + self.end
            total += max(0, min(we, l1) - max(ws, l0))
        return total


def calc_settings(svc) -> Dict:
    sla_h = svc.rt("replies", "sla_hours")
    try:
        sla_h = float(sla_h)
    except (TypeError, ValueError):
        sla_h = 24.0
    start, end = parse_work_hours(svc.rt("replies", "work_hours") or "")
    return {"sla_hours": max(0.25, sla_h), "sla_business": bool(svc.rt("replies", "sla_business")),
            "match_subject": bool(svc.rt("replies", "match_subject")),
            "work_days": parse_work_days(svc.rt("replies", "work_days") or ""),
            "work_start": start, "work_end": end,
            "exclude": [str(p).strip().lower() for p in (svc.rt("replies", "exclude_senders") or []) if str(p).strip()]}


def compile_excludes(patterns: Sequence[str]) -> Callable[[str], bool]:
    """Подходит ли адрес под исключения: «адрес», «@домен» или «домен» (и его поддомены), маски * и ?."""
    exact: Set[str] = set()
    doms: List[str] = []
    masks: List[str] = []
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if "*" in p or "?" in p:
            masks.append(p)
        elif p.startswith("@"):
            doms.append(p[1:])
        elif "@" in p:
            exact.add(p)
        else:
            doms.append(p)

    def match(addr: str) -> bool:
        a = (addr or "").lower()
        if not a:
            return False
        if a in exact:
            return True
        d = _domain(a)
        if any(d == x or d.endswith("." + x) for x in doms):
            return True
        return any(fnmatchcase(a, m) for m in masks)
    return match


# ===========================================================================
#  Расчёт показателей
# ===========================================================================
DELAY_BUCKETS = [(0, 900, "до 15 мин"), (900, 3600, "15–60 мин"), (3600, 4 * 3600, "1–4 ч"),
                 (4 * 3600, 8 * 3600, "4–8 ч"), (8 * 3600, 86400, "8–24 ч"), (86400, 3 * 86400, "1–3 дня"),
                 (3 * 86400, 7 * 86400, "3–7 дней"), (7 * 86400, math.inf, "больше недели")]
AGE_BUCKETS = [(0, 86400, "до суток"), (86400, 3 * 86400, "1–3 дня"), (3 * 86400, 7 * 86400, "3–7 дней"),
               (7 * 86400, 30 * 86400, "1–4 недели"), (30 * 86400, 90 * 86400, "1–3 месяца"),
               (90 * 86400, math.inf, "больше 3 месяцев")]
THREAD_BUCKETS = [(1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 5, "4–5"), (6, 10, "6–10"), (11, 20, "11–20"),
                  (21, 10 ** 9, "больше 20")]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _bucket_index(value: float, buckets) -> int:
    for i, (lo, hi, _l) in enumerate(buckets):
        if lo <= value < hi:
            return i
    return len(buckets) - 1


def pct(sorted_values: Sequence[float], p: float) -> Optional[float]:
    """Процентиль ``p`` (0…1) уже отсортированного списка, с интерполяцией."""
    n = len(sorted_values)
    if not n:
        return None
    k = (n - 1) * p
    f = math.floor(k)
    c = min(n - 1, f + 1)
    return float(sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f))


def _rate(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def _stats(delays: List[int]) -> Dict:
    s = sorted(delays)
    return {"n": len(s), "median_s": pct(s, 0.5), "p75_s": pct(s, 0.75), "p90_s": pct(s, 0.9),
            "mean_s": (sum(s) / len(s)) if s else None, "min_s": s[0] if s else None, "max_s": s[-1] if s else None}


class _Group:
    """Счётчики по входящим одной группы (ящик, отправитель, домен, час, ячейка тепловой карты…).

    Счётчики — в списке ``c`` (быстрее атрибутов на сотнях тысяч писем): см. ``C_*``.
    """
    __slots__ = ("c", "delays", "bdelays")

    def __init__(self):
        self.c = [0, 0, 0, 0, 0, 0, 0, 0]
        self.delays: List[int] = []
        self.bdelays: List[int] = []

    n = property(lambda self: self.c[C_N])
    own = property(lambda self: self.c[C_OWN])
    coll = property(lambda self: self.c[C_COLL])
    flag = property(lambda self: self.c[C_FLAG])
    none = property(lambda self: self.c[C_NONE])
    overdue = property(lambda self: self.c[C_OVERDUE])
    ontime = property(lambda self: self.c[C_ONTIME])
    late = property(lambda self: self.c[C_LATE])

    @property
    def answered(self) -> int:
        return self.c[C_OWN] + self.c[C_COLL] + self.c[C_FLAG]

    def add(self, other: "_Group") -> None:
        self.c = [a + b for a, b in zip(self.c, other.c)]
        self.delays += other.delays
        self.bdelays += other.bdelays

    def as_dict(self, with_stats: bool = True) -> Dict:
        d = {"count": self.n, "answered": self.answered, "own": self.own, "colleague": self.coll, "flag": self.flag,
             "unanswered": self.none, "overdue": self.overdue, "ontime": self.ontime, "late": self.late,
             "answered_pct": _rate(self.answered, self.n),
             "sla_pct": _rate(self.ontime, self.ontime + self.late + self.overdue)}
        if with_stats:
            s = sorted(self.delays)
            d.update({"median_s": pct(s, 0.5), "p90_s": pct(s, 0.9),
                      "mean_s": (sum(s) / len(s)) if s else None})
            if self.bdelays:
                d["median_business_s"] = pct(sorted(self.bdelays), 0.5)
        return d


C_N, C_OWN, C_COLL, C_FLAG, C_NONE, C_OVERDUE, C_ONTIME, C_LATE = range(8)


class _OutGroup:
    __slots__ = ("n", "replied", "noreply", "waits", "answers", "new")

    def __init__(self):
        self.n = self.replied = self.noreply = self.answers = self.new = 0
        self.waits: List[int] = []

    def as_dict(self) -> Dict:
        s = sorted(self.waits)
        return {"count": self.n, "replied": self.replied, "noreply": self.noreply, "answers": self.answers,
                "new": self.new, "replied_pct": _rate(self.replied, self.n), "median_wait_s": pct(s, 0.5),
                "p90_wait_s": pct(s, 0.9)}


def _bucket_key(day: int, size: str) -> int:
    if size == "day":
        return day
    if size == "week":
        return day - (day + 3) % 7
    d = date(1970, 1, 1) + timedelta(days=day)
    return d.year * 12 + d.month - 1


def _bucket_label(key: int, size: str) -> str:
    if size == "month":
        return f"{_MONTHS[key % 12]} {key // 12}"
    d = date(1970, 1, 1) + timedelta(days=key)
    return f"{d.day:02d}.{d.month:02d}" + ("" if size == "day" else f".{d.year % 100:02d}")


def _bucket_keys(day0: int, day1: int, size: str) -> List[int]:
    keys: List[int] = []
    d = day0
    while d <= day1:
        k = _bucket_key(d, size)
        if not keys or keys[-1] != k:
            keys.append(k)
        d += 1
    return keys


def _choose_bucket(days: float) -> str:
    if days <= 45:
        return "day"
    if days <= 400:
        return "week"
    return "month"


_cache: Dict[tuple, Tuple[float, Dict]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_S = 300.0


def _cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


#: Колонки строки индекса в расчёте (кортежи — в разы быстрее sqlite3.Row на сотнях тысяч писем).
_COLS = ("id, account_id, mid, parent, root, dir, kind, addressed, peer, domains, ts, flag_ans, reply_ts, other_ts, "
         "reply_how")
(R_ID, R_ACC, R_MID, R_PARENT, R_ROOT, R_DIR, R_KIND, R_ADDR, R_PEER, R_DOMS, R_TS, R_FLAG, R_REPLY, R_OTHER,
 R_HOW) = range(15)


def _reply_of(row, match_subject: bool) -> Optional[int]:
    """Время первого ответа строки; связь «по теме» не в счёт, если она выключена настройкой."""
    if row[R_REPLY] is None or (row[R_HOW] == 2 and not match_subject):
        return None
    return row[R_REPLY]


def _tuples(db, sql: str, params: tuple) -> List[tuple]:
    cur = db.connect().cursor()
    cur.row_factory = None
    try:
        return cur.execute(sql, params).fetchall()
    finally:
        cur.close()


def _fetch(db, account_ids: Optional[Sequence[int]], since: int, until: int) -> List[tuple]:
    if account_ids is None:
        return _tuples(db, f"SELECT {_COLS} FROM {TABLE} WHERE ts >= ? AND ts < ? AND dir IN (1, 2)", (since, until))
    ids = [int(i) for i in account_ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return _tuples(db, f"SELECT {_COLS} FROM {TABLE} WHERE account_id IN ({marks}) AND ts >= ? AND ts < ? "
                       f"AND dir IN (1, 2)", (*ids, since, until))


class Filters:
    """Отбор писем для расчёта: типы, свои/чужие, где ящик в адресатах, только новые обращения."""

    def __init__(self, ctx: Context, opts: Dict, kinds: Set[int], scope: str, addressed: str,
                 incoming: str = "all"):
        self.org = ctx.org_domains
        self.patterns = bool(opts["exclude"])
        self.excluded = compile_excludes(opts["exclude"])
        self.kinds = kinds
        self.scope = scope if scope in ("internal", "external") else "all"
        self.addressed = addressed if addressed in ("to", "to_cc") else "all"
        # new — только новые обращения: входящие, которые не ответ на письмо (ответы на наши письма не в счёт)
        self.incoming = "new" if incoming == "new" else "all"
        self._excl_cache: Dict[str, bool] = {}
        self._int_cache: Dict[str, bool] = {}

    def kind_of(self, row) -> int:
        k = row[R_KIND]
        if k == PERSONAL and self.patterns:
            peer = row[R_PEER]
            hit = self._excl_cache.get(peer)
            if hit is None:
                hit = self._excl_cache[peer] = self.excluded(peer)
            if hit:
                return EXCLUDED
        return k

    def internal(self, row) -> bool:
        doms = row[R_DOMS] or ""
        hit = self._int_cache.get(doms)
        if hit is None:
            parts = [d for d in doms.split(",") if d]
            hit = self._int_cache[doms] = bool(parts) and all(d in self.org for d in parts)
        return hit

    def keep(self, row, kind: int) -> bool:
        if kind not in self.kinds:
            return False
        if self.scope != "all" and (self.internal(row) != (self.scope == "internal")):
            return False
        if row[R_DIR] == IN:
            if self.addressed != "all":
                a = row[R_ADDR]
                if a != ADDR_TO and not (self.addressed == "to_cc" and a == ADDR_CC):
                    return False
            if self.incoming == "new" and row[R_PARENT] is not None:
                return False
        return True


def analyze(svc, *, account_ids: Optional[Sequence[int]] = None, since: Optional[int] = None,
            until: Optional[int] = None, kinds: Optional[Set[int]] = None, scope: str = "all",
            addressed: str = "all", incoming: str = "all", sla_hours: Optional[float] = None,
            sla_business: Optional[bool] = None, now: Optional[int] = None, use_cache: bool = True) -> Dict:
    """Все показатели раздела «Ответы на письма» за период [since, until)."""
    db = svc.db
    now = int(now if now is not None else time.time())
    opts = calc_settings(svc)
    if sla_hours is not None:
        opts["sla_hours"] = max(0.25, float(sla_hours))
    if sla_business is not None:
        opts["sla_business"] = bool(sla_business)
    kinds = set(kinds) if kinds else {PERSONAL}
    if until is None:
        until = now + 1
    if since is None:
        first = db.scalar(f"SELECT MIN(ts) FROM {TABLE} WHERE dir IN (1, 2)")
        since = int(first) if first is not None else until - 30 * 86400
    since, until = int(since), int(until)
    key = (tuple(sorted(account_ids)) if account_ids is not None else None, since, until, tuple(sorted(kinds)),
           scope, addressed, incoming, opts["sla_hours"], opts["sla_business"], now if not use_cache else None,
           db.get_meta(_META_HIGH), db.get_meta(_META_LOW), db.get_meta(_META_REDO), db.get_meta(_META_GEN),
           json.dumps(sorted(opts["exclude"])), opts["match_subject"],
           opts["work_days"], opts["work_start"], opts["work_end"],
           str(svc.rt("replies", "internal_domains")), str(svc.rt("scheduler", "timezone")))
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < _CACHE_TTL_S:
                return hit[1]
    started = time.time()
    ctx = Context(svc)
    sched = Schedule(svc.local_tz(), opts["work_days"], opts["work_start"], opts["work_end"])
    flt = Filters(ctx, opts, kinds, scope, addressed, incoming)
    span_days = max(1.0, (until - since) / 86400)
    bucket = _choose_bucket(span_days)
    cur = _Pass(svc, ctx, sched, flt, opts, now, since, until, bucket, full=True)
    cur.run(_fetch(db, account_ids, since, until))
    result = cur.result()
    if since > 0 and until - since <= 5 * 366 * 86400:
        p_since, p_until = since - (until - since), since
        prev = _Pass(svc, ctx, sched, flt, opts, now, p_since, p_until, bucket, full=False)
        prev.run(_fetch(db, account_ids, p_since, p_until))
        pr = prev.result()
        result["prev"] = pr["summary"]
        result["prev"]["since"], result["prev"]["until"] = p_since, p_until
        result["trend_prev"] = pr["trend"]
    else:
        result["prev"] = None
        result["trend_prev"] = []
    result["period"] = {"since": since, "until": until, "days": round(span_days, 1), "bucket": bucket}
    result["params"] = {"sla_hours": opts["sla_hours"], "sla_business": opts["sla_business"],
                        "work_days": sorted(opts["work_days"]),
                        "work_hours": f"{opts['work_start'] // 60:02d}:{opts['work_start'] % 60:02d}-"
                                      f"{opts['work_end'] // 60:02d}:{opts['work_end'] % 60:02d}",
                        "kinds": sorted(kinds), "scope": flt.scope, "addressed": flt.addressed,
                        "incoming": flt.incoming,
                        "internal_domains": sorted(ctx.org_domains)[:50], "timezone": str(svc.local_tz())}
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["took_ms"] = int((time.time() - started) * 1000)
    with _cache_lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (time.time(), result)
    return result


def _new_trend() -> Dict:
    return {"in": 0, "ontime": 0, "late": 0, "flag": 0, "waiting": 0, "overdue": 0, "delays": [], "out": 0,
            "out_replied": 0, "waits": []}


class _Pass:
    """Один проход по письмам периода: все счётчики сразу."""

    def __init__(self, svc, ctx: Context, sched: Schedule, flt: Filters, opts: Dict, now: int, since: int,
                 until: int, bucket: str, full: bool):
        self.svc, self.ctx, self.sched, self.flt, self.now = svc, ctx, sched, flt, now
        self.full = full
        self.sla = int(opts["sla_hours"] * 3600)
        self.sla_business = bool(opts["sla_business"])
        self.match_subject = bool(opts["match_subject"])
        self.by_subject = [0, 0]                # ответов найдено по теме: на входящие, на исходящие
        self.bucket = bucket
        self.since, self.until = since, until
        self.inc = _Group()
        self.out = _OutGroup()
        self.followups = [0, 0]                 # входящие-ответы на наши письма: всего, без ответа
        self.kind_counts: Counter = Counter()
        self.trend: Dict[int, Dict] = {}
        self._bkey: Dict[int, int] = {}
        if full:
            self.heat = [[_Group() for _ in range(24)] for _ in range(7)]
            self.work = {True: _Group(), False: _Group()}
            self.inner = {True: _Group(), False: _Group()}
            self.addr = {ADDR_TO: _Group(), ADDR_CC: _Group(), ADDR_OTHER: _Group()}
            self.thread_kind = {False: _Group(), True: _Group()}     # новое обращение / ответ в переписке
            self.accounts: Dict[int, _Group] = defaultdict(_Group)
            self.acc_out: Dict[int, _OutGroup] = defaultdict(_OutGroup)
            self.senders: Dict[str, _Group] = defaultdict(_Group)
            self.domains: Dict[str, _Group] = defaultdict(_Group)
            self.out_domains: Dict[str, _OutGroup] = defaultdict(_OutGroup)
            self.delay_hist = [0] * len(DELAY_BUCKETS)
            self.wait_hist = [0] * len(DELAY_BUCKETS)
            self.age_hist = [0] * len(AGE_BUCKETS)
            self.threads: Dict[int, list] = {}

    # -- один проход ---------------------------------------------------------
    def _trend(self, local: int) -> Dict:
        day = local // 86400
        k = self._bkey.get(day)
        if k is None:
            k = self._bkey[day] = _bucket_key(day, self.bucket)
        t = self.trend.get(k)
        if t is None:
            t = self.trend[k] = _new_trend()
        return t

    def _span(self, start: int, end: int) -> int:
        """Задержка для сравнения со сроком — в календарных или рабочих секундах."""
        return self.sched.business_seconds(start, end) if self.sla_business else end - start

    def run(self, rows) -> None:
        full = self.full
        flt = self.flt
        kind_counts = self.kind_counts
        local_of = self.sched.local
        threads = self.threads if full else None
        kinds = flt.kinds
        # без исключений и отборов (обычный случай) — без вызовов фильтра на каждое письмо
        simple = not flt.patterns and flt.scope == "all" and flt.addressed == "all" and flt.incoming == "all"
        for row in rows:
            d = row[R_DIR]
            if simple:
                kind = row[R_KIND]
                if d == IN:
                    kind_counts[kind] += 1
                if kind not in kinds:
                    continue
            else:
                kind = flt.kind_of(row)
                if d == IN:
                    kind_counts[kind] += 1
                if not flt.keep(row, kind):
                    continue
            ts = row[R_TS]
            local = local_of(ts)
            if d == IN:
                self._incoming(row, ts, local, full)
            else:
                self._outgoing(row, ts, local, full)
            if full:
                root = row[R_ROOT] if row[R_ROOT] is not None else row[R_MID]
                th = threads.get(root)
                if th is None:
                    # [письма (отпечатки), первое, последнее, id последнего, ящики, входящие, исходящие]
                    th = threads[root] = [set(), ts, ts, row[R_ID], set(), set(), set()]
                th[0].add(row[R_MID])
                th[4].add(row[R_ACC])
                if ts < th[1]:
                    th[1] = ts
                if ts >= th[2]:
                    th[2], th[3] = ts, row[R_ID]
                th[5 if d == IN else 6].add(row[R_MID])

    def _incoming(self, row, ts: int, local: int, full: bool) -> None:
        reply_ts, other_ts = _reply_of(row, self.match_subject), row[R_OTHER]
        if reply_ts is not None and row[R_HOW] == 2:
            self.by_subject[0] += 1
        if reply_ts is not None:
            status, first = 1, reply_ts
        elif other_ts is not None:
            status, first = 2, other_ts
        elif row[R_FLAG]:
            status, first = 3, None
        else:
            status, first = 0, None
        delay = bdelay = None
        late = overdue = False
        if first is not None:
            end = first if first > ts else ts
            delay = end - ts
            if full or self.sla_business:
                bdelay = self.sched.business_seconds(ts, end)
            late = (bdelay if self.sla_business else delay) > self.sla
        elif status == 0:
            overdue = self._span(ts, self.now) > self.sla
        t = self._trend(local)
        t["in"] += 1
        if row[R_PARENT] is not None:
            self.followups[0] += 1
            if status == 0:
                self.followups[1] += 1
        groups: List[_Group] = [self.inc]
        bgroups: List[_Group] = [self.inc]
        if full:
            wd, hr = (local // 86400 + 3) % 7, (local % 86400) // 3600
            acc = self.accounts[row[R_ACC]]
            work = self.work[self.sched.in_hours(ts)]
            inner = self.inner[self.flt.internal(row)]
            addr = self.addr[row[R_ADDR]]
            tk = self.thread_kind[row[R_PARENT] is not None]
            groups += [self.heat[wd][hr], work, inner, addr, tk, acc]
            bgroups += [work, inner, addr, tk, acc]
            if row[R_PEER]:
                groups.append(self.senders[row[R_PEER]])
            if row[R_DOMS]:
                groups.append(self.domains[row[R_DOMS]])
        slot = (C_NONE, C_OWN, C_COLL, C_FLAG)[status]
        extra = (C_LATE if late else C_ONTIME) if delay is not None else (C_OVERDUE if overdue else -1)
        if delay is not None:
            for g in groups:
                c = g.c
                c[0] += 1
                c[slot] += 1
                c[extra] += 1
                g.delays.append(delay)
        elif extra >= 0:
            for g in groups:
                c = g.c
                c[0] += 1
                c[slot] += 1
                c[extra] += 1
        else:
            for g in groups:
                c = g.c
                c[0] += 1
                c[slot] += 1
        if bdelay is not None:
            for g in bgroups:
                g.bdelays.append(bdelay)
        if delay is not None:
            t["late" if late else "ontime"] += 1
            t["delays"].append(delay)
            if full:
                self.delay_hist[_bucket_index(delay, DELAY_BUCKETS)] += 1
        elif status == 3:
            t["flag"] += 1
        else:
            t["overdue" if overdue else "waiting"] += 1
            if full:
                self.age_hist[_bucket_index(self.now - ts, AGE_BUCKETS)] += 1

    def _outgoing(self, row, ts: int, local: int, full: bool) -> None:
        reply_ts = _reply_of(row, self.match_subject)
        if reply_ts is not None and row[R_HOW] == 2:
            self.by_subject[1] += 1
        wait = (reply_ts - ts if reply_ts > ts else 0) if reply_ts is not None else None
        groups: List[_OutGroup] = [self.out]
        if full:
            groups.append(self.acc_out[row[R_ACC]])
            dom = (row[R_DOMS] or "").split(",", 1)[0]
            if dom:
                groups.append(self.out_domains[dom])
        is_answer = row[R_PARENT] is not None
        for g in groups:
            g.n += 1
            if is_answer:
                g.answers += 1
            else:
                g.new += 1
            if wait is not None:
                g.replied += 1
                g.waits.append(wait)
            else:
                g.noreply += 1
        t = self._trend(local)
        t["out"] += 1
        if wait is not None:
            t["out_replied"] += 1
            t["waits"].append(wait)
            if full:
                self.wait_hist[_bucket_index(wait, DELAY_BUCKETS)] += 1

    # -- итог ------------------------------------------------------------------
    def _summary(self) -> Dict:
        g = self.inc
        st = _stats(g.delays)
        b = sorted(g.bdelays)
        o = self.out.as_dict()
        waits = sorted(self.out.waits)
        return {
            "in_total": g.n, "answered": g.answered, "answered_pct": _rate(g.answered, g.n),
            "own": g.own, "colleague": g.coll, "flag": g.flag, "unanswered": g.none,
            "unanswered_pct": _rate(g.none, g.n), "overdue": g.overdue, "waiting": g.none - g.overdue,
            "ontime": g.ontime, "late": g.late, "sla_pct": _rate(g.ontime, g.ontime + g.late + g.overdue),
            "in_followups": self.followups[0], "in_followups_unanswered": self.followups[1],
            "by_subject_in": self.by_subject[0], "by_subject_out": self.by_subject[1],
            "median_s": st["median_s"], "mean_s": st["mean_s"], "p75_s": st["p75_s"], "p90_s": st["p90_s"],
            "min_s": st["min_s"], "max_s": st["max_s"], "median_business_s": pct(b, 0.5),
            "p90_business_s": pct(b, 0.9),
            "out_total": o["count"], "out_answers": o["answers"], "out_new": o["new"],
            "out_replied": o["replied"], "out_replied_pct": o["replied_pct"], "out_noreply": o["noreply"],
            "out_median_wait_s": pct(waits, 0.5), "out_p90_wait_s": pct(waits, 0.9),
            "kinds": [{"kind": k, "label": KIND_LABELS[k], "value": int(self.kind_counts.get(k, 0))}
                      for k in (PERSONAL, AUTOREPLY, BULK, NOTIFY, EXCLUDED)],
        }

    def _trend_list(self) -> List[Dict]:
        day0 = self.sched.local(self.since) // 86400
        day1 = self.sched.local(max(self.since, self.until - 1)) // 86400
        keys = _bucket_keys(day0, day1, self.bucket)
        if len(keys) > 500:
            keys = keys[-500:]
        out = []
        for k in keys:
            t = self.trend.get(k) or _new_trend()
            s = sorted(t["delays"])
            w = sorted(t["waits"])
            answered = t["ontime"] + t["late"] + t["flag"]
            out.append({"key": k, "label": _bucket_label(k, self.bucket), "in": t["in"], "ontime": t["ontime"],
                        "late": t["late"], "flag": t["flag"], "waiting": t["waiting"], "overdue": t["overdue"],
                        "answered_pct": _rate(answered, t["in"]), "median_s": pct(s, 0.5), "p90_s": pct(s, 0.9),
                        "out": t["out"], "out_replied": t["out_replied"],
                        "out_replied_pct": _rate(t["out_replied"], t["out"]), "out_median_wait_s": pct(w, 0.5)})
        return out

    def result(self) -> Dict:
        res = {"summary": self._summary(), "trend": self._trend_list()}
        if not self.full:
            return res
        res["delay_hist"] = [{"label": b[2], "value": v} for b, v in zip(DELAY_BUCKETS, self.delay_hist)]
        res["wait_hist"] = [{"label": b[2], "value": v} for b, v in zip(DELAY_BUCKETS, self.wait_hist)]
        res["age_hist"] = [{"label": b[2], "value": v} for b, v in zip(AGE_BUCKETS, self.age_hist)]
        s = self.inc
        res["how_answered"] = [{"key": "own", "label": "Ответил сам ящик", "value": s.own},
                               {"key": "colleague", "label": "Ответил коллега", "value": s.coll},
                               {"key": "flag", "label": "Отмечено «отвечено»", "value": s.flag},
                               {"key": "waiting", "label": "Ждёт ответа (срок не вышел)", "value": s.none - s.overdue},
                               {"key": "overdue", "label": "Без ответа, срок вышел", "value": s.overdue}]
        hours = [_Group() for _ in range(24)]
        wdays = [_Group() for _ in range(7)]
        for wd, row in enumerate(self.heat):            # часы и дни недели — суммы ячеек тепловой карты
            for hr, cell in enumerate(row):
                hours[hr].add(cell)
                wdays[wd].add(cell)
        res["by_hour"] = [{"label": f"{h:02d}", **g.as_dict()} for h, g in enumerate(hours)]
        res["by_weekday"] = [{"label": WEEKDAYS[i], **g.as_dict()} for i, g in enumerate(wdays)]
        res["heat"] = {"rows": WEEKDAYS,
                       "count": [[c.n for c in row] for row in self.heat],
                       "unanswered_pct": [[(_rate(c.none, c.n) or 0) for c in row] for row in self.heat],
                       "median_h": [[round((pct(sorted(c.delays), 0.5) or 0) / 3600, 2) for c in row]
                                    for row in self.heat]}
        res["work_split"] = [{"key": "work", "label": "Пришли в рабочее время", **self.work[True].as_dict()},
                             {"key": "off", "label": "Пришли в нерабочее время", **self.work[False].as_dict()}]
        res["internal_split"] = [{"key": "internal", "label": "От своих (внутренние)", **self.inner[True].as_dict()},
                                 {"key": "external", "label": "От внешних", **self.inner[False].as_dict()}]
        res["thread_split"] = [
            {"key": "new", "label": "Новое обращение", **self.thread_kind[False].as_dict()},
            {"key": "followup", "label": "Ответ в переписке (в т. ч. на наше письмо)", **self.thread_kind[True].as_dict()}]
        res["addressed_split"] = [
            {"key": "to", "label": "Ящик в «Кому»", **self.addr[ADDR_TO].as_dict()},
            {"key": "cc", "label": "Ящик в «Копии»", **self.addr[ADDR_CC].as_dict()},
            {"key": "other", "label": "Не указан (скрытая копия, список)", **self.addr[ADDR_OTHER].as_dict()}]
        res.update(self._accounts())
        res.update(self._peers())
        res["threads"] = self._threads()
        return res

    def _accounts(self) -> Dict:
        db = self.svc.db
        ids = sorted(set(self.accounts) | set(self.acc_out))
        info: Dict[int, Dict] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in db.query(f"SELECT a.id, a.name, a.username, e.full_name, e.department, e.position "
                              f"FROM accounts a LEFT JOIN employees e ON e.id = a.employee_id "
                              f"WHERE a.id IN ({marks})", tuple(ids)):
                info[int(r["id"])] = {"name": r["name"], "username": r["username"], "employee": r["full_name"] or "",
                                      "department": (r["department"] or "").strip(), "position": r["position"] or ""}
        rows = []
        deps: Dict[str, Dict] = {}
        for aid in ids:
            g = self.accounts.get(aid) or _Group()
            o = self.acc_out.get(aid) or _OutGroup()
            meta = info.get(aid) or {"name": self.ctx.names.get(aid, f"#{aid}"), "username": "", "employee": "",
                                     "department": "", "position": ""}
            od = o.as_dict()
            rows.append({"id": aid, **meta, **g.as_dict(), "out": od["count"], "out_answers": od["answers"],
                         "out_replied_pct": od["replied_pct"], "out_median_wait_s": od["median_wait_s"],
                         "out_noreply": od["noreply"]})
            dep = meta["department"] or "Без отдела"
            dd = deps.get(dep)
            if dd is None:
                dd = deps[dep] = {"g": _Group(), "o": _OutGroup(), "accounts": 0}
            dd["accounts"] += 1
            dd["g"].add(g)
            for attr in ("n", "replied", "noreply", "answers", "new"):
                setattr(dd["o"], attr, getattr(dd["o"], attr) + getattr(o, attr))
            dd["o"].waits += o.waits
        rows.sort(key=lambda r: -r["count"])
        departments = []
        for name, dd in deps.items():
            od = dd["o"].as_dict()
            departments.append({"name": name, "accounts": dd["accounts"], **dd["g"].as_dict(), "out": od["count"],
                                "out_replied_pct": od["replied_pct"], "out_median_wait_s": od["median_wait_s"]})
        departments.sort(key=lambda r: -r["count"])
        return {"accounts": rows, "departments": departments}

    def _peers(self) -> Dict:
        import heapq

        def top_groups(groups: Dict[str, _Group], key: str, sort_key, pred=None) -> List[Dict]:
            # as_dict (с сортировкой задержек) — только для попавших в таблицу: отправителей бывают сотни тысяч
            items = [(k, g) for k, g in groups.items() if pred is None or pred(g)]
            best = heapq.nsmallest(TOP_N, items, key=lambda kg: sort_key(kg[0], kg[1]))
            return [{key: k, **g.as_dict()} for k, g in best]

        with_delays = {k: g for k, g in self.domains.items() if len(g.delays) >= 3}
        medians = {k: pct(sorted(g.delays), 0.5) or 0 for k, g in with_delays.items()}
        out_domains = [{"domain": k, **g.as_dict()} for k, g in self.out_domains.items()]
        silent = [d for d in out_domains if d["count"] >= 3]
        return {
            "senders": top_groups(self.senders, "peer", lambda k, g: (-g.n, k)),
            "senders_ignored": top_groups(self.senders, "peer", lambda k, g: (-g.none, -g.n, k), lambda g: g.none > 0),
            "domains": top_groups(self.domains, "domain", lambda k, g: (-g.n, k)),
            "domains_slow": top_groups(with_delays, "domain", lambda k, g: (-medians[k], k)),
            "domains_fast": top_groups(with_delays, "domain", lambda k, g: (medians[k], k)),
            "out_domains": sorted(out_domains, key=lambda r: (-r["count"], r["domain"]))[:TOP_N],
            "out_domains_silent": sorted(silent, key=lambda r: ((r["replied_pct"] or 0), -r["count"],
                                                                r["domain"]))[:TOP_N],
            "senders_total": len(self.senders), "domains_total": len(self.domains),
        }

    def _threads(self) -> Dict:
        lengths = [len(t[0]) for t in self.threads.values()]
        hist = [0] * len(THREAD_BUCKETS)
        for n in lengths:
            for i, (lo, hi, _l) in enumerate(THREAD_BUCKETS):
                if lo <= n <= hi:
                    hist[i] += 1
                    break
        multi = [n for n in lengths if n > 1]
        import heapq
        longest = heapq.nsmallest(10, self.threads.values(), key=lambda t: (-len(t[0]), -t[2]))
        ids = [int(t[3]) for t in longest]
        subj: Dict[int, Dict] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in self.svc.db.query(f"SELECT m.id, m.account_id, m.folder, m.subject FROM messages m "
                                       f"WHERE m.id IN ({marks})", tuple(ids)):
                subj[int(r["id"])] = dict(r)
        top = []
        for t in longest:
            m = subj.get(int(t[3])) or {}
            top.append({"messages": len(t[0]), "incoming": len(t[5]), "outgoing": len(t[6]), "mailboxes": len(t[4]),
                        "first_ts": t[1], "last_ts": t[2], "duration_s": t[2] - t[1],
                        "subject": m.get("subject") or "", "message_id": int(t[3]),
                        "account_id": m.get("account_id"), "folder": m.get("folder")})
        return {"total": len(lengths), "with_replies": len(multi),
                "avg_len": round(sum(lengths) / len(lengths), 2) if lengths else None,
                "avg_len_multi": round(sum(multi) / len(multi), 2) if multi else None,
                "max_len": max(lengths) if lengths else 0,
                "hist": [{"label": b[2], "value": v} for b, v in zip(THREAD_BUCKETS, hist)], "longest": top}


# ===========================================================================
#  Списки писем
# ===========================================================================
LIST_MODES = {
    "unanswered": "Входящие без ответа",
    "overdue": "Входящие без ответа, срок вышел",
    "late": "Отвечены позже срока",
    "answered": "Отвеченные входящие",
    "out_noreply": "Исходящие без ответа",
    "out_replied": "Исходящие, на которые ответили",
}
LIST_MAX = 50_000


def list_messages(svc, *, mode: str, account_ids: Optional[Sequence[int]] = None, since: Optional[int] = None,
                  until: Optional[int] = None, kinds: Optional[Set[int]] = None, scope: str = "all",
                  addressed: str = "all", incoming: str = "all", sla_hours: Optional[float] = None,
                  sla_business: Optional[bool] = None, q: str = "", order: str = "old", offset: int = 0, limit: int = 100,
                  now: Optional[int] = None) -> Dict:
    """Письма выбранного вида постранично (``offset``/``limit``) — для таблиц и выгрузки в CSV."""
    if mode not in LIST_MODES:
        raise ValueError(f"Неизвестный список «{mode}».")
    db = svc.db
    now = int(now if now is not None else time.time())
    opts = calc_settings(svc)
    if sla_hours is not None:
        opts["sla_hours"] = max(0.25, float(sla_hours))
    if sla_business is not None:
        opts["sla_business"] = bool(sla_business)
    sla = int(opts["sla_hours"] * 3600)
    ctx = Context(svc)
    sched = Schedule(svc.local_tz(), opts["work_days"], opts["work_start"], opts["work_end"])
    flt = Filters(ctx, opts, set(kinds) if kinds else {PERSONAL}, scope, addressed, incoming)
    direction = OUT if mode.startswith("out_") else IN
    where = ["r.dir = ?"]
    params: List = [direction]
    if since is not None:
        where.append("r.ts >= ?")
        params.append(int(since))
    if until is not None:
        where.append("r.ts < ?")
        params.append(int(until))
    if account_ids is not None:
        ids = [int(i) for i in account_ids]
        if not ids:
            return {"items": [], "total": 0, "offset": offset, "mode": mode, "title": LIST_MODES[mode]}
        where.append(f"r.account_id IN ({','.join('?' * len(ids))})")
        params += ids
    # связь по теме может быть выключена — тогда её ответ не в счёт: такие строки отбираются в Python
    loose = "" if opts["match_subject"] else " OR r.reply_how = 2"
    if mode in ("unanswered", "overdue"):
        where.append(f"(r.reply_ts IS NULL{loose}) AND r.other_ts IS NULL AND r.flag_ans = 0")
    elif mode in ("late", "answered"):
        where.append("(r.reply_ts IS NOT NULL OR r.other_ts IS NOT NULL)")
    elif mode == "out_noreply":
        where.append(f"(r.reply_ts IS NULL{loose})")
    else:
        where.append("r.reply_ts IS NOT NULL")
    text = (q or "").strip().lower()
    if text:
        esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(lower(r.peer) LIKE ? ESCAPE '\\' OR lower(COALESCE(m.subject, '')) LIKE ? ESCAPE '\\')")
        params += [f"%{esc}%", f"%{esc}%"]
    cols = ", ".join("r." + c.strip() for c in _COLS.split(","))
    rows = _tuples(db, f"SELECT {cols} FROM {TABLE} r JOIN messages m ON m.id = r.id WHERE {' AND '.join(where)}",
                   tuple(params))
    picked: List[Tuple[int, int, Optional[int], int]] = []
    for r in rows:
        if not flt.keep(r, flt.kind_of(r)):
            continue
        ts = int(r[R_TS])
        own_reply = _reply_of(r, opts["match_subject"])
        first = own_reply if own_reply is not None else r[R_OTHER]
        if mode in ("late", "answered", "out_replied") and first is None:
            continue                           # ответ был найден только по теме, а связь по теме выключена
        if mode in ("unanswered", "overdue", "out_noreply") and own_reply is not None:
            continue
        if mode in ("overdue", "late"):
            end = now if mode == "overdue" else max(ts, int(first))
            span = sched.business_seconds(ts, end) if opts["sla_business"] else end - ts
            if span <= sla:
                continue
        picked.append((ts, int(r[R_ID]), int(first) if first is not None else None, int(r[R_ACC])))
    picked.sort(key=lambda x: (x[0], x[1]), reverse=(order == "new"))
    total = len(picked)
    offset = max(0, int(offset))
    page = picked[offset:offset + max(1, min(int(limit), LIST_MAX))]
    details: Dict[int, Dict] = {}
    if page:
        marks = ",".join("?" * len(page))
        for r in db.query(f"SELECT m.id, m.folder, m.subject, m.from_addr, m.size, a.name AS account_name, "
                          f"r.peer, r.domains, r.kind, r.addressed, r.reply_id, r.parent, r.rcpt, r.reply_how "
                          f"FROM messages m JOIN {TABLE} r ON r.id = m.id LEFT JOIN accounts a ON a.id = m.account_id "
                          f"WHERE m.id IN ({marks})", tuple(p[1] for p in page)):
            details[int(r["id"])] = dict(r)
    items = []
    for ts, mid, first, aid in page:
        d = details.get(mid) or {}
        delay = (first - ts) if first is not None else None
        items.append({"id": mid, "account_id": aid, "account_name": d.get("account_name") or f"#{aid}",
                      "folder": d.get("folder") or "", "subject": d.get("subject") or "",
                      "from": d.get("from_addr") or "", "peer": d.get("peer") or "",
                      "domains": d.get("domains") or "", "kind": d.get("kind"),
                      "kind_label": KIND_LABELS.get(int(d.get("kind") or 0), ""),
                      "addressed": d.get("addressed"), "rcpt": d.get("rcpt"), "ts": ts,
                      "date": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                      "age_s": now - ts, "delay_s": max(0, delay) if delay is not None else None,
                      "reply_id": d.get("reply_id"), "is_reply": d.get("parent") is not None,
                      "by_subject": d.get("reply_how") == 2 and delay is not None})
    return {"items": items, "total": total, "offset": offset, "mode": mode, "title": LIST_MODES[mode],
            "sla_hours": opts["sla_hours"], "sla_business": opts["sla_business"]}


def _human_delay(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 3600:
        return f"{max(1, s // 60)} мин"
    if s < 86400:
        return f"{s // 3600} ч {(s % 3600) // 60:02d} мин"
    return f"{s // 86400} д {(s % 86400) // 3600} ч"


def list_csv(svc, tz, listing: Dict) -> str:
    """Список писем — CSV (разделитель «;», как ждёт русский Excel; с BOM)."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Дата", "Ящик", "Папка", "Собеседник", "Тема", "Тип", "Ждёт / ответ через", "Секунд", "Ответ найден"])
    for it in listing["items"]:
        when = datetime.fromtimestamp(it["ts"], tz).strftime("%d.%m.%Y %H:%M")
        span = it["delay_s"] if it["delay_s"] is not None else it["age_s"]
        how = "" if it["delay_s"] is None else ("по теме" if it.get("by_subject") else "по заголовкам")
        w.writerow([when, it["account_name"], it["folder"], it["peer"], _csv_safe(it["subject"]), it["kind_label"],
                    _human_delay(span), int(span or 0), how])
    return buf.getvalue()


def _csv_safe(value: str) -> str:
    """Тема письма не должна стать формулой при открытии в Excel."""
    v = str(value or "")
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
