"""
Почтовый клиент раздела «Почта» (1.17.0): работа с ящиком на почтовом сервере прямо из архива.

Раздел по-прежнему показывает письма **из архива** (индекс ``messages`` и файлы хранилища) — так письмо
открывается мгновенно и не зависит от того, доступен ли сервер. Клиент добавляет к этому действия на
сервере и обратный путь в архив:

* **папки сервера** — LIST и STATUS (сколько писем и непрочитанных), особые папки (Отправленные,
  Черновики, Корзина, Спам, Архив) — по отметкам SPECIAL-USE или привычным именам; кэш —
  ``webmail_folders``;
* **проверка почты** — :func:`sync_folder`: новые письма открытой папки скачиваются в архив сразу
  (как это сделало бы копирование), письма, которых на сервере больше нет, отмечаются в архиве
  ``gone = 1`` (копия остаётся), флаги свежих писем (прочитано, помечено) — сверяются;
* **действия** — флаги, перенос, удаление (в корзину или насовсем), спам, папки: команды IMAP, после
  которых индекс архива сразу приводится к новому состоянию сервера;
* **отправка** — SMTP (пароль ящика, OAuth2 XOAUTH2, отдельные учётные данные или ретранслятор без
  входа), копия в «Отправленные», отметка «Отвечено» у исходного письма. Черновики — в базе архива
  (``webmail_drafts``), вложения к ним — в каталоге данных (зашифрованы, если включено шифрование копии).

Права (кто может действовать и отправлять) проверяет веб-слой (``web/webmail_api.py``); функции
модуля считают, что вызов уже разрешён, и принимают ``actor`` — имя для журнала аудита.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import format_datetime, getaddresses, make_msgid
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import (ImapAuthError, ImapConnectionError, ImapTimeoutError, MailArchiverError,
                     ValidationError)
from .imap.client import ConnectOptions, FolderInfo, ImapConnection
from .logging_setup import get_logger
from .models import Account, AuthType, JobStatus, JobType
from .util import decode_mime_header, ensure_dir, smtp_login, utcnow_iso

log = get_logger("webmail")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webmail_identities (
    account_id    INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    display_name  TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',     -- адрес отправителя ('' — логин ящика или адрес сотрудника)
    signature     TEXT NOT NULL DEFAULT '',     -- HTML
    reply_to      TEXT NOT NULL DEFAULT '',
    smtp_host     TEXT NOT NULL DEFAULT '',     -- пусто — подобрать по серверу IMAP
    smtp_port     INTEGER NOT NULL DEFAULT 0,
    smtp_security TEXT NOT NULL DEFAULT '',     -- ssl | starttls | none ('' — подобрать)
    smtp_auth     TEXT NOT NULL DEFAULT 'account',  -- account | custom | none
    smtp_username TEXT NOT NULL DEFAULT '',
    smtp_password TEXT NOT NULL DEFAULT '',     -- зашифровано
    save_sent     TEXT NOT NULL DEFAULT '',     -- '' — как в настройках | auto | always | never
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS webmail_folders (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    delimiter   TEXT NOT NULL DEFAULT '/',
    flags       TEXT NOT NULL DEFAULT '',
    selectable  INTEGER NOT NULL DEFAULT 1,
    special     TEXT NOT NULL DEFAULT '',
    total       INTEGER,
    unseen      INTEGER,
    updated_at  TEXT,
    PRIMARY KEY (account_id, name)
);
-- сколько адресатов получили письма ящика (предел отправки в час, 1.21.0)
CREATE TABLE IF NOT EXISTS webmail_send_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    at         TEXT NOT NULL,
    rcpts      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webmail_send_log ON webmail_send_log(account_id, at);
CREATE TABLE IF NOT EXISTS webmail_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    owner           TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'new',  -- new | reply | reply_all | forward | edit
    ref_id          INTEGER,                      -- письмо архива: на него отвечают, его пересылают
    server_draft_id INTEGER,                      -- черновик с сервера (письмо архива), продолженный здесь
    to_addrs        TEXT NOT NULL DEFAULT '',
    cc_addrs        TEXT NOT NULL DEFAULT '',
    bcc_addrs       TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',     -- HTML
    in_reply_to     TEXT NOT NULL DEFAULT '',
    refs            TEXT NOT NULL DEFAULT '',
    attachments     TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft | sending | sent
    error           TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL DEFAULT '',
    created_at      TEXT,
    updated_at      TEXT,
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_webmail_drafts_owner ON webmail_drafts(owner, account_id, updated_at);
"""

#: Особые папки: вид → (отметка SPECIAL-USE, привычные имена).
SPECIAL_KINDS = ("inbox", "drafts", "sent", "archive", "junk", "trash", "all")
#: Писем за одну проверку почты, которые скачиваются в архив сразу (остальные — копированием).
SYNC_DOWNLOAD_MAX = 200
#: У скольких самых свежих писем папки сверяются флаги при проверке почты.
SYNC_FLAGS_RECENT = 300
#: Сколько секунд держать открытым неиспользуемое соединение с ящиком.
POOL_IDLE_S = 120
#: После отказа во входе не пробовать снова столько секунд: повторные неудачные входы с адреса архива
#: заканчиваются блокировкой этого адреса (fail2ban) — и вставало бы копирование всех ящиков.
AUTH_BACKOFF_S = 60
#: Кэш папок сервера считается свежим столько секунд.
FOLDERS_FRESH_S = 300
MAX_RECIPIENTS = 100
MAX_SUBJECT = 998
#: Писем за одно действие (флаги, перенос, удаление).
MAX_ACTION_IDS = 5000


#: Колонки, добавленные после 1.17.0: (таблица, колонка, объявление).
_LATE_COLUMNS = (
    ("webmail_identities", "aliases", "TEXT NOT NULL DEFAULT ''"),       # другие адреса отправителя
    ("webmail_drafts", "from_addr", "TEXT NOT NULL DEFAULT ''"),         # от какого адреса (псевдонима)
    ("webmail_drafts", "priority", "INTEGER NOT NULL DEFAULT 0"),        # 1 — важное
    ("webmail_drafts", "receipt", "INTEGER NOT NULL DEFAULT 0"),         # запросить уведомление о прочтении
    ("webmail_drafts", "send_at", "TEXT"),                               # отправить в это время (UTC, ISO)
    ("webmail_drafts", "actor", "TEXT NOT NULL DEFAULT ''"),             # кто отправляет (для аудита)
    ("webmail_drafts", "server_copy", "TEXT NOT NULL DEFAULT ''"),       # копия в «Черновиках» сервера (1.19.0)
    ("webmail_drafts", "smime_sign", "INTEGER NOT NULL DEFAULT 0"),      # подписать S/MIME (1.20.0)
    ("webmail_drafts", "smime_encrypt", "INTEGER NOT NULL DEFAULT 0"),   # зашифровать S/MIME (1.20.0)
)


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    for table, col, decl in _LATE_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    from .webmail_secure import ensure_schema as _secure_schema
    _secure_schema(conn)
    from .webmail_tools import ensure_schema as _tools_schema
    _tools_schema(conn)
    from .webmail_more import ensure_schema as _more_schema
    _more_schema(conn)


def _now() -> float:
    return time.time()


# ===========================================================================
#  Соединения с ящиками
# ===========================================================================
class _Entry:
    __slots__ = ("conn", "lock", "used", "key")

    def __init__(self) -> None:
        self.conn: Optional[ImapConnection] = None
        self.lock = threading.RLock()
        self.used = 0.0
        self.key = ""


def _account_key(account: Account) -> str:
    """Отпечаток того, как входить в ящик: изменился (новый пароль, сервер) — соединение открывается заново."""
    raw = "\n".join(str(x) for x in (account.host, account.port, account.security, account.username,
                                       account.auth_type, account.password, account.oauth_refresh_token,
                                       account.server_id))
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=12).hexdigest()


class ImapPool:
    """Соединения с ящиками для действий клиента: по одному на ящик, закрываются через POOL_IDLE_S.

    Действия одного ящика идут по очереди (замок на ящик): IMAP-соединение не терпит команд из двух потоков.
    Вход заново на каждое действие — секунда ожидания и лишняя запись в журнале сервера, поэтому соединение
    держится открытым, пока с ящиком работают.
    """

    def __init__(self, svc) -> None:
        self.svc = svc
        self._lock = threading.Lock()
        self._items: Dict[int, _Entry] = {}
        self._auth_failed: Dict[int, Tuple[float, str]] = {}
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _options(self) -> ConnectOptions:
        opt = self.svc.connect_options()
        # человек ждёт ответа на экране: не ждём сервер по две минуты
        opt.connect_timeout_s = min(int(opt.connect_timeout_s or 30), 20)
        opt.socket_timeout_s = min(int(opt.socket_timeout_s or 120), 90)
        return opt

    def _entry(self, account_id: int) -> _Entry:
        with self._lock:
            entry = self._items.get(account_id)
            if entry is None:
                entry = self._items[account_id] = _Entry()
            if self._reaper is None or not self._reaper.is_alive():
                self._stop.clear()
                self._reaper = threading.Thread(target=self._reap_loop, name="webmail-pool", daemon=True)
                self._reaper.start()
            return entry

    @contextlib.contextmanager
    def session(self, account: Account):
        entry = self._entry(int(account.id))
        with entry.lock:
            conn = entry.conn
            key = _account_key(account)
            if conn is not None and (entry.key != key or not self._alive(conn, entry.used)):
                self._close(conn)
                conn = entry.conn = None
            if conn is None:
                failed = self._auth_failed.get(int(account.id))
                if failed and _now() - failed[0] < AUTH_BACKOFF_S and failed[1] == key:
                    raise ImapAuthError(
                        f"Сервер не принял вход в ящик «{account.name}» меньше минуты назад — повторите позже.",
                        hint="Частые неудачные входы с адреса архива сервер может заблокировать (fail2ban). "
                             "Проверьте пароль ящика («Почтовые ящики» → «Проверить»).")
                conn = ImapConnection(account, self._options())
                try:
                    conn.connect()
                except ImapAuthError as exc:
                    self._auth_failed[int(account.id)] = (_now(), key)
                    try:
                        # как «Проверить вход»: ящик с неверным паролем клиент больше не трогает сам
                        self.svc.db.set_login_status(int(account.id), "auth_error", exc.message[:500])
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                self._auth_failed.pop(int(account.id), None)
                if account.login_status == "auth_error":
                    try:
                        self.svc.db.set_login_status(int(account.id), "ok")
                    except Exception:  # noqa: BLE001
                        pass
                entry.conn, entry.key = conn, key
            try:
                yield conn
            except (ImapConnectionError, ImapTimeoutError):
                self._close(conn)
                entry.conn = None
                raise
            except MailArchiverError:
                raise
            except Exception:
                # чужое исключение посреди команды: поток ответов сервера мог остаться в неизвестном состоянии
                self._close(conn)
                entry.conn = None
                raise
            finally:
                entry.used = _now()

    @staticmethod
    def _alive(conn: ImapConnection, used: float) -> bool:
        if _now() - used < 15:
            return True
        try:
            conn.client.noop()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _close(conn: Optional[ImapConnection]) -> None:
        if conn is None:
            return
        try:
            conn.close(force=False)
        except Exception:  # noqa: BLE001
            pass

    def drop(self, account_id: int) -> None:
        with self._lock:
            entry = self._items.pop(int(account_id), None)
        if entry is not None:
            with entry.lock:
                self._close(entry.conn)
                entry.conn = None

    def reap(self, idle_s: float = POOL_IDLE_S) -> int:
        closed = 0
        with self._lock:
            items = list(self._items.items())
        for _aid, entry in items:
            if entry.conn is None or _now() - entry.used < idle_s:
                continue
            if entry.lock.acquire(blocking=False):
                try:
                    if entry.conn is not None and _now() - entry.used >= idle_s:
                        self._close(entry.conn)
                        entry.conn = None
                        closed += 1
                finally:
                    entry.lock.release()
        return closed

    def _reap_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self.reap()
            except Exception:  # noqa: BLE001
                log.debug("Сбой при закрытии простаивающих соединений", exc_info=True)

    def close_all(self) -> None:
        self._stop.set()
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for entry in items:
            with entry.lock:
                self._close(entry.conn)
                entry.conn = None


_POOLS: Dict[int, ImapPool] = {}
_POOLS_LOCK = threading.Lock()


def pool(svc) -> ImapPool:
    with _POOLS_LOCK:
        p = _POOLS.get(id(svc))
        if p is None or p.svc is not svc:
            p = _POOLS[id(svc)] = ImapPool(svc)
        return p


def shutdown(svc) -> None:
    """Закрыть соединения клиента (остановка службы)."""
    try:
        from .webmail_more import stop_push
        stop_push(svc)
    except Exception:  # noqa: BLE001
        pass
    with _POOLS_LOCK:
        p = _POOLS.pop(id(svc), None)
    if p is not None:
        p.close_all()


# ===========================================================================
#  Особые папки и список папок
# ===========================================================================
def _leaf(name: str, delimiter: str) -> str:
    if delimiter and delimiter in name:
        return name.rsplit(delimiter, 1)[1]
    return name


def detect_special(folders: Sequence[FolderInfo]) -> Dict[str, str]:
    """Особые папки ящика: вид → имя папки (по отметкам SPECIAL-USE или привычным именам)."""
    from .serverops.imap_ops import DRAFT_NAMES, JUNK_NAMES, SENT_NAMES, TRASH_NAMES, special_folder
    out: Dict[str, str] = {}
    for f in folders:
        if f.name.upper() == "INBOX":
            out["inbox"] = f.name
            break
    table = {
        "sent": ("\\Sent", SENT_NAMES), "drafts": ("\\Drafts", DRAFT_NAMES), "trash": ("\\Trash", TRASH_NAMES),
        "junk": ("\\Junk", JUNK_NAMES), "archive": ("\\Archive", ("archive", "архив", "archives")),
        "all": ("\\All", ()),
    }
    for kind, (flag, names) in table.items():
        found, _why = special_folder(folders, flag, names)
        if found is not None:
            out[kind] = found.name
    return out


def _folder_rows(svc, account_id: int):
    return svc.db.query("SELECT * FROM webmail_folders WHERE account_id=? ORDER BY name", (account_id,))


