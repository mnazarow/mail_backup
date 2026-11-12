"""
Пересылка аудита в syslog / SIEM (1.21.0).

Каждая запись аудита (с подписью цепочки) уходит на сервер журналов в формате RFC 5424:
``<PRI>1 ВРЕМЯ УЗЕЛ mailarchiver PID audit [ma@32473 id="…" user="…" action="…" hash="…"] текст``.
Транспорт — ``audit.syslog_proto``: ``udp`` (RFC 5426), ``tcp`` или ``tls`` (RFC 6587/5425, кадры
«длина пробел сообщение»). Отправка — в отдельном потоке через очередь: недоступный сервер журналов
не задерживает действия пользователей; очередь ограничена, при переполнении старые записи
отбрасываются с предупреждением в журнале (в базе аудита они остаются).

Копия аудита вне сервера архива — главное условие, чтобы удаление записей прямо в базе было видно не
только по разрыву цепочки, но и по журналу SIEM.
"""
from __future__ import annotations

import os
import queue
import socket
import ssl
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from .logging_setup import get_logger

log = get_logger("auditlog")

PROTOCOLS = ("udp", "tcp", "tls")
QUEUE_MAX = 10000
APP = "mailarchiver"
#: SD-ID с номером частного предприятия (как в примерах RFC 5424 — пространство имён архива)
SD_ID = "ma@32473"
FACILITY_AUTHPRIV = 10
SEVERITY_NOTICE = 5


def _sd_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def format_rfc5424(rec: Dict, hostname: str = "") -> bytes:
    """Запись аудита → сообщение syslog RFC 5424 (UTF-8 с BOM в тексте, как требует стандарт)."""
    pri = FACILITY_AUTHPRIV * 8 + SEVERITY_NOTICE
    ts = rec.get("ts") or datetime.now().astimezone().isoformat()
    host = (hostname or socket.gethostname() or "-").replace(" ", "_")[:255]
    sd = (f'[{SD_ID} id="{_sd_escape(rec.get("id", ""))}" user="{_sd_escape(rec.get("user", ""))}" '
          f'action="{_sd_escape(rec.get("action", ""))}" hash="{_sd_escape(rec.get("hash", ""))}"]')
    msg = f'{rec.get("user", "")}: {rec.get("action", "")} {rec.get("detail", "")}'.strip()
    head = f"<{pri}>1 {ts} {host} {APP} {os.getpid()} audit {sd} "
    return head.encode("utf-8") + b"\xef\xbb\xbf" + msg.encode("utf-8")


class Forwarder:
    """Очередь и поток отправки. Настройки читаются при каждой отправке — их можно менять на ходу."""

    def __init__(self, svc) -> None:
        self.svc = svc
        self.q: "queue.Queue[Dict]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._sock = None
        self._sock_key = None
        self._lock = threading.Lock()
        self.sent = 0
        self.dropped = 0
        self.last_error = ""

    def _cfg(self) -> Dict:
        rt = self.svc.rt
        return {"host": str(rt("audit", "syslog_host") or "").strip(),
                "port": int(rt("audit", "syslog_port") or 514),
                "proto": str(rt("audit", "syslog_proto") or "udp").strip().lower(),
                "verify": bool(rt("audit", "syslog_tls_verify")),
                "hostname": str(rt("audit", "syslog_hostname") or "").strip()}

    def enabled(self) -> bool:
        try:
            return bool(self._cfg()["host"])
        except Exception:  # noqa: BLE001
            return False

    def push(self, rec: Dict) -> None:
        if not self.enabled():
            return
        try:
            self.q.put_nowait(rec)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 1000 == 1:
                log.warning("Очередь пересылки аудита переполнена: записи не уходят на сервер журналов")
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="audit-syslog", daemon=True)
                self._thread.start()

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._sock_key = None

    def _connect(self, cfg: Dict):
        key = (cfg["host"], cfg["port"], cfg["proto"], cfg["verify"])
        if self._sock is not None and self._sock_key == key:
            return self._sock
        self._close()
        if cfg["proto"] == "udp":
            fam = socket.getaddrinfo(cfg["host"], cfg["port"], 0, socket.SOCK_DGRAM)[0]
            sock = socket.socket(fam[0], socket.SOCK_DGRAM)
            sock.connect(fam[4])
        else:
            sock = socket.create_connection((cfg["host"], cfg["port"]), timeout=10)
            if cfg["proto"] == "tls":
                ctx = ssl.create_default_context()
                if not cfg["verify"]:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=cfg["host"])
        self._sock, self._sock_key = sock, key
        return sock

    def send_one(self, rec: Dict, cfg: Optional[Dict] = None) -> None:
        cfg = cfg or self._cfg()
        if cfg["proto"] not in PROTOCOLS:
            raise ValueError(f"неизвестный протокол {cfg['proto']}")
        data = format_rfc5424(rec, cfg["hostname"])
        sock = self._connect(cfg)
        if cfg["proto"] == "udp":
            sock.send(data[:8192])
        else:
            sock.sendall(str(len(data)).encode("ascii") + b" " + data)       # RFC 6587: октеты с длиной

    def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                rec = self.q.get(timeout=60)
            except queue.Empty:
                self._close()
                return
            while True:
                try:
                    self.send_one(rec)
                    self.sent += 1
                    backoff = 1.0
                    break
                except Exception as exc:  # noqa: BLE001 - сервер журналов недоступен: повторяем позже
                    self._close()
                    if str(exc) != self.last_error:
                        log.warning("Аудит не отправлен на сервер журналов: %s", exc)
                    self.last_error = str(exc)[:300]
                    if not self.enabled():
                        break
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)

    def status(self) -> Dict:
        return {"enabled": self.enabled(), "queued": self.q.qsize(), "sent": self.sent, "dropped": self.dropped,
                "last_error": self.last_error}


def install(svc) -> Forwarder:
    fwd = Forwarder(svc)
    svc.db.audit_hook = fwd.push
    svc.audit_forwarder = fwd
    return fwd


def test_send(svc) -> Dict:
    """Проверка из «Настроек»: одна запись прямо сейчас, с ошибкой подключения — в ответе."""
    fwd = Forwarder(svc)
    cfg = fwd._cfg()  # noqa: SLF001
    if not cfg["host"]:
        return {"ok": False, "error": "Не указан сервер журналов (audit.syslog_host)."}
    try:
        fwd.send_one({"id": 0, "ts": datetime.now().astimezone().isoformat(), "user": "system",
                      "action": "audit_syslog_test", "detail": "проверка пересылки аудита", "hash": ""}, cfg)
        return {"ok": True, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        fwd._close()  # noqa: SLF001
