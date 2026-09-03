"""
Уведомления по e-mail (SMTP). Отправляются по завершении заданий согласно
настройкам ``notifications`` (об ошибках и/или об успехе). Полностью
опциональны и не влияют на основную работу при сбое отправки.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import List

from .logging_setup import get_logger

log = get_logger("notify")


class Notifier:
    def __init__(self, services) -> None:
        self.services = services

    def _cfg(self) -> dict:
        return self.services.cfg.notifications

    def enabled(self) -> bool:
        return bool(self.services.db.get_setting("notifications.enabled",
                                                 self._cfg().get("enabled", False)))

    def notify_job(self, job_type: str, status: str, subject: str, body: str) -> None:
        cfg = self._cfg()
        if not self.enabled():
            return
        on_failure = self.services.db.get_setting("notifications.on_failure", cfg.get("on_failure", True))
        on_success = self.services.db.get_setting("notifications.on_success", cfg.get("on_success", False))
        is_failure = status in ("failed", "partial")
        if is_failure and not on_failure:
            return
        if not is_failure and not on_success:
            return
        try:
            self.send(subject, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить уведомление: %s", exc)

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