def _store_folders(svc, account_id: int, infos: Sequence[FolderInfo], special: Dict[str, str],
                   counts: Dict[str, Dict[str, int]]) -> None:
    by_name = {v: k for k, v in special.items()}
    now = utcnow_iso()
    conn = svc.db.connect()
    with svc.db._write_lock:  # noqa: SLF001 — одна транзакция на весь список папок
        try:
            conn.execute("DELETE FROM webmail_folders WHERE account_id=?", (account_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO webmail_folders(account_id, name, delimiter, flags, selectable, special, "
                "total, unseen, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(account_id, f.name, f.delimiter or "", ",".join(f.flags), 1 if f.selectable else 0,
                  by_name.get(f.name, ""), (counts.get(f.name) or {}).get("messages"),
                  (counts.get(f.name) or {}).get("unseen"), now) for f in infos])
            conn.commit()
        except BaseException:
            svc.db._safe_rollback(conn)  # noqa: SLF001
            raise


def refresh_folders(svc, account: Account, *, counts: bool = True) -> List[FolderInfo]:
    """Папки ящика с сервера (LIST, затем STATUS по каждой) — в кэш ``webmail_folders``."""
    with pool(svc).session(account) as conn:
        infos = conn.list_folders()
        stats: Dict[str, Dict[str, int]] = {}
        if counts:
            for f in infos[:400]:
                if not f.selectable:
                    continue
                c = conn.message_counts(f.name)
                if c is not None:
                    stats[f.name] = c
    special = detect_special(infos)
    _store_folders(svc, int(account.id), infos, special, stats)
    return infos


def folders(svc, account: Account, *, refresh: bool = False, online: bool = True) -> Dict:
    """Папки для клиента: с сервера (кэш, обновляется при ``refresh`` или если устарел) и «только в архиве».

    ``online=False`` — сервер не спрашивать (клиент выключен, ящик выключен, нет прав): только кэш и архив.
    """
    rows = _folder_rows(svc, int(account.id))
    error = None
    stale = not rows or any(_age_s(r["updated_at"]) > FOLDERS_FRESH_S for r in rows[:1])
    if online and (refresh or stale):
        try:
            refresh_folders(svc, account)
            rows = _folder_rows(svc, int(account.id))
        except MailArchiverError as exc:
            error = {"message": exc.message, "hint": exc.hint}
    archived = {r["folder"]: (int(r["cnt"] or 0), int(r["bytes"] or 0)) for r in svc.db.folders_summary(account.id)}
    out = []
    seen = set()
    special = {}
    for r in rows:
        seen.add(r["name"])
        if r["special"]:
            special[r["special"]] = r["name"]
        out.append({"name": r["name"], "delimiter": r["delimiter"], "special": r["special"] or "",
                    "selectable": bool(r["selectable"]), "total": r["total"], "unseen": r["unseen"],
                    "archived": archived.get(r["name"], (0, 0))[0], "on_server": True})
    for name, (cnt, _bytes) in sorted(archived.items()):
        if name in seen:
            continue
        out.append({"name": name, "delimiter": "", "special": "", "selectable": True, "total": None,
                    "unseen": None, "archived": cnt, "on_server": False})
    if not rows:
        # сервер ещё ни разу не спрашивали (или он недоступен): папки архива показываются как обычные
        for item in out:
            item["on_server"] = None
    order = {k: i for i, k in enumerate(SPECIAL_KINDS)}
    out.sort(key=lambda f: (0 if f["on_server"] is not False else 1,
                            order.get(f["special"], len(order)), f["name"].lower()))
    updated = rows[0]["updated_at"] if rows else None
    return {"folders": out, "special": special, "updated_at": updated, "error": error}


def _age_s(iso: Optional[str]) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return 1e9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - dt.timestamp())


def special_folders(svc, account: Account) -> Dict[str, str]:
    rows = _folder_rows(svc, int(account.id))
    if not rows:
        refresh_folders(svc, account, counts=False)
        rows = _folder_rows(svc, int(account.id))
    return {r["special"]: r["name"] for r in rows if r["special"]}


def _delimiter_of(svc, account_id: int, folder: str) -> str:
    row = svc.db.query_one("SELECT delimiter FROM webmail_folders WHERE account_id=? AND name=?", (account_id, folder))
    if row is not None and row["delimiter"] is not None:
        return row["delimiter"]
    row = svc.db.query_one("SELECT delimiter FROM webmail_folders WHERE account_id=? AND delimiter<>'' LIMIT 1",
                           (account_id,))
    return row["delimiter"] if row is not None else "/"


def _require_server_folder(svc, account: Account, name: str) -> str:
    name = str(name or "")
    rows = _folder_rows(svc, int(account.id))
    if not any(r["name"] == name and r["selectable"] for r in rows):
        refresh_folders(svc, account, counts=False)
        rows = _folder_rows(svc, int(account.id))
    if not any(r["name"] == name and r["selectable"] for r in rows):
        raise ValidationError(f"Папки «{name}» на сервере нет.", hint="Обновите список папок.")
    return name


# ===========================================================================
#  Письма архива и их место на сервере
# ===========================================================================
def _flags_of(row) -> List[str]:
    return [f for f in (row["flags"] or "").split(",") if f]


def _rows(svc, account_id: int, ids: Iterable[int], *, include_gone: bool = False) -> List:
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        raise ValidationError("Не выбрано ни одного письма.")
    if len(ids) > MAX_ACTION_IDS:
        raise ValidationError(f"За один раз — не больше {MAX_ACTION_IDS} писем.")
    rows = list(svc.db.messages_by_ids(account_id, ids))
    if not include_gone:
        rows = [r for r in rows if not r["gone"]]
    return rows


def _groups(rows) -> Dict[Tuple[str, int], List]:
    out: Dict[Tuple[str, int], List] = {}
    for r in rows:
        out.setdefault((r["folder"], int(r["uidvalidity"] or 0)), []).append(r)
    return out


def _resolve(conn: ImapConnection, info: Dict[str, int], uidvalidity: int, rows) -> Tuple[Dict[int, int], List]:
    """Письма архива → UID на сервере в открытой папке: ``({id: uid}, [строки, которых на сервере нет])``.

    Совпал UIDVALIDITY — номер из архива; нет (папку пересоздали) — поиск по Message-ID. Номера проверяются
    поиском: письмо могли удалить с телефона минуту назад.
    """
    mapping: Dict[int, int] = {}
    if info.get("uidvalidity") and int(info["uidvalidity"]) == int(uidvalidity or 0):
        for r in rows:
            mapping[int(r["id"])] = int(r["uid"])
        present = conn.uids_present(sorted(set(mapping.values())))
        if present is not None:
            mapping = {k: v for k, v in mapping.items() if v in present}
    else:
        for r in rows:
            mid = (r["message_id"] or "").strip()
            if not mid:
                continue
            found = conn.search_header_messageid(mid)
            if found:
                mapping[int(r["id"])] = int(sorted(found)[0])
    missing = [r for r in rows if int(r["id"]) not in mapping]
    return mapping, missing


def _set_local_flags(svc, updates: Sequence[Tuple[int, List[str]]]) -> None:
    if not updates:
        return
    conn = svc.db.connect()
    with svc.db._write_lock:  # noqa: SLF001
        try:
            conn.executemany("UPDATE messages SET flags=? WHERE id=?", [(",".join(f), int(i)) for i, f in updates])
            conn.commit()
        except BaseException:
            svc.db._safe_rollback(conn)  # noqa: SLF001
            raise


def _mark_gone_ids(svc, ids: Sequence[int], gone: int = 1) -> None:
    ids = [int(i) for i in ids]
    if not ids:
        return
    conn = svc.db.connect()
    with svc.db._write_lock:  # noqa: SLF001
        try:
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                conn.execute(f"UPDATE messages SET gone=? WHERE id IN ({','.join('?' * len(part))})", (gone, *part))
            conn.commit()
        except BaseException:
            svc.db._safe_rollback(conn)  # noqa: SLF001
            raise


def _norm_flag(flag: str) -> str:
    f = str(flag or "").strip()
    low = f.lower()
    for std in ("\\Seen", "\\Flagged", "\\Answered", "\\Deleted", "\\Draft"):
        if low == std.lower():
            return std
    if not re.fullmatch(r"\$?[A-Za-z][A-Za-z0-9_.\-]{0,40}", f):
        raise ValidationError(f"Недопустимый флаг письма: «{flag}».")
    return f


# ===========================================================================
#  Действия над письмами
# ===========================================================================
def set_flags(svc, account: Account, ids: Iterable[int], *, add: Sequence[str] = (),
              remove: Sequence[str] = ()) -> Dict:
    """Поставить и снять флаги (прочитано, помечено…) — на сервере и в архиве."""
    add = [_norm_flag(f) for f in add]
    remove = [_norm_flag(f) for f in remove]
    if not add and not remove:
        raise ValidationError("Не указано, какие флаги менять.")
    rows = _rows(svc, int(account.id), ids)
    changed: List[Tuple[int, List[str]]] = []
    missing: List[int] = []
    with pool(svc).session(account) as conn:
        for (folder, uidv), group in _groups(rows).items():
            info = conn.select(folder, readonly=False)
            mapping, gone = _resolve(conn, info, uidv, group)
            missing.extend(int(r["id"]) for r in gone)
            uids = sorted(set(mapping.values()))
            if uids and add:
                conn.add_flags(uids, add)
            if uids and remove:
                conn.remove_flags(uids, remove)
            for r in group:
                if int(r["id"]) not in mapping:
                    continue
                flags = [f for f in _flags_of(r) if f not in remove]
                flags += [f for f in add if f not in flags]
                changed.append((int(r["id"]), flags))
    _set_local_flags(svc, changed)
    _mark_gone_ids(svc, missing)
    if "\\Seen" in add or "\\Seen" in remove:
        _touch_counts(svc, account, {r["folder"] for r in rows})
    return {"changed": len(changed), "missing": len(missing)}


def _expand_set(spec: str) -> List[int]:
    out: List[int] = []
    for part in spec.split(","):
        if ":" in part:
            a, b = (int(x) for x in part.split(":", 1))
            out.extend(range(a, b + 1) if a <= b else range(a, b - 1, -1))
        elif part:
            out.append(int(part))
    return out


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(_text(x) for x in value)
    if isinstance(value, bytes):
        return value.decode("latin-1", "replace")
    return str(value or "")


def parse_copyuid(value) -> Tuple[int, Dict[int, int]]:
    """Ответ сервера с COPYUID (RFC 4315) → (UIDVALIDITY папки назначения, {старый UID: новый UID}).

    ``value`` — «COPYUID 1000 4,6:7 21:23» или то, что imaplib сохраняет по коду ответа: «1000 4,6:7 21:23».
    """
    text = _text(value)
    m = re.search(r"(?:COPYUID\s+)?(\d+)\s+([\d:,]+)\s+([\d:,]+)", text, re.I)
    if not m:
        return 0, {}
    old, new = _expand_set(m.group(2)), _expand_set(m.group(3))
    if len(old) != len(new):
        return 0, {}
    return int(m.group(1)), dict(zip(old, new))


def parse_appenduid(value) -> Tuple[int, int]:
    """«[APPENDUID 38505 3955] APPEND completed» → (UIDVALIDITY, UID); (0, 0) — сервер не сообщил."""
    m = re.search(r"APPENDUID\s+(\d+)\s+(\d+)", _text(value), re.I)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _untagged(conn: ImapConnection) -> dict:
    imap = getattr(conn.client, "_imap", None)
    found = getattr(imap, "untagged_responses", None)
    return found if isinstance(found, dict) else {}


def _take_copyuid(conn: ImapConnection) -> Tuple[int, Dict[int, int]]:
    """COPYUID последней команды: imaplib кладёт коды ответов (и с завершающей строки, и с промежуточной
    у MOVE) в ``untagged_responses`` под их именем."""
    uidv, mapping = 0, {}
    for item in list(_untagged(conn).pop("COPYUID", None) or []):
        v, m = parse_copyuid(item)
        if m:
            uidv = uidv or v
            mapping.update(m)
    return uidv, mapping


def _move_group(conn: ImapConnection, uids: List[int], dest: str) -> Tuple[Dict[int, int], int]:
    """Перенести письма открытой папки в ``dest``: MOVE, иначе COPY + удаление. → ({старый: новый UID}, UIDVALIDITY)."""
    from .imap.client import _map_exception
    mapping: Dict[int, int] = {}
    uidv = 0
    use_move = conn.has_capability("MOVE")
    for start in range(0, len(uids), 500):
        part = uids[start:start + 500]
        _untagged(conn).pop("COPYUID", None)
        try:
            if use_move:
                conn.client.move(part, dest)
            else:
                conn.client.copy(part, dest)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        v, m = _take_copyuid(conn)
        uidv = uidv or v
        mapping.update(m)
    if not use_move:
        conn.add_flags(uids, ["\\Deleted"])
        if conn.has_capability("UIDPLUS"):
            conn.expunge_uids(uids)
        else:
            conn.expunge_all()
    return mapping, uidv


