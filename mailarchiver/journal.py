"""
Приём журналирования по SMTP (1.20.0): копия каждого письма организации — сразу в архив.

Копирование по IMAP видит только то, что лежит в ящике в момент копирования: письмо,
пришедшее и удалённое между двумя копиями, в архив не попадёт. Журналирование закрывает
этот пробел: почтовый сервер отправляет копию каждого письма на служебный адрес, а
MailArchiver принимает её по SMTP и кладёт в архив ящиков отправителя и получателей —
в папку «Журнал» (``journal.folder``).

Источники:

* **Microsoft Exchange** (правило журналирования, «журнальный отчёт»): исходное письмо —
  вложением, адресаты (в том числе скрытые) — в тексте отчёта; отчёт разворачивается.
* **Postfix** (``always_bcc``, ``sender_bcc_maps``/``recipient_bcc_maps``), **Exim**, **Dovecot**,
  **Zimbra**, **CommuniGate** и другие, умеющие пересылать копию: письмо приходит как есть,
  ящики находятся по адресам из From, To, Cc и конверта.

Ящик находится по адресу: логину ящика или адресу сотрудника из справочника. Письма
других адресов не сохраняются (считаются в «без ящика»). Одно и то же письмо (Message-ID)
в одном ящике не сохраняется дважды.

Принимать письма можно только с адресов ``journal.allowed_ips`` (почтовые серверы); с
сертификатом (``journal.tls_cert``/``tls_key``) включается STARTTLS, а ``journal.require_tls``
не принимает письма без шифрования.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import socketserver
import ssl
import threading
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Dict, List, Optional, Set, Tuple

from .logging_setup import get_logger
from .util import decode_mime_header, utcnow_iso

log = get_logger("journal")

MAX_RCPTS = 1000
_lock = threading.Lock()
_server: Optional["JournalServer"] = None
_stats: Dict[str, object] = {"received": 0, "stored": 0, "unmatched": 0, "duplicates": 0, "rejected": 0,
                             "errors": 0, "last_at": "", "last_error": ""}


# ---------------------------------------------------------------------------
#  Разбор письма
# ---------------------------------------------------------------------------
_ADDR_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)


def _addrs(*values: str) -> Set[str]:
    out = set()
    for _n, a in getaddresses([v for v in values if v]):
        a = (a or "").strip().lower()
        if "@" in a:
            out.add(a)
    return out


def unwrap(raw: bytes) -> Tuple[bytes, Set[str]]:
    """Журнальный отчёт Exchange → (исходное письмо, адреса из отчёта); иначе (письмо, пусто)."""
    head_end = re.search(rb"\r?\n\r?\n", raw[:256 * 1024])
    head = raw[:head_end.end()] if head_end else raw[:256 * 1024]
    if b"x-ms-journal-report" not in head.lower():
        return raw, set()
    msg = message_from_bytes(raw, policy=policy.compat32)
    report_addrs: Set[str] = set()
    original = None
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain" and not report_addrs:
            try:
                text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
            except LookupError:
                text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            for line in text.splitlines():
                key, _sep, value = line.partition(":")
                if key.strip().lower() in ("sender", "on-behalf-of", "to", "cc", "bcc", "recipient"):
                    report_addrs |= {a.lower() for a in _ADDR_RE.findall(value)}
        elif ctype == "message/rfc822" and original is None:
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                original = inner[0].as_bytes(policy=policy.compat32)
    return (original or raw), report_addrs


def _header_info(raw: bytes) -> Dict:
    end = re.search(rb"\r?\n\r?\n", raw[:512 * 1024])
    msg = message_from_bytes(raw[:end.end()] if end else raw[:512 * 1024], policy=policy.compat32)

    def h(name):
        v = msg.get(name)
        return str(v) if v is not None else ""
    when = None
    try:
        when = parsedate_to_datetime(h("Date"))
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        when = None
    ctype = h("Content-Type").lower()
    return {"from": decode_mime_header(h("From")), "subject": decode_mime_header(h("Subject")),
            "message_id": h("Message-ID").strip()[:500],
            "addrs": _addrs(h("From"), h("Sender"), h("To"), h("Cc"), h("Bcc"), h("Delivered-To")),
            "date": (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "has_attach": "multipart/mixed" in ctype or "multipart/report" in ctype}


def _accounts_for(svc, addrs: Set[str]) -> Dict[int, str]:
    """Ящики по адресам: логин ящика или адрес сотрудника (его ящики)."""
    out: Dict[int, str] = {}
    if not addrs:
        return out
    ids = sorted(addrs)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in svc.db.query(f"SELECT id, username FROM accounts WHERE lower(username) IN ({marks})", tuple(chunk)):
            out[int(r["id"])] = (r["username"] or "").lower()
        for r in svc.db.query(f"SELECT a.id, lower(e.email) AS email FROM employees e JOIN accounts a ON "
                              f"a.employee_id = e.id WHERE lower(e.email) IN ({marks})", tuple(chunk)):
            out.setdefault(int(r["id"]), r["email"])
    return out


def store(svc, raw: bytes, envelope_from: str = "", envelope_rcpts: Optional[List[str]] = None) -> Dict:
    """Сохранить письмо из журнала в архив ящиков его участников. → итог."""
    if bool(svc.rt("journal", "unwrap_exchange")):
        raw, report_addrs = unwrap(raw)
    else:
        report_addrs = set()
    info = _header_info(raw)
    journal_addr = str(svc.rt("journal", "address") or "").strip().lower()
    addrs = set(info["addrs"]) | report_addrs
    if envelope_from and "@" in envelope_from:
        addrs.add(envelope_from.lower())
    for r in envelope_rcpts or []:
        if r and r.lower() != journal_addr:
            addrs.add(r.lower())
    accounts = _accounts_for(svc, addrs)
    folder = str(svc.rt("journal", "folder") or "Журнал").strip() or "Журнал"
    stored = dup = 0
    sha = hashlib.sha256(raw).hexdigest()
    for account_id in sorted(accounts):
        with _lock:
            if info["message_id"] and svc.db.query_one(
                    "SELECT 1 FROM messages WHERE account_id=? AND folder=? AND message_id=?",
                    (account_id, folder, info["message_id"])):
                dup += 1
                continue
            if not info["message_id"] and svc.db.query_one(
                    "SELECT 1 FROM messages WHERE account_id=? AND folder=? AND sha256=?", (account_id, folder, sha)):
                dup += 1
                continue
            uid = int(svc.db.scalar("SELECT COALESCE(MAX(uid), 0) FROM messages WHERE account_id=? AND folder=? "
                                    "AND uidvalidity=1", (account_id, folder)) or 0) + 1
            rel, digest, size = svc.store.store_message(account_id, folder, "/", uid, raw)
            svc.db.add_message_index_batch([(account_id, folder, 1, uid, info["message_id"], size, info["date"],
                                             "\\Seen", rel, digest, info["subject"][:1000], info["from"][:1000],
                                             1 if info["has_attach"] else 0)])
            stored += 1
    with _lock:
        _stats["received"] = int(_stats["received"]) + 1
        _stats["stored"] = int(_stats["stored"]) + stored
        _stats["duplicates"] = int(_stats["duplicates"]) + dup
        if not accounts:
            _stats["unmatched"] = int(_stats["unmatched"]) + 1
        _stats["last_at"] = utcnow_iso()
    return {"accounts": sorted(accounts), "stored": stored, "duplicates": dup}


# ---------------------------------------------------------------------------
#  SMTP-сервер
# ---------------------------------------------------------------------------
def _allowed(svc, ip: str) -> bool:
    nets = svc.rt("journal", "allowed_ips") or []
    if isinstance(nets, str):
        nets = [x.strip() for x in nets.split(",") if x.strip()]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for n in nets:
        try:
            if addr in ipaddress.ip_network(str(n).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


class _Handler(socketserver.StreamRequestHandler):
    timeout = 300

    def setup(self) -> None:
        super().setup()
        self.svc = self.server.svc
        self.tls = False

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("utf-8"))
        self.wfile.flush()

    def _readline(self) -> Optional[bytes]:
        line = self.rfile.readline(4096)
        return line if line else None

    def handle(self) -> None:  # noqa: C901 - протокол — один цикл команд
        ip = self.client_address[0]
        if not _allowed(self.svc, ip):
            self._send("554 5.7.1 Access denied")
            with _lock:
                _stats["rejected"] = int(_stats["rejected"]) + 1
            log.warning("Журналирование: отклонено подключение с %s (нет в journal.allowed_ips).", ip)
            return
        host = socket.gethostname()
        max_size = int(self.svc.rt("journal", "max_size_mb") or 50) * 1024 * 1024
        ctx = self.server.ssl_context
        self._send(f"220 {host} MailArchiver journal ESMTP")
        mail_from: Optional[str] = None
        rcpts: List[str] = []
        while True:
            line = self._readline()
            if line is None:
                return
            cmd = line.decode("utf-8", "replace").rstrip("\r\n")
            verb = cmd.split(" ", 1)[0].upper()
            arg = cmd[len(verb):].strip()
            if verb in ("EHLO", "HELO"):
                mail_from, rcpts = None, []
                if verb == "HELO":
                    self._send(f"250 {host}")
                    continue
                ext = [f"250-{host}", f"250-SIZE {max_size}", "250-8BITMIME"]
                if ctx is not None and not self.tls:
                    ext.append("250-STARTTLS")
                ext.append("250 ENHANCEDSTATUSCODES")
                for item in ext:
                    self.wfile.write((item + "\r\n").encode())
                self.wfile.flush()
            elif verb == "STARTTLS":
                if ctx is None or self.tls:
                    self._send("502 5.5.1 STARTTLS not available")
                    continue
                self._send("220 2.0.0 Ready to start TLS")
                try:
                    self.request = ctx.wrap_socket(self.request, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    log.warning("Журналирование: TLS с %s не установлен: %s", ip, exc)
                    return
                self.rfile = self.request.makefile("rb")
                self.wfile = self.request.makefile("wb")
                self.tls = True
                mail_from, rcpts = None, []
            elif verb == "MAIL":
                if bool(self.svc.rt("journal", "require_tls")) and not self.tls:
                    self._send("530 5.7.0 Must issue a STARTTLS command first")
                    continue
                m = re.match(r"(?i)FROM:\s*<([^>]*)>(.*)", arg)
                if not m:
                    self._send("501 5.5.4 Syntax: MAIL FROM:<address>")
                    continue
                size = re.search(r"(?i)\bSIZE=(\d+)", m.group(2) or "")
                if size and int(size.group(1)) > max_size:
                    self._send("552 5.3.4 Message too big")
                    continue
                mail_from, rcpts = m.group(1).strip(), []
                self._send("250 2.1.0 OK")
            elif verb == "RCPT":
                if mail_from is None:
                    self._send("503 5.5.1 Need MAIL first")
                    continue
                m = re.match(r"(?i)TO:\s*<([^>]*)>", arg)
                if not m:
                    self._send("501 5.5.4 Syntax: RCPT TO:<address>")
                    continue
                if len(rcpts) >= MAX_RCPTS:
                    self._send("452 4.5.3 Too many recipients")
                    continue
                rcpts.append(m.group(1).strip())
                self._send("250 2.1.5 OK")
            elif verb == "DATA":
                if mail_from is None or not rcpts:
                    self._send("503 5.5.1 Need RCPT first")
                    continue
                self._send("354 End data with <CR><LF>.<CR><LF>")
                chunks: List[bytes] = []
                total = 0
                too_big = False
                while True:
                    data = self.rfile.readline(1024 * 1024)
                    if not data:
                        return
                    if data in (b".\r\n", b".\n"):
                        break
                    if data.startswith(b"."):
                        data = data[1:]
                    total += len(data)
                    if total > max_size:
                        too_big = True
                        continue
                    chunks.append(data)
                if too_big:
                    self._send("552 5.3.4 Message too big")
                else:
                    try:
                        res = store(self.svc, b"".join(chunks), mail_from, rcpts)
                        self._send(f"250 2.0.0 OK: stored {res['stored']}")
                    except Exception as exc:  # noqa: BLE001 - временная ошибка: сервер повторит позже
                        log.exception("Журналирование: письмо не сохранено")
                        with _lock:
                            _stats["errors"] = int(_stats["errors"]) + 1
                            _stats["last_error"] = str(exc)[:300]
                        self._send("451 4.3.0 Temporary failure, try again later")
                mail_from, rcpts = None, []
            elif verb == "RSET":
                mail_from, rcpts = None, []
                self._send("250 2.0.0 OK")
            elif verb == "NOOP":
                self._send("250 2.0.0 OK")
            elif verb == "QUIT":
                self._send("221 2.0.0 Bye")
                return
            else:
                self._send("502 5.5.2 Command not implemented")


class JournalServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, svc, host: str, port: int, ssl_context: Optional[ssl.SSLContext]) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), _Handler)
        self.svc = svc
        self.ssl_context = ssl_context
        self.thread = threading.Thread(target=self.serve_forever, name="journal-smtp", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def _ssl_context(svc) -> Optional[ssl.SSLContext]:
    cert = str(svc.rt("journal", "tls_cert") or "").strip()
    key = str(svc.rt("journal", "tls_key") or "").strip()
    if not cert:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key or None)
    return ctx


def ensure(svc) -> Dict:
    """Запустить или остановить приём по настройкам (при старте службы и после их сохранения)."""
    global _server
    want = bool(svc.rt("journal", "enabled"))
    host = str(svc.rt("journal", "listen") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(svc.rt("journal", "port") or 2525)
    with _lock:
        cur = _server
    if cur is not None and (not want or (cur.server_address[0], cur.server_address[1]) != (host, port)
                            or cur.svc is not svc):
        cur.stop()
        with _lock:
            _server = None
        log.info("Приём журналирования остановлен.")
    if want and _server is None:
        try:
            srv = JournalServer(svc, host, port, _ssl_context(svc))
        except (OSError, ssl.SSLError, ValueError) as exc:
            with _lock:
                _stats["last_error"] = f"не запущен: {exc}"[:300]
            log.error("Приём журналирования не запущен (%s:%s): %s", host, port, exc)
            return status(svc)
        srv.start()
        with _lock:
            _server = srv
            _stats["last_error"] = ""
        log.info("Приём журналирования: SMTP на %s:%s.", host, port)
    return status(svc)


def stop() -> None:
    global _server
    with _lock:
        srv, _server = _server, None
    if srv is not None:
        srv.stop()


def status(svc) -> Dict:
    with _lock:
        srv = _server
        out = dict(_stats)
    out["running"] = srv is not None
    out["listen"] = f"{srv.server_address[0]}:{srv.server_address[1]}" if srv is not None else ""
    out["tls"] = bool(srv is not None and srv.ssl_context is not None)
    out["enabled"] = bool(svc.rt("journal", "enabled"))
    folder = str(svc.rt("journal", "folder") or "Журнал")
    out["in_archive"] = int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE folder=?", (folder,)) or 0)
    return out


def _reset_stats() -> None:
    with _lock:
        for k in list(_stats):
            _stats[k] = "" if isinstance(_stats[k], str) else 0
        _stats["started"] = time.time()
