"""Почтовый клиент (1.19.0): мгновенная почта (IMAP IDLE), беседы, сохранённые поиски, «Архивировать»,
подлинность отправителя и фишинг, S/MIME, правила и автоответ на почтовом сервере (Sieve), черновики в папке
сервера, квота ящика.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import webmail as wm
from . import webmail_tools as wt
from .errors import ImapAuthError, MailArchiverError, ValidationError
from .logging_setup import get_logger
from .models import Account, AuthType
from .util import utcnow_iso

log = get_logger("webmail")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webmail_searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    q           TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'account',
    attach      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_webmail_searches_owner ON webmail_searches(owner, name);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ===========================================================================
#  Мгновенная почта: IMAP IDLE
# ===========================================================================
#: IDLE переподаётся раньше 29 минут (RFC 2177); проверка ответа сервера — раз в столько секунд.
PUSH_IDLE_RENEW_S = 25 * 60
PUSH_CHECK_S = 30
#: Клиент не спрашивал столько секунд — соединение IDLE закрывается.
PUSH_LINGER_S = 180
#: Одновременных соединений IDLE на службу.
PUSH_MAX = 200

_PUSH_LOCK = threading.Lock()
_VERSIONS: Dict[int, int] = {}
_WATCHERS: Dict[Tuple[int, int], "_Watcher"] = {}


def version(account_id: int) -> int:
    return _VERSIONS.get(int(account_id), 0)


def bump(account_id: int) -> int:
    with _PUSH_LOCK:
        _VERSIONS[int(account_id)] = _VERSIONS.get(int(account_id), 0) + 1
        return _VERSIONS[int(account_id)]


class _Watcher:
    """Отдельное соединение с ящиком в режиме IDLE: сервер сам сообщает о новом письме во «Входящих»."""

    def __init__(self, svc, account_id: int) -> None:
        self.svc, self.account_id = svc, int(account_id)
        self.wanted = time.monotonic()
        self.status = "starting"
        self.error = ""
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"webmail-idle-{account_id}", daemon=True)

    def alive(self) -> bool:
        return self.thread.is_alive() and not self.stop.is_set()

    def _usable(self):
        acc = self.svc.db.get_account(self.account_id)
        if acc is None or not acc.enabled or acc.login_status == "auth_error" or acc.secret_broken:
            return None
        if not bool(self.svc.rt("webmail", "enabled")) or not bool(self.svc.rt("webmail", "push")):
            return None
        return acc

    def _changed(self, acc: Account, inbox: str) -> None:
        try:
            if not wm.backup_running(self.svc, self.account_id):
                wm.sync_folder(self.svc, acc, inbox)
                wt.process_new(self.svc, acc)
        except (MailArchiverError, ValidationError) as exc:
            log.info("Новая почта ящика «%s» не скачана: %s", acc.name, exc.message)
        bump(self.account_id)

    def _run(self) -> None:
        from .imap.client import ImapConnection
        try:
            while not self.stop.is_set() and time.monotonic() - self.wanted < PUSH_LINGER_S:
                acc = self._usable()
                if acc is None:
                    self.status = "off"
                    return
                conn = None
                try:
                    conn = ImapConnection(acc, wm.pool(self.svc)._options())  # noqa: SLF001
                    conn.connect()
                    if not conn.has_capability("IDLE"):
                        self.status = "unsupported"
                        return
                    inbox = wt._inbox(self.svc, acc)  # noqa: SLF001
                    conn.select(inbox, readonly=True)
                    conn.client.idle()
                    self.status, self.error = "idle", ""
                    started = time.monotonic()
                    while not self.stop.is_set() and time.monotonic() - self.wanted < PUSH_LINGER_S:
                        resp = conn.client.idle_check(timeout=PUSH_CHECK_S)
                        hit = any(isinstance(r, tuple) and len(r) > 1 and
                                  (r[1] if isinstance(r[1], bytes) else str(r[1]).encode()).upper()
                                  in (b"EXISTS", b"EXPUNGE", b"FETCH", b"RECENT") for r in resp or ())
                        renew = time.monotonic() - started > PUSH_IDLE_RENEW_S
                        if hit or renew:
                            conn.client.idle_done()
                            if hit:
                                self._changed(acc, inbox)
                            conn.client.idle()
                            started = time.monotonic()
                    try:
                        conn.client.idle_done()
                    except Exception:  # noqa: BLE001
                        pass
                except ImapAuthError as exc:
                    self.status, self.error = "error", exc.message
                    return
                except Exception as exc:  # noqa: BLE001
                    self.status = "error"
                    self.error = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
                    log.info("IDLE ящика #%s прерван: %s", self.account_id, self.error)
                    if self.stop.wait(30):
                        return
                finally:
                    if conn is not None:
                        try:
                            conn.close(force=True)
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            with _PUSH_LOCK:
                key = (id(self.svc), self.account_id)
                if _WATCHERS.get(key) is self:
                    _WATCHERS.pop(key, None)
            if self.status in ("idle", "starting"):
                self.status = "stopped"


def want_push(svc, account: Account) -> Dict:
    """Клиент ждёт новую почту ящика: держать соединение IDLE (запустить, если его нет)."""
    if not bool(svc.rt("webmail", "push")):
        return {"push": "off"}
    key = (id(svc), int(account.id))
    with _PUSH_LOCK:
        w = _WATCHERS.get(key)
        if w is not None and w.alive():
            w.wanted = time.monotonic()
            return {"push": w.status, "error": w.error}
        if w is not None and w.status == "unsupported":
            return {"push": "unsupported"}
        if len([x for x in _WATCHERS.values() if x.alive()]) >= PUSH_MAX:
            return {"push": "limit"}
        w = _WATCHERS[key] = _Watcher(svc, int(account.id))
        w.thread.start()
    return {"push": w.status}


def push_status(svc, account_id: int) -> Dict:
    w = _WATCHERS.get((id(svc), int(account_id)))
    return {"push": w.status if w is not None else "none", "error": w.error if w is not None else ""}


def stop_push(svc=None) -> None:
    with _PUSH_LOCK:
        items = [(k, w) for k, w in _WATCHERS.items() if svc is None or k[0] == id(svc)]
    for _k, w in items:
        w.stop.set()


# ===========================================================================
#  Беседы: письма одной переписки — одной строкой списка
# ===========================================================================
_TKEY = "COALESCE(NULLIF(r.root, ''), NULLIF(r.mid, ''), 'id:' || m.id)"


def list_threads(svc, account_id: int, folder: str, *, offset: int = 0, limit: int = 50, filt: str = "all",
                 show_gone: bool = False) -> Dict:
    cond, args = wm._list_cond(filt, show_gone)  # noqa: SLF001
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    where = f"m.account_id=? AND m.folder=?{cond}"
    base = (f"FROM messages m LEFT JOIN reply_msgs r ON r.id = m.id WHERE {where}")
    groups = svc.db.query(
        f"SELECT {_TKEY} AS tkey, MAX(m.internaldate) AS last, COUNT(*) AS n, "
        f"SUM(CASE WHEN (',' || COALESCE(m.flags, '') || ',') LIKE '%,\\Seen,%' THEN 0 ELSE 1 END) AS unread "
        f"{base} GROUP BY tkey ORDER BY last DESC LIMIT ? OFFSET ?", (account_id, folder, *args, limit, offset))
    total = int(svc.db.scalar(f"SELECT COUNT(DISTINCT {_TKEY}) {base}", (account_id, folder, *args)) or 0)
    keys = [g["tkey"] for g in groups]
    members: Dict[str, List] = {}
    if keys:
        marks = ",".join("?" * len(keys))
        for r in svc.db.query(
                f"SELECT m.id, m.folder, m.subject, m.from_addr, m.internaldate, m.size, m.has_attach, m.flags, m.gone, "
                f"m.stored_path, r.peer AS peer, r.dir AS dir, {_TKEY} AS tkey {base} AND {_TKEY} IN ({marks}) "
                f"ORDER BY m.internaldate DESC, m.id DESC", (account_id, folder, *args, *keys)):
            members.setdefault(r["tkey"], []).append(r)
    out = []
    for g in groups:
        rows = members.get(g["tkey"]) or []
        if not rows:
            continue
        out.append({"row": rows[0], "ids": [int(r["id"]) for r in rows], "count": int(g["n"]),
                    "unread": int(g["unread"] or 0),
                    "senders": list(dict.fromkeys((r["from_addr"] or "").split("@")[0] for r in rows))[:4]})
    return {"threads": out, "total": total, "offset": offset, "limit": limit}


# ===========================================================================
#  Сохранённые поиски (свои у каждого пользователя)
# ===========================================================================
def list_searches(svc, owner: str) -> List[Dict]:
    return [dict(r) | {"attach": bool(r["attach"])} for r in svc.db.query(
        "SELECT id, name, q, scope, attach FROM webmail_searches WHERE owner=? ORDER BY name COLLATE NOCASE", (owner,))]


def save_search(svc, owner: str, data: Dict) -> Dict:
    name = wt._clip(data.get("name"), 80)  # noqa: SLF001
    q = str(data.get("q") or "").strip()[:500]
    if not q:
        raise ValidationError("Пустой поиск сохранять незачем.")
    if int(svc.db.scalar("SELECT COUNT(*) FROM webmail_searches WHERE owner=?", (owner,)) or 0) >= 100:
        raise ValidationError("Сохранённых поисков — не больше 100.")
    scope = "all" if data.get("scope") == "all" else "account"
    cur = svc.db.execute("INSERT INTO webmail_searches(owner, name, q, scope, attach, created_at) VALUES(?,?,?,?,?,?)",
                         (owner, name or q[:80], q, scope, 1 if data.get("attach") else 0, utcnow_iso()))
    return next(s for s in list_searches(svc, owner) if s["id"] == int(cur.lastrowid))


def delete_search(svc, owner: str, search_id: int) -> None:
    svc.db.execute("DELETE FROM webmail_searches WHERE id=? AND owner=?", (int(search_id), owner))


# ===========================================================================
#  «Архивировать»
# ===========================================================================
ARCHIVE_FOLDER = "Архив"


def archive(svc, account: Account, ids: Iterable[int], *, actor: str = "") -> Dict:
    """Убрать письма из «Входящих» в папку «Архив» сервера (нет её — создать)."""
    dest = wm.special_folders(svc, account).get("archive")
    if not dest:
        names = {r["name"] for r in wm._folder_rows(svc, int(account.id))}  # noqa: SLF001
        dest = next((n for n in names if n.lower() in ("archive", "архив")), "") or \
            wm.create_folder(svc, account, ARCHIVE_FOLDER, "")["folder"]
    return wm.move(svc, account, ids, dest, actor=actor, audit_action="webmail_archive")


# ===========================================================================
#  Подлинность отправителя и фишинг
# ===========================================================================
_ORG_CACHE: Dict[int, Tuple[float, frozenset]] = {}


def org_domains(svc) -> frozenset:
    """Свои домены: домены ящиков и адресов сотрудников (кроме общедоступных) и replies.internal_domains."""
    hit = _ORG_CACHE.get(id(svc))
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    from .replies import PUBLIC_DOMAINS
    doms = set()
    for r in svc.db.query("SELECT username FROM accounts"):
        u = (r["username"] or "").strip().lower()
        if "@" in u:
            doms.add(u.rsplit("@", 1)[1])
    for r in svc.db.query("SELECT email FROM employees WHERE email IS NOT NULL AND email<>''"):
        doms.add(r["email"].strip().lower().rsplit("@", 1)[-1])
    doms = {d for d in doms if d and d not in PUBLIC_DOMAINS}
    doms |= {str(d).strip().lower().lstrip("@") for d in (svc.rt("replies", "internal_domains") or []) if str(d).strip()}
    out = frozenset(doms)
    _ORG_CACHE[id(svc)] = (time.monotonic(), out)
    return out


def _org(domain: str) -> str:
    """Организационный домен (упрощённо: два последних уровня, для com.ru/co.uk и т. п. — три)."""
    parts = [p for p in (domain or "").lower().strip(".").split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("com", "co", "org", "net", "gov", "ac", "edu", "msk", "spb"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _aligned(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _org(a) == _org(b)


_AUTH_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)([^;]*)", re.I)


def parse_auth(hdrs) -> Dict:
    """Результаты проверки SPF, DKIM и DMARC, которые записал принимающий сервер (верхний Authentication-Results)."""
    res: Dict = {"spf": "", "dkim": "", "dmarc": "", "dkim_domains": [], "spf_domain": "", "source": ""}
    try:
        all_ar = hdrs.get_all("Authentication-Results") or []
    except Exception:  # noqa: BLE001
        all_ar = []
    if all_ar:
        text = " ".join(str(all_ar[0]).split())
        res["source"] = text.split(";", 1)[0].strip()[:120]
        for kind, verdict, rest in _AUTH_RE.findall(text):
            kind, verdict = kind.lower(), verdict.lower()
            if kind == "dkim":
                m = re.search(r"header\.(?:d|i)\s*=\s*@?([\w.\-]+)", rest, re.I)
                if verdict == "pass" and m:
                    res["dkim_domains"].append(m.group(1).lower())
                if res["dkim"] != "pass":
                    res["dkim"] = verdict
            elif kind == "spf":
                res["spf"] = res["spf"] or verdict
                m = re.search(r"smtp\.mailfrom\s*=\s*(?:[^@\s;]*@)?([\w.\-]+)", rest, re.I)
                if m:
                    res["spf_domain"] = m.group(1).lower()
            else:
                res["dmarc"] = res["dmarc"] or verdict
    if not res["spf"]:
        rs = str(hdrs.get("Received-SPF") or "").strip().lower()
        if rs:
            res["spf"] = rs.split()[0].strip("(;") if rs.split() else ""
    return res


def _skeleton(domain: str) -> str:
    """Домен для сравнения «на глаз»: похожие буквы и цифры — одинаковые (rn→m, 0→o, 1→l, кириллица→латиница)."""
    d = unicodedata.normalize("NFKC", domain.lower())
    table = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
                           "х": "x", "у": "y", "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "і": "i", "ј": "j",
                           "ӏ": "l", "_": "-"})
    d = d.translate(table).replace("rn", "m").replace("vv", "w").replace("cl", "d")
    return d.replace("i", "l")


def _distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _lookalike(domain: str, org: frozenset) -> str:
    """Домен похож на свой, но не свой → какой свой он изображает ('' — не похож)."""
    if not domain or domain in org or any(domain.endswith("." + o) for o in org):
        return ""
    try:
        shown = domain.encode("ascii").decode("idna") if "xn--" in domain else domain
    except (UnicodeError, ValueError):
        shown = domain
    base = _org(shown)
    for o in org:
        if o in shown:                 # company.ru.evil.com — свой домен в начале чужого
            return o
        ob = _org(o)
        if base == ob:
            continue
        if _skeleton(base) == _skeleton(ob):
            return o
        name, oname = base.split(".")[0], ob.split(".")[0]
        if len(oname) >= 5 and (_distance(base, ob) <= 1 or (_distance(name, oname) <= 1 and name != oname)):
            return o
        if len(oname) >= 4 and oname in base.replace("-", "") and base != ob:
            return o
    return ""


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return " ".join(sorted(re.findall(r"[\w]+", s)))


def sender_check(svc, account: Account, row, hdrs) -> Dict:
    """Насколько можно верить отправителю: проверки сервера, «внешний», подозрительные признаки."""
    from_raw = wt._h(hdrs, "From")  # noqa: SLF001
    name, addr = parseaddr(from_raw)
    addr = addr.strip().lower()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    org = org_domains(svc)
    own = wm.own_addresses(svc, account)
    auth = parse_auth(hdrs)
    verdict = "none"
    if auth["dmarc"] == "pass" or any(_aligned(d, domain) for d in auth["dkim_domains"]):
        verdict = "pass"
    elif auth["dmarc"] in ("fail", "quarantine", "reject") or (auth["spf"] in ("fail", "softfail") and
                                                            auth["dkim"] != "pass" and auth["dmarc"] != "pass"):
        verdict = "fail"
    elif auth["spf"] == "pass" and _aligned(auth["spf_domain"] or domain, domain):
        verdict = "pass"
    auth["verdict"] = verdict
    external = bool(domain) and bool(org) and domain not in org and not any(domain.endswith("." + o) for o in org)
    warnings: List[Dict] = []
    if verdict == "fail":
        warnings.append({"level": "danger", "code": "auth_fail",
                         "text": f"Почтовый сервер не подтвердил, что письмо отправлено с {domain} "
                                 f"(SPF {auth['spf'] or '—'}, DKIM {auth['dkim'] or '—'}, DMARC {auth['dmarc'] or '—'}). "
                                 f"Отправитель может быть поддельным."})
    shown_addrs = re.findall(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+", name or "")
    if any(a.lower() != addr for a in shown_addrs):
        warnings.append({"level": "danger", "code": "display_spoof",
                         "text": f"В имени отправителя написан адрес {shown_addrs[0]}, а настоящий адрес — {addr}."})
    look = _lookalike(domain, org)
    if look:
        warnings.append({"level": "danger", "code": "lookalike",
                         "text": f"Домен отправителя {domain} похож на ваш {look}, но это другой домен."})
    if external and name.strip():
        key = _norm_name(name)
        if key and len(key) > 5:
            for r in svc.db.query("SELECT full_name, email FROM employees WHERE full_name IS NOT NULL AND full_name<>'' "
                                  "AND status<>'dismissed' LIMIT 20000"):
                if _norm_name(r["full_name"]) == key:
                    warnings.append({"level": "warn", "code": "employee_name",
                                     "text": f"Имя отправителя совпадает с сотрудником «{r['full_name']}», но письмо — "
                                             f"с внешнего адреса {addr}."})
                    break
    rt_name, rt = parseaddr(wt._h(hdrs, "Reply-To"))  # noqa: SLF001
    rt = rt.strip().lower()
    if rt and "@" in rt and rt not in own:
        rt_dom = rt.rsplit("@", 1)[1]
        if domain and not _aligned(rt_dom, domain) and rt_dom not in org:
            warnings.append({"level": "warn", "code": "reply_to",
                             "text": f"Ответ уйдёт не отправителю, а на {rt} (другой домен)."})
    first = False
    if external and addr:
        n = int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND lower(from_addr)=? AND id<>?",
                              (int(account.id), addr, int(row["id"]))) or 0)
        first = n == 0
    return {"auth": auth, "external": external, "warnings": warnings, "first_time": first, "address": addr,
            "domain": domain, "report_to": str(svc.rt("webmail", "phishing_report_to") or "").strip()}


def report_phishing(svc, account: Account, ids: Sequence[int], *, actor: str = "") -> Dict:
    """«Сообщить о фишинге»: письма — вложением на адрес службы безопасности (если задан) и в «Спам»."""
    ids = [int(i) for i in ids][:20]
    rows = list(svc.db.messages_by_ids(int(account.id), ids))
    if not rows:
        raise ValidationError("Письма не найдены.")
    to = str(svc.rt("webmail", "phishing_report_to") or "").strip()
    reported = False
    if to:
        problem = wm.send_problem(svc, account)
        if problem:
            raise ValidationError(f"Сообщить не получится: {problem}")
        ident = wm.get_identity(svc, account)
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = wm.display_addr(ident["display_name"], ident["address"])
        msg["To"] = to
        msg["Subject"] = "Подозрительное письмо: " + ((rows[0]["subject"] or "без темы") if len(rows) == 1
                                                      else f"писем {len(rows)}")
        msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
        domain = ident["address"].split("@", 1)[1] if "@" in ident["address"] else None
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(f"Ящик «{account.name}» ({ident['address']}) сообщает о подозрительном письме. "
                        f"Исходные письма — во вложении целиком, со всеми заголовками.\n")
        for i, r in enumerate(rows, 1):
            raw = svc.store.read_message(int(account.id), r["stored_path"])
            msg.add_attachment(message_from_bytes(raw, policy=policy.default),
                               filename=wm._eml_name(r["subject"] or "", i))  # noqa: SLF001
        wm._smtp_send(svc, account, ident, ident["address"], [to], msg.as_bytes())  # noqa: SLF001
        reported = True
    moved = 0
    if wm.special_folders(svc, account).get("junk"):
        moved = int(wm.junk(svc, account, ids, spam=True).get("moved") or 0)
    if actor:
        svc.db.add_audit(actor, "webmail_phish", f"ящик «{account.name}»: писем {len(rows)}"
                                                 + (f", сообщено на {to}" if reported else ""))
    return {"reported": reported, "to": to, "moved": moved}


# ===========================================================================
#  S/MIME: подписанные и зашифрованные письма
# ===========================================================================
def _smime_kind(raw_head: bytes) -> str:
    head = raw_head.lower()
    m = re.search(rb"(?im)^content-type:\s*([^;\r\n]+)((?:;[^\r\n]*|\r?\n[ \t][^\r\n]*)*)", raw_head)
    if not m:
        return ""
    ctype = m.group(1).strip().lower()
    params = m.group(2).lower()
    if ctype == b"multipart/signed" and b"pkcs7-signature" in params:
        return "signed"
    if ctype in (b"application/pkcs7-mime", b"application/x-pkcs7-mime"):
        if b"enveloped-data" in params or b"smime.p7m" in head[:4096] and b"signed-data" not in params:
            return "encrypted"
        return "signed_opaque"
    return ""


def _openssl() -> str:
    return shutil.which("openssl") or ""


def smime_status(svc, account: Account, row) -> Optional[Dict]:
    """Подпись S/MIME: проверяется программой openssl (подпись и цепочка сертификатов до доверенного центра)."""
    with svc.store.open_message(int(account.id), row["stored_path"]) as fh:
        head = fh.read(64 * 1024)
    end = re.search(rb"\r?\n\r?\n", head)
    kind = _smime_kind(head[:end.end()] if end else head)
    if not kind:
        return None
    if kind == "encrypted":
        return {"kind": "encrypted", "text": "Письмо зашифровано S/MIME — прочитать его может только получатель "
                                             "в почтовой программе со своим сертификатом."}
    out: Dict = {"kind": "signed", "valid": None, "trusted": None, "signer": {}, "text": ""}
    exe = _openssl()
    if not exe:
        out["text"] = "Письмо подписано S/MIME. Проверить подпись нельзя: на сервере нет программы openssl."
        return out
    if int(row["size"] or 0) > 25 * 1024 * 1024:
        out["text"] = "Письмо подписано S/MIME; слишком большое, чтобы проверять подпись здесь."
        return out
    raw = svc.store.read_message(int(account.id), row["stored_path"])
    tmp = tempfile.mkdtemp(prefix="smime-", dir=wt._tmp_dir(svc))  # noqa: SLF001
    try:
        src, cert = os.path.join(tmp, "m.eml"), os.path.join(tmp, "signer.pem")
        with open(src, "wb") as fh:
            fh.write(raw)

        def run(*extra):
            return subprocess.run([exe, "smime", "-verify", "-in", src, "-signer", cert, "-out", os.devnull, *extra],
                                  capture_output=True, timeout=15, check=False)
        r = run("-purpose", "any")
        if r.returncode == 0:
            out["valid"], out["trusted"] = True, True
        else:
            r2 = run("-noverify")
            out["valid"] = r2.returncode == 0
            out["trusted"] = False if out["valid"] else None
            if not out["valid"]:
                out["error"] = (r2.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:][0][:300] \
                    if r2.stderr else ""
        if os.path.exists(cert):
            out["signer"] = _cert_info(open(cert, "rb").read())
    except (OSError, subprocess.SubprocessError) as exc:
        out["text"] = f"Подпись S/MIME не проверена: {exc}"
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    sender = parseaddr(str(message_from_bytes(raw[:65536], policy=policy.default).get("From") or ""))[1].lower()
    emails = [e.lower() for e in out["signer"].get("emails") or []]
    out["matches_sender"] = bool(sender) and sender in emails
    if out["valid"] and out["trusted"]:
        out["text"] = "Подпись S/MIME верна, сертификат выдан доверенным центром."
    elif out["valid"]:
        out["text"] = "Подпись S/MIME верна, но сертификат выдан не доверенным центром (или самоподписанный)."
    else:
        out["text"] = "Подпись S/MIME неверна: письмо изменили после подписи или подпись повреждена."
    if out["valid"] and emails and not out["matches_sender"]:
        out["text"] += f" Сертификат выдан на {', '.join(emails)}, а не на адрес отправителя."
    return out


def _cert_info(pem: bytes) -> Dict:
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID, NameOID
        cert = x509.load_pem_x509_certificate(pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n")
    except Exception:  # noqa: BLE001
        return {}

    def attr(name, oid):
        try:
            v = name.get_attributes_for_oid(oid)
            return str(v[0].value) if v else ""
        except Exception:  # noqa: BLE001
            return ""
    emails = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        emails = [str(e) for e in san.get_values_for_type(x509.RFC822Name)]
    except Exception:  # noqa: BLE001
        pass
    if not emails:
        e = attr(cert.subject, NameOID.EMAIL_ADDRESS)
        emails = [e] if e else []
    try:
        until = cert.not_valid_after_utc
    except AttributeError:
        until = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return {"name": attr(cert.subject, NameOID.COMMON_NAME), "emails": emails,
            "issuer": attr(cert.issuer, NameOID.COMMON_NAME) or attr(cert.issuer, NameOID.ORGANIZATION_NAME),
            "valid_until": until.isoformat(), "expired": until < datetime.now(timezone.utc)}


# ===========================================================================
#  Правила и автоответ на почтовом сервере: Sieve (RFC 5228) по ManageSieve (RFC 5804)
# ===========================================================================
SIEVE_SCRIPT = "mailarchiver"
SIEVE_CONNECT = None          # подменяют тесты: (host, port, timeout) → сокет


def _q(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pattern(value: str) -> str:
    """Текст для :matches: звёздочки и вопросительные знаки из текста — не шаблон."""
    return str(value).replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _sieve_test(c: Dict, req: set) -> str:
    field, op, value = c["field"], c["op"], c.get("value", "")
    if field == "attach":
        t = 'header :contains "Content-Type" "multipart/mixed"'
        return t if op == "yes" else f"not {t}"
    if field == "size":
        mb = float(value or 0)
        amount = f"{int(round(mb * 1024))}K"
        return f"size :over {amount}" if op == "gt" else f"size :under {amount}"
    if field == "list":
        t = 'anyof (exists "List-Id", exists "List-Unsubscribe")'
        return t if op == "yes" else f"not {t}"
    if field == "priority":
        t = 'anyof (header :matches "X-Priority" ["1*", "2*"], header :is "Importance" "high")'
        return t if op == "yes" else f"not {t}"
    neg = op in ("not_contains", "not_equals")
    if field == "body":
        req.add("body")
        t = f"body :text :contains {_q(value)}"
    else:
        hdr = {"from": '"from"', "to": '"to"', "cc": '"cc"', "to_cc": '["to", "cc"]', "subject": '"subject"'}[field]
        is_addr = field != "subject"
        if op in ("equals", "not_equals"):
            t = f"address :is {hdr} {_q(value)}" if is_addr and "@" in value else f"header :is {hdr} {_q(value)}"
        elif op == "starts":
            t = f"{'address' if is_addr else 'header'} :matches {hdr} {_q(_pattern(value) + '*')}"
        elif op == "ends":
            t = f"{'address' if is_addr else 'header'} :matches {hdr} {_q('*' + _pattern(value))}"
        else:
            t = f"header :contains {hdr} {_q(value)}"
    return f"not {t}" if neg else t


def sieve_script(svc, account: Account) -> str:
    """Сценарий Sieve из правил и автоответа ящика."""
    req: set = set()
    body: List[str] = []
    special = wm.special_folders(svc, account)
    ar = wt.get_autoreply(svc, int(account.id))
    if ar["enabled"]:
        req.add("vacation")
        own = sorted(wm.own_addresses(svc, account))
        text = wm.html_to_text(ar["body"] or "").strip() or "Меня нет на месте."
        subj = ar["subject"] or "Автоответ"
        vac = (f"vacation :days {int(ar['days'] or 4)} :subject {_q(subj)}"
               + (f" :addresses [{', '.join(_q(a) for a in own)}]" if own else "") + f" {_q(text)};")
        conds = []
        if ar["date_from"]:
            req |= {"date", "relational"}
            conds.append(f'currentdate :value "ge" "date" {_q(ar["date_from"])}')
        if ar["date_to"]:
            req |= {"date", "relational"}
            conds.append(f'currentdate :value "le" "date" {_q(ar["date_to"])}')
        guard = 'not anyof (exists "List-Id", header :contains "Precedence" ["bulk", "list", "junk"])'
        conds.append(guard)
        body.append(f"# автоответ\nif allof ({', '.join(conds)}) {{\n    {vac}\n}}")
    for r in wt.list_rules(svc, int(account.id)):
        if not r["enabled"]:
            continue
        tests = [_sieve_test(c, req) for c in r["conditions"]]
        cond = (f"allof ({', '.join(tests)})" if r["match"] == "all" else f"anyof ({', '.join(tests)})") \
            if len(tests) > 1 else tests[0]
        acts = []
        for a in r["actions"]:
            t = a["type"]
            if t == "move" and a.get("folder"):
                req.add("fileinto")
                acts.append(f"fileinto {_q(a['folder'])};")
            elif t == "read":
                req.add("imap4flags")
                acts.append('addflag "\\\\Seen";')
            elif t in ("flag", "important"):
                req.add("imap4flags")
                acts.append('addflag "\\\\Flagged";')
            elif t == "tag":
                req.add("imap4flags")
                acts.append(f"addflag {_q(a['keyword'])};")
            elif t in ("junk", "delete"):
                dest = (special.get("junk") if t == "junk" else None) or special.get("trash")
                if dest:
                    req.add("fileinto")
                    acts.append(f"fileinto {_q(dest)};")
                else:
                    acts.append("discard;")
        if r["stop"] or r["kind"] == "block":
            acts.append("stop;")
        if acts:
            title = r["name"].replace("\n", " ")
            body.append(f"# {title}\nif {cond} {{\n    " + "\n    ".join(acts) + "\n}")
    head = "# MailArchiver: правила и автоответ ящика. Изменения здесь перезапишет клиент.\n"
    if req:
        head += f"require [{', '.join(_q(x) for x in sorted(req))}];\n"
    return head + "\n" + "\n\n".join(body) + "\n"


class ManageSieve:
    """Клиент ManageSieve (RFC 5804): STARTTLS, AUTHENTICATE PLAIN, LISTSCRIPTS, PUTSCRIPT, SETACTIVE."""

    def __init__(self, host: str, port: int, security: str, timeout: int = 20) -> None:
        self.host, self.port, self.security = host, int(port), security
        if SIEVE_CONNECT is not None:
            self.sock = SIEVE_CONNECT(host, int(port), timeout)
        else:
            self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        if security == "ssl":
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=host)
        self.buf = b""
        self.caps = self._caps()
        if security == "starttls":
            if "STARTTLS" not in self.caps:
                raise ValidationError(f"Сервер {host}:{port} не умеет STARTTLS — пароль по открытому каналу не "
                                      f"отправляется.")
            self._cmd("STARTTLS")
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=host)
            self.buf = b""
            self.caps = self._caps()

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ValidationError("Сервер Sieve закрыл соединение.")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        m = re.search(rb"\{(\d+)\+?\}$", line)
        if m:                                   # строка-литерал: {n}\r\n<n байт>
            n = int(m.group(1))
            while len(self.buf) < n:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buf += chunk
            line = line[:m.start()] + b'"' + self.buf[:n].replace(b'"', b"'") + b'"'
            self.buf = self.buf[n:]
            rest = self._line() if not self.buf.startswith(b"\r\n") else b""
            if self.buf.startswith(b"\r\n"):
                self.buf = self.buf[2:]
            line += rest
        return line

    def _response(self) -> Tuple[str, List[bytes], str]:
        lines = []
        while True:
            line = self._line()
            up = line.upper()
            if up.startswith((b"OK", b"NO", b"BYE")):
                status = up.split()[0].decode()
                text = re.findall(rb'"((?:[^"\\]|\\.)*)"', line)
                return status, lines, text[-1].decode("utf-8", "replace") if text else ""
            lines.append(line)

    def _caps(self) -> Dict[str, str]:
        status, lines, text = self._response()
        if status != "OK":
            raise ValidationError(f"Сервер Sieve не принял соединение: {text}")
        caps = {}
        for ln in lines:
            parts = re.findall(rb'"((?:[^"\\]|\\.)*)"', ln)
            if parts:
                caps[parts[0].decode().upper()] = parts[1].decode() if len(parts) > 1 else ""
        return caps

    def _cmd(self, command: str, literal: Optional[bytes] = None) -> Tuple[List[bytes], str]:
        data = command.encode("utf-8")
        if literal is not None:
            data += b" {" + str(len(literal)).encode() + b"+}\r\n" + literal
        self.sock.sendall(data + b"\r\n")
        status, lines, text = self._response()
        if status != "OK":
            raise ValidationError(f"Сервер Sieve отказал ({command.split()[0]}): {text or status}")
        return lines, text

    def login(self, user: str, password: str) -> None:
        if "PLAIN" not in (self.caps.get("SASL") or "").upper().split():
            raise ValidationError("Сервер Sieve не принимает вход по паролю (SASL PLAIN).")
        token = base64.b64encode(b"\0" + user.encode() + b"\0" + password.encode()).decode()
        try:
            self._cmd(f'AUTHENTICATE "PLAIN" "{token}"')
        except ValidationError as exc:
            raise ValidationError(f"Сервер Sieve не пустил: {exc.message}") from None

    def scripts(self) -> List[Tuple[str, bool]]:
        lines, _t = self._cmd("LISTSCRIPTS")
        out = []
        for ln in lines:
            m = re.match(rb'"((?:[^"\\]|\\.)*)"\s*(ACTIVE)?', ln.strip(), re.I)
            if m:
                out.append((m.group(1).decode("utf-8", "replace"), bool(m.group(2))))
        return out

    def put(self, name: str, text: str) -> None:
        self._cmd(f"PUTSCRIPT {_q(name)}", text.encode("utf-8"))

    def activate(self, name: str) -> None:
        self._cmd(f"SETACTIVE {_q(name)}")

    def delete(self, name: str) -> None:
        self._cmd(f"DELETESCRIPT {_q(name)}")

    def close(self) -> None:
        try:
            self.sock.sendall(b"LOGOUT\r\n")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def sieve_settings(svc, account: Account) -> Dict:
    try:
        cur = json.loads(wt._kv_get(svc, int(account.id), "sieve") or "{}")  # noqa: SLF001
    except (TypeError, ValueError):
        cur = {}
    return {"enabled": bool(cur.get("enabled")), "host": cur.get("host") or account.host,
            "port": int(cur.get("port") or 4190), "security": cur.get("security") or "starttls",
            "synced_at": cur.get("synced_at"), "error": cur.get("error") or "",
            "supported": account.auth_type == AuthType.PASSWORD and bool(account.password),
            "other_active": cur.get("other_active") or ""}


def _sieve_store(svc, account: Account, **changes) -> Dict:
    cur = {k: v for k, v in sieve_settings(svc, account).items() if k not in ("supported",)}
    cur.update(changes)
    wt._kv_set(svc, int(account.id), "sieve", json.dumps(cur, ensure_ascii=False))  # noqa: SLF001
    return sieve_settings(svc, account)


def sieve_active(svc, account_id: int) -> bool:
    """Правила и автоответ выполняет сервер (сценарий загружен) — служба их сама не выполняет."""
    try:
        cur = json.loads(wt._kv_get(svc, int(account_id), "sieve") or "{}")  # noqa: SLF001
    except (TypeError, ValueError):
        return False
    return bool(cur.get("enabled")) and bool(cur.get("synced_at")) and not cur.get("error")


def _sieve_connect(svc, account: Account, st: Dict) -> ManageSieve:
    host = str(st["host"] or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-]+|\[[0-9a-fA-F:.]+\]", host):
        raise ValidationError(f"«{host}» — не имя сервера.")
    if st["security"] == "none" and not wm._is_local(host):  # noqa: SLF001
        raise ValidationError("Пароль ящика не отправляется на сервер Sieve без шифрования.",
                              hint="Выберите STARTTLS или SSL.")
    if not (account.auth_type == AuthType.PASSWORD and account.password):
        raise ValidationError("Правила на сервере доступны ящикам с паролем: ящику с OAuth или входом через "
                              "администратора почты сервер Sieve не откроет.")
    try:
        ms = ManageSieve(host, int(st["port"]), st["security"])
    except (OSError, ssl.SSLError) as exc:
        raise ValidationError(f"Нет связи с сервером Sieve {host}:{st['port']}: {exc}",
                              hint="ManageSieve обычно работает на порту 4190 (Dovecot Pigeonhole, Cyrus, Stalwart). "
                                   "Exchange, Gmail и Яндекс его не поддерживают.") from None
    ms.login(account.username, account.password)
    return ms


def sieve_save(svc, account: Account, data: Dict, *, actor: str = "", force: bool = False) -> Dict:
    """Включить или выключить правила на сервере (и загрузить сценарий)."""
    st = sieve_settings(svc, account)
    host = str(data.get("host") or st["host"] or "").strip()
    port = int(data.get("port") or st["port"] or 4190)
    security = data.get("security") or st["security"]
    if security not in ("starttls", "ssl", "none"):
        raise ValidationError("Защита: starttls, ssl или none.")
    if not 1 <= port <= 65535:
        raise ValidationError("Порт — число от 1 до 65535.")
    st.update(host=host, port=port, security=security)
    if not data.get("enabled"):
        if st["enabled"] and st["synced_at"]:
            try:
                ms = _sieve_connect(svc, account, st)
                try:
                    if any(n == SIEVE_SCRIPT and a for n, a in ms.scripts()):
                        ms.activate("")
                    if any(n == SIEVE_SCRIPT for n, _a in ms.scripts()):
                        ms.delete(SIEVE_SCRIPT)
                finally:
                    ms.close()
            except ValidationError as exc:
                log.info("Сценарий Sieve ящика «%s» не убран с сервера: %s", account.name, exc.message)
        res = _sieve_store(svc, account, enabled=False, host=host, port=port, security=security, synced_at=None,
                           error="", other_active="")
        if actor:
            svc.db.add_audit(actor, "webmail_sieve", f"ящик «{account.name}»: правила на сервере выключены")
        return res
    _sieve_store(svc, account, enabled=True, host=host, port=port, security=security)
    return sieve_sync(svc, account, actor=actor, force=force)


def sieve_sync(svc, account: Account, *, actor: str = "", force: bool = False) -> Dict:
    st = sieve_settings(svc, account)
    if not st["enabled"]:
        return st
    text = sieve_script(svc, account)
    try:
        ms = _sieve_connect(svc, account, st)
        try:
            other = [n for n, a in ms.scripts() if a and n != SIEVE_SCRIPT]
            if other and not force:
                _sieve_store(svc, account, error=f"на сервере уже работает сценарий «{other[0]}»",
                             other_active=other[0], synced_at=None)
                raise ValidationError(
                    f"На сервере уже работает сценарий «{other[0]}» (его сделали в другой программе, например в "
                    f"Roundcube). Активным может быть только один сценарий.", code="sieve_conflict",
                    hint="«Заменить» сделает активным сценарий клиента; прежний останется на сервере неактивным.")
            ms.put(SIEVE_SCRIPT, text)
            ms.activate(SIEVE_SCRIPT)
        finally:
            ms.close()
    except ValidationError as exc:
        if exc.code != "sieve_conflict":
            _sieve_store(svc, account, error=exc.message[:500], synced_at=None)
        raise
    res = _sieve_store(svc, account, synced_at=utcnow_iso(), error="", other_active="")
    if actor:
        svc.db.add_audit(actor, "webmail_sieve", f"ящик «{account.name}»: правила загружены на сервер "
                                                 f"{st['host']}:{st['port']}")
    return res | {"script": text}


def sieve_resync_quiet(svc, account: Account) -> str:
    """После изменения правил или автоответа: загрузить сценарий заново ('' или текст ошибки)."""
    if not sieve_settings(svc, account)["enabled"]:
        return ""
    try:
        sieve_sync(svc, account, force=False)
        return ""
    except ValidationError as exc:
        return exc.message


# ===========================================================================
#  Черновики в папке «Черновики» сервера
# ===========================================================================
def _server_copy(row) -> Dict:
    try:
        return json.loads(row["server_copy"] or "{}") if "server_copy" in row.keys() else {}
    except (TypeError, ValueError):
        return {}


def drop_server_copy(svc, account: Account, row) -> None:
    sc = _server_copy(row)
    if not sc.get("uid"):
        return
    try:
        with wm.pool(svc).session(account) as conn:
            info = conn.select(sc["folder"], readonly=False)
            if int(info.get("uidvalidity") or 0) == int(sc.get("uidv") or 0):
                conn.add_flags([int(sc["uid"])], ["\\Deleted"])
                if conn.has_capability("UIDPLUS"):
                    conn.expunge_uids([int(sc["uid"])])
                else:
                    conn.expunge_all()
        svc.db.execute("UPDATE messages SET gone=1 WHERE account_id=? AND folder=? AND uidvalidity=? AND uid=?",
                       (int(account.id), sc["folder"], int(sc.get("uidv") or 0), int(sc["uid"])))
    except (MailArchiverError, ValidationError) as exc:
        log.info("Прежняя копия черновика на сервере не удалена: %s", exc)
    svc.db.execute("UPDATE webmail_drafts SET server_copy='' WHERE id=?", (int(row["id"]),))


def save_draft_to_server(svc, account: Account, draft_id: int, owner: str) -> Dict:
    """Положить черновик в папку «Черновики» сервера (прежнюю копию — убрать), чтобы он был виден и на телефоне."""
    if not bool(svc.rt("webmail", "drafts_on_server")):
        return {"saved": False}
    row = wm.get_draft_row(svc, draft_id, owner)
    folder = wm.special_folders(svc, account).get("drafts")
    if not folder:
        return {"saved": False, "warning": "Папки «Черновики» на сервере нет — черновик хранится только здесь."}
    ident = wm.get_identity(svc, account)
    msg, _rcpts, _atts = wm.build_message(svc, account, row, ident, as_draft=True)
    data = msg.as_bytes()
    with wm.pool(svc).session(account) as conn:
        wm._untagged(conn).pop("APPENDUID", None)  # noqa: SLF001
        try:
            resp = conn.client.append(folder, data, flags=["\\Draft", "\\Seen"], msg_time=datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001
            from .imap.client import _map_exception
            raise _map_exception(exc) from exc
        uidv, uid = wm.parse_appenduid(resp)
    drop_server_copy(svc, account, row)
    sc = {"folder": folder, "uidv": uidv, "uid": uid} if uid else {}
    svc.db.execute("UPDATE webmail_drafts SET server_copy=? WHERE id=?", (json.dumps(sc), int(draft_id)))
    return {"saved": True, "folder": folder}


# ===========================================================================
#  Квота ящика
# ===========================================================================
def quota(svc, account: Account, *, refresh: bool = False) -> Dict:
    """Занято и предел ящика (GETQUOTAROOT); кэш — 10 минут."""
    try:
        cached = json.loads(wt._kv_get(svc, int(account.id), "quota") or "{}")  # noqa: SLF001
    except (TypeError, ValueError):
        cached = {}
    if cached and not refresh and time.time() - float(cached.get("ts") or 0) < 600:
        return cached
    items = []
    try:
        with wm.pool(svc).session(account) as conn:
            items = conn.quota("INBOX")
    except (MailArchiverError, ValidationError) as exc:
        return cached or {"error": exc.message}
    st = next((q for q in items if q["resource"] == "STORAGE" and q["limit"]), None)
    out = {"ts": time.time(), "used": int(st["usage"]) * 1024 if st else None,
           "limit": int(st["limit"]) * 1024 if st else None}
    if out["used"] is not None and out["limit"]:
        out["percent"] = round(100.0 * out["used"] / out["limit"], 1)
    wt._kv_set(svc, int(account.id), "quota", json.dumps(out))  # noqa: SLF001
    return out