def _copy_into_archive(svc, account: Account, rows, dest: str, dest_uidv: int, mapping: Dict[int, int],
                       *, extra_flags: Sequence[str] = ()) -> List[int]:
    """Письма, перенесённые на сервере, — в архив под новой папкой и номером (файл копируется из архива).

    Копирование сделало бы то же самое, только скачав письма с сервера заново.
    """
    if not dest_uidv or not mapping:
        return []
    from .imap.backup import BackupEngine
    delim = _delimiter_of(svc, int(account.id), dest)
    batch = []
    for r in rows:
        new_uid = mapping.get(int(r["uid"]))
        if not new_uid:
            continue
        try:
            raw = svc.store.read_message(int(account.id), r["stored_path"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Письмо %s не прочитано из архива для переноса: %s", r["id"], exc)
            continue
        flags = [f for f in _flags_of(r) if f != "\\Deleted"] + [f for f in extra_flags if f not in _flags_of(r)]
        epoch = None
        if r["internaldate"]:
            try:
                epoch = datetime.fromisoformat(r["internaldate"]).timestamp()
            except (TypeError, ValueError):
                epoch = None
        rel, digest, size = svc.store.store_message(int(account.id), dest, delim, int(new_uid), raw, flags=flags,
                                                    internaldate=epoch)
        subject, from_addr, has_attach = (r["subject"] or "", r["from_addr"] or "", r["has_attach"])
        if r["subject"] is None:
            subject, from_addr, has_attach = BackupEngine._extract_meta(raw)  # noqa: SLF001
        batch.append((int(account.id), dest, int(dest_uidv), int(new_uid), r["message_id"] or "", size,
                      r["internaldate"] or "", ",".join(flags), rel, digest, subject, from_addr,
                      1 if has_attach else 0))
    if not batch:
        return []
    svc.db.add_message_index_batch(batch)
    _ensure_folder_state(svc, int(account.id), dest)
    uids = [b[3] for b in batch]
    marks = ",".join("?" * len(uids))
    return [int(r["id"]) for r in svc.db.query(
        f"SELECT id FROM messages WHERE account_id=? AND folder=? AND uidvalidity=? AND uid IN ({marks})",
        (int(account.id), dest, int(dest_uidv), *uids))]


def move(svc, account: Account, ids: Iterable[int], dest: str, *, actor: str = "",
         audit_action: str = "webmail_move") -> Dict:
    """Перенести письма в другую папку сервера; в архиве они появляются в новой папке сразу."""
    dest = _require_server_folder(svc, account, dest)
    rows = _rows(svc, int(account.id), ids)
    moved = missing = archived = 0
    touched = {dest}
    #: чтобы отменить перенос: письма в новой папке (id архива) → папка, откуда они пришли
    undo: List[Dict] = []
    with pool(svc).session(account) as conn:
        for (folder, uidv), group in _groups(rows).items():
            if folder == dest:
                continue
            touched.add(folder)
            info = conn.select(folder, readonly=False)
            mapping, gone = _resolve(conn, info, uidv, group)
            _mark_gone_ids(svc, [r["id"] for r in gone])
            missing += len(gone)
            uids = sorted(set(mapping.values()))
            if not uids:
                continue
            uidmap, new_uidv = _move_group(conn, uids, dest)
            moved += len(uids)
            ok_rows = [r for r in group if int(r["id"]) in mapping]
            _mark_gone_ids(svc, [r["id"] for r in ok_rows])
            by_old = {mapping[int(r["id"])]: r for r in ok_rows}
            if not uidmap or not new_uidv:
                # сервер без UIDPLUS: номера в новой папке — поиском по Message-ID
                dest_info = conn.select(dest, readonly=True)
                new_uidv = int(dest_info.get("uidvalidity") or 0)
                uidmap = {}
                for old_uid, r in by_old.items():
                    mid = (r["message_id"] or "").strip()
                    if mid:
                        found = conn.search_header_messageid(mid)
                        if len(found) == 1:
                            uidmap[old_uid] = int(found[0])
            if uidmap and new_uidv:
                copies = []
                for old_uid, r in by_old.items():
                    if old_uid in uidmap:
                        item = dict(r)
                        item["uid"] = old_uid
                        copies.append(item)
                new_ids = _copy_into_archive(svc, account, copies, dest, new_uidv, uidmap)
                archived += len(new_ids)
                if new_ids:
                    undo.append({"folder": folder, "ids": new_ids})
    _touch_counts(svc, account, touched)
    if actor and moved:
        svc.db.add_audit(actor, audit_action, f"ящик «{account.name}»: писем {moved} → «{dest}»")
    return {"moved": moved, "missing": missing, "archived": archived, "folder": dest, "undo": undo}


def delete(svc, account: Account, ids: Iterable[int], *, permanent: bool = False, actor: str = "") -> Dict:
    """Удалить письма: в корзину, а из самой корзины (или с ``permanent``) — насовсем. Копия в архиве остаётся."""
    rows = _rows(svc, int(account.id), ids)
    special = special_folders(svc, account)
    trash = special.get("trash")
    to_trash = [r for r in rows if not permanent and trash and r["folder"] != trash]
    to_erase = [r for r in rows if r not in to_trash]
    result = {"trashed": 0, "deleted": 0, "missing": 0, "undo": []}
    if to_trash:
        res = move(svc, account, [r["id"] for r in to_trash], trash)
        result["trashed"] = res["moved"]
        result["missing"] += res["missing"]
        result["undo"] = res["undo"]
    if to_erase:
        with pool(svc).session(account) as conn:
            for (folder, uidv), group in _groups(to_erase).items():
                info = conn.select(folder, readonly=False)
                mapping, gone = _resolve(conn, info, uidv, group)
                result["missing"] += len(gone)
                uids = sorted(set(mapping.values()))
                if uids:
                    conn.add_flags(uids, ["\\Deleted"])
                    if conn.has_capability("UIDPLUS"):
                        conn.expunge_uids(uids)
                    else:
                        conn.expunge_all()
                    result["deleted"] += len(uids)
                _mark_gone_ids(svc, [r["id"] for r in group])
        _touch_counts(svc, account, {r["folder"] for r in to_erase})
    if actor and (result["trashed"] or result["deleted"]):
        parts = []
        if result["trashed"]:
            parts.append(f"в корзину {result['trashed']}")
        if result["deleted"]:
            parts.append(f"удалено с сервера {result['deleted']}")
        svc.db.add_audit(actor, "webmail_delete", f"ящик «{account.name}»: " + ", ".join(parts))
    return result


def junk(svc, account: Account, ids: Iterable[int], *, spam: bool = True, actor: str = "") -> Dict:
    """«Спам» — в папку спама; «Не спам» — обратно во «Входящие»."""
    special = special_folders(svc, account)
    dest = special.get("junk") if spam else special.get("inbox", "INBOX")
    if not dest:
        raise ValidationError("Папки «Спам» на сервере нет.", hint="Перенесите письма в нужную папку вручную.")
    return move(svc, account, ids, dest, actor=actor, audit_action="webmail_junk" if spam else "webmail_move")


def _touch_counts(svc, account: Account, names: Iterable[str]) -> None:
    """Счётчики затронутых папок в кэше после действия (STATUS)."""
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return
    try:
        with pool(svc).session(account) as conn:
            updates = []
            for name in names[:20]:
                c = conn.message_counts(name)
                if c is not None:
                    updates.append((c["messages"], c["unseen"], int(account.id), name))
        if updates:
            db_conn = svc.db.connect()
            with svc.db._write_lock:  # noqa: SLF001
                db_conn.executemany("UPDATE webmail_folders SET total=?, unseen=? WHERE account_id=? AND name=?",
                                    updates)
                db_conn.commit()
    except MailArchiverError as exc:
        log.debug("Счётчики папок не обновлены: %s", exc)


def mark_all_read(svc, account: Account, folder: str) -> Dict:
    """«Отметить все прочитанными» — все непрочитанные письма папки на сервере и в архиве."""
    acc_id = int(account.id)
    with pool(svc).session(account) as conn:
        info = conn.select(folder, readonly=False)
        uids = conn.search(["UNSEEN"])
        if uids:
            conn.add_flags(uids, ["\\Seen"])
        uidv = int(info.get("uidvalidity") or 0)
    changed = []
    if uids:
        for start in range(0, len(uids), 500):
            part = uids[start:start + 500]
            for r in svc.db.query(f"SELECT id, flags FROM messages WHERE account_id=? AND folder=? AND uidvalidity=? "
                                  f"AND uid IN ({','.join('?' * len(part))})", (acc_id, folder, uidv, *part)):
                flags = _flags_of(r)
                if "\\Seen" not in flags:
                    changed.append((int(r["id"]), flags + ["\\Seen"]))
    _set_local_flags(svc, changed)
    _touch_counts(svc, account, [folder])
    return {"marked": len(uids)}


def empty_folder(svc, account: Account, folder: str, *, actor: str = "") -> Dict:
    """Очистить «Корзину» или «Спам»: удалить с сервера все письма папки. Копии остаются в архиве."""
    special = special_folders(svc, account)
    kind = next((k for k, v in special.items() if v == folder), "")
    if kind not in ("trash", "junk"):
        raise ValidationError("Очистить целиком можно только «Корзину» и «Спам».",
                              hint="Письма другой папки выберите и удалите.")
    with pool(svc).session(account) as conn:
        conn.select(folder, readonly=False)
        uids = conn.search(["ALL"])
        if uids:
            conn.add_flags(uids, ["\\Deleted"])
            if conn.has_capability("UIDPLUS"):
                conn.expunge_uids(uids)
            else:
                conn.expunge_all()
    svc.db.execute("UPDATE messages SET gone=1 WHERE account_id=? AND folder=? AND gone=0", (int(account.id), folder))
    _touch_counts(svc, account, [folder])
    if actor and uids:
        svc.db.add_audit(actor, "webmail_empty", f"ящик «{account.name}»: папка «{folder}», писем {len(uids)}")
    return {"deleted": len(uids)}


# ===========================================================================
#  Папки: создать, переименовать, удалить
# ===========================================================================
_BAD_FOLDER_CHARS = re.compile(r"[\x00-\x1f\x7f*%\"\\]")


def _check_folder_leaf(name: str, delimiter: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise ValidationError("Укажите имя папки.")
    if len(name) > 200:
        raise ValidationError("Имя папки длиннее 200 знаков.")
    if _BAD_FOLDER_CHARS.search(name) or (delimiter and delimiter in name):
        raise ValidationError(f"В имени папки нельзя использовать знаки * % \" \\ и разделитель «{delimiter}».")
    if name.upper() == "INBOX":
        raise ValidationError("Имя INBOX занято «Входящими».")
    return name


def create_folder(svc, account: Account, name: str, parent: str = "", *, actor: str = "") -> Dict:
    rows = {r["name"]: r for r in _folder_rows(svc, int(account.id))}
    if not rows:
        refresh_folders(svc, account, counts=False)
        rows = {r["name"]: r for r in _folder_rows(svc, int(account.id))}
    delim = next((r["delimiter"] for r in rows.values() if r["delimiter"]), "/")
    leaf = _check_folder_leaf(name, delim)
    if parent and parent not in rows:
        raise ValidationError(f"Папки «{parent}» на сервере нет.")
    full = f"{parent}{delim}{leaf}" if parent else leaf
    if full in rows:
        raise ValidationError(f"Папка «{full}» уже есть.")
    with pool(svc).session(account) as conn:
        conn.ensure_folder(full)
        try:
            conn.subscribe_folder(full)
        except MailArchiverError:
            pass
    refresh_folders(svc, account)
    if actor:
        svc.db.add_audit(actor, "webmail_folder_create", f"ящик «{account.name}»: папка «{full}»")
    return {"folder": full}


def _protected(row) -> bool:
    return row["name"].upper() == "INBOX" or bool(row["special"])


def rename_folder(svc, account: Account, folder: str, new_leaf: str, *, actor: str = "",
                  new_parent: Optional[str] = None) -> Dict:
    """Переименовать папку и (с ``new_parent``) перенести её в другую ('' — в корень ящика)."""
    rows = {r["name"]: r for r in _folder_rows(svc, int(account.id))}
    row = rows.get(folder)
    if row is None:
        raise ValidationError(f"Папки «{folder}» на сервере нет.", hint="Обновите список папок.")
    if _protected(row):
        raise ValidationError("«Входящие» и особые папки (Отправленные, Черновики, Корзина, Спам) не переименовываются.")
    delim = row["delimiter"] or ""
    leaf = _check_folder_leaf(new_leaf, delim)
    parent = folder.rsplit(delim, 1)[0] if delim and delim in folder else ""
    if new_parent is not None:
        if new_parent and new_parent not in rows:
            raise ValidationError(f"Папки «{new_parent}» на сервере нет.")
        if new_parent and (new_parent == folder or (delim and new_parent.startswith(folder + delim))):
            raise ValidationError("Папку нельзя перенести в неё саму или во вложенную в неё.")
        parent = new_parent
    new = f"{parent}{delim}{leaf}" if parent else leaf
    if new == folder:
        return {"folder": folder}
    if new in rows:
        raise ValidationError(f"Папка «{new}» уже есть.")
    with pool(svc).session(account) as conn:
        conn.rename_folder(folder, new)
        try:
            conn.subscribe_folder(new)
        except MailArchiverError:
            pass
    _rename_in_archive(svc, int(account.id), folder, new, delim)
    refresh_folders(svc, account)
    if actor:
        svc.db.add_audit(actor, "webmail_folder_rename", f"ящик «{account.name}»: «{folder}» → «{new}»")
    return {"folder": new}


def _rename_in_archive(svc, account_id: int, old: str, new: str, delim: str) -> None:
    """Письма архива переименованной папки (и вложенных) — под новым именем: иначе копирование скачало бы их
    заново как письма «новой» папки. UIDVALIDITY, если сервер его сменил, сверит копирование (по Message-ID)."""
    conn = svc.db.connect()
    pairs = [(old, new)]
    if delim:
        for r in svc.db.query("SELECT DISTINCT folder FROM messages WHERE account_id=? AND folder LIKE ? ESCAPE '!'",
                              (account_id, _like_prefix(old + delim))):
            pairs.append((r["folder"], new + r["folder"][len(old):]))
    with svc.db._write_lock:  # noqa: SLF001
        try:
            for a, b in pairs:
                conn.execute("UPDATE messages SET folder=? WHERE account_id=? AND folder=?", (b, account_id, a))
                conn.execute("UPDATE OR IGNORE folders SET folder=? WHERE account_id=? AND folder=?", (b, account_id, a))
                conn.execute("UPDATE OR IGNORE folder_problems SET folder=? WHERE account_id=? AND folder=?",
                             (b, account_id, a))
            conn.commit()
        except BaseException:
            svc.db._safe_rollback(conn)  # noqa: SLF001
            raise
    for _a, b in pairs:
        _ensure_folder_state(svc, account_id, b)


def _ensure_folder_state(svc, account_id: int, folder: str) -> None:
    """Состояние папки для копирования, если его ещё нет: по письмам архива этой папки.

    Без него копирование не узнало бы письма папки, переименованной или созданной в клиенте, когда сервер
    сменил ей UIDVALIDITY, — и скачало бы их заново (сверка по Message-ID идёт только при известном прежнем
    UIDVALIDITY)."""
    if svc.db.get_folder_state(account_id, folder) is not None:
        return
    row = svc.db.query_one("SELECT uidvalidity, MAX(uid) AS last_uid, COUNT(*) AS n FROM messages "
                           "WHERE account_id=? AND folder=? GROUP BY uidvalidity ORDER BY n DESC LIMIT 1",
                           (account_id, folder))
    if row is not None and row["uidvalidity"]:
        svc.db.upsert_folder_state(account_id, folder, int(row["uidvalidity"]), int(row["last_uid"] or 0),
                                   int(row["n"] or 0))


def _like_prefix(prefix: str) -> str:
    return prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def delete_folder(svc, account: Account, folder: str, *, actor: str = "") -> Dict:
    rows = {r["name"]: r for r in _folder_rows(svc, int(account.id))}
    row = rows.get(folder)
    if row is None:
        raise ValidationError(f"Папки «{folder}» на сервере нет.", hint="Обновите список папок.")
    if _protected(row):
        raise ValidationError("«Входящие» и особые папки (Отправленные, Черновики, Корзина, Спам) не удаляются.")
    delim = row["delimiter"] or ""
    if delim and any(n.startswith(folder + delim) for n in rows):
        raise ValidationError("В папке есть вложенные папки — сначала удалите или перенесите их.")
    with pool(svc).session(account) as conn:
        try:
            conn.client.unselect_folder()
        except Exception:  # noqa: BLE001 — папка могла быть не открыта
            pass
        try:
            conn.select("INBOX", readonly=True)
        except MailArchiverError:
            pass
        left = conn.messages_now(folder)
        conn.delete_folder(folder)
    svc.db.execute("UPDATE messages SET gone=1 WHERE account_id=? AND folder=? AND gone=0", (int(account.id), folder))
    refresh_folders(svc, account)
    if actor:
        svc.db.add_audit(actor, "webmail_folder_delete",
                         f"ящик «{account.name}»: папка «{folder}»" + (f" (писем в ней было: {left})" if left else ""))
    return {"deleted": folder, "messages": left}


# ===========================================================================
#  Проверка почты: новые письма — в архив, удалённые — отметить, флаги — сверить
# ===========================================================================
def backup_running(svc, account_id: int) -> bool:
    row = svc.db.query_one("SELECT 1 FROM jobs WHERE account_id=? AND type=? AND status=? LIMIT 1",
                           (int(account_id), JobType.BACKUP, JobStatus.RUNNING))
    return row is not None


def sync_folder(svc, account: Account, folder: str, *, download: bool = True,
                flags_recent: int = SYNC_FLAGS_RECENT) -> Dict:
    """Проверить папку на сервере и привести к ней архив (то, что копирование сделает при следующем прогоне)."""
    from .imap.backup import BackupEngine
    acc_id = int(account.id)
    res = {"folder": folder, "new": 0, "pending": 0, "gone": 0, "flags": 0, "total": None, "unseen": None,
           "uidvalidity_changed": False, "backup_running": False}
    with pool(svc).session(account) as conn:
        info = conn.select(folder, readonly=True)
        uidv = int(info.get("uidvalidity") or 0)
        state = svc.db.get_folder_state(acc_id, folder)
        old_uidv = int(state["uidvalidity"] or 0) if state else 0
        if state is not None and uidv and old_uidv and uidv != old_uidv:
            # папку пересоздали (или почту перевезли): сверку по Message-ID делает копирование
            res["uidvalidity_changed"] = True
            _enqueue_backup(svc, acc_id)
            return res
        all_uids = conn.search_uids(info)
        present = set(all_uids)
        rows = svc.db.query("SELECT id, uid, flags, gone FROM messages WHERE account_id=? AND folder=? AND uidvalidity=?",
                            (acc_id, folder, uidv))
        by_uid = {int(r["uid"]): r for r in rows}
        gone_ids = [int(r["id"]) for r in rows if int(r["uid"]) not in present and not r["gone"]]
        back_ids = [int(r["id"]) for r in rows if int(r["uid"]) in present and r["gone"]]
        _mark_gone_ids(svc, gone_ids)
        _mark_gone_ids(svc, back_ids, gone=0)
        if uidv:
            svc.db.mark_messages_gone(acc_id, folder, uidv, [], other_uidvalidity=True)
        res["gone"] = len(gone_ids)
        known = svc.db.existing_uids(acc_id, folder, uidv)
        new = sorted(u for u in all_uids if u not in known)
        res["pending"] = len(new)
        running = backup_running(svc, acc_id)
        res["backup_running"] = running
        if new and download and not running:
            take = new[-SYNC_DOWNLOAD_MAX:]
            delim = _delimiter_of(svc, acc_id, folder)
            skip = int(svc.rt("backup", "skip_larger_than_mb") or 0) * 1024 * 1024
            keep_flags = bool(svc.rt("backup", "download_flags"))
            batch = []
            for msg in conn.fetch_messages(take, skip_larger_than=skip):
                if msg.get("error") is not None or msg.get("missing"):
                    continue
                raw = msg["raw"]
                flags = [f for f in msg["flags"] if f != "\\Recent"] if keep_flags else []
                rel, digest, size = svc.store.store_message(acc_id, folder, delim, msg["uid"], raw, flags=flags,
                                                            internaldate=msg["internaldate"])
                subject, from_addr, has_attach = BackupEngine._extract_meta(raw)  # noqa: SLF001
                batch.append((acc_id, folder, uidv, int(msg["uid"]), BackupEngine._extract_message_id(raw),  # noqa: SLF001
                              size, BackupEngine._epoch_to_iso(msg["internaldate"]), ",".join(flags), rel,  # noqa: SLF001
                              digest, subject[:500], from_addr[:300], 1 if has_attach else 0))
                if len(batch) >= 100:
                    svc.db.add_message_index_batch(batch)
                    batch.clear()
            if batch:
                svc.db.add_message_index_batch(batch)
            downloaded = len(svc.db.existing_uids(acc_id, folder, uidv) & set(take))
            res["new"] = downloaded
            res["pending"] = max(0, len(new) - downloaded)
        # флаги свежих писем (прочитаны и помечены на телефоне)
        recent = [u for u in sorted(present)[-max(0, int(flags_recent)):] if u in by_uid]
        if recent:
            res["flags"] = _sync_flags(svc, conn, by_uid, recent)
        try:
            unseen = len(conn.search(["UNSEEN"]))
        except MailArchiverError:
            unseen = None
        res["total"], res["unseen"] = len(all_uids), unseen
        if all_uids:
            svc.db.upsert_folder_state(acc_id, folder, uidv, max(all_uids), int(info.get("exists") or len(all_uids)))
    svc.db.execute("UPDATE webmail_folders SET total=?, unseen=?, updated_at=updated_at WHERE account_id=? AND name=?",
                   (res["total"], res["unseen"], acc_id, folder))
    return res


def _sync_flags(svc, conn: ImapConnection, by_uid: Dict[int, object], uids: List[int]) -> int:
    updates = []
    for start in range(0, len(uids), 500):
        part = uids[start:start + 500]
        try:
            data = conn.client.fetch(part, [b"FLAGS"])
        except Exception as exc:  # noqa: BLE001
            from .imap.client import _map_exception
            mapped = _map_exception(exc)
            if isinstance(mapped, (ImapConnectionError, ImapTimeoutError)):
                raise mapped from exc
            return 0
        for uid, item in (data or {}).items():
            row = by_uid.get(int(uid))
            if row is None:
                continue
            server = [f.decode("latin-1") if isinstance(f, bytes) else str(f) for f in (item.get(b"FLAGS") or ())]
            server = [f for f in server if f != "\\Recent"]
            if set(server) != set(_flags_of(row)):
                updates.append((int(row["id"]), server))
    _set_local_flags(svc, updates)
    return len(updates)


def _enqueue_backup(svc, account_id: int) -> Optional[int]:
    """Копирование ящика (сверку пересозданной папки) — если такое задание ещё не стоит в очереди."""
    row = svc.db.query_one("SELECT id FROM jobs WHERE account_id=? AND type=? AND status IN (?, ?) LIMIT 1",
                           (int(account_id), JobType.BACKUP, JobStatus.QUEUED, JobStatus.RUNNING))
    if row is not None:
        return int(row["id"])
    try:
        return svc.queue.enqueue(JobType.BACKUP, int(account_id), {}, priority=3, created_by="почтовый клиент")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось поставить копирование ящика %s: %s", account_id, exc)
        return None


# ===========================================================================
#  Список писем папки (архив): сортировка, отборы, письма, которых нет на сервере
# ===========================================================================
LIST_SORTS = {
    "date_desc": "m.internaldate DESC, m.id DESC",
    "date_asc": "m.internaldate ASC, m.id ASC",
    "from": "m.from_addr COLLATE NOCASE ASC, m.internaldate DESC, m.id DESC",
    "subject": "m.subject COLLATE NOCASE ASC, m.internaldate DESC, m.id DESC",
    "size": "m.size DESC, m.id DESC",
}
LIST_FILTERS = {
    "all": "",
    "unread": " AND (',' || COALESCE(m.flags, '') || ',') NOT LIKE '%,\\Seen,%'",
    "flagged": " AND (',' || COALESCE(m.flags, '') || ',') LIKE '%,\\Flagged,%'",
    "answered": " AND (',' || COALESCE(m.flags, '') || ',') LIKE '%,\\Answered,%'",
    "attach": " AND m.has_attach = 1",
}


def _list_cond(filt: str, show_gone: bool) -> Tuple[str, Tuple]:
    """Условие отбора писем списка: вид отбора («непрочитанные», «с меткой …») и письма, которых нет на сервере."""
    filt = str(filt or "all")
    args: Tuple = ()
    if filt.startswith("tag:"):
        cond = " AND instr(',' || COALESCE(m.flags, '') || ',', ?) > 0"
        args = (f",{_norm_flag(filt[4:])},",)
    else:
        cond = LIST_FILTERS.get(filt, "")
    if not show_gone:
        cond += " AND m.gone = 0"
    return cond, args


def list_messages(svc, account_id: int, folder: str, *, offset: int = 0, limit: int = 50, sort: str = "date_desc",
                  filt: str = "all", show_gone: bool = True) -> Dict:
    order = LIST_SORTS.get(sort, LIST_SORTS["date_desc"])
    cond, args = _list_cond(filt, show_gone)
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    where = f"m.account_id=? AND m.folder=?{cond}"
    rows = svc.db.query(
        f"SELECT m.id, m.folder, m.subject, m.from_addr, m.internaldate, m.size, m.has_attach, m.flags, m.gone, "
        f"m.stored_path, r.peer AS peer, r.dir AS dir FROM messages m LEFT JOIN reply_msgs r ON r.id = m.id "
        f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (account_id, folder, *args, limit, offset))
    if not cond:
        total = svc.db.count_folder_messages(account_id, folder)
    else:
        total = int(svc.db.scalar(f"SELECT COUNT(*) FROM messages m WHERE {where}", (account_id, folder, *args)) or 0)
    gone_total = int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND folder=? AND gone=1",
                                   (account_id, folder)) or 0)
    return {"rows": rows, "total": total, "gone": gone_total, "offset": offset, "limit": limit}


def list_ids(svc, account_id: int, folder: str, *, filt: str = "all", show_gone: bool = False,
             limit: int = MAX_ACTION_IDS) -> Dict:
    """Все письма папки (с тем же отбором) — для «Выбрать все письма папки»."""
    cond, args = _list_cond(filt, show_gone)
    where = f"m.account_id=? AND m.folder=?{cond}"
    ids = [int(r["id"]) for r in svc.db.query(
        f"SELECT m.id FROM messages m WHERE {where} ORDER BY m.internaldate DESC, m.id DESC LIMIT ?",
        (account_id, folder, *args, int(limit) + 1))]
    return {"ids": ids[:limit], "more": len(ids) > limit}


# ===========================================================================
#  HTML письма: очистка, текст, цитата
# ===========================================================================
_ALLOWED_TAGS = {"a", "b", "strong", "i", "em", "u", "s", "strike", "del", "br", "p", "div", "span", "ul", "ol", "li",
                 "blockquote", "h1", "h2", "h3", "h4", "pre", "code", "table", "thead", "tbody", "tfoot", "tr", "td",
                 "th", "img", "hr", "sub", "sup", "font", "small", "big", "center"}
_DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "head", "title", "svg",
                      "math", "select", "textarea", "button", "frameset", "frame", "applet", "audio", "video"}
