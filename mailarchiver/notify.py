"""
Уведомления по e-mail (SMTP). Отправляются по завершении заданий согласно
настройкам ``notifications`` (об ошибках и/или об успехе). Полностью
опциональны и не влияют на основную работу при сбое отправки.
"""
from __future__ import annotations

import queue
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from typing import List, Optional

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

    def send(self, subject: str, body: str) -> None:
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
