"""
Уведомления: e-mail (SMTP), Telegram и вебхук (1.20.0). Отправляются по завершении заданий
согласно настройкам ``notifications`` (об ошибках и/или об успехе), а также сводка и
предупреждения мониторинга. Каждый настроенный канал получает то же сообщение; сбой одного
канала не мешает остальным. Полностью опциональны и не влияют на основную работу.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import queue
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Dict, List, Optional

from .logging_setup import get_logger

log = get_logger("notify")


class Notifier:
    #: После сбоя отправки не пытаемся снова столько секунд: зависший SMTP-релей
    #: не должен задерживать ни задания, ни очередь уведомлений.
    PAUSE_AFTER_FAILURE_S = 600

    def __init__(self, services) -> None:
        self.services = services
        self._queue: "queue.Queue" = queue.Queue(maxsize=200)
        self._worker: Optional[threading.Thread] = None
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def notify_job_async(self, job_type: str, status: str, subject: str, body: str) -> None:
        """Отправить уведомление В ФОНЕ.

        Раньше письмо отправлялось прямо в воркере задания с таймаутом 30 с на
        каждую операцию: при зависшем SMTP каждое задание после «Готово» ещё
        полминуты занимало слот очереди (на 580 ночных бэкапах — часы).
        """
        if not self.enabled():
            return
        try:
            self._queue.put_nowait((job_type, status, subject, body))
        except queue.Full:
            log.warning("Очередь уведомлений переполнена — уведомление «%s» пропущено.", subject)
            return
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="notify", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=30)
            except queue.Empty:
                return
            if time.time() < self._paused_until:
                log.warning("Уведомление «%s» не отправлено: SMTP недавно не отвечал.", item[2])
                continue
            if not self.notify_job(*item):
                self._paused_until = time.time() + self.PAUSE_AFTER_FAILURE_S

    def _cfg(self) -> dict:
        return self.services.cfg.notifications

    def enabled(self) -> bool:
        return bool(self.services.db.get_setting("notifications.enabled",
                                                 self._cfg().get("enabled", False)))

    def notify_job(self, job_type: str, status: str, subject: str, body: str) -> bool:
        """Отправить уведомление, если настройки этого требуют. False — сбой отправки."""
        cfg = self._cfg()
        if not self.enabled():
            return True
        on_failure = self.services.db.get_setting("notifications.on_failure", cfg.get("on_failure", True))
        on_success = self.services.db.get_setting("notifications.on_success", cfg.get("on_success", False))
        is_failure = status in ("failed", "partial")
        if is_failure and not on_failure:
            return True
        if not is_failure and not on_success:
            return True
        try:
            self.send(subject, body)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить уведомление: %s", exc)
            return False

    def _get(self, key: str, default=None):
        return self.services.db.get_setting(f"notifications.{key}", self._cfg().get(key, default))

    def channels(self) -> Dict[str, bool]:
        """Какие каналы настроены."""
        mail_to = self._get("mail_to", [])
        return {"email": bool(self._get("smtp_host", "")) and bool(mail_to),
                "telegram": bool(self._get("telegram_bot_token", "")) and bool(str(self._get("telegram_chat_id", "")).strip()),
                "webhook": bool(str(self._get("webhook_url", "") or "").strip())}

    def send(self, subject: str, body: str) -> None:
        """Отправить во все настроенные каналы. Ошибка — если не ушло ни в один."""
        results = self.send_all(subject, body)
        if not results:
            raise RuntimeError("Не настроен ни один канал уведомлений: заполните SMTP и получателей, Telegram "
                               "или адрес вебхука.")
        if all(results.values()):
            raise RuntimeError("; ".join(f"{k}: {v}" for k, v in results.items()))
        for name, err in results.items():
            if err:
                log.warning("Уведомление «%s» не ушло в канал %s: %s", subject, name, err)

    def send_all(self, subject: str, body: str) -> Dict[str, str]:
        """Отправить во все настроенные каналы: {канал: текст ошибки или ''}."""
        out: Dict[str, str] = {}
        senders = {"email": self.send_email, "telegram": self.send_telegram, "webhook": self.send_webhook}
        for name, on in self.channels().items():
            if not on:
                continue
            try:
                senders[name](subject, body)
                out[name] = ""
            except Exception as exc:  # noqa: BLE001 - один канал не мешает другим
                out[name] = str(getattr(exc, "reason", None) or exc)[:300]
        return out

    # --- Telegram ---
    def send_telegram(self, subject: str, body: str) -> None:
        token = str(self._get("telegram_bot_token", "") or "").strip()
        chat = str(self._get("telegram_chat_id", "") or "").strip()
        base = str(self._get("telegram_api_url", "") or "https://api.telegram.org").strip().rstrip("/")
        if not token or not chat:
            raise RuntimeError("Не заданы токен бота и чат Telegram.")
        if not base.startswith("https://"):
            raise RuntimeError("Адрес API Telegram должен начинаться с https://.")
        text = f"{subject}\n\n{body}"
        if len(text) > TELEGRAM_MAX:
            text = text[:TELEGRAM_MAX - 20] + "\n… (сокращено)"
        payload = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        data = _post(f"{base}/bot{token}/sendMessage", payload, {"Content-Type": "application/json"},
                     secret_in_url=token)
        try:
            ok = bool(json.loads(data.decode("utf-8", "replace")).get("ok"))
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError("Telegram не принял сообщение: " + data.decode("utf-8", "replace")[:200])
        log.info("Уведомление отправлено в Telegram: %s", subject)

    # --- вебхук ---
    def send_webhook(self, subject: str, body: str) -> None:
        from .version import __version__
        url = str(self._get("webhook_url", "") or "").strip()
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("Адрес вебхука должен начинаться с https:// или http://.")
        fmt = str(self._get("webhook_format", "json") or "json").strip().lower()
        if fmt == "slack":
            doc = {"text": f"*{subject}*\n{body}"}
        else:
            doc = {"event": "notification", "subject": subject, "text": body, "host": socket.gethostname(),
                   "version": __version__, "ts": int(time.time())}
        payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        secret = str(self._get("webhook_secret", "") or "")
        if secret:
            headers["X-MailArchiver-Signature"] = "sha256=" + hmac.new(secret.encode("utf-8"), payload,
                                                                       hashlib.sha256).hexdigest()
        _post(url, payload, headers)
        log.info("Уведомление отправлено на вебхук: %s", subject)

    # --- e-mail ---
    def send_email(self, subject: str, body: str) -> None:
        cfg = self._cfg()
        host = self.services.db.get_setting("notifications.smtp_host", cfg.get("smtp_host", ""))
        if not host:
            raise RuntimeError("Не задан SMTP-хост для уведомлений.")
        port = int(self.services.db.get_setting("notifications.smtp_port", cfg.get("smtp_port", 587)))
        security = self.services.db.get_setting("notifications.smtp_security", cfg.get("smtp_security", "starttls"))
        user = self.services.db.get_setting("notifications.smtp_user", cfg.get("smtp_user", ""))
        password = self.services.db.get_setting("notifications.smtp_password", cfg.get("smtp_password", ""))
        mail_from = self.services.db.get_setting("notifications.mail_from", cfg.get("mail_from", "")) or user
        mail_to: List[str] = self.services.db.get_setting("notifications.mail_to", cfg.get("mail_to", []))
        if isinstance(mail_to, str):
            mail_to = [x.strip() for x in mail_to.split(",") if x.strip()]
        if not mail_to:
            raise RuntimeError("Не задан получатель уведомлений (mail_to).")

        msg = EmailMessage()
        msg["From"] = mail_from
        msg["To"] = ", ".join(mail_to)
        msg["Subject"] = subject
        msg.set_content(body)

        security = str(security or "").strip().lower()
        if user and security not in ("ssl", "starttls"):
            # Иначе пароль ушёл бы на сервер открытым текстом (например при
            # опечатке в настройке: «tls», «none» и т.п.).
            log.warning(
                "Уведомление не отправлено: SMTP-аутентификация по незашифрованному каналу "
                "запрещена (notifications.smtp_security=%r).", security
            )
            raise RuntimeError(
                "SMTP-аутентификация по незашифрованному каналу запрещена. "
                "Укажите notifications.smtp_security = 'ssl' или 'starttls' "
                "(либо уберите имя пользователя, если сервер не требует входа)."
            )

        ctx = ssl.create_default_context()
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if security == "starttls":
                    s.starttls(context=ctx)
                if user:
                    s.login(user, password)
                s.send_message(msg)
        log.info("Уведомление отправлено: %s", subject)


#: Предел длины сообщения Telegram (4096 знаков).
TELEGRAM_MAX = 4000


def _post(url: str, payload: bytes, headers: Dict[str, str], secret_in_url: str = "") -> bytes:
    """POST с таймаутом; текст ошибки — без секрета из адреса (токен бота)."""
    req = urllib.request.Request(url, data=payload, headers={**headers, "User-Agent": "MailArchiver"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
            return resp.read(65536)
    except urllib.error.HTTPError as exc:
        text = exc.read(300).decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise RuntimeError(f"HTTP {exc.code}: {text}".replace(secret_in_url, "***") if secret_in_url
                           else f"HTTP {exc.code}: {text}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = str(getattr(exc, "reason", None) or exc)
        if secret_in_url:
            reason = reason.replace(secret_in_url, "***")
        raise RuntimeError(f"нет связи: {reason}") from None