_VOID_TAGS = {"br", "img", "hr"}
_STYLE_PROPS = {"color", "background-color", "font-weight", "font-style", "font-size", "font-family",
                "text-decoration", "text-align", "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
                "padding", "padding-left", "border-left", "border", "width", "max-width", "white-space"}
_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)


def _safe_url(value: str, *, image: bool = False) -> Optional[str]:
    v = (value or "").strip()
    low = re.sub(r"[\s\x00-\x1f]", "", v).lower()
    if image:
        if _DATA_IMAGE.match(v) or low.startswith(("cid:", "https://", "http://")):
            return v
        return None
    if low.startswith(("http://", "https://", "mailto:", "#")):
        return v
    return None


def _safe_style(value: str) -> str:
    out = []
    for decl in (value or "").split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop, val = prop.strip().lower(), val.strip()
        low = val.lower()
        if prop in _STYLE_PROPS and val and not any(x in low for x in ("url(", "expression", "javascript", "@import",
                                                                       "\\", "<", ">")):
            out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0
        self.stack: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            if tag not in _VOID_TAGS:
                self.skip += 1
            return
        if self.skip or tag not in _ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name.startswith("on"):
                continue
            if name == "href" and tag == "a":
                url = _safe_url(value)
                if url:
                    kept.append(("href", url))
            elif name == "src" and tag == "img":
                url = _safe_url(value, image=True)
                if url:
                    kept.append(("src", url))
            elif name == "style":
                st = _safe_style(value)
                if st:
                    kept.append(("style", st))
            elif name in ("alt", "title", "width", "height", "colspan", "rowspan", "align", "valign", "color",
                          "face", "size", "type", "border", "cellpadding", "cellspacing", "start"):
                kept.append((name, value[:300]))
        if tag == "a":
            kept += [("target", "_blank"), ("rel", "noopener noreferrer")]
        if tag == "img" and not any(n == "src" for n, _v in kept):
            return
        attr = "".join(f' {n}="{html_mod.escape(v, quote=True)}"' for n, v in kept)
        self.out.append(f"<{tag}{attr}>")
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS and self.stack and self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html_mod.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def sanitize_html(value: str) -> str:
    """HTML письма, написанного в клиенте: только безопасные теги и атрибуты (без скриптов, форм, фреймов)."""
    p = _Sanitizer()
    try:
        p.feed(str(value or ""))
        p.close()
    except Exception:  # noqa: BLE001
        return html_mod.escape(str(value or ""))
    return "".join(p.out)


