"""
Проверка восстановления (1.21.0): раз в месяц — выборочная «учебная» проверка, что из архива
действительно можно восстановить письма.

Полная проверка целостности (задание «Проверка целостности») сверяет все файлы ящика; учения
отвечают на другой вопрос — «если сегодня понадобится восстановить почту, получится ли»:

1. случайные письма из каждого ящика (``drill.sample`` на ящик) читаются из хранилища (с
   расшифровкой), их SHA-256 сверяется с записанной при сохранении, письмо разбирается целиком
   (заголовки, части, вложения) — как при восстановлении и выгрузке;
2. проверяется, что снимок базы и копия вне сервера (если включены) свежие;
3. итог — в событиях задания и аудите (``restore_drill``); при любой проблеме — уведомление.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from typing import Dict, List

from .logging_setup import get_logger

log = get_logger("drill")

MAX_SAMPLE = 1000


def _check_message(svc, row) -> str:
    """'' — письмо читается и цело; иначе — что не так."""
    try:
        raw = svc.store.read_message(int(row["account_id"]), row["stored_path"])
    except Exception as exc:  # noqa: BLE001 - нет файла, нет ключа, сбой диска
        return f"не читается: {getattr(exc, 'message', None) or exc}"
    if row["sha256"] and hashlib.sha256(raw).hexdigest() != row["sha256"]:
        return "содержимое изменилось (SHA-256 не совпадает с записанной при сохранении)"
    try:
        msg = message_from_bytes(raw, policy=policy.default)
        for part in msg.walk():
            if not part.is_multipart():
                part.get_payload(decode=True)
    except Exception as exc:  # noqa: BLE001
        return f"не разбирается как письмо: {exc}"
    return ""


def run(svc, *, progress=None, cancelled=None, event=None) -> Dict:
    try:
        sample = max(1, min(MAX_SAMPLE, int(svc.rt("drill", "sample") or 50)))
    except (TypeError, ValueError):
        sample = 50
    rng = random.SystemRandom()
    accounts = svc.db.list_accounts()
    checked = bad = 0
    problems: List[str] = []
    for n, acc in enumerate(accounts, start=1):
        if cancelled is not None and cancelled():
            break
        total = int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND stored_path<>''",
                                  (acc.id,)) or 0)
        if not total:
            continue
        offsets = sorted(rng.sample(range(total), min(sample, total)))
        for off in offsets:
            row = svc.db.query_one("SELECT id, account_id, stored_path, sha256 FROM messages WHERE account_id=? "
                                   "AND stored_path<>'' ORDER BY id LIMIT 1 OFFSET ?", (acc.id, off))
            if row is None:
                continue
            checked += 1
            err = _check_message(svc, row)
            if err:
                bad += 1
                problems.append(f"«{acc.name}», письмо #{row['id']}: {err}")
                if event is not None and bad <= 20:
                    event("ERROR", f"Ящик «{acc.name}», письмо #{row['id']}: {err}")
        if progress is not None:
            progress(n, len(accounts), f"Проверено ящиков: {n} из {len(accounts)}, писем: {checked}")
    notes: List[str] = []
    checks = [("db_snapshot", "снимок базы", False)]
    try:
        checks.append(("replicate", "копия вне сервера", bool(svc.rt("replica", "enabled"))))
    except Exception:  # noqa: BLE001
        pass
    for job_type, what, required in checks:
        try:
            row = svc.db.query_one("SELECT finished_at, status FROM jobs WHERE type=? AND finished_at IS NOT NULL "
                                   "ORDER BY id DESC LIMIT 1", (job_type,))
            if row is None:
                if required:
                    notes.append(f"{what}: включена, но ещё ни разу не делалась")
                continue
            if row["status"] not in ("success", "partial"):
                notes.append(f"{what}: последний раз — со статусом «{row['status']}»")
                continue
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["finished_at"])
            if age > timedelta(days=2):
                notes.append(f"{what}: последний раз {age.days} дн. назад")
        except Exception as exc:  # noqa: BLE001 - сведения о копиях — дополнение
            log.debug("%s: состояние не проверено: %s", what, exc)
    ok = not bad and not notes
    summary = (f"Проверено писем: {checked} в {len(accounts)} ящиках; повреждённых или нечитаемых: {bad}."
               + (" Замечания: " + "; ".join(notes) + "." if notes else ""))
    svc.db.add_audit("system", "restore_drill", summary[:1000])
    if not ok:
        notifier = getattr(svc, "notifier", None)
        if notifier is not None:
            try:
                notifier.send_all("MailArchiver: проверка восстановления нашла проблемы",
                                  summary + ("\n\n" + "\n".join(problems[:50]) if problems else ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("Уведомление о проверке восстановления не ушло: %s", exc)
    return {"checked": checked, "bad": bad, "notes": notes, "problems": problems[:200], "ok": ok,
            "summary": summary}
