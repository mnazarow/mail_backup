"""Почтовый клиент (1.18.0): правила сортировки, автоответ, метки, шаблоны, адресная книга, «напомнить позже»,
отложенная отправка, архивы .zip, уведомления о прочтении, приглашения в календарь.

Что работает и без открытого клиента — раз в минуту из планировщика (:func:`tick`):

* письма, отправку которых запланировали, уходят в назначенное время;
* письма, отложенные «напомнить позже», возвращаются во «Входящие» непрочитанными;
* правила сортировки и автоответ: каждые ``webmail.rules_interval_min`` минут клиент сам проверяет
  «Входящие» ящиков, у которых есть правила или включён автоответ (0 — только когда клиент открыт).

Правила и автоответ касаются только новых писем — пришедших не раньше трёх суток назад и после того, как появилось
первое правило (или включили автоответ). Письмо, которое вернули во «Входящие» вручную, правила не трогают второй
раз: обработанные письма запоминаются по содержимому (хешу), а не по номеру в архиве.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import webmail as wm
from .errors import MailArchiverError, ValidationError
from .logging_setup import get_logger
from .models import Account
from .util import ensure_dir, utcnow_iso

log = get_logger("webmail")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webmail_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',        -- '' — правило; block — заблокированный отправитель
    enabled     INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    match       TEXT NOT NULL DEFAULT 'all',     -- all — все условия; any — любое
    conditions  TEXT NOT NULL DEFAULT '[]',
    actions     TEXT NOT NULL DEFAULT '[]',
    stop        INTEGER NOT NULL DEFAULT 0,      -- остальные правила к письму не применять
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_webmail_rules_acc ON webmail_rules(account_id, position);
-- до какого письма архива правила и автоответ уже смотрели «Входящие»
CREATE TABLE IF NOT EXISTS webmail_watch (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    last_id     INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    last_error  TEXT NOT NULL DEFAULT ''
);
-- письма, которые правила уже обработали (по хешу содержимого: перенесённая копия — то же письмо)
CREATE TABLE IF NOT EXISTS webmail_rule_done (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    sha256      TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (account_id, sha256)
);
CREATE TABLE IF NOT EXISTS webmail_autoreply (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    enabled     INTEGER NOT NULL DEFAULT 0,
    date_from   TEXT NOT NULL DEFAULT '',        -- YYYY-MM-DD ('' — сразу)
    date_to     TEXT NOT NULL DEFAULT '',        -- YYYY-MM-DD включительно ('' — пока не выключат)
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',        -- HTML
    days        INTEGER NOT NULL DEFAULT 4,      -- одному отправителю — не чаще раза в столько дней
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS webmail_autoreply_log (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    addr        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, addr)
);
CREATE TABLE IF NOT EXISTS webmail_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#2563eb',
    keyword     TEXT NOT NULL,                   -- ключевое слово IMAP: метка видна и в других программах
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, keyword)
);
CREATE TABLE IF NOT EXISTS webmail_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_webmail_templates_owner ON webmail_templates(owner, name);
CREATE TABLE IF NOT EXISTS webmail_book (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    groups      TEXT NOT NULL DEFAULT '',        -- через запятую
    updated_at  TEXT,
    UNIQUE (account_id, email)
);
CREATE TABLE IF NOT EXISTS webmail_snooze (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_pk    INTEGER,                       -- письмо архива в папке «Отложенные»
    msgid         TEXT NOT NULL DEFAULT '',      -- Message-ID (если сервер не сообщил номер копии)
    folder        TEXT NOT NULL,                 -- где письмо лежит, пока отложено
    return_folder TEXT NOT NULL,
    until         TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_webmail_snooze_until ON webmail_snooze(until);
CREATE TABLE IF NOT EXISTS webmail_kv (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, key)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


#: Правил на ящик, условий и действий в правиле.
MAX_RULES = 200
MAX_CONDITIONS = 10
MAX_ACTIONS = 6
#: Писем за один проход правил (остальные — в следующий).
RULES_BATCH = 300
#: Правила и автоответ — только письмам не старше, суток (иначе первое копирование ящика «сработало» бы на всё).
NEW_MAIL_DAYS = 3
#: «Напомнить позже» — папка на сервере.
SNOOZE_FOLDER = "Отложенные"
#: Архив .zip выбранных писем: писем и объём (несжатый).
ZIP_MAX_MESSAGES = 2000
ZIP_MAX_BYTES = 2 * 1024 ** 3
MAX_TEMPLATES = 200
MAX_BOOK = 5000

_EMAIL_RE = wm._EMAIL_RE  # noqa: SLF001


def _kv_get(svc, account_id: int, key: str) -> Optional[str]:
    row = svc.db.query_one("SELECT value FROM webmail_kv WHERE account_id=? AND key=?", (int(account_id), key))
    return row["value"] if row is not None else None


def _kv_set(svc, account_id: int, key: str, value: str) -> None:
    svc.db.execute("INSERT INTO webmail_kv(account_id, key, value) VALUES(?,?,?) "
                   "ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value", (int(account_id), key, value))


def _clip(value, limit: int) -> str:
    return wm._one_line(str(value or ""), limit).strip()  # noqa: SLF001


# ===========================================================================
#  Заголовки письма архива (без разбора тела)
# ===========================================================================
def headers_of(svc, account: Account, row):
    with svc.store.open_message(int(account.id), row["stored_path"]) as fh:
        head = fh.read(256 * 1024)
    m = re.search(rb"\r?\n\r?\n", head)
    return BytesHeaderParser(policy=policy.default).parsebytes(head[:m.end()] if m else head)


def _h(hdrs, name: str) -> str:
    return wm._h(hdrs, name)  # noqa: SLF001


def _addr_list(value: str) -> List[str]:
    return [a.lower() for _n, a in getaddresses([value or ""]) if a and "@" in a]


# ===========================================================================
#  Метки (ключевые слова IMAP)
# ===========================================================================
#: Как в Thunderbird: те же ключевые слова — метки видны и там.
DEFAULT_TAGS = (("Важно", "#dc2626", "$label1"), ("Работа", "#ea580c", "$label2"), ("Личное", "#16a34a", "$label3"),
                ("Сделать", "#2563eb", "$label4"), ("Позже", "#7c3aed", "$label5"))
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def list_tags(svc, account_id: int) -> List[Dict]:
    if _kv_get(svc, account_id, "tags_seeded") is None:
        for i, (name, color, kw) in enumerate(DEFAULT_TAGS):
            svc.db.execute("INSERT OR IGNORE INTO webmail_tags(account_id, name, color, keyword, position) "
                           "VALUES(?,?,?,?,?)", (int(account_id), name, color, kw, i))
        _kv_set(svc, account_id, "tags_seeded", "1")
    return [dict(r) for r in svc.db.query("SELECT id, name, color, keyword, position FROM webmail_tags "
                                          "WHERE account_id=? ORDER BY position, id", (int(account_id),))]


def save_tag(svc, account_id: int, data: Dict, tag_id: Optional[int] = None) -> Dict:
    list_tags(svc, account_id)
    name = _clip(data.get("name"), 40)
    if not name:
        raise ValidationError("Укажите название метки.")
    color = str(data.get("color") or "#2563eb")
    if not _COLOR_RE.fullmatch(color):
        raise ValidationError("Цвет метки — в виде #RRGGBB.")
    dup = [t for t in list_tags(svc, account_id) if t["name"].casefold() == name.casefold() and t["id"] != int(tag_id or 0)]
    if dup:
        raise ValidationError(f"Метка «{name}» уже есть.")
    if tag_id:
        cur = svc.db.execute("UPDATE webmail_tags SET name=?, color=? WHERE id=? AND account_id=?",
                             (name, color, int(tag_id), int(account_id)))
        if not int(cur.rowcount or 0):
            raise ValidationError("Метка не найдена.", code="not_found")
        return dict(svc.db.query_one("SELECT * FROM webmail_tags WHERE id=?", (int(tag_id),)))
    if int(svc.db.scalar("SELECT COUNT(*) FROM webmail_tags WHERE account_id=?", (int(account_id),)) or 0) >= 50:
        raise ValidationError("Меток — не больше 50.")
    pos = int(svc.db.scalar("SELECT COALESCE(MAX(position), 0) + 1 FROM webmail_tags WHERE account_id=?",
                            (int(account_id),)) or 0)
    cur = svc.db.execute("INSERT INTO webmail_tags(account_id, name, color, keyword, position) VALUES(?,?,?,?,?)",
                         (int(account_id), name, color, f"$tmp{time.time_ns()}", pos))
    tid = int(cur.lastrowid)
    svc.db.execute("UPDATE webmail_tags SET keyword=? WHERE id=?", (f"$MA{tid}", tid))
    return dict(svc.db.query_one("SELECT * FROM webmail_tags WHERE id=?", (tid,)))


def delete_tag(svc, account_id: int, tag_id: int) -> None:
    svc.db.execute("DELETE FROM webmail_tags WHERE id=? AND account_id=?", (int(tag_id), int(account_id)))


def _tag_keyword(svc, account_id: int, keyword: str) -> str:
    kw = wm._norm_flag(keyword)  # noqa: SLF001
    if not any(t["keyword"] == kw for t in list_tags(svc, account_id)):
        raise ValidationError("Такой метки нет.", hint="Создайте её: «⚙ Настройки клиента → Метки».")
    return kw


def tag_messages(svc, account: Account, ids: Iterable[int], keyword: str, on: bool = True) -> Dict:
    kw = _tag_keyword(svc, int(account.id), keyword)
    return wm.set_flags(svc, account, ids, add=[kw] if on else [], remove=[] if on else [kw])


# ===========================================================================
#  Шаблоны писем (у каждого пользователя свои)
# ===========================================================================
def list_templates(svc, owner: str) -> List[Dict]:
    return [dict(r) for r in svc.db.query("SELECT id, name, subject, body, updated_at FROM webmail_templates "
                                          "WHERE owner=? ORDER BY name COLLATE NOCASE", (owner,))]


def save_template(svc, owner: str, data: Dict, template_id: Optional[int] = None) -> Dict:
    name = _clip(data.get("name"), 100)
    if not name:
        raise ValidationError("Укажите название шаблона.")
    subject = _clip(data.get("subject"), wm.MAX_SUBJECT)
    body = wm.sanitize_html(str(data.get("body") or ""))
    if len(body) > 2_000_000:
        raise ValidationError("Шаблон слишком большой (картинки в шаблоне — до 1 МБ).")
    now = utcnow_iso()
    if template_id:
        cur = svc.db.execute("UPDATE webmail_templates SET name=?, subject=?, body=?, updated_at=? WHERE id=? AND owner=?",
                             (name, subject, body, now, int(template_id), owner))
        if not int(cur.rowcount or 0):
            raise ValidationError("Шаблон не найден.", code="not_found")
        tid = int(template_id)
    else:
        if int(svc.db.scalar("SELECT COUNT(*) FROM webmail_templates WHERE owner=?", (owner,)) or 0) >= MAX_TEMPLATES:
            raise ValidationError(f"Шаблонов — не больше {MAX_TEMPLATES}.")
        cur = svc.db.execute("INSERT INTO webmail_templates(owner, name, subject, body, updated_at) VALUES(?,?,?,?,?)",
                             (owner, name, subject, body, now))
        tid = int(cur.lastrowid)
    return dict(svc.db.query_one("SELECT id, name, subject, body, updated_at FROM webmail_templates WHERE id=?", (tid,)))


def delete_template(svc, owner: str, template_id: int) -> None:
    svc.db.execute("DELETE FROM webmail_templates WHERE id=? AND owner=?", (int(template_id), owner))


# ===========================================================================
#  Адресная книга ящика и группы
# ===========================================================================
def _groups_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    names = [_clip(g, 60) for g in str(value or "").split(",")]
    return ", ".join(dict.fromkeys(g for g in names if g))


def list_book(svc, account_id: int, q: str = "", group: str = "") -> List[Dict]:
    # отбор — здесь, а не в SQL: lower() и LIKE в SQLite не различают регистр только у латиницы
    rows = [dict(r) for r in svc.db.query(f"SELECT * FROM webmail_book WHERE account_id=? LIMIT {MAX_BOOK}",
                                          (int(account_id),))]
    q, group = (q or "").strip().casefold(), (group or "").strip().casefold()
    if q:
        rows = [r for r in rows if any(q in (r[k] or "").casefold() for k in ("name", "email", "company", "phone"))]
    if group:
        rows = [r for r in rows if group in {g.strip().casefold() for g in (r["groups"] or "").split(",")}]
    return sorted(rows, key=lambda r: ((r["name"] or r["email"]).casefold(), r["email"]))


def book_groups(svc, account_id: int) -> List[Dict]:
    counts: Dict[str, int] = {}
    for r in svc.db.query("SELECT groups FROM webmail_book WHERE account_id=? AND groups<>''", (int(account_id),)):
        for g in (x.strip() for x in r["groups"].split(",")):
            if g:
                counts[g] = counts.get(g, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[0].lower())]


def save_contact(svc, account_id: int, data: Dict, contact_id: Optional[int] = None) -> Dict:
    email = _clip(data.get("email"), 300)
    if not _EMAIL_RE.fullmatch(email):
        raise ValidationError(f"«{email}» — не адрес электронной почты.")
    vals = (_clip(data.get("name"), 200), email, _clip(data.get("phone"), 100), _clip(data.get("company"), 200),
            str(data.get("note") or "")[:2000], _groups_text(data.get("groups")), utcnow_iso())
    dup = svc.db.query_one("SELECT id FROM webmail_book WHERE account_id=? AND lower(email)=lower(?) AND id<>?",
                           (int(account_id), email, int(contact_id or 0)))
    if contact_id:
        if dup is not None:
            raise ValidationError(f"Адрес {email} уже есть в адресной книге.")
        cur = svc.db.execute("UPDATE webmail_book SET name=?, email=?, phone=?, company=?, note=?, groups=?, updated_at=? "
                             "WHERE id=? AND account_id=?", (*vals, int(contact_id), int(account_id)))
        if not int(cur.rowcount or 0):
            raise ValidationError("Контакт не найден.", code="not_found")
        cid = int(contact_id)
    elif dup is not None:
        cid = int(dup["id"])
        svc.db.execute("UPDATE webmail_book SET name=CASE WHEN ?<>'' THEN ? ELSE name END, phone=CASE WHEN ?<>'' THEN ? "
                       "ELSE phone END, company=CASE WHEN ?<>'' THEN ? ELSE company END, note=CASE WHEN ?<>'' THEN ? "
                       "ELSE note END, groups=CASE WHEN ?<>'' THEN ? ELSE groups END, updated_at=? WHERE id=?",
                       (vals[0], vals[0], vals[2], vals[2], vals[3], vals[3], vals[4], vals[4], vals[5], vals[5],
                        vals[6], cid))
    else:
        if int(svc.db.scalar("SELECT COUNT(*) FROM webmail_book WHERE account_id=?", (int(account_id),)) or 0) >= MAX_BOOK:
            raise ValidationError(f"В адресной книге — не больше {MAX_BOOK} адресов.")
        cur = svc.db.execute("INSERT INTO webmail_book(account_id, name, email, phone, company, note, groups, updated_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (int(account_id), *vals))
        cid = int(cur.lastrowid)
    return dict(svc.db.query_one("SELECT * FROM webmail_book WHERE id=?", (cid,)))


def delete_contacts(svc, account_id: int, ids: Sequence[int]) -> int:
    n = 0
    for i in ids:
        cur = svc.db.execute("DELETE FROM webmail_book WHERE id=? AND account_id=?", (int(i), int(account_id)))
        n += int(cur.rowcount or 0)
    return n


def _vcard_unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_vcards(text: str) -> List[Dict]:
    text = re.sub(r"\r?\n[ \t]", "", text)          # склеить перенесённые строки
    out: List[Dict] = []
    cur: Optional[Dict] = None
    for line in text.splitlines():
        if line.upper().startswith("BEGIN:VCARD"):
            cur = {"name": "", "emails": [], "phone": "", "company": "", "note": "", "groups": ""}
        elif line.upper().startswith("END:VCARD"):
            if cur is not None:
                for e in cur["emails"] or []:
                    out.append({k: v for k, v in cur.items() if k != "emails"} | {"email": e})
            cur = None
        elif cur is not None and ":" in line:
            key, _sep, value = line.partition(":")
            prop = key.split(";")[0].split(".")[-1].upper()
            value = _vcard_unescape(value.strip())
            if prop == "FN":
                cur["name"] = value
            elif prop == "N" and not cur["name"]:
                parts = [p for p in value.split(";")[:3] if p]
                cur["name"] = " ".join(parts)
            elif prop == "EMAIL" and value:
                cur["emails"].append(value)
            elif prop == "TEL" and not cur["phone"]:
                cur["phone"] = value
            elif prop == "ORG":
                cur["company"] = value.split(";")[0]
            elif prop == "NOTE":
                cur["note"] = value
            elif prop == "CATEGORIES":
                cur["groups"] = value
    return out


_CSV_KEYS = {
    "name": ("name", "имя", "фио", "display name", "full name", "отображаемое имя"),
    "email": ("email", "e-mail", "почта", "адрес", "e-mail address", "email address", "адрес эл. почты",
              "primary email", "электронная почта"),
    "phone": ("phone", "телефон", "mobile phone", "мобильный телефон", "business phone", "рабочий телефон"),
    "company": ("company", "организация", "компания", "organization"),
    "note": ("note", "notes", "заметка", "заметки"),
    "groups": ("groups", "группы", "группа", "categories", "категории"),
}


def _parse_csv(text: str) -> List[Dict]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []
    head = [h.strip().lower().lstrip("\ufeff") for h in rows[0]]
    col: Dict[str, int] = {}
    for key, names in _CSV_KEYS.items():
        for i, h in enumerate(head):
            if h in names and key not in col:
                col[key] = i
    if "email" not in col:
        # без заголовка: ищем столбец с адресами
        for i, v in enumerate(rows[0]):
            if _EMAIL_RE.fullmatch(v.strip()):
                col = {"email": i}
                if i > 0:
                    col["name"] = 0
                break
        else:
            raise ValidationError("В файле не найден столбец с адресами (Email, «Почта»).")
        body = rows
    else:
        body = rows[1:]
    out = []
    for r in body:
        item = {k: (r[i].strip() if i < len(r) else "") for k, i in col.items()}
        if item.get("email"):
            out.append(item)
    return out


def import_book(svc, account_id: int, filename: str, data: bytes) -> Dict:
    if len(data) > 5 * 1024 * 1024:
        raise ValidationError("Файл больше 5 МБ.")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", "replace")
    if "BEGIN:VCARD" in text.upper() or filename.lower().endswith((".vcf", ".vcard")):
        items = _parse_vcards(text)
    else:
        items = _parse_csv(text)
    added = updated = skipped = 0
    before = int(svc.db.scalar("SELECT COUNT(*) FROM webmail_book WHERE account_id=?", (int(account_id),)) or 0)
    for item in items[:MAX_BOOK]:
        try:
            save_contact(svc, account_id, item)
        except ValidationError:
            skipped += 1
    after = int(svc.db.scalar("SELECT COUNT(*) FROM webmail_book WHERE account_id=?", (int(account_id),)) or 0)
    added = after - before
    updated = max(0, len(items[:MAX_BOOK]) - skipped - added)
    return {"added": added, "updated": updated, "skipped": skipped, "total": len(items)}


def _vcard_escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def export_book(svc, account_id: int, fmt: str = "vcf") -> Tuple[bytes, str, str]:
    rows = list_book(svc, account_id)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Имя", "Email", "Телефон", "Организация", "Группы", "Заметка"])
        for r in rows:
            w.writerow([r["name"], r["email"], r["phone"], r["company"], r["groups"], r["note"]])
        return ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "contacts.csv"
    out = []
    for r in rows:
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_vcard_escape(r['name'] or r['email'])}",
                 f"EMAIL;TYPE=INTERNET:{r['email']}"]
        if r["phone"]:
            lines.append(f"TEL:{_vcard_escape(r['phone'])}")
        if r["company"]:
            lines.append(f"ORG:{_vcard_escape(r['company'])}")
        if r["groups"]:
            lines.append("CATEGORIES:" + ",".join(_vcard_escape(g.strip()) for g in r["groups"].split(",")))
        if r["note"]:
            lines.append(f"NOTE:{_vcard_escape(r['note'])}")
        lines.append("END:VCARD")
        out.append("\r\n".join(lines))
    return ("\r\n".join(out) + "\r\n").encode("utf-8"), "text/vcard; charset=utf-8", "contacts.vcf"


def book_suggest(svc, account_id: int, q: str, limit: int = 10) -> List[Dict]:
    """Подсказка адресов из адресной книги и группы (группа раскрывается во все её адреса)."""
    q = (q or "").strip().casefold()
    if len(q) < 1:
        return []
    out: List[Dict] = []
    for g in book_groups(svc, account_id):
        if q in g["name"].casefold():
            members = list_book(svc, account_id, group=g["name"])
            addrs = ", ".join(wm.display_addr(m["name"], m["email"]) for m in members[:wm.MAX_RECIPIENTS])
            out.append({"name": g["name"], "email": "", "group": g["name"], "members": addrs,
                        "label": f"👥 {g['name']} — адресов: {len(members)}"})
    for r in list_book(svc, account_id):
        if q in r["name"].casefold() or q in r["email"].casefold() or q in (r["company"] or "").casefold():
            out.append({"name": r["name"], "email": r["email"], "book": True,
                        "label": wm.display_addr(r["name"], r["email"])})
            if len(out) >= limit:
                break
    return out[:limit]


# ===========================================================================
#  Правила сортировки
# ===========================================================================
RULE_FIELDS = ("from", "to", "cc", "to_cc", "subject", "body", "attach", "size", "list", "priority")
TEXT_OPS = ("contains", "not_contains", "equals", "not_equals", "starts", "ends")
ACTION_TYPES = ("move", "read", "flag", "tag", "delete", "junk", "important")


def _rule_dict(row) -> Dict:
    d = dict(row)
    for k in ("conditions", "actions"):
        try:
            d[k] = json.loads(d[k] or "[]")
        except (TypeError, ValueError):
            d[k] = []
    d["enabled"], d["stop"] = bool(d["enabled"]), bool(d["stop"])
    return d


def list_rules(svc, account_id: int) -> List[Dict]:
    return [_rule_dict(r) for r in svc.db.query("SELECT * FROM webmail_rules WHERE account_id=? ORDER BY position, id",
                                                (int(account_id),))]


def _check_rule(svc, account: Account, data: Dict) -> Dict:
    name = _clip(data.get("name"), 100) or "Правило"
    match = data.get("match") if data.get("match") in ("all", "any") else "all"
    conds = []
    for c in list(data.get("conditions") or [])[:MAX_CONDITIONS]:
        field = str(c.get("field") or "")
        op = str(c.get("op") or "contains")
        value = str(c.get("value") or "").strip()[:300]
        if field not in RULE_FIELDS:
            raise ValidationError(f"Неизвестное условие правила: {field}.")
        if field in ("attach", "list", "priority"):
            op = "yes" if op in ("yes", "true", "1") else "no"
            value = ""
        elif field == "size":
            if op not in ("gt", "lt"):
                raise ValidationError("Размер: «больше» или «меньше».")
            try:
                value = str(max(0.0, float(value.replace(",", "."))))
            except ValueError:
                raise ValidationError("Размер письма в правиле — число мегабайт.") from None
        else:
            if op not in TEXT_OPS:
                raise ValidationError(f"Неизвестное сравнение: {op}.")
            if not value:
                raise ValidationError("Заполните значения условий правила.")
        conds.append({"field": field, "op": op, "value": value})
    if not conds:
        raise ValidationError("Добавьте хотя бы одно условие.")
    acts = []
    folders = {r["name"] for r in wm._folder_rows(svc, int(account.id)) if r["selectable"]}  # noqa: SLF001
    for a in list(data.get("actions") or [])[:MAX_ACTIONS]:
        t = str(a.get("type") or "")
        if t not in ACTION_TYPES:
            raise ValidationError(f"Неизвестное действие правила: {t}.")
        item = {"type": t}
        if t == "move":
            folder = str(a.get("folder") or "")
            if folder not in folders:
                raise ValidationError(f"Папки «{folder}» на сервере нет.", hint="Обновите список папок.")
            item["folder"] = folder
        elif t == "tag":
            item["keyword"] = _tag_keyword(svc, int(account.id), str(a.get("keyword") or ""))
        acts.append(item)
    if not acts:
        raise ValidationError("Добавьте хотя бы одно действие.")
    return {"name": name, "match": match, "conditions": conds, "actions": acts, "stop": bool(data.get("stop")),
            "enabled": data.get("enabled", True) is not False}


def _init_watch(svc, account_id: int) -> None:
    if svc.db.query_one("SELECT 1 FROM webmail_watch WHERE account_id=?", (int(account_id),)) is None:
        last = int(svc.db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages WHERE account_id=?",
                                 (int(account_id),)) or 0)
        svc.db.execute("INSERT OR IGNORE INTO webmail_watch(account_id, last_id) VALUES(?, ?)", (int(account_id), last))


def save_rule(svc, account: Account, data: Dict, rule_id: Optional[int] = None, *, actor: str = "",
              kind: str = "") -> Dict:
    r = _check_rule(svc, account, data)
    acc_id = int(account.id)
    _init_watch(svc, acc_id)
    if rule_id:
        cur = svc.db.execute("UPDATE webmail_rules SET name=?, enabled=?, match=?, conditions=?, actions=?, stop=? "
                             "WHERE id=? AND account_id=?",
                             (r["name"], 1 if r["enabled"] else 0, r["match"], json.dumps(r["conditions"], ensure_ascii=False),
                              json.dumps(r["actions"], ensure_ascii=False), 1 if r["stop"] else 0, int(rule_id), acc_id))
        if not int(cur.rowcount or 0):
            raise ValidationError("Правило не найдено.", code="not_found")
        rid = int(rule_id)
    else:
        if int(svc.db.scalar("SELECT COUNT(*) FROM webmail_rules WHERE account_id=?", (acc_id,)) or 0) >= MAX_RULES:
            raise ValidationError(f"Правил — не больше {MAX_RULES}.")
        pos = int(svc.db.scalar("SELECT COALESCE(MAX(position), 0) + 1 FROM webmail_rules WHERE account_id=?",
                                (acc_id,)) or 0)
        cur = svc.db.execute(
            "INSERT INTO webmail_rules(account_id, name, kind, enabled, position, match, conditions, actions, stop, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (acc_id, r["name"], kind, 1 if r["enabled"] else 0, pos, r["match"],
             json.dumps(r["conditions"], ensure_ascii=False), json.dumps(r["actions"], ensure_ascii=False),
             1 if r["stop"] else 0, actor, utcnow_iso()))
        rid = int(cur.lastrowid)
    if actor:
        svc.db.add_audit(actor, "webmail_rule", f"ящик «{account.name}»: правило «{r['name']}»")
    return _rule_dict(svc.db.query_one("SELECT * FROM webmail_rules WHERE id=?", (rid,)))


def delete_rule(svc, account: Account, rule_id: int, *, actor: str = "") -> None:
    row = svc.db.query_one("SELECT name FROM webmail_rules WHERE id=? AND account_id=?", (int(rule_id), int(account.id)))
    if row is None:
        raise ValidationError("Правило не найдено.", code="not_found")
    svc.db.execute("DELETE FROM webmail_rules WHERE id=?", (int(rule_id),))
    if actor:
        svc.db.add_audit(actor, "webmail_rule", f"ящик «{account.name}»: удалено правило «{row['name']}»")


def reorder_rules(svc, account_id: int, ids: Sequence[int]) -> None:
    for pos, rid in enumerate(ids):
        svc.db.execute("UPDATE webmail_rules SET position=? WHERE id=? AND account_id=?", (pos, int(rid), int(account_id)))


def block_sender(svc, account: Account, address: str, *, ids: Sequence[int] = (), actor: str = "") -> Dict:
    """«Заблокировать отправителя»: его письма — сразу в «Спам» (нет «Спама» — в корзину), и уже пришедшие тоже."""
    addr = parseaddr(address or "")[1].strip().lower() or str(address or "").strip().lower()
    if not _EMAIL_RE.fullmatch(addr):
        raise ValidationError(f"«{address}» — не адрес электронной почты.")
    if addr in wm.own_addresses(svc, account):
        raise ValidationError("Свой адрес заблокировать нельзя.")
    special = wm.special_folders(svc, account)
    action = "junk" if special.get("junk") else "delete"
    exists = [r for r in list_rules(svc, int(account.id)) if r["kind"] == "block"
              and any(c["field"] == "from" and c["value"].lower() == addr for c in r["conditions"])]
    if not exists:
        save_rule(svc, account, {"name": f"Заблокирован: {addr}", "conditions": [{"field": "from", "op": "equals",
                                                                               "value": addr}],
                                 "actions": [{"type": action}], "stop": True}, actor=actor, kind="block")
    moved = 0
    if ids:
        res = wm.junk(svc, account, ids, spam=True) if action == "junk" else wm.delete(svc, account, ids)
        moved = int(res.get("moved") or res.get("trashed") or 0)
    return {"address": addr, "moved": moved, "action": action}


class _Msg:
    """Письмо для проверки условий правил: заголовки и (по требованию) текст."""

    def __init__(self, svc, account: Account, row) -> None:
        self.svc, self.account, self.row = svc, account, row
        self._hdrs = None
        self._body: Optional[str] = None

    @property
    def hdrs(self):
        if self._hdrs is None:
            try:
                self._hdrs = headers_of(self.svc, self.account, self.row)
            except Exception:  # noqa: BLE001
                self._hdrs = BytesHeaderParser(policy=policy.default).parsebytes(b"\r\n")
        return self._hdrs

    def header(self, name: str) -> str:
        return _h(self.hdrs, name)

    def body(self) -> str:
        if self._body is None:
            try:
                from .mailview import summarize_source
                stored = self.row["stored_path"]
                size = self.svc.store.message_size(int(self.account.id), stored)
                parsed = summarize_source(lambda: self.svc.store.open_message(int(self.account.id), stored), size)
                self._body = wm._plain_of(parsed)[:2_000_000]  # noqa: SLF001
            except Exception:  # noqa: BLE001
                self._body = ""
        return self._body

    def high_priority(self) -> bool:
        xp = self.header("X-Priority").strip()
        return xp[:1] in ("1", "2") or self.header("Importance").strip().lower() == "high" or \
            self.header("Priority").strip().lower() == "urgent"


def _text_match(op: str, haystack: str, value: str, addrs: Optional[List[str]] = None) -> bool:
    h, v = (haystack or "").casefold(), value.casefold()
    if op == "contains":
        return v in h
    if op == "not_contains":
        return v not in h
    if op in ("equals", "not_equals"):
        eq = (v in addrs) if addrs is not None and "@" in v else (h.strip() == v.strip())
        return eq if op == "equals" else not eq
    if op == "starts":
        return any(x.startswith(v) for x in (addrs or [])) or h.startswith(v)
    if op == "ends":
        return any(x.endswith(v) for x in (addrs or [])) or h.endswith(v)
    return False


def _cond_ok(m: _Msg, c: Dict) -> bool:
    field, op, value = c["field"], c["op"], c["value"]
    if field == "attach":
        return bool(m.row["has_attach"]) == (op == "yes")
    if field == "size":
        mb = float(value or 0) * 1024 * 1024
        size = int(m.row["size"] or 0)
        return size > mb if op == "gt" else size < mb
    if field == "list":
        is_list = bool(m.header("List-Id") or m.header("List-Unsubscribe")) or \
            m.header("Precedence").strip().lower() in ("bulk", "list")
        return is_list == (op == "yes")
    if field == "priority":
        return m.high_priority() == (op == "yes")
    if field == "subject":
        return _text_match(op, m.row["subject"] or m.header("Subject"), value)
    if field == "body":
        return _text_match(op, m.body(), value)
    if field == "from":
        text = m.header("From") or (m.row["from_addr"] or "")
        return _text_match(op, text, value, _addr_list(text) or [(m.row["from_addr"] or "").lower()])
    names = {"to": ("To",), "cc": ("Cc",), "to_cc": ("To", "Cc")}[field]
    text = ", ".join(m.header(n) for n in names)
    return _text_match(op, text, value, _addr_list(text))


def rule_matches(m: _Msg, rule: Dict) -> bool:
    results = (_cond_ok(m, c) for c in rule["conditions"])
    return all(results) if rule["match"] == "all" else any(results)


def _plan(svc, account: Account, rows, rules: List[Dict]) -> Tuple[Dict, Dict, Dict[int, int], set]:
    """Что сделать с письмами: флаги → письма, папка → письма, сколько писем совпало с каждым правилом."""
    flag_plan: Dict[frozenset, List[int]] = {}
    move_plan: Dict[str, List[int]] = {}
    hits: Dict[int, int] = {}
    removed: set = set()
    special = None
    for row in rows:
        m = _Msg(svc, account, row)
        add: List[str] = []
        dest: Optional[str] = None
        for rule in rules:
            try:
                ok = rule_matches(m, rule)
            except Exception as exc:  # noqa: BLE001
                log.warning("Правило «%s»: письмо %s не проверено: %s", rule["name"], row["id"], exc)
                ok = False
            if not ok:
                continue
            hits[rule["id"]] = hits.get(rule["id"], 0) + 1
            for a in rule["actions"]:
                t = a["type"]
                if t == "read":
                    add.append("\\Seen")
                elif t in ("flag", "important"):
                    add.append("\\Flagged")
                elif t == "tag":
                    add.append(a["keyword"])
                elif t == "move":
                    dest = a["folder"]
                elif t in ("junk", "delete"):
                    if special is None:
                        special = wm.special_folders(svc, account)
                    target = (special.get("junk") if t == "junk" else None) or special.get("trash")
                    dest = target or dest
            if rule["stop"]:
                break
        add = [f for f in dict.fromkeys(add) if f not in wm._flags_of(row)]  # noqa: SLF001
        if add:
            flag_plan.setdefault(frozenset(add), []).append(int(row["id"]))
        if dest and dest != row["folder"]:
            move_plan.setdefault(dest, []).append(int(row["id"]))
            removed.add(int(row["id"]))
    return flag_plan, move_plan, hits, removed


def _apply_plan(svc, account: Account, flag_plan: Dict, move_plan: Dict, hits: Dict[int, int]) -> Dict:
    flagged = moved = 0
    errors: List[str] = []
    for flags, ids in flag_plan.items():
        try:
            flagged += int(wm.set_flags(svc, account, ids, add=sorted(flags)).get("changed") or 0)
        except (MailArchiverError, ValidationError) as exc:
            errors.append(exc.message)
    for dest, ids in move_plan.items():
        try:
            moved += int(wm.move(svc, account, ids, dest).get("moved") or 0)
        except (MailArchiverError, ValidationError) as exc:
            errors.append(exc.message)
    now = utcnow_iso()
    for rid, n in hits.items():
        svc.db.execute("UPDATE webmail_rules SET hits=hits+?, last_hit_at=? WHERE id=?", (int(n), now, int(rid)))
    return {"matched": sum(hits.values()), "flagged": flagged, "moved": moved, "errors": errors[:5]}


def apply_rules_to_folder(svc, account: Account, folder: str, *, rule_ids: Optional[Sequence[int]] = None,
                          limit: int = 2000) -> Dict:
    """«Применить к письмам папки»: правила — к уже лежащим в папке письмам (последним ``limit``)."""
    rules = [r for r in list_rules(svc, int(account.id)) if r["enabled"]
             and (rule_ids is None or r["id"] in {int(i) for i in rule_ids})]
    if not rules:
        raise ValidationError("Нет включённых правил.")
    rows = svc.db.query("SELECT * FROM messages WHERE account_id=? AND folder=? AND gone=0 "
                        "ORDER BY internaldate DESC, id DESC LIMIT ?", (int(account.id), folder, int(limit)))
    flag_plan, move_plan, hits, _removed = _plan(svc, account, rows, rules)
    res = _apply_plan(svc, account, flag_plan, move_plan, hits)
    res["checked"] = len(rows)
    return res


# ===========================================================================
#  Автоответ («меня нет на месте»)
# ===========================================================================
def get_autoreply(svc, account_id: int) -> Dict:
    row = svc.db.query_one("SELECT * FROM webmail_autoreply WHERE account_id=?", (int(account_id),))
    d = dict(row) if row is not None else {"account_id": int(account_id), "enabled": 0, "date_from": "", "date_to": "",
                                           "subject": "", "body": "", "days": 4, "updated_by": "", "updated_at": None}
    d["enabled"] = bool(d["enabled"])
    d["active"] = autoreply_active(d)
    return d


def autoreply_active(d: Dict, today: Optional[str] = None) -> bool:
    if not d.get("enabled"):
        return False
    today = today or datetime.now().strftime("%Y-%m-%d")
    if d.get("date_from") and today < d["date_from"]:
        return False
    if d.get("date_to") and today > d["date_to"]:
        return False
    return True


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def save_autoreply(svc, account: Account, data: Dict, *, actor: str = "") -> Dict:
    enabled = bool(data.get("enabled"))
    date_from, date_to = str(data.get("date_from") or ""), str(data.get("date_to") or "")
    for v in (date_from, date_to):
        if v and not _DATE_RE.fullmatch(v):
            raise ValidationError("Даты автоответа — в виде ГГГГ-ММ-ДД.")
    if date_from and date_to and date_to < date_from:
        raise ValidationError("Дата окончания автоответа раньше даты начала.")
    body = wm.sanitize_html(str(data.get("body") or ""))
    if enabled and not wm.html_to_text(body).strip():
        raise ValidationError("Напишите текст автоответа.")
    if len(body) > 300_000:
        raise ValidationError("Текст автоответа слишком большой.")
    try:
        days = max(1, min(30, int(data.get("days") or 4)))
    except (TypeError, ValueError):
        days = 4
    subject = _clip(data.get("subject"), 300)
    if enabled:
        _init_watch(svc, int(account.id))
    svc.db.execute(
        "INSERT INTO webmail_autoreply(account_id, enabled, date_from, date_to, subject, body, days, updated_by, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled, "
        "date_from=excluded.date_from, date_to=excluded.date_to, subject=excluded.subject, body=excluded.body, "
        "days=excluded.days, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (int(account.id), 1 if enabled else 0, date_from, date_to, subject, body, days, actor, utcnow_iso()))
    if actor:
        svc.db.add_audit(actor, "webmail_autoreply",
                         f"ящик «{account.name}»: автоответ {'включён' if enabled else 'выключен'}")
    return get_autoreply(svc, int(account.id))


_ROBOT_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?|notifications?|"
                          r"daemon|robot|auto|news(letter)?|info-?noreply)([+.\-_].*)?$", re.I)


def _autoreply_skip_reason(m: _Msg, own: set) -> str:
    sender = parseaddr(m.header("Reply-To") or m.header("From"))[1].strip().lower()
    if not sender or "@" not in sender:
        return "нет адреса отправителя"
    if sender in own:
        return "своё письмо"
    if _ROBOT_LOCAL.match(sender.split("@", 1)[0]):
        return "адрес робота"
    auto = m.header("Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return "автоматическое письмо"
    if m.header("Precedence").strip().lower() in ("bulk", "list", "junk"):
        return "рассылка"
    if m.header("List-Id") or m.header("List-Unsubscribe"):
        return "рассылка"
    if re.search(r"\b(all|oof|autoreply)\b", m.header("X-Auto-Response-Suppress"), re.I):
        return "отправитель просит не отвечать автоматически"
    if m.header("Return-Path").strip() == "<>":
        return "уведомление сервера"
    rcpts = set(_addr_list(m.header("To")) + _addr_list(m.header("Cc")))
    if own and not (rcpts & own):
        return "ящик не в «Кому» и не в «Копии»"
    return ""


def _send_autoreply(svc, account: Account, m: _Msg, ar: Dict, ident: Dict) -> str:
    sender = parseaddr(m.header("Reply-To") or m.header("From"))[1].strip()
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = wm.display_addr(ident["display_name"], ident["address"])
    msg["To"] = sender
    orig_subject = m.header("Subject")
    msg["Subject"] = ar["subject"] or ("Автоответ: " + orig_subject if orig_subject else "Автоответ")
    msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
    domain = ident["address"].split("@", 1)[1] if "@" in ident["address"] else None
    msg["Message-ID"] = make_msgid(domain=domain)
    mid = m.header("Message-ID").strip()
    if mid:
        msg["In-Reply-To"] = mid
        msg["References"] = " ".join((m.header("References").split() + [mid])[-20:])
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg["Precedence"] = "auto_reply"
    body = ar["body"] or ""
    msg.set_content(wm.html_to_text(body) or "\n")
    msg.add_alternative(f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{body}</body></html>',
                        subtype="html")
    wm._smtp_send(svc, account, ident, ident["address"], [sender], msg.as_bytes())  # noqa: SLF001
    return sender.lower()


# ===========================================================================
#  Новые письма во «Входящих»: правила и автоответ
# ===========================================================================
_ACCOUNT_LOCKS: Dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _account_lock(account_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(int(account_id), threading.Lock())


def _inbox(svc, account: Account) -> str:
    try:
        return wm.special_folders(svc, account).get("inbox") or "INBOX"
    except (MailArchiverError, ValidationError):
        return "INBOX"


def has_automation(svc, account_id: int) -> bool:
    from .webmail_more import sieve_active
    if sieve_active(svc, account_id):
        return False            # правила и автоответ выполняет почтовый сервер (Sieve)
    if svc.db.query_one("SELECT 1 FROM webmail_rules WHERE account_id=? AND enabled=1 LIMIT 1",
                        (int(account_id),)) is not None:
        return True
    return autoreply_active(get_autoreply(svc, account_id))


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def process_new(svc, account: Account, *, sync: bool = False) -> Dict:
    """Новые письма «Входящих» — через правила и автоответ. ``sync`` — сначала проверить «Входящие» на сервере."""
    acc_id = int(account.id)
    res = {"checked": 0, "matched": 0, "moved": 0, "flagged": 0, "autoreplied": 0, "errors": []}
    if not has_automation(svc, acc_id):
        return res
    lock = _account_lock(acc_id)
    if not lock.acquire(blocking=False):
        return res
    try:
        inbox = _inbox(svc, account)
        if sync and not wm.backup_running(svc, acc_id):
            wm.sync_folder(svc, account, inbox)
        _init_watch(svc, acc_id)
        last = int(svc.db.scalar("SELECT last_id FROM webmail_watch WHERE account_id=?", (acc_id,)) or 0)
        rows = svc.db.query("SELECT * FROM messages WHERE account_id=? AND folder=? AND id>? AND gone=0 "
                            "ORDER BY id LIMIT ?", (acc_id, inbox, last, RULES_BATCH))
        cutoff = datetime.now(timezone.utc) - timedelta(days=NEW_MAIL_DAYS)
        fresh = []
        now = utcnow_iso()
        for r in rows:
            dt = _parse_iso(r["internaldate"] or "")
            if dt is not None and dt < cutoff:
                continue
            # письмо «забирает» тот, чья запись появилась: правила может проверять и процесс заданий
            # (server.worker_process), и веб-процесс при проверке почты — второй его пропустит
            key = r["sha256"] or f"id:{int(r['id'])}"
            cur = svc.db.execute("INSERT OR IGNORE INTO webmail_rule_done(account_id, sha256, at) VALUES(?,?,?)",
                                 (acc_id, key, now))
            if int(cur.rowcount or 0):
                fresh.append(r)
        if rows:
            svc.db.execute("UPDATE webmail_watch SET last_id=MAX(last_id, ?) WHERE account_id=?",
                           (max(int(r["id"]) for r in rows), acc_id))
        res["checked"] = len(fresh)
        if not fresh:
            return res
        rules = [r for r in list_rules(svc, acc_id) if r["enabled"]]
        removed: set = set()
        if rules:
            flag_plan, move_plan, hits, removed = _plan(svc, account, fresh, rules)
            out = _apply_plan(svc, account, flag_plan, move_plan, hits)
            for k in ("matched", "moved", "flagged"):
                res[k] = out[k]
            res["errors"] += out["errors"]
        ar = get_autoreply(svc, acc_id)
        if ar["active"]:
            res["autoreplied"] = _autoreply_batch(svc, account, [r for r in fresh if int(r["id"]) not in removed], ar,
                                                  res["errors"])
        return res
    finally:
        svc.db.execute("UPDATE webmail_watch SET last_run_at=?, last_error=? WHERE account_id=?",
                       (utcnow_iso(), "; ".join(res["errors"])[:500], acc_id))
        lock.release()


def _autoreply_batch(svc, account: Account, rows, ar: Dict, errors: List[str]) -> int:
    if not rows:
        return 0
    problem = wm.send_problem(svc, account)
    if problem:
        errors.append(f"Автоответ не отправлен: {problem}")
        return 0
    ident = wm.get_identity(svc, account)
    own = wm.own_addresses(svc, account)
    since = (datetime.now(timezone.utc) - timedelta(days=int(ar["days"] or 4))).isoformat()
    sent = 0
    for row in rows[:50]:
        m = _Msg(svc, account, row)
        if _autoreply_skip_reason(m, own):
            continue
        addr = parseaddr(m.header("Reply-To") or m.header("From"))[1].strip().lower()
        prev = svc.db.query_one("SELECT sent_at FROM webmail_autoreply_log WHERE account_id=? AND addr=?",
                                (int(account.id), addr))
        if prev is not None and prev["sent_at"] >= since:
            continue
        try:
            _send_autoreply(svc, account, m, ar, ident)
        except (MailArchiverError, ValidationError) as exc:
            errors.append(f"Автоответ {addr}: {exc.message}")
            break
        svc.db.execute("INSERT INTO webmail_autoreply_log(account_id, addr, sent_at) VALUES(?,?,?) "
                       "ON CONFLICT(account_id, addr) DO UPDATE SET sent_at=excluded.sent_at",
                       (int(account.id), addr, utcnow_iso()))
        sent += 1
    return sent


def automation_status(svc, account_id: int) -> Dict:
    row = svc.db.query_one("SELECT last_run_at, last_error FROM webmail_watch WHERE account_id=?", (int(account_id),))
    return {"last_run_at": row["last_run_at"] if row is not None else None,
            "last_error": row["last_error"] if row is not None else "",
            "interval_min": int(svc.rt("webmail", "rules_interval_min") or 0)}


# ===========================================================================
#  «Напомнить позже»
# ===========================================================================
def _snooze_folder(svc, account: Account) -> str:
    rows = wm._folder_rows(svc, int(account.id))  # noqa: SLF001
    for r in rows:
        leaf = r["name"].split(r["delimiter"])[-1] if r["delimiter"] else r["name"]
        if r["selectable"] and leaf.lower() in (SNOOZE_FOLDER.lower(), "snoozed"):
            return r["name"]
    return wm.create_folder(svc, account, SNOOZE_FOLDER, "")["folder"]


def snooze(svc, account: Account, ids: Iterable[int], until: str, *, actor: str = "") -> Dict:
    when = _parse_iso(until)
    now = datetime.now(timezone.utc)
    if when is None:
        raise ValidationError("Не понятно, когда напомнить.")
    if when <= now + timedelta(seconds=30):
        raise ValidationError("Время напоминания уже прошло.")
    if when > now + timedelta(days=366):
        raise ValidationError("Напомнить можно не позже чем через год.")
    rows = wm._rows(svc, int(account.id), ids)  # noqa: SLF001
    if not rows:
        raise ValidationError("Письма не найдены.")
    folder = _snooze_folder(svc, account)
    by_id = {int(r["id"]): r for r in rows}
    res = wm.move(svc, account, list(by_id), folder, actor=actor, audit_action="webmail_snooze")
    new_pk = [pk for u in res["undo"] for pk in u["ids"]]
    new_rows = list(svc.db.messages_by_ids(int(account.id), new_pk))
    by_hash = {r["sha256"]: r for r in new_rows if r["sha256"]}
    by_mid = {(r["message_id"] or "").strip(): r for r in new_rows if (r["message_id"] or "").strip()}
    n = 0
    for r in rows:
        if r["folder"] == folder:
            continue
        mid = (r["message_id"] or "").strip()
        nr = by_hash.get(r["sha256"]) if r["sha256"] else None
        if nr is None and mid:
            nr = by_mid.get(mid)
        svc.db.execute("INSERT INTO webmail_snooze(account_id, message_pk, msgid, folder, return_folder, until, subject, "
                       "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (int(account.id), int(nr["id"]) if nr is not None else None, mid, folder, r["folder"],
                        when.astimezone(timezone.utc).isoformat(), (r["subject"] or "")[:300], actor, utcnow_iso()))
        n += 1
    return {"snoozed": res["moved"], "folder": folder, "until": when.isoformat(), "recorded": n}


def list_snoozed(svc, account_id: int) -> List[Dict]:
    return [dict(r) for r in svc.db.query("SELECT * FROM webmail_snooze WHERE account_id=? ORDER BY until",
                                          (int(account_id),))]


def _find_snoozed_pk(svc, account: Account, item) -> Optional[int]:
    if item["message_pk"]:
        row = svc.db.query_one("SELECT id FROM messages WHERE id=? AND account_id=? AND folder=? AND gone=0",
                               (int(item["message_pk"]), int(account.id), item["folder"]))
        if row is not None:
            return int(row["id"])
    if item["msgid"]:
        row = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND folder=? AND message_id=? AND gone=0 "
                               "ORDER BY id DESC LIMIT 1", (int(account.id), item["folder"], item["msgid"]))
        if row is not None:
            return int(row["id"])
    return None


def unsnooze(svc, account: Account, snooze_ids: Optional[Sequence[int]] = None, *, due_only: bool = False) -> Dict:
    """Вернуть отложенные письма во «Входящие» (туда, откуда отложили) непрочитанными."""
    cond, args = "account_id=?", [int(account.id)]
    if snooze_ids is not None:
        ids = [int(i) for i in snooze_ids] or [-1]
        cond += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    if due_only:
        cond += " AND until<=?"
        args.append(datetime.now(timezone.utc).isoformat())
    rows = svc.db.query(f"SELECT * FROM webmail_snooze WHERE {cond}", tuple(args))
    # запись забирает тот, кто её удалил (возвращать может и процесс заданий, и кнопка в клиенте)
    items = [it for it in rows
             if int(svc.db.execute("DELETE FROM webmail_snooze WHERE id=?", (int(it["id"]),)).rowcount or 0)]
    if not items:
        return {"returned": 0}
    synced = set()
    groups: Dict[str, List[Tuple[int, object]]] = {}
    for it in items:
        pk = _find_snoozed_pk(svc, account, it)
        if pk is None and it["folder"] not in synced:
            synced.add(it["folder"])
            try:
                wm.sync_folder(svc, account, it["folder"])
            except (MailArchiverError, ValidationError):
                pass
            pk = _find_snoozed_pk(svc, account, it)
        if pk is None:
            continue          # письмо из «Отложенных» уже убрали — напоминать не о чем
        groups.setdefault(it["return_folder"], []).append((pk, it))
    returned = 0
    for dest, pairs in groups.items():
        pks = [pk for pk, _it in pairs]
        try:
            dest = wm._require_server_folder(svc, account, dest)  # noqa: SLF001
        except ValidationError:
            dest = _inbox(svc, account)
        try:
            res = wm.move(svc, account, pks, dest)
        except BaseException:
            # не вернулось — запись обратно: попробуем в следующий раз
            for _pk, it in pairs:
                svc.db.execute("INSERT INTO webmail_snooze(id, account_id, message_pk, msgid, folder, return_folder, "
                               "until, subject, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               tuple(it[k] for k in ("id", "account_id", "message_pk", "msgid", "folder",
                                                     "return_folder", "until", "subject", "created_by",
                                                     "created_at")))
            raise
        returned += int(res["moved"])
        new_ids = [pk for u in res["undo"] for pk in u["ids"]]
        if new_ids:
            try:
                wm.set_flags(svc, account, new_ids, remove=["\\Seen"])
            except (MailArchiverError, ValidationError) as exc:
                log.info("Отложенные письма вернулись, но не отмечены непрочитанными: %s", exc)
    return {"returned": returned}


# ===========================================================================
#  Отложенная отправка
# ===========================================================================
def schedule_send(svc, account: Account, draft_id: int, owner: str, send_at: str, *, actor: str = "",
                  role: str = "") -> Dict:
    row = wm.get_draft_row(svc, draft_id, owner)
    if int(row["account_id"]) != int(account.id):
        raise ValidationError("Черновик относится к другому ящику.")
    if row["status"] != "draft":
        raise ValidationError("Это письмо уже отправляется или запланировано.")
    when = _parse_iso(send_at)
    now = datetime.now(timezone.utc)
    if when is None:
        raise ValidationError("Не понятно, когда отправить письмо.")
    if when <= now + timedelta(seconds=30):
        raise ValidationError("Время отправки уже прошло — отправьте письмо сейчас.")
    if when > now + timedelta(days=366):
        raise ValidationError("Запланировать отправку можно не дальше чем на год.")
    problem = wm.send_problem(svc, account)
    if problem:
        raise ValidationError(problem)
    ident = wm.get_identity(svc, account)
    wm.build_message(svc, account, row, ident)       # проверить адресатов и вложения сейчас, а не в момент отправки
    svc.db.execute("UPDATE webmail_drafts SET status='scheduled', send_at=?, actor=?, error='', updated_at=? "
                   "WHERE id=? AND status='draft'",
                   (when.astimezone(timezone.utc).isoformat(), f"{role}:{actor}", utcnow_iso(), int(draft_id)))
    if actor:
        svc.db.add_audit(actor, "webmail_schedule", f"ящик «{account.name}»: «{_clip(row['subject'], 120)}» — "
                                                    f"отправка {when.astimezone().strftime('%d.%m.%Y %H:%M')}")
    return wm._draft_dict(wm.get_draft_row(svc, draft_id, owner))  # noqa: SLF001


def cancel_schedule(svc, draft_id: int, owner: str) -> Dict:
    row = wm.get_draft_row(svc, draft_id, owner)
    if row["status"] != "scheduled":
        raise ValidationError("Отправка этого письма не запланирована.")
    svc.db.execute("UPDATE webmail_drafts SET status='draft', send_at=NULL, updated_at=? WHERE id=? AND status='scheduled'",
                   (utcnow_iso(), int(draft_id)))
    return wm._draft_dict(wm.get_draft_row(svc, draft_id, owner))  # noqa: SLF001


def _bg_problem(svc, account, role: str) -> str:
    """Можно ли сейчас действовать в ящике без пользователя (запланированная отправка)."""
    if not bool(svc.rt("webmail", "enabled")):
        return "почтовый клиент выключен в настройках"
    if account is None:
        return "ящик удалён"
    if not account.enabled:
        return f"ящик «{account.name}» выключен"
    if role == "admin" and not bool(svc.rt("webmail", "admin_actions")):
        return "администратору писать из ящиков сотрудников запрещено настройкой"
    return ""


def send_due(svc) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = svc.db.query("SELECT * FROM webmail_drafts WHERE status='scheduled' AND send_at<=? ORDER BY send_at LIMIT 50",
                        (now,))
    sent = 0
    for row in rows:
        acc = svc.db.get_account(int(row["account_id"]))
        role, _sep, actor = (row["actor"] or "").partition(":")
        problem = _bg_problem(svc, acc, role)
        if problem:
            svc.db.execute("UPDATE webmail_drafts SET status='draft', send_at=NULL, error=?, updated_at=? WHERE id=?",
                           (f"Запланированная отправка не состоялась: {problem}.", utcnow_iso(), int(row["id"])))
            continue
        try:
            wm.send(svc, acc, int(row["id"]), row["owner"], actor=actor or "", confirmed=True)
            sent += 1
        except (MailArchiverError, ValidationError) as exc:
            log.warning("Запланированное письмо #%s не отправлено: %s", row["id"], exc.message)
        except Exception:  # noqa: BLE001
            log.exception("Запланированное письмо #%s не отправлено", row["id"])
    return sent


# ===========================================================================
#  Архивы .zip: выбранные письма, все вложения письма
# ===========================================================================
def _tmp_dir(svc) -> str:
    return ensure_dir(os.path.join(svc.cfg.data_dir, "webmail", "tmp"), 0o700)


def _zip_name(name: str, used: set) -> str:
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "").strip()[:120] or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    candidate, i = base, 1
    while candidate.lower() in used:
        i += 1
        candidate = f"{stem} ({i}).{ext}" if ext else f"{base} ({i})"
    used.add(candidate.lower())
    return candidate


def zip_messages(svc, account: Account, ids: Sequence[int]):
    """Выбранные письма — файлами .eml в архиве .zip. → открытый временный файл (удаляется при закрытии)."""
    ids = list(dict.fromkeys(int(i) for i in ids))[:ZIP_MAX_MESSAGES]
    rows = list(svc.db.messages_by_ids(int(account.id), ids))
    if not rows:
        raise ValidationError("Письма не найдены.")
    if sum(int(r["size"] or 0) for r in rows) > ZIP_MAX_BYTES:
        raise ValidationError("Выбрано больше 2 ГБ писем — скачайте их частями или выгрузкой ящика.")
    fh = tempfile.TemporaryFile(dir=_tmp_dir(svc))
    used: set = set()
    with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for r in sorted(rows, key=lambda x: (x["internaldate"] or "", x["id"])):
            date = (r["internaldate"] or "")[:10]
            name = _zip_name(f"{date} {r['subject'] or 'без темы'}.eml", used)
            with svc.store.open_message(int(account.id), r["stored_path"]) as src, zf.open(name, "w") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
    fh.seek(0)
    return fh


def zip_attachments(svc, account: Account, row):
    """Все вложения письма (кроме картинок в тексте) — архивом .zip."""
    from .mailview import attachment_from_source, summarize_source
    stored = row["stored_path"]
    size = svc.store.message_size(int(account.id), stored)
    opener = (lambda: svc.store.open_message(int(account.id), stored))
    parsed = summarize_source(opener, size)
    atts = [a for a in parsed.get("attachments") or [] if not a.get("inline")]
    if not atts:
        raise ValidationError("В письме нет вложений.")
    fh = tempfile.TemporaryFile(dir=_tmp_dir(svc))
    used: set = set()
    with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for a in atts:
            att = attachment_from_source(opener, size, int(a["index"]))
            if att is None:
                continue
            name, _ctype, chunks, _length = att
            with zf.open(_zip_name(name or a["filename"], used), "w") as dst:
                for chunk in chunks:
                    dst.write(chunk)
    fh.seek(0)
    subject = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", row["subject"] or "").strip()[:80] or "Вложения"
    return fh, f"{subject}.zip"


# ===========================================================================
#  Сведения о письме: отписка, важность, уведомление о прочтении, приглашение
# ===========================================================================
def _unsubscribe(hdrs) -> Dict:
    raw = _h(hdrs, "List-Unsubscribe")
    links = re.findall(r"<([^>]+)>", raw)
    mailto = next((u for u in links if u.lower().startswith("mailto:")), "")
    https = next((u for u in links if u.lower().startswith("https://")), "")
    one_click = "one-click" in _h(hdrs, "List-Unsubscribe-Post").lower() and bool(https)
    if not (mailto or https):
        return {}
    return {"mailto": mailto, "url": https, "one_click": one_click}


def _priority(hdrs) -> str:
    xp = _h(hdrs, "X-Priority").strip()[:1]
    imp = _h(hdrs, "Importance").strip().lower()
    if xp in ("1", "2") or imp == "high" or _h(hdrs, "Priority").strip().lower() == "urgent":
        return "high"
    if xp in ("4", "5") or imp == "low" or _h(hdrs, "Priority").strip().lower() == "non-urgent":
        return "low"
    return ""


def _ical_unfold(text: str) -> List[Tuple[str, Dict[str, str], str]]:
    text = re.sub(r"\r?\n[ \t]", "", text)
    out = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        name, *params = key.split(";")
        p = {}
        for item in params:
            k, _s, v = item.partition("=")
            p[k.upper()] = v.strip('"')
        out.append((name.upper(), p, value))
    return out


def _ical_text(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ical_dt(value: str, params: Dict[str, str]) -> str:
    """Время события по-человечески (как в приглашении; со «Z» — в местном времени сервера)."""
    v = value.strip()
    try:
        if len(v) == 8:
            return datetime.strptime(v, "%Y%m%d").strftime("%d.%m.%Y")
        if v.endswith("Z"):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone()
            return dt.strftime("%d.%m.%Y %H:%M")
        dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tz = params.get("TZID", "")
        return dt.strftime("%d.%m.%Y %H:%M") + (f" ({tz})" if tz else "")
    except ValueError:
        return v


def parse_invite(ical: str) -> Dict:
    lines = _ical_unfold(ical)
    method = next((v for n, _p, v in lines if n == "METHOD"), "").upper()
    ev: Dict = {"method": method, "attendees": []}
    inside = False
    raw_props: Dict[str, Tuple[Dict, str]] = {}
    for name, params, value in lines:
        if name == "BEGIN" and value.upper() == "VEVENT":
            inside = True
            continue
        if name == "END" and value.upper() == "VEVENT":
            break
        if not inside:
            continue
        if name == "ATTENDEE":
            ev["attendees"].append({"email": re.sub(r"(?i)^mailto:", "", value), "name": params.get("CN", ""),
                                    "partstat": params.get("PARTSTAT", "")})
        elif name not in raw_props:
            raw_props[name] = (params, value)
    if not raw_props:
        return {}
    g = (lambda n: raw_props.get(n, ({}, ""))[1])
    ev.update({
        "uid": g("UID"), "sequence": g("SEQUENCE") or "0", "summary": _ical_text(g("SUMMARY")),
        "location": _ical_text(g("LOCATION")), "description": _ical_text(g("DESCRIPTION"))[:2000],
        "start": _ical_dt(g("DTSTART"), raw_props.get("DTSTART", ({}, ""))[0]) if g("DTSTART") else "",
        "end": _ical_dt(g("DTEND"), raw_props.get("DTEND", ({}, ""))[0]) if g("DTEND") else "",
        "organizer": re.sub(r"(?i)^mailto:", "", g("ORGANIZER")),
        "organizer_name": raw_props.get("ORGANIZER", ({}, ""))[0].get("CN", ""),
        "raw_dtstart": g("DTSTART"), "raw_dtstart_tz": raw_props.get("DTSTART", ({}, ""))[0].get("TZID", ""),
        "status": g("STATUS"),
    })
    ev["attendees"] = ev["attendees"][:50]
    return ev


def _invite_of(svc, account: Account, row) -> Tuple[Dict, str]:
    """Приглашение из письма (часть text/calendar или вложение .ics) → (сведения, текст iCalendar)."""
    size = svc.store.message_size(int(account.id), row["stored_path"])
    if size > 25 * 1024 * 1024:
        return {}, ""
    raw = svc.store.read_message(int(account.id), row["stored_path"])
    if b"text/calendar" not in raw and b".ics" not in raw.lower():
        return {}, ""
    msg = message_from_bytes(raw, policy=policy.default)
    for part in msg.walk():
        ctype = part.get_content_type()
        name = (part.get_filename() or "").lower()
        if ctype == "text/calendar" or (ctype == "application/ics") or name.endswith(".ics"):
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            info = parse_invite(text)
            if info:
                return info, text
    return {}, ""


def message_extras(svc, account: Account, row, *, can_act: bool) -> Dict:
    hdrs = headers_of(svc, account, row)
    flags = wm._flags_of(row)  # noqa: SLF001
    out: Dict = {"priority": _priority(hdrs), "unsubscribe": _unsubscribe(hdrs)}
    dnt = _h(hdrs, "Disposition-Notification-To") or _h(hdrs, "Return-Receipt-To")
    if dnt and "$MDNSent" not in flags and can_act:
        to = parseaddr(dnt)[1]
        own = wm.own_addresses(svc, account)
        sender = parseaddr(_h(hdrs, "From"))[1].lower()
        if to and sender not in own and row["folder"] != wm.special_folders(svc, account).get("sent"):
            out["receipt"] = {"to": to}
    try:
        invite, _text = _invite_of(svc, account, row)
    except Exception as exc:  # noqa: BLE001
        log.info("Приглашение в письме %s не разобрано: %s", row["id"], exc)
        invite = {}
    if invite:
        own = wm.own_addresses(svc, account)
        me = next((a for a in invite["attendees"] if a["email"].lower() in own), None)
        invite["my_status"] = me["partstat"] if me else ""
        invite["can_reply"] = bool(invite.get("method") == "REQUEST" and invite.get("organizer") and can_act
                                   and invite["organizer"].lower() not in own)
        out["invite"] = {k: v for k, v in invite.items() if not k.startswith("raw_")}
    from_addr = parseaddr(_h(hdrs, "From"))[1].lower()
    if from_addr:
        out["sender"] = from_addr
        out["in_book"] = svc.db.query_one("SELECT 1 FROM webmail_book WHERE account_id=? AND lower(email)=?",
                                          (int(account.id), from_addr)) is not None
        out["blocked"] = any(r["kind"] == "block" and any(c["value"].lower() == from_addr for c in r["conditions"])
                             for r in list_rules(svc, int(account.id)))
    return out


def _send_simple(svc, account: Account, msg, rcpts: List[str]) -> None:
    problem = wm.send_problem(svc, account)
    if problem:
        raise ValidationError(problem)
    ident = wm.get_identity(svc, account)
    wm._smtp_send(svc, account, ident, ident["address"], rcpts, msg.as_bytes())  # noqa: SLF001


def send_receipt(svc, account: Account, row, *, send: bool = True, actor: str = "") -> Dict:
    """Уведомление о прочтении (MDN, RFC 8098) — или «не отправлять»; в обоих случаях флаг $MDNSent."""
    hdrs = headers_of(svc, account, row)
    to = parseaddr(_h(hdrs, "Disposition-Notification-To") or _h(hdrs, "Return-Receipt-To"))[1]
    if send:
        if not to:
            raise ValidationError("Отправитель не просил уведомления о прочтении.")
        ident = wm.get_identity(svc, account)
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        report = MIMEMultipart("report", report_type="disposition-notification")
        report["From"] = wm.display_addr(ident["display_name"], ident["address"])
        report["To"] = to
        report["Subject"] = "Прочитано: " + (_h(hdrs, "Subject") or "")
        report["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
        domain = ident["address"].split("@", 1)[1] if "@" in ident["address"] else None
        report["Message-ID"] = make_msgid(domain=domain)
        report["Auto-Submitted"] = "auto-replied"
        orig_mid = _h(hdrs, "Message-ID").strip()
        if orig_mid:
            report["In-Reply-To"] = orig_mid
            report["References"] = orig_mid
        when = datetime.now().strftime("%d.%m.%Y %H:%M")
        report.attach(MIMEText(f"Ваше письмо «{_h(hdrs, 'Subject')}» прочитано {when}.\n", "plain", "utf-8"))
        fields = MIMEBase("message", "disposition-notification")
        lines = ["Reporting-UA: MailArchiver", f"Final-Recipient: rfc822;{ident['address']}"]
        if orig_mid:
            lines.append(f"Original-Message-ID: {orig_mid}")
        lines.append("Disposition: manual-action/MDN-sent-manually; displayed")
        fields.set_payload("\r\n".join(lines) + "\r\n")
        report.attach(fields)
        _send_simple(svc, account, report, [to])
        if actor:
            svc.db.add_audit(actor, "webmail_mdn", f"ящик «{account.name}»: уведомление о прочтении → {to}")
    try:
        wm.set_flags(svc, account, [int(row["id"])], add=["$MDNSent"])
    except (MailArchiverError, ValidationError) as exc:
        log.info("Флаг $MDNSent не поставлен: %s", exc)
    return {"ok": True, "sent": bool(send), "to": to}


PARTSTATS = {"ACCEPTED": "Принято", "TENTATIVE": "Под вопросом", "DECLINED": "Отклонено"}


def invite_reply(svc, account: Account, row, partstat: str, *, comment: str = "", actor: str = "") -> Dict:
    """Ответ на приглашение (iTIP REPLY): «Принять», «Под вопросом», «Отклонить» — письмом организатору."""
    partstat = str(partstat or "").upper()
    if partstat not in PARTSTATS:
        raise ValidationError("Ответ на приглашение: ACCEPTED, TENTATIVE или DECLINED.")
    info, _text = _invite_of(svc, account, row)
    if not info or not info.get("organizer") or not info.get("uid"):
        raise ValidationError("В письме нет приглашения, на которое можно ответить.")
    ident = wm.get_identity(svc, account)
    me = ident["address"]
    own = wm.own_addresses(svc, account)
    att = next((a for a in info["attendees"] if a["email"].lower() in own), None)
    if att:
        me = att["email"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def esc(v: str) -> str:
        return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    dtstart = info.get("raw_dtstart") or ""
    tz = info.get("raw_dtstart_tz") or ""
    lines = ["BEGIN:VCALENDAR", "PRODID:-//MailArchiver//RU", "VERSION:2.0", "METHOD:REPLY", "BEGIN:VEVENT",
             f"UID:{info['uid']}", f"SEQUENCE:{info.get('sequence') or 0}", f"DTSTAMP:{stamp}",
             f"ORGANIZER:mailto:{info['organizer']}",
             f"ATTENDEE;PARTSTAT={partstat};CN={esc(ident['display_name'])}:mailto:{me}",
             f"SUMMARY:{esc(info.get('summary') or '')}"]
    if dtstart:
        lines.append(f"DTSTART;TZID={tz}:{dtstart}" if tz else f"DTSTART:{dtstart}")
    if comment:
        lines.append(f"COMMENT:{esc(comment[:1000])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    ical = "\r\n".join(lines) + "\r\n"
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = wm.display_addr(ident["display_name"], me)
    msg["To"] = info["organizer"]
    msg["Subject"] = f"{PARTSTATS[partstat]}: {info.get('summary') or ''}".strip()
    msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
    domain = me.split("@", 1)[1] if "@" in me else None
    msg["Message-ID"] = make_msgid(domain=domain)
    text = f"{ident['display_name']}: {PARTSTATS[partstat].lower()} — «{info.get('summary') or ''}»."
    if comment:
        text += f"\n\n{comment}"
    msg.set_content(text + "\n")
    msg.add_alternative(ical, subtype="calendar", params={"method": "REPLY"})
    _send_simple(svc, account, msg, [info["organizer"]])
    if actor:
        svc.db.add_audit(actor, "webmail_invite", f"ящик «{account.name}»: {PARTSTATS[partstat].lower()} — "
                                                  f"«{_clip(info.get('summary'), 100)}»")
    try:
        wm.set_flags(svc, account, [int(row["id"])], add=["\\Answered"])
    except (MailArchiverError, ValidationError):
        pass
    return {"ok": True, "partstat": partstat, "to": info["organizer"]}


def invite_ics(svc, account: Account, row) -> bytes:
    _info, text = _invite_of(svc, account, row)
    if not text:
        raise ValidationError("В письме нет приглашения.")
    return text.encode("utf-8")


# ===========================================================================
#  Раз в минуту из планировщика
# ===========================================================================
_LAST_RULES: Dict[int, float] = {}


def tick(svc) -> Dict:
    """Запланированная отправка, возврат отложенных писем, правила и автоответ (если пора)."""
    out = {"sent": 0, "unsnoozed": 0, "rules": 0}
    try:
        out["sent"] = send_due(svc)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка запланированной отправки")
    if not bool(svc.rt("webmail", "enabled")):
        return out
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in svc.db.query("SELECT DISTINCT account_id FROM webmail_snooze WHERE until<=?", (now_iso,)):
        acc = svc.db.get_account(int(r["account_id"]))
        if acc is None or not acc.enabled or acc.login_status == "auth_error":
            continue
        try:
            out["unsnoozed"] += unsnooze(svc, acc, due_only=True)["returned"]
        except (MailArchiverError, ValidationError) as exc:
            log.warning("Отложенные письма ящика «%s» не вернулись: %s", acc.name, exc.message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка возврата отложенных писем")
    interval = int(svc.rt("webmail", "rules_interval_min") or 0)
    if interval <= 0:
        return out
    ids = {int(r["account_id"]) for r in svc.db.query("SELECT DISTINCT account_id FROM webmail_rules WHERE enabled=1")}
    ids |= {int(r["account_id"]) for r in svc.db.query("SELECT account_id FROM webmail_autoreply WHERE enabled=1")}
    mono = time.monotonic()
    for acc_id in sorted(ids):
        if mono - _LAST_RULES.get(acc_id, -1e9) < interval * 60 - 5:
            continue
        acc = svc.db.get_account(acc_id)
        if acc is None or not acc.enabled or acc.login_status == "auth_error":
            continue
        if acc.secret_broken:
            continue
        _LAST_RULES[acc_id] = mono
        try:
            res = process_new(svc, acc, sync=True)
            out["rules"] += int(res.get("matched") or 0)
        except (MailArchiverError, ValidationError) as exc:
            svc.db.execute("UPDATE webmail_watch SET last_run_at=?, last_error=? WHERE account_id=?",
                           (utcnow_iso(), exc.message[:500], acc_id))
        except Exception:  # noqa: BLE001
            log.exception("Ошибка правил ящика #%s", acc_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    svc.db.execute("DELETE FROM webmail_rule_done WHERE at<?", (cutoff,))
    return out