class _TextOut(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre", "table", "ul", "ol", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.href: List[Optional[str]] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")
        if tag == "blockquote":
            self.parts.append("\n\x02\n")
        if tag == "a":
            self.href.append(dict(attrs).get("href"))
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[{alt}]")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "a" and self.href:
            href = self.href.pop()
            if href and href.startswith(("http://", "https://")) and not "".join(self.parts[-1:]).strip().endswith(href):
                self.parts.append(f" <{href}>")
        if tag == "blockquote":
            self.parts.append("\n\x03\n")
        if tag in self._BLOCK and tag not in ("br", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_text(value: str) -> str:
    """Текстовая часть письма из HTML редактора (multipart/alternative)."""
    p = _TextOut()
    try:
        p.feed(str(value or ""))
        p.close()
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", "", str(value or ""))
    out: List[str] = []
    depth = 0
    for raw in "".join(p.parts).split("\n"):
        if "\x02" in raw or "\x03" in raw:
            depth = max(0, depth + raw.count("\x02") - raw.count("\x03"))
            raw = raw.replace("\x02", "").replace("\x03", "")
            if not raw.strip():
                continue
        line = raw.strip()
        prefix = "> " * depth
        if not line:
            if out and out[-1].strip("> ") == "":
                continue
            out.append(prefix.rstrip())
            continue
        out.append(prefix + line)
    return "\n".join(out).strip("\n") + "\n"


def text_to_html(text: str) -> str:
    return html_mod.escape(str(text or "")).replace("\r\n", "\n").replace("\n", "<br>")


def _quote_block(head: str, text: str) -> str:
    return (f'<p><br></p><div>{html_mod.escape(head)}</div>'
            f'<blockquote type="cite" style="margin: 0 0 0 0.8ex; border-left: 2px solid #c8c8c8; '
            f'padding-left: 1ex">{text_to_html(text)}</blockquote>')


# ===========================================================================
#  Отправитель и сервер отправки (SMTP)
# ===========================================================================
#: Сервер IMAP → (SMTP-сервер, порт, защита). Порядок важен: первые совпадения — точнее.
_SMTP_KNOWN = (
    (r"(^|\.)gmail\.com$|(^|\.)googlemail\.com$", ("smtp.gmail.com", 465, "ssl")),
    (r"^outlook\.office365\.com$|^outlook\.office\.com$", ("smtp.office365.com", 587, "starttls")),
    (r"^imap-mail\.outlook\.com$", ("smtp-mail.outlook.com", 587, "starttls")),
    (r"(^|\.)yandex\.(ru|com|net)$", ("smtp.yandex.ru", 465, "ssl")),
    (r"(^|\.)mail\.ru$", ("smtp.mail.ru", 465, "ssl")),
    (r"(^|\.)rambler\.ru$", ("smtp.rambler.ru", 465, "ssl")),
    (r"(^|\.)zoho\.(com|eu)$", ("smtp.zoho.com", 465, "ssl")),
    (r"(^|\.)icloud\.com$|(^|\.)me\.com$", ("smtp.mail.me.com", 587, "starttls")),
)
#: Серверы, которые сами кладут отправленное по SMTP в «Отправленные»: копия от клиента — дубль.
_AUTO_SAVES_SENT = re.compile(r"(^|\.)(gmail|googlemail)\.com$|^smtp\.office365\.com$|^smtp-mail\.outlook\.com$", re.I)


def smtp_defaults(imap_host: str) -> Dict:
    host = (imap_host or "").strip().lower()
    for pattern, (h, port, sec) in _SMTP_KNOWN:
        if re.search(pattern, host):
            return {"host": h, "port": port, "security": sec}
    if host.startswith("imap."):
        return {"host": "smtp." + host[5:], "port": 587, "security": "starttls"}
    return {"host": host, "port": 587, "security": "starttls"}


def _identity_row(svc, account_id: int):
    return svc.db.query_one("SELECT * FROM webmail_identities WHERE account_id=?", (int(account_id),))


def _employee_email(svc, account: Account) -> str:
    if not account.employee_id:
        return ""
    row = svc.db.query_one("SELECT email FROM employees WHERE id=?", (int(account.employee_id),))
    return (row["email"] or "").strip() if row is not None else ""


def _employee_name(svc, account: Account) -> str:
    if not account.employee_id:
        return ""
    row = svc.db.query_one("SELECT full_name FROM employees WHERE id=?", (int(account.employee_id),))
    return (row["full_name"] or "").strip() if row is not None else ""


def get_identity(svc, account: Account) -> Dict:
    """Отправитель и сервер отправки ящика: сохранённое и то, что действует (с подобранными значениями)."""
    row = _identity_row(svc, int(account.id))
    get = (lambda k, d="": (row[k] if row is not None and row[k] is not None else d))
    defaults = smtp_defaults(account.host)
    address = get("address") or (account.username if "@" in (account.username or "") else "") or \
        _employee_email(svc, account)
    host = get("smtp_host") or defaults["host"]
    port = int(get("smtp_port", 0) or 0) or (defaults["port"] if not get("smtp_host") else
                                              (465 if get("smtp_security") == "ssl" else 587))
    security = get("smtp_security") or (defaults["security"] if not get("smtp_host") else "starttls")
    auth = get("smtp_auth", "account") or "account"
    save_sent = get("save_sent") or str(svc.rt("webmail", "save_sent") or "auto")
    aliases = [a for a in re.split(r"[\s,;]+", get("aliases") or "") if a]
    return {
        "account_id": int(account.id),
        "aliases": aliases,
        #: адреса, от которых можно отправлять: основной и псевдонимы
        "from_choices": [address] + [a for a in aliases if a.lower() != (address or "").lower()] if address else aliases,
        "display_name": get("display_name") or _employee_name(svc, account) or account.name,
        "address": address,
        "signature": get("signature"),
        "reply_to": get("reply_to"),
        "smtp_host": host, "smtp_port": port, "smtp_security": security, "smtp_auth": auth,
        "smtp_username": get("smtp_username"),
        "smtp_password_set": bool(get("smtp_password")),
        "save_sent": save_sent,
        "stored": {k: get(k) for k in ("display_name", "address", "smtp_host", "smtp_security", "save_sent")}
        | {"smtp_port": int(get("smtp_port", 0) or 0)},
        "auto": defaults,
    }


_IDENTITY_USER_FIELDS = ("display_name", "signature", "reply_to")
_IDENTITY_ADMIN_FIELDS = ("address", "smtp_host", "smtp_port", "smtp_security", "smtp_auth", "smtp_username",
                          "save_sent", "aliases")


def save_identity(svc, account: Account, data: Dict, *, admin: bool) -> Dict:
    """Сохранить отправителя (имя, подпись, «Ответ на») и — администратору — сервер отправки."""
    row = _identity_row(svc, int(account.id))
    cur = dict(row) if row is not None else {}
    vals = {k: cur.get(k, "") or "" for k in ("display_name", "address", "signature", "reply_to", "smtp_host",
                                             "smtp_security", "smtp_auth", "smtp_username", "smtp_password",
                                             "save_sent", "aliases")}
    vals["smtp_port"] = int(cur.get("smtp_port") or 0)
    if not vals["smtp_auth"]:
        vals["smtp_auth"] = "account"
    for key in _IDENTITY_USER_FIELDS:
        if key in data:
            vals[key] = str(data[key] or "")
    if admin:
        for key in _IDENTITY_ADMIN_FIELDS:
            if key in data:
                vals[key] = data[key]
        if "smtp_password" in data and data["smtp_password"] is not None:
            pw = str(data["smtp_password"])
            vals["smtp_password"] = svc.secret_box.encrypt(pw) if pw else ""
    vals["display_name"] = _one_line(vals["display_name"], 200)
    vals["reply_to"] = _one_line(vals["reply_to"], 300)
    if vals["reply_to"]:
        _addresses(vals["reply_to"], what="«Ответ на»", limit=5)
    vals["signature"] = sanitize_html(vals["signature"])
    if len(vals["signature"]) > 200_000:
        raise ValidationError("Подпись слишком большая (картинки в подписи — до 100 КБ).")
    vals["address"] = _one_line(str(vals["address"] or ""), 300).strip()
    if vals["address"] and not _EMAIL_RE.fullmatch(vals["address"]):
        raise ValidationError(f"Адрес отправителя «{vals['address']}» — не адрес электронной почты.")
    vals["smtp_host"] = _one_line(str(vals["smtp_host"] or ""), 255).strip().lower()
    if vals["smtp_host"] and not re.fullmatch(r"[a-z0-9.\-]+|\[[0-9a-f:.]+\]", vals["smtp_host"]):
        raise ValidationError(f"Сервер отправки «{vals['smtp_host']}» — не имя сервера.")
    try:
        vals["smtp_port"] = int(vals["smtp_port"] or 0)
    except (TypeError, ValueError):
        raise ValidationError("Порт сервера отправки — число от 1 до 65535.") from None
    if vals["smtp_port"] and not 1 <= vals["smtp_port"] <= 65535:
        raise ValidationError("Порт сервера отправки — число от 1 до 65535.")
    if vals["smtp_security"] not in ("", "ssl", "starttls", "none"):
        raise ValidationError("Защита соединения: ssl, starttls или none.")
    if vals["smtp_auth"] not in ("account", "custom", "none"):
        raise ValidationError("Вход на сервер отправки: account, custom или none.")
    if vals["save_sent"] not in ("", "auto", "always", "never"):
        raise ValidationError("Копия в «Отправленные»: auto, always или never.")
    vals["smtp_username"] = _one_line(str(vals["smtp_username"] or ""), 300)
    aliases = vals["aliases"]
    if isinstance(aliases, (list, tuple)):
        aliases = ",".join(str(a) for a in aliases)
    aliases = [a.strip() for a in re.split(r"[\s,;]+", str(aliases or "")) if a.strip()]
    for a in aliases:
        if not _EMAIL_RE.fullmatch(a):
            raise ValidationError(f"Псевдоним «{a}» — не адрес электронной почты.")
    if len(aliases) > 20:
        raise ValidationError("Псевдонимов — не больше 20.")
    vals["aliases"] = ", ".join(dict.fromkeys(aliases))
    svc.db.execute(
        "INSERT INTO webmail_identities(account_id, display_name, address, signature, reply_to, smtp_host, smtp_port, "
        "smtp_security, smtp_auth, smtp_username, smtp_password, save_sent, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET display_name=excluded.display_name, "
        "address=excluded.address, signature=excluded.signature, reply_to=excluded.reply_to, "
        "smtp_host=excluded.smtp_host, smtp_port=excluded.smtp_port, smtp_security=excluded.smtp_security, "
        "smtp_auth=excluded.smtp_auth, smtp_username=excluded.smtp_username, smtp_password=excluded.smtp_password, "
        "save_sent=excluded.save_sent, updated_at=excluded.updated_at",
        (int(account.id), vals["display_name"], vals["address"], vals["signature"], vals["reply_to"],
         vals["smtp_host"], vals["smtp_port"], vals["smtp_security"], vals["smtp_auth"], vals["smtp_username"],
         vals["smtp_password"], vals["save_sent"], utcnow_iso()))
    svc.db.execute("UPDATE webmail_identities SET aliases=? WHERE account_id=?", (vals["aliases"], int(account.id)))
    return get_identity(svc, account)


def _one_line(value: str, limit: int) -> str:
    return re.sub(r"[\r\n\x00]+", " ", str(value or ""))[:limit]


def send_problem(svc, account: Account) -> str:
    """Почему с ящика нельзя отправить письмо ('' — можно)."""
    ident = get_identity(svc, account)
    if not ident["address"]:
        return ("У ящика не известен адрес отправителя: логин не похож на адрес, у сотрудника адреса нет. "
                "Укажите адрес в настройках отправки ящика.")
    if not ident["smtp_host"]:
        return "Не указан сервер отправки (SMTP) — заполните настройки отправки ящика."
    auth = ident["smtp_auth"]
    if auth == "custom":
        row = _identity_row(svc, int(account.id))
        if not (row is not None and row["smtp_username"] and row["smtp_password"]):
            return "Для сервера отправки выбран отдельный вход, а логин или пароль не заданы."
        return ""
    if auth == "none":
        return ""
    if account.auth_type in (AuthType.PASSWORD, AuthType.AXIGEN_FTP):
        if account.secret_broken:
            return "Пароль ящика не расшифровывается текущим ключом — введите его заново в карточке ящика."
        if not account.password:
            return "У ящика нет пароля — задайте его в карточке ящика или отдельный вход на сервер отправки."
        return ""
    if account.auth_type == AuthType.OAUTH2:
        return "" if account.oauth_refresh_token else "У ящика нет токена OAuth2 — переподключите ящик."
    if account.auth_type == AuthType.SERVER:
        return ""      # токен приложения (Microsoft 365, Google) проверяется при отправке
    return ("Ящик копируется входом администратора почты: своего пароля у архива нет. Для отправки укажите в "
            "настройках отправки отдельный вход на сервер отправки (или ретранслятор без входа).")


def _smtp_credentials(svc, account: Account, ident: Dict) -> Tuple[str, str, str]:
    """(способ, логин, секрет): способ — login | xoauth2 | none."""
    auth = ident["smtp_auth"]
    if auth == "none":
        return "none", "", ""
    if auth == "custom":
        row = _identity_row(svc, int(account.id))
        try:
            secret = svc.secret_box.decrypt(row["smtp_password"]) if row is not None and row["smtp_password"] else ""
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Пароль сервера отправки не расшифровывается текущим ключом — введите его заново.",
                                  cause=exc) from exc
        return "login", (row["smtp_username"] if row is not None else ""), secret
    problem = send_problem(svc, account)
    if problem:
        raise ValidationError(problem)
    if account.auth_type == AuthType.OAUTH2:
        from .imap.oauth import refresh_access_token
        access, _exp, new_refresh = refresh_access_token(
            account.oauth_token_url, account.oauth_client_id, account.oauth_client_secret,
            account.oauth_refresh_token, timeout=30, with_refresh=True)
        if new_refresh and new_refresh != account.oauth_refresh_token:
            svc.db.set_account_oauth_refresh(int(account.id), new_refresh)
        return "xoauth2", account.username, access
    if account.auth_type == AuthType.SERVER:
        auth_info = svc.server_imap_auth(account)
        if getattr(auth_info, "mech", "") == "xoauth2":
            return "xoauth2", auth_info.user or account.username, auth_info.secret
        raise ValidationError(
            "Ящик копируется входом администратора почтового сервера: пароль администратора на сервер отправки "
            "архив не передаёт.", hint="Укажите в настройках отправки ящика отдельный вход на сервер отправки или "
                                       "ретранслятор без входа.")
    return "login", account.username, account.password


#: Подмена соединения SMTP для тестов: функция (host, port, security, timeout, context) → объект smtplib.SMTP.
SMTP_CONNECT = None


def _smtp_connect(host: str, port: int, security: str, timeout: int, context: ssl.SSLContext):
    if SMTP_CONNECT is not None:
        return SMTP_CONNECT(host, port, security, timeout, context)
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    server = smtplib.SMTP(host, port, timeout=timeout)
    if security == "starttls":
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    return server


