"""
Проверка восстановления (1.21.0): раз в месяц — выборочная «учебная» проверка, что из архива
действительно можно восстановить письма.

Полная проверка целостности (задание «Проверка целостности») сверяет все файлы ящика; учения
отвечают на другой вопрос — «если сегодня понадобится восстановить почту, получится ли»:

1. случайные письма из каждого ящика (``drill.sample`` на ящик) читаются из хранилища (с
   расшифровкой), их SHA-256 сверяется с записанной при сохранении, письмо разбирается целиком
   (заголовки, части, вложения) — как при восстановлении и выгрузке;
2. проверяется, что снимок базы и копия вне сервера (если включены) свежие;
3. самый свежий снимок базы разворачивается на пробу (1.26.0, ``drill.snapshot``): проверяется подпись,
   SQLite — распаковка во временный файл, ``PRAGMA quick_check`` и число ящиков и писем; PostgreSQL — выгрузка
   читается целиком той же проверкой, что перед ``psql`` (без разворачивания), и считаются строки таблиц;
4. итог — в событиях задания и аудите (``restore_drill``); при любой проблеме — уведомление.
"""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import uuid
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


def _snapshot_drill(svc, event=None) -> Dict:
    """Развернуть самый свежий снимок базы на пробу. → ``{"name", "problems", "notes", "info"}``."""
    from .replica import snapshots
    from .util import disk_free_bytes, human_size
    out: Dict = {"name": "", "problems": [], "notes": [], "info": ""}
    pg = bool(getattr(svc.db, "is_pg", False))
    snaps = [s for s in snapshots.list_snapshots(svc.cfg) if s["kind"] == ("postgresql" if pg else "sqlite")]
    if not snaps:
        return out
    snap = snaps[0]
    name = out["name"] = snap["name"]
    sig = snapshots.verify_signature(svc.db.secret, snap["path"])
    if sig["status"] == "missing":
        if not snap["encrypted"]:
            out["notes"].append(f"снимок {name}: без подписи (снят до 1.26.0) — restore-snapshot попросит "
                                f"--allow-unsigned")
    elif sig["status"] != "ok":
        out["problems"].append(f"снимок {name}: {sig['message']}")
        return out
    cipher = None
    if snap["encrypted"]:
        cipher = getattr(svc.store, "cipher", None)
        if cipher is None:
            out["problems"].append(f"снимок {name} зашифрован, а ключа шифрования копии у службы нет — им не "
                                   f"восстановиться")
            return out
    if pg:
        try:
            rows = snapshots.check_pg_snapshot(snap["path"], cipher)
        except Exception as exc:  # noqa: BLE001 - любой сбой чтения = снимок непригоден
            out["problems"].append(f"снимок {name} не читается: {getattr(exc, 'message', None) or exc}")
            return out
        accounts, messages = rows.get("accounts", 0), rows.get("messages", 0)
        out["info"] = (f"снимок {name} прочитан целиком (выгрузка PostgreSQL, проверка как перед psql): ящиков "
                       f"{accounts}, писем {messages}")
        # и разворачивается во временную базу на том же сервере (1.27.0), как restore-snapshot
        try:
            trial = snapshots.trial_pg_restore(svc.cfg.db_url, snap["path"], cipher)
        except Exception as exc:  # noqa: BLE001 - psql или сервер базы не приняли снимок
            out["problems"].append(f"снимок {name} не разворачивается во временную базу: "
                                   f"{getattr(exc, 'message', None) or exc}")
            return out
        if trial.get("skipped"):
            out["info"] += f"; во временную базу не развёрнут: {trial['skipped']}"
        else:
            if (trial["accounts"], trial["messages"]) != (accounts, messages):
                out["problems"].append(f"снимок {name}: в выгрузке ящиков {accounts} и писем {messages}, а "
                                       f"развёрнуто {trial['accounts']} и {trial['messages']}")
                return out
            out["info"] = (f"снимок {name} развёрнут на пробу во временную базу PostgreSQL (psql, база удалена): "
                           f"ящиков {accounts}, писем {messages}")
    else:
        tmp_dir = svc.cfg.tmp_dir
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            need = int(os.path.getsize(svc.cfg.db_path) * 1.2) + 64 * 1024 * 1024
        except OSError:
            need = 64 * 1024 * 1024
        free = disk_free_bytes(tmp_dir)
        if free < need:
            out["notes"].append(f"снимок {name} не развёрнут на пробу: во временном каталоге свободно "
                                f"{human_size(free)}, нужно около {human_size(need)}")
            return out
        dest = os.path.join(tmp_dir, f"drill-{uuid.uuid4().hex}.db")
        try:
            snapshots.restore_snapshot(snap["path"], dest, cipher)
            conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            try:
                accounts = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] or 0)
                messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            out["problems"].append(f"снимок {name} не разворачивается: {getattr(exc, 'message', None) or exc}")
            return out
        finally:
            for suffix in ("", "-wal", "-shm", ".restore-tmp"):
                try:
                    os.unlink(dest + suffix)
                except OSError:
                    pass
        out["info"] = f"снимок {name} развёрнут на пробу (quick_check — ok): ящиков {accounts}, писем {messages}"
    try:
        now_messages = int(svc.db.scalar("SELECT COUNT(*) FROM messages") or 0)
    except Exception:  # noqa: BLE001
        now_messages = 0
    if now_messages and not messages:
        out["notes"].append(f"в снимке {name} нет ни одного письма, а в базе их {now_messages}")
    if event is not None:
        event("INFO", out["info"][:1].upper() + out["info"][1:] + ".")
    return out


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
    try:
        from . import settings_backup
        if bool(svc.rt("settings_backup", "enabled")):
            age = settings_backup.daily_age_s(svc.cfg)
            if age is not None and age > 2 * 86400:     # первую делает ежечасное обслуживание
                notes.append(f"копия настроек: последний раз {int(age // 86400)} дн. назад")
    except Exception as exc:  # noqa: BLE001
        log.debug("копия настроек: состояние не проверено: %s", exc)
    snap: Dict = {"name": "", "problems": [], "notes": [], "info": ""}
    if (cancelled is None or not cancelled()) and _enabled(svc, "snapshot"):
        try:
            snap = _snapshot_drill(svc, event)
        except Exception as exc:  # noqa: BLE001
            log.exception("Учебное разворачивание снимка не удалось")
            snap["problems"].append(f"снимок базы не проверен: {exc}")
        for text in snap["problems"]:
            bad += 1
            problems.append(text)
            if event is not None:
                event("ERROR", text[:1].upper() + text[1:])
        notes.extend(snap["notes"])
    ok = not bad and not notes
    summary = (f"Проверено писем: {checked} в {len(accounts)} ящиках; повреждённых или нечитаемых: "
               f"{bad - len(snap['problems'])}."
               + (f" Снимок базы: {snap['info']}." if snap["info"] and not snap["problems"] else "")
               + (" Снимок базы: " + "; ".join(snap["problems"]) + "." if snap["problems"] else "")
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
            "summary": summary, "snapshot": snap}


def _enabled(svc, key: str) -> bool:
    try:
        return bool(svc.rt("drill", key))
    except Exception:  # noqa: BLE001
        return True