def _smtp_error(exc: BaseException, host: str, port: int) -> MailArchiverError:
    def reply(e) -> str:
        code = getattr(e, "smtp_code", "")
        msg = getattr(e, "smtp_error", b"")
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        return f"{code} {msg}".strip()
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return MailArchiverError(
            f"Сервер отправки {host} не принял вход: {reply(exc)}", code="smtp_auth",
            hint="Проверьте пароль ящика (или вход в настройках отправки). Gmail и Яндекс требуют пароль приложения; "
                 "в Microsoft 365 у ящика должна быть включена отправка SMTP AUTH, а у приложения OAuth — право "
                 "SMTP.Send.", cause=exc)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        bad = ", ".join(f"{a} ({c} {m.decode('utf-8', 'replace') if isinstance(m, bytes) else m})"
                        for a, (c, m) in list(exc.recipients.items())[:5])
        return MailArchiverError(f"Сервер отправки отказался принять адресатов: {bad}", code="smtp_rcpt",
                                 hint="Проверьте адреса получателей.", cause=exc)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return MailArchiverError(f"Сервер отправки не принял отправителя {exc.sender}: {reply(exc)}", code="smtp_sender",
                                 hint="Адрес отправителя должен принадлежать ящику (или ящику должно быть разрешено "
                                      "отправлять от этого адреса).", cause=exc)
    if isinstance(exc, smtplib.SMTPDataError):
        return MailArchiverError(f"Сервер отправки отклонил письмо: {reply(exc)}", code="smtp_data",
                                 hint="Частая причина — размер письма больше разрешённого сервером.", cause=exc)
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return MailArchiverError(f"Сервер отправки {host}:{port} не поддерживает нужную команду: {exc}",
                                 code="smtp_unsupported",
                                 hint="Проверьте защиту соединения в настройках отправки (SSL — порт 465, STARTTLS — "
                                      "587).", cause=exc)
    if isinstance(exc, smtplib.SMTPResponseException):
        return MailArchiverError(f"Сервер отправки ответил ошибкой: {reply(exc)}", code="smtp_error", cause=exc)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return MailArchiverError(f"Сервер отправки {host}:{port} не ответил вовремя.", code="smtp_timeout",
                                 retryable=True, hint="Проверьте сервер и порт отправки и доступ к нему из сети "
                                                      "архива.", cause=exc)
    if isinstance(exc, ssl.SSLError):
        return MailArchiverError(f"Не удалось установить защищённое соединение с {host}:{port}: {exc}", code="smtp_tls",
                                 hint="Проверьте защиту соединения (SSL — порт 465, STARTTLS — 587) и сертификат "
                                      "сервера.", cause=exc)
    return MailArchiverError(f"Не удалось связаться с сервером отправки {host}:{port}: {exc}", code="smtp_connect",
                             retryable=True, hint="Проверьте сервер и порт отправки в настройках отправки ящика и "
                                                  "доступ к нему из сети архива.", cause=exc)


def _smtp_send(svc, account: Account, ident: Dict, sender: str, rcpts: List[str], data: bytes) -> Dict[str, tuple]:
    mech, user, secret = _smtp_credentials(svc, account, ident)
    host, port, security = ident["smtp_host"], int(ident["smtp_port"]), ident["smtp_security"]
    context = ssl.create_default_context()
    if not bool(svc.rt("security", "imap_ssl_verify")):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if mech != "none" and security == "none" and not _is_local(host):
        raise ValidationError(
            f"Пароль не отправляется на сервер {host} без шифрования.",
            hint="Выберите в настройках отправки SSL или STARTTLS — или ретранслятор без входа.")
    server = None
    try:
        server = _smtp_connect(host, port, security, 30, context)
        if mech == "login":
            smtp_login(server, user, secret)
        elif mech == "xoauth2":
            token = f"user={user}\x01auth=Bearer {secret}\x01\x01"
            server.auth("XOAUTH2", lambda challenge=None: token if challenge is None else "", initial_response_ok=True)
        refused = server.sendmail(sender, rcpts, data)
        return dict(refused or {})
    except MailArchiverError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _smtp_error(exc, host, port) from exc
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                try:
                    server.close()
                except Exception:  # noqa: BLE001
                    pass


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


# ===========================================================================
#  Адреса
# ===========================================================================
_EMAIL_RE = re.compile(r"[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+|[^@\s<>,;\"']+@localhost")


def _addresses(value: str, *, what: str = "Кому", limit: int = MAX_RECIPIENTS) -> List[Tuple[str, str]]:
    text = str(value or "")
    if re.search(r"[\r\n\x00]", text):
        raise ValidationError(f"В поле «{what}» перевод строки — так нельзя.")
    pairs = []
    # пустые части («a@b, , c@d» — так бывает после подсказки адресов) строгий разбор Python 3.13 считает
    # ошибкой и не возвращает ни одного адреса: убираем их заранее
    text = ",".join(x for x in text.replace(";", ",").split(",") if x.strip())
    for name, addr in getaddresses([text]):
        addr = (addr or "").strip()
        if not addr and not (name or "").strip():
            continue
        if not _EMAIL_RE.fullmatch(addr):
            raise ValidationError(f"«{(name + ' ' + addr).strip() or text.strip()}» в поле «{what}» — не адрес "
                                  f"электронной почты.")
        pairs.append(((name or "").strip(), addr))
    if len(pairs) > limit:
        raise ValidationError(f"В поле «{what}» больше {limit} адресов.")
    return pairs


def display_addr(name: str, addr: str) -> str:
    """«Имя <адрес>» без кодирования RFC 2047 — для полей письма в интерфейсе (кодирует при отправке email)."""
    name = re.sub(r"[\r\n\x00]+", " ", str(name or "")).strip()
    if not name or name.lower() == (addr or "").lower():
        return addr
    if re.search(r'[,;:<>@"()\[\]\\]', name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _fmt_addrs(pairs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(display_addr(n, a) for n, a in pairs)


def own_addresses(svc, account: Account) -> set:
    own = {(account.username or "").lower()}
    ident = get_identity(svc, account)
    if ident["address"]:
        own.add(ident["address"].lower())
    own.update(a.lower() for a in ident.get("aliases") or [])
    if account.employee_id:
        row = svc.db.query_one("SELECT email, known_logins FROM employees WHERE id=?", (int(account.employee_id),))
        if row is not None:
            if row["email"]:
                own.add(row["email"].strip().lower())
            try:
                own.update(str(x).lower() for x in json.loads(row["known_logins"] or "[]"))
            except (TypeError, ValueError):
                pass
    return {x for x in own if "@" in x}


def contacts(svc, account: Account, q: str, limit: int = 10) -> List[Dict]:
    """Подсказка адресов: сотрудники, отправители писем ящика, адресаты его писем."""
    q = (q or "").strip().lower()
    from .webmail_tools import book_suggest
    book = book_suggest(svc, int(account.id), q, limit)
    if len(q) < 2:
        return book
    out: Dict[str, Dict] = {}
    for r in svc.db.query("SELECT full_name, email FROM employees WHERE email IS NOT NULL AND email<>'' "
                          "AND status<>'dismissed' LIMIT 20000"):
        name, email = (r["full_name"] or "").strip(), (r["email"] or "").strip()
        if q in name.lower() or q in email.lower():
            out.setdefault(email.lower(), {"name": name, "email": email, "count": 1000})
        if len(out) >= limit:
            break
    like = f"%{q}%"
    for r in svc.db.query("SELECT MIN(from_addr) AS a, COUNT(*) AS n FROM messages WHERE account_id=? AND from_addr LIKE ? "
                          "GROUP BY lower(from_addr) ORDER BY n DESC LIMIT 20", (int(account.id), like)):
        a = (r["a"] or "").strip()
        if "@" in a:
            out.setdefault(a.lower(), {"name": "", "email": a, "count": int(r["n"])})
    for r in svc.db.query("SELECT peer AS a, COUNT(*) AS n FROM reply_msgs WHERE account_id=? AND dir=2 AND peer LIKE ? "
                          "GROUP BY peer ORDER BY n DESC LIMIT 20", (int(account.id), like)):
        a = (r["a"] or "").strip()
        if "@" in a:
            item = out.setdefault(a.lower(), {"name": "", "email": a, "count": 0})
            item["count"] += int(r["n"])
    known = {b["email"].lower() for b in book if b.get("email")}
    items = [x for x in sorted(out.values(), key=lambda x: (-x["count"], x["email"])) if x["email"].lower() not in known]
    for it in items:
        it["label"] = display_addr(it["name"], it["email"])
        it.pop("count", None)
    return (book + items)[:limit]


# ===========================================================================
#  Цепочка переписки
# ===========================================================================
def thread(svc, account_id: int, pk: int, limit: int = 60) -> List[Dict]:
    """Письма той же переписки в ящике (по индексу ответов: Message-ID, In-Reply-To, References)."""
    row = svc.db.query_one("SELECT mid, root, parent FROM reply_msgs WHERE id=? AND account_id=?", (pk, account_id))
    if row is None:
        return []
    key = row["root"] or row["mid"]
    if not key:
        return []
    ids = [r["id"] for r in svc.db.query(
        "SELECT id FROM reply_msgs WHERE account_id=? AND (root=? OR mid=?) AND (dir<>0 OR id=?) LIMIT ?",
        (account_id, key, key, pk, int(limit) * 3))]
    if len(ids) <= 1:
        return []
    rows = list(svc.db.messages_by_ids(account_id, ids))
    seen_mid: Dict[str, Dict] = {}
    out = []
    for r in sorted(rows, key=lambda x: (x["internaldate"] or "", x["id"])):
        mid = (r["message_id"] or f"#{r['id']}").strip()
        if mid in seen_mid and int(r["id"]) != pk:
            continue
        item = {"id": int(r["id"]), "folder": r["folder"], "subject": r["subject"] or "", "from": r["from_addr"] or "",
                "date": r["internaldate"], "gone": bool(r["gone"]),
                "seen": "\\Seen" in _flags_of(r), "current": int(r["id"]) == pk}
        if mid in seen_mid:
            out.remove(seen_mid[mid])
        seen_mid[mid] = item
        out.append(item)
    return out[-limit:]


# ===========================================================================
#  Черновики
# ===========================================================================
DRAFT_FIELDS = ("to_addrs", "cc_addrs", "bcc_addrs", "subject", "body", "from_addr", "priority", "receipt",
                "smime_sign", "smime_encrypt")
_BOOL_FIELDS = ("priority", "receipt", "smime_sign", "smime_encrypt")


def _drafts_root(svc) -> str:
    return os.path.join(svc.cfg.data_dir, "webmail", "drafts")


def _draft_dir(svc, draft_id: int) -> str:
    return os.path.join(_drafts_root(svc), str(int(draft_id)))


def _draft_dict(row) -> Dict:
    d = {k: row[k] for k in row.keys()}
    try:
        d["attachments"] = json.loads(row["attachments"] or "[]")
    except (TypeError, ValueError):
        d["attachments"] = []
    for a in d["attachments"]:
        a.pop("file", None)
    return d


#: Текст письма (HTML с картинками, вставленными в текст) — не длиннее, знаков.
MAX_BODY_CHARS = 12_000_000


def _clean_body(value: str) -> str:
    body = sanitize_html(value or "")
    if len(body) > MAX_BODY_CHARS:
        raise ValidationError("Текст письма слишком большой — скорее всего, из-за картинок, вставленных в текст.",
                              hint="Крупные картинки прикрепите файлами («📎 Прикрепить»).")
    return body


def get_draft_row(svc, draft_id: int, owner: str):
    row = svc.db.query_one("SELECT * FROM webmail_drafts WHERE id=?", (int(draft_id),))
    if row is None or row["owner"] != owner:
        raise ValidationError("Черновик не найден.", code="not_found")
    return row


def list_drafts(svc, owner: str, account_ids: Optional[Sequence[int]] = None) -> List[Dict]:
    purge_drafts(svc)
    cond, args = "owner=? AND status<>'sent'", [owner]
    if account_ids is not None:
        ids = [int(i) for i in account_ids] or [-1]
        cond += f" AND account_id IN ({','.join('?' * len(ids))})"
        args += ids
    rows = svc.db.query(f"SELECT * FROM webmail_drafts WHERE {cond} ORDER BY updated_at DESC LIMIT 200", tuple(args))
    return [_draft_dict(r) for r in rows]


def create_draft(svc, account: Account, owner: str, fields: Optional[Dict] = None) -> Dict:
    f = dict(fields or {})
    now = utcnow_iso()
    cur = svc.db.execute(
        "INSERT INTO webmail_drafts(account_id, owner, mode, ref_id, server_draft_id, to_addrs, cc_addrs, bcc_addrs, "
        "subject, body, in_reply_to, refs, attachments, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(account.id), owner, f.get("mode") or "new", f.get("ref_id"), f.get("server_draft_id"),
         _one_line(f.get("to_addrs", ""), 5000), _one_line(f.get("cc_addrs", ""), 5000),
         _one_line(f.get("bcc_addrs", ""), 5000), _one_line(f.get("subject", ""), MAX_SUBJECT),
         _clean_body(f.get("body", "")), _one_line(f.get("in_reply_to", ""), 1000),
         _one_line(f.get("refs", ""), 8000), json.dumps(f.get("attachments") or [], ensure_ascii=False), now, now))
    return _draft_dict(get_draft_row(svc, int(cur.lastrowid), owner))


def _not_editable(row) -> None:
    if row["status"] == "scheduled":
        raise ValidationError("Письмо запланировано к отправке — сначала отмените отправку.")
    if row["status"] != "draft":
        raise ValidationError("Письмо уже отправляется — изменить его нельзя.")


def update_draft(svc, draft_id: int, owner: str, fields: Dict) -> Dict:
    row = get_draft_row(svc, draft_id, owner)
    _not_editable(row)
    vals = {}
    for key in DRAFT_FIELDS:
        if key in fields:
            v = fields[key]
            if key == "body":
                vals[key] = _clean_body(v)
            elif key in _BOOL_FIELDS:
                vals[key] = 1 if v else 0
            else:
                vals[key] = _one_line(v, MAX_SUBJECT if key == "subject" else 300 if key == "from_addr" else 5000)
    if fields.get("account_id") and int(fields["account_id"]) != int(row["account_id"]):
        vals["account_id"] = int(fields["account_id"])
    if not vals:
        return _draft_dict(row)
    vals["updated_at"] = utcnow_iso()
    sets = ", ".join(f"{k}=?" for k in vals)
    svc.db.execute(f"UPDATE webmail_drafts SET {sets} WHERE id=?", (*vals.values(), int(draft_id)))
    return _draft_dict(get_draft_row(svc, draft_id, owner))


def delete_draft(svc, draft_id: int, owner: str) -> None:
    row = get_draft_row(svc, draft_id, owner)
    if "server_copy" in row.keys() and row["server_copy"]:
        acc = svc.db.get_account(int(row["account_id"]))
        if acc is not None:
            from .webmail_more import drop_server_copy
            drop_server_copy(svc, acc, row)
    svc.db.execute("DELETE FROM webmail_drafts WHERE id=?", (int(draft_id),))
    shutil.rmtree(_draft_dir(svc, draft_id), ignore_errors=True)


def purge_drafts(svc) -> int:
    """Черновики, которых не касались ``webmail.drafts_keep_days`` дней, и записи отправленных — удалить."""
    days = max(1, int(svc.rt("webmail", "drafts_keep_days") or 30))
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
    sent_cutoff = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
    rows = svc.db.query("SELECT id FROM webmail_drafts WHERE (status='draft' AND updated_at < ?) "
                        "OR (status='sent' AND COALESCE(sent_at, updated_at) < ?) "
                        "OR (status='sending' AND updated_at < ?)", (cutoff, sent_cutoff, sent_cutoff))
    for r in rows:
        svc.db.execute("DELETE FROM webmail_drafts WHERE id=?", (r["id"],))
        shutil.rmtree(_draft_dir(svc, r["id"]), ignore_errors=True)
    svc.db.execute("DELETE FROM webmail_send_log WHERE at < ?", (sent_cutoff,))
    # каталоги черновиков, записи которых уже нет (удалён ящик)
    root = _drafts_root(svc)
    if os.path.isdir(root):
        known = {str(r["id"]) for r in svc.db.query("SELECT id FROM webmail_drafts")}
        for name in os.listdir(root):
            if name not in known and name.isdigit():
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return len(rows)


def _attachments_of(row) -> List[Dict]:
    try:
        return list(json.loads(row["attachments"] or "[]"))
    except (TypeError, ValueError):
        return []


def _save_attachments(svc, draft_id: int, items: List[Dict]) -> None:
    svc.db.execute("UPDATE webmail_drafts SET attachments=?, updated_at=? WHERE id=?",
                   (json.dumps(items, ensure_ascii=False), utcnow_iso(), int(draft_id)))


def _safe_filename(name: str) -> str:
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "file"
    return name[:200]


def add_attachment(svc, draft_id: int, owner: str, filename: str, ctype: str, fileobj) -> Dict:
    row = get_draft_row(svc, draft_id, owner)
    _not_editable(row)
    limit = int(svc.rt("webmail", "max_attachments_mb") or 25) * 1024 * 1024
    items = _attachments_of(row)
    used = sum(int(a.get("size") or 0) for a in items)
    data = fileobj.read(limit - used + 1)
    if used + len(data) > limit:
        raise ValidationError(f"Вложения письма больше {limit // (1024 * 1024)} МБ.",
                              hint="Предел — «Настройки → Почтовый клиент → Вложения письма, МБ».")
    folder = ensure_dir(_draft_dir(svc, draft_id), 0o700)
    n = max([int(a.get("n") or 0) for a in items] + [0]) + 1
    path = os.path.join(folder, f"{n}.bin")
    blob = data
    encrypted = False
    if svc.store.encrypt and svc.store.cipher is not None:
        blob = svc.store.cipher.encrypt(data)
        encrypted = True
    with open(path, "wb") as fh:
        fh.write(blob)
    os.chmod(path, 0o600)
    ctype = (ctype or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.+\-]+/[a-z0-9.+\-]+", ctype):
        ctype = "application/octet-stream"
    item = {"n": n, "name": _safe_filename(filename), "ctype": ctype, "size": len(data), "file": f"{n}.bin",
            "enc": encrypted}
    items.append(item)
    _save_attachments(svc, draft_id, items)
    return {k: v for k, v in item.items() if k not in ("file", "enc")}


def remove_attachment(svc, draft_id: int, owner: str, n: int) -> None:
    row = get_draft_row(svc, draft_id, owner)
    _not_editable(row)
    items = _attachments_of(row)
    keep = [a for a in items if int(a.get("n") or 0) != int(n)]
    for a in items:
        if int(a.get("n") or 0) == int(n) and a.get("file"):
            try:
                os.remove(os.path.join(_draft_dir(svc, draft_id), a["file"]))
            except OSError:
                pass
    _save_attachments(svc, draft_id, keep)


def _attachment_bytes(svc, account: Account, draft_id: int, item: Dict) -> bytes:
    if item.get("eml"):
        # письмо из архива целиком — «Переслать как вложение»
        row = svc.db.get_message(int((item.get("eml") or {}).get("id") or 0))
        if row is None or int(row["account_id"]) != int(account.id):
            raise ValidationError(f"Письмо «{item.get('name')}» больше не найдено в архиве.")
        return svc.store.read_message(int(account.id), row["stored_path"])
    if item.get("file"):
        with open(os.path.join(_draft_dir(svc, draft_id), item["file"]), "rb") as fh:
            blob = fh.read()
        if item.get("enc"):
            blob = svc.store._need_cipher().decrypt(blob)  # noqa: SLF001
        return blob
    src = item.get("from") or {}
    row = svc.db.get_message(int(src.get("id") or 0))
    if row is None or int(row["account_id"]) != int(account.id):
        raise ValidationError(f"Вложение «{item.get('name')}» взято из письма, которого больше нет в архиве.")
    from .mailview import attachment_from_source
    size = svc.store.message_size(int(account.id), row["stored_path"])
    att = attachment_from_source(lambda: svc.store.open_message(int(account.id), row["stored_path"]), size,
                                 int(src.get("index") or 0))
    if att is None:
        raise ValidationError(f"Вложение «{item.get('name')}» не найдено в исходном письме.")
    _name, _ctype, chunks, _length = att
    return b"".join(chunks)


# ===========================================================================
#  Ответ, ответ всем, пересылка, продолжение черновика с сервера
# ===========================================================================
def _orig_headers(svc, account: Account, row) -> Tuple[object, Dict]:
    from .mailview import summarize_source
    stored = row["stored_path"]
    size = svc.store.message_size(int(account.id), stored)
    with svc.store.open_message(int(account.id), stored) as fh:
        head = fh.read(256 * 1024)
    m = re.search(rb"\r?\n\r?\n", head)
    hdrs = BytesHeaderParser(policy=policy.default).parsebytes(head[:m.end()] if m else head)
    parsed = summarize_source(lambda: svc.store.open_message(int(account.id), stored), size)
    return hdrs, parsed


def _h(hdrs, name: str) -> str:
    try:
        v = hdrs.get(name)
    except Exception:  # noqa: BLE001
        v = None
    return decode_mime_header(str(v)) if v is not None else ""


def _prefixed(subject: str, prefix: str) -> str:
    s = (subject or "").strip()
    if re.match(rf"^\s*{re.escape(prefix)}\s*:", s, re.I):
        return s
    if prefix.lower() == "re" and re.match(r"^\s*(re|отв|aw|sv)\s*(\[\d+\])?\s*:", s, re.I):
        return s
    if prefix.lower() == "fwd" and re.match(r"^\s*(fwd?|fw|пересл)\s*:", s, re.I):
        return s
    return f"{prefix}: {s}" if s else f"{prefix}:"


def _plain_of(parsed: Dict) -> str:
    text = parsed.get("text") or ""
    if not text.strip() and parsed.get("html"):
        text = html_to_text(parsed["html"])
    return text.strip()


def _eml_name(subject: str, n: int) -> str:
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", subject or "").strip()[:80] or f"Письмо {n}"
    return f"{base}.eml"


def forward_attached_fields(svc, account: Account, ref_ids: Sequence[int]) -> Dict:
    """«Переслать как вложение»: исходные письма целиком — файлами .eml."""
    ids = list(dict.fromkeys(int(i) for i in ref_ids))[:50]
    rows = [r for r in svc.db.messages_by_ids(int(account.id), ids)] if ids else []
    if not rows:
        raise ValidationError("Письма не найдены в архиве.", code="not_found")
    by_id = {int(r["id"]): r for r in rows}
    atts, names = [], []
    for i, pk in enumerate(ids):
        r = by_id.get(pk)
        if r is None:
            continue
        names.append(r["subject"] or "")
        atts.append({"n": len(atts) + 1, "name": _eml_name(r["subject"], i + 1), "ctype": "message/rfc822",
                     "size": int(r["size"] or 0), "eml": {"id": pk}})
    subject = _prefixed(names[0], "Fwd") if len(names) == 1 else f"Fwd: писем {len(names)}"
    return {"mode": "forward", "ref_id": ids[0] if len(ids) == 1 else None, "to_addrs": "", "subject": subject,
            "body": "", "in_reply_to": "", "refs": "", "attachments": atts}


def compose_fields(svc, account: Account, mode: str, ref_id: Optional[int],
                   ref_ids: Optional[Sequence[int]] = None) -> Dict:
    """Поля нового письма: ответ, ответ всем, пересылка или продолжение черновика с сервера."""
    if mode == "forward_attach":
        return forward_attached_fields(svc, account, list(ref_ids or []) or ([int(ref_id)] if ref_id else []))
    if mode == "new" or not ref_id:
        return {"mode": "new"}
    row = svc.db.get_message(int(ref_id))
    if row is None or int(row["account_id"]) != int(account.id):
        raise ValidationError("Письмо не найдено в архиве.", code="not_found")
    hdrs, parsed = _orig_headers(svc, account, row)
    subject = _h(hdrs, "Subject")
    sender = _h(hdrs, "From")
    date = _h(hdrs, "Date")
    msgid = _h(hdrs, "Message-ID").strip()
    refs = " ".join(_h(hdrs, "References").split())
    own = own_addresses(svc, account)
    text = _plain_of(parsed)
    if mode == "edit":
        body = sanitize_html(parsed.get("html") or "") if (parsed.get("html") or "").strip() else text_to_html(text)
        atts = [{"n": i + 1, "name": a["filename"], "ctype": a.get("content_type") or "application/octet-stream",
                 "size": int(a.get("size") or 0), "from": {"id": int(row["id"]), "index": int(a["index"])}}
                for i, a in enumerate(parsed.get("attachments") or []) if not a.get("inline")]
        return {"mode": "edit", "ref_id": None, "server_draft_id": int(row["id"]), "to_addrs": _h(hdrs, "To"),
                "cc_addrs": _h(hdrs, "Cc"), "bcc_addrs": _h(hdrs, "Bcc"), "subject": subject, "body": body,
                "in_reply_to": _h(hdrs, "In-Reply-To"), "refs": refs, "attachments": atts}
    if mode in ("reply", "reply_all"):
        reply_to = _h(hdrs, "Reply-To") or sender
        to_pairs = [(n, a) for n, a in getaddresses([reply_to]) if a]
        if all(a.lower() in own for _n, a in to_pairs):
            # отвечаем на своё письмо (из «Отправленных») — адресатам этого письма
            to_pairs = [(n, a) for n, a in getaddresses([_h(hdrs, "To")]) if a]
        cc_pairs: List[Tuple[str, str]] = []
        if mode == "reply_all":
            seen = {a.lower() for _n, a in to_pairs} | own
            for n, a in getaddresses([_h(hdrs, "To"), _h(hdrs, "Cc")]):
                if a and a.lower() not in seen:
                    cc_pairs.append((n, a))
                    seen.add(a.lower())
        new_refs = " ".join(x for x in (refs.split() + ([msgid] if msgid else []))[-20:])
        head = f"{_short_date(date)}, {sender} пишет:" if date else f"{sender} пишет:"
        return {"mode": mode, "ref_id": int(row["id"]), "to_addrs": _fmt_addrs(to_pairs),
                "cc_addrs": _fmt_addrs(cc_pairs), "subject": _prefixed(subject, "Re"),
                "body": _quote_block(head, text), "in_reply_to": msgid, "refs": new_refs, "attachments": []}
    if mode == "forward":
        lines = ["-------- Пересылаемое сообщение --------", f"Тема: {subject}", f"Дата: {date}", f"От: {sender}",
                 f"Кому: {_h(hdrs, 'To')}"]
        if _h(hdrs, "Cc"):
            lines.append(f"Копия: {_h(hdrs, 'Cc')}")
        body = ('<p><br></p><div>' + "<br>".join(html_mod.escape(x) for x in lines) + '</div><p><br></p>'
                + text_to_html(text))
        atts = [{"n": i + 1, "name": a["filename"], "ctype": a.get("content_type") or "application/octet-stream",
                 "size": int(a.get("size") or 0), "from": {"id": int(row["id"]), "index": int(a["index"])}}
                for i, a in enumerate(parsed.get("attachments") or []) if not a.get("inline")]
        return {"mode": "forward", "ref_id": int(row["id"]), "to_addrs": "", "subject": _prefixed(subject, "Fwd"),
                "body": body, "in_reply_to": "", "refs": "", "attachments": atts}
    raise ValidationError(f"Неизвестный вид письма: {mode}.")


def _short_date(value: str) -> str:
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(value)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return value


# ===========================================================================
#  Отправка
# ===========================================================================
_DATA_IMG_TAG = re.compile(r'(<img\b[^>]*?\bsrc=")(data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+))(")', re.I)


def _draft_addrs(value: str, what: str, as_draft: bool) -> List[Tuple[str, str]]:
    if not as_draft:
        return _addresses(value, what=what)
    try:
        return _addresses(value, what=what)
    except ValidationError:          # недописанный адрес в черновике — не повод не сохранить черновик
        return [(n, a) for n, a in getaddresses([value or ""]) if a and "@" in a][:MAX_RECIPIENTS]


def build_message(svc, account: Account, draft, ident: Dict, *, as_draft: bool = False
                  ) -> Tuple[EmailMessage, List[str], List[Dict]]:
    """Письмо из черновика: заголовки, текст и HTML, вложения. → (письмо, все адресаты, вложения).

    ``as_draft`` — для папки «Черновики» сервера: получателей может не быть, «Скрытая копия» — в заголовке.
    """
    to = _draft_addrs(draft["to_addrs"], "Кому", as_draft)
    cc = _draft_addrs(draft["cc_addrs"], "Копия", as_draft)
    bcc = _draft_addrs(draft["bcc_addrs"], "Скрытая копия", as_draft)
    rcpts = [a for _n, a in to + cc + bcc]
    if not rcpts and not as_draft:
        raise ValidationError("Укажите хотя бы одного получателя.")
    if len(rcpts) > MAX_RECIPIENTS:
        raise ValidationError(f"Получателей больше {MAX_RECIPIENTS}.")
    sender = draft_sender(ident, draft)
    domain = sender.split("@", 1)[1] if "@" in sender else None
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = display_addr(ident["display_name"], sender)
    if to:
        msg["To"] = _fmt_addrs(to)
    if cc:
        msg["Cc"] = _fmt_addrs(cc)
    if as_draft and bcc:
        msg["Bcc"] = _fmt_addrs(bcc)
    if ident.get("reply_to"):
        msg["Reply-To"] = ident["reply_to"]
    msg["Subject"] = draft["subject"] or ""
    msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
    msg["Message-ID"] = make_msgid(domain=domain)
    if draft["in_reply_to"]:
        msg["In-Reply-To"] = draft["in_reply_to"]
    if draft["refs"]:
        msg["References"] = draft["refs"]
    keys = draft.keys() if hasattr(draft, "keys") else ()
    if "priority" in keys and draft["priority"]:
        msg["X-Priority"] = "1 (Highest)"
        msg["Importance"] = "High"
    if "receipt" in keys and draft["receipt"]:
        msg["Disposition-Notification-To"] = sender
    from .version import __version__
    msg["User-Agent"] = f"MailArchiver/{__version__}"
    body = sanitize_html(draft["body"] or "")
    inline: List[Tuple[str, str, bytes]] = []

    def repl(m):
        try:
            data = base64.b64decode(re.sub(r"\s+", "", m.group(4)), validate=True)
        except Exception:  # noqa: BLE001
            return m.group(0)
        cid = make_msgid(domain=domain)[1:-1]
        sub = m.group(3).lower().replace("jpg", "jpeg")
        inline.append((cid, sub, data))
        return f"{m.group(1)}cid:{cid}{m.group(5)}"
    body = _DATA_IMG_TAG.sub(repl, body)
    html_doc = (f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{body}</body></html>')
    msg.set_content(html_to_text(body) if body.strip() else "\n")
    msg.add_alternative(html_doc, subtype="html")
    if inline:
        html_part = msg.get_payload()[1]
        for cid, sub, data in inline:
            html_part.add_related(data, maintype="image", subtype=sub, cid=f"<{cid}>")
    limit = int(svc.rt("webmail", "max_attachments_mb") or 25) * 1024 * 1024
    total = 0
    atts = _attachments_of(draft)
    for item in atts:
        data = _attachment_bytes(svc, account, int(draft["id"]), item)
        total += len(data)
        if total > limit:
            raise ValidationError(f"Вложения письма больше {limit // (1024 * 1024)} МБ.")
        if item.get("eml") or (item.get("ctype") or "").lower() == "message/rfc822":
            msg.add_attachment(message_from_bytes(data, policy=policy.default), filename=item.get("name") or "message.eml")
            continue
        maintype, _sep, subtype = (item.get("ctype") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                           filename=item.get("name") or "file")
    return msg, rcpts, atts


def draft_sender(ident: Dict, draft) -> str:
    """Адрес отправителя письма: выбранный псевдоним (если он из разрешённых) или основной адрес."""
    keys = draft.keys() if hasattr(draft, "keys") else ()
    chosen = (draft["from_addr"] if "from_addr" in keys else "") or ""
    for a in ident.get("from_choices") or []:
        if chosen and a.lower() == chosen.strip().lower():
            return a
    return ident["address"]


def _save_policy(ident: Dict) -> bool:
    mode = ident.get("save_sent") or "auto"
    if mode == "never":
        return False
    if mode == "always":
        return True
    return not _AUTO_SAVES_SENT.search(ident.get("smtp_host") or "")


class PresendWarning(ValidationError):
    """Перед отправкой есть предупреждения: письмо уйдёт, если пользователь подтвердит (1.20.0)."""
    code = "presend_warn"

    def __init__(self, items: List[Dict]) -> None:
        super().__init__("; ".join(i["text"] for i in items)[:1000])
        self.items = items


def check_send_limit(svc, account: Account, count: int, *, actor: str = "") -> None:
    """Предел отправки: адресатов в час с одного ящика (``webmail.send_limit_hour``, 1.21.0).

    Украденная сессия сотрудника не превратится в рассылку с его адреса: превышение — отказ,
    запись в аудите и уведомление администратору (раз в час на ящик).
    """
    try:
        limit = int(svc.rt("webmail", "send_limit_hour") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    used = int(svc.db.scalar("SELECT COALESCE(SUM(rcpts), 0) FROM webmail_send_log WHERE account_id=? AND at>=?",
                             (int(account.id), since)) or 0)
    if used + int(count) <= limit:
        return
    last = svc.db.scalar("SELECT MAX(ts) FROM audit WHERE action='webmail_send_limit' AND detail LIKE ? AND ts>=?",
                         (f"ящик «{account.name}»%", since))
    if not last:
        svc.db.add_audit(actor or "system", "webmail_send_limit",
                         f"ящик «{account.name}»: за час {used} адресатов, письмо ещё на {count} — отправка остановлена "
                         f"(предел {limit})")
        notifier = getattr(svc, "notifier", None)
        if notifier is not None:
            try:
                notifier.send_all(f"MailArchiver: предел отправки ящика «{account.name}»",
                                  f"С ящика «{account.name}» за последний час отправлено писем на {used} адресатов; "
                                  f"новое письмо ещё на {count} не отправлено (предел {limit} в час, "
                                  f"«Настройки → Почтовый клиент»). Если это не рассылка сотрудника — смените "
                                  f"пароль ящика и закройте его сессии.")
            except Exception as exc:  # noqa: BLE001 - уведомление не должно мешать ответу
                log.warning("Уведомление о пределе отправки не ушло: %s", exc)
    raise ValidationError(f"С этого ящика за последний час отправлено писем на {used} адресатов — предел {limit} в "
                          f"час. Письмо не отправлено.", code="send_limit",
                          hint="Подождите или попросите администратора поднять предел (Настройки → Почтовый клиент).")


def _log_send(svc, account: Account, count: int) -> None:
    try:
        svc.db.execute("INSERT INTO webmail_send_log(account_id, at, rcpts) VALUES(?,?,?)",
                       (int(account.id), utcnow_iso(), int(count)))
    except Exception as exc:  # noqa: BLE001 - журнал предела не должен мешать отправке
        log.debug("Журнал отправки не записан: %s", exc)


def check_before_send(svc, account: Account, row, msg_rcpts: Sequence[str], atts: Sequence[Dict], *,
                      confirmed: bool, actor: str = "") -> List[Dict]:
    """Проверки перед отправкой: запрет — ошибка, предупреждения — PresendWarning (если не подтверждены)."""
    from . import webmail_secure as wsec
    from .attachtext import kind_of
    files: List[Tuple[str, str, bytes]] = []
    for item in atts:
        name, ctype = item.get("name") or "", item.get("ctype") or ""
        if not kind_of(name, ctype) or int(item.get("size") or 0) > 10 * 1024 * 1024:
            continue
        try:
            files.append((name, ctype, _attachment_bytes(svc, account, int(row["id"]), item)))
        except (MailArchiverError, OSError):
            continue
    items = wsec.presend_checks(svc, account, row, msg_rcpts, files)
    blocks = [i for i in items if i["level"] == "block"]
    if blocks:
        raise ValidationError(blocks[0]["text"], code="presend_block",
                              hint="Уберите эти данные из письма или отправьте его только адресатам организации.")
    if items and not confirmed:
        raise PresendWarning(items)
    if items and actor and any(i["code"] == "sensitive" for i in items):
        svc.db.add_audit(actor, "webmail_dlp_override",
                         f"ящик «{account.name}»: «{_one_line(row['subject'], 120)}» — "
                         + "; ".join(i["text"] for i in items if i["code"] == "sensitive")[:600])
    return items


def send(svc, account: Account, draft_id: int, owner: str, *, actor: str = "", confirmed: bool = True) -> Dict:
    """Отправить черновик: SMTP, копия в «Отправленные» (и в архив), отметка «Отвечено» у исходного письма.

    ``confirmed=False`` (кнопка «Отправить» в клиенте) — предупреждения проверки перед отправкой возвращаются
    пользователю (:class:`PresendWarning`); запреты (``webmail.dlp_mode = block_external``) действуют всегда.
    """
    row = get_draft_row(svc, draft_id, owner)
    if int(row["account_id"]) != int(account.id):
        raise ValidationError("Черновик относится к другому ящику.")
    cur = svc.db.execute("UPDATE webmail_drafts SET status='sending', error='', updated_at=? "
                         "WHERE id=? AND status IN ('draft', 'scheduled')", (utcnow_iso(), int(draft_id)))
    if not int(cur.rowcount or 0):
        raise ValidationError("Это письмо уже отправляется или отправлено.")
    try:
        ident = get_identity(svc, account)
        problem = send_problem(svc, account)
        if problem:
            raise ValidationError(problem)
        msg, rcpts, _atts = build_message(svc, account, row, ident)
        check_before_send(svc, account, row, rcpts, _atts, confirmed=confirmed, actor=actor)
        check_send_limit(svc, account, len(rcpts), actor=actor)
        keys = row.keys()
        sign = bool(row["smime_sign"]) if "smime_sign" in keys else False
        encrypt = bool(row["smime_encrypt"]) if "smime_encrypt" in keys else False
        sender = draft_sender(ident, row)
        if sign or encrypt:
            from .webmail_secure import protect
            bcc = [a.lower() for _n, a in _draft_addrs(row["bcc_addrs"], "Скрытая копия", False)] if encrypt else []
            main = [r for r in rcpts if r.lower() not in set(bcc)]
            # Зашифрованное письмо перечисляет сертификаты всех получателей — скрытые адресаты
            # получают отдельные копии, зашифрованные только для них (1.21.0).
            copies = [(addr, protect(svc, account, msg, [addr], sender, sign=sign, encrypt=encrypt)) for addr in bcc]
            data = protect(svc, account, msg, main, sender, sign=sign, encrypt=encrypt)
            refused = _smtp_send(svc, account, ident, sender, main, data) if main else {}
            for addr, copy in copies:
                refused.update(_smtp_send(svc, account, ident, sender, [addr], copy))
        else:
            data = msg.as_bytes()
            refused = _smtp_send(svc, account, ident, sender, rcpts, data)
        _log_send(svc, account, len(rcpts))
    except BaseException as exc:
        text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
        if isinstance(exc, PresendWarning):
            text = ""                          # не ошибка: ждём подтверждения пользователя
        svc.db.execute("UPDATE webmail_drafts SET status='draft', send_at=NULL, error=?, updated_at=? WHERE id=?",
                       (text[:1000], utcnow_iso(), int(draft_id)))
        raise
    message_id = str(msg["Message-ID"])
    now = utcnow_iso()
    svc.db.execute("UPDATE webmail_drafts SET status='sent', sent_at=?, updated_at=?, message_id=?, error='' "
                   "WHERE id=?", (now, now, message_id, int(draft_id)))
    warnings: List[str] = []
    if refused:
        warnings.append("Не приняты адресаты: " + ", ".join(list(refused)[:10]))
    saved_to = None
    if _save_policy(ident):
        try:
            saved_to = _save_sent_copy(svc, account, data)
            if not saved_to:
                warnings.append("Папка «Отправленные» на сервере не найдена — копия письма не сохранена.")
        except MailArchiverError as exc:
            warnings.append(f"Копия в «Отправленные» не сохранена: {exc.message}")
    if row["ref_id"] and row["mode"] in ("reply", "reply_all", "forward"):
        try:
            set_flags(svc, account, [int(row["ref_id"])],
                      add=["\\Answered"] if row["mode"] != "forward" else ["$Forwarded"])
        except (MailArchiverError, ValidationError) as exc:
            log.info("Отметка «Отвечено» не поставлена: %s", exc)
    if row["server_draft_id"]:
        try:
            delete(svc, account, [int(row["server_draft_id"])], permanent=True)
        except (MailArchiverError, ValidationError) as exc:
            warnings.append(f"Черновик на сервере не удалён: {exc.message}")
    if actor:
        shown = ", ".join(rcpts[:5]) + (f" и ещё {len(rcpts) - 5}" if len(rcpts) > 5 else "")
        svc.db.add_audit(actor, "webmail_send",
                         f"ящик «{account.name}» ({draft_sender(ident, row)}): «{_one_line(row['subject'], 120)}» "
                         f"→ {shown}")
    if "server_copy" in row.keys() and row["server_copy"]:
        from .webmail_more import drop_server_copy
        drop_server_copy(svc, account, row)
    svc.db.execute("DELETE FROM webmail_drafts WHERE id=?", (int(draft_id),))
    shutil.rmtree(_draft_dir(svc, draft_id), ignore_errors=True)
    return {"ok": True, "message_id": message_id, "recipients": len(rcpts), "saved_to": saved_to,
            "warnings": warnings}


def _save_sent_copy(svc, account: Account, data: bytes) -> Optional[str]:
    """Положить отправленное письмо в «Отправленные» на сервере и сразу — в архив."""
    special = special_folders(svc, account)
    sent = special.get("sent")
    if not sent:
        return None
    from .imap.backup import BackupEngine
    from .imap.client import _map_exception
    now = datetime.now(timezone.utc)
    with pool(svc).session(account) as conn:
        _untagged(conn).pop("APPENDUID", None)
        try:
            resp = conn.client.append(sent, data, flags=["\\Seen"], msg_time=now)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        uidv, uid = parse_appenduid(resp)
        if not uid:
            for item in list(_untagged(conn).pop("APPENDUID", None) or []):
                uidv, uid = parse_appenduid(b"APPENDUID " + (item if isinstance(item, bytes) else str(item).encode()))
                if uid:
                    break
    if uid and uidv:
        try:
            delim = _delimiter_of(svc, int(account.id), sent)
            rel, digest, size = svc.store.store_message(int(account.id), sent, delim, uid, data, flags=["\\Seen"],
                                                        internaldate=now.timestamp())
            subject, from_addr, has_attach = BackupEngine._extract_meta(data)  # noqa: SLF001
            mid = BackupEngine._extract_message_id(data)  # noqa: SLF001
            svc.db.add_message_index_batch([(int(account.id), sent, uidv, uid, mid, size, now.isoformat(), "\\Seen",
                                             rel, digest, subject[:500], from_addr[:300], 1 if has_attach else 0)])
        except MailArchiverError as exc:
            log.warning("Отправленное письмо не сохранено в архив: %s", exc)
    _touch_counts(svc, account, [sent])
    return sent
