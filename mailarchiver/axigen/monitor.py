"""
Состояние сервера Axigen: версия, лицензия, домены, очередь — и что требует внимания.

Проверка (раз в 15 минут, если включена) открывает одну сессию CLI, читает
состояние и сравнивает его с порогами из настроек. О новой проблеме
администраторы узнают письмом (если включены уведомления) — не чаще раза в
сутки на каждую; в разделе «Сервер Axigen» и в метриках /metrics она видна
сразу. Состояние хранится в базе (``meta``), чтобы раздел открывался без
обращения к серверу.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..logging_setup import get_logger
from . import parse
from . import store as axstore
from .cli import AxigenCLI
from .common import AxigenError, AxigenSettings, load_settings, tls_unverified

log = get_logger("axigen.monitor")

STATUS_KEY = "axigen_status"
ALERTED_KEY = "axigen_alerted"
#: Через сколько неудачных проверок подряд «сервер недоступен» становится проблемой.
UNREACHABLE_AFTER = 2
ALERT_REPEAT_HOURS = 24


def read_status(cli: AxigenCLI) -> Dict:
    """Версия, лицензия, домены и очередь — каждое поле отдельно, ошибки не мешают остальным."""
    out: Dict = {"version": cli.version, "license": {}, "license_raw": {}, "domains": [], "queue": None,
                 "warnings": []}
    try:
        reply = cli.run("SHOW LicenseInfo")
        raw = parse.parse_show(reply.lines)
        out["license_raw"] = {k[:60]: v[:200] for k, v in list(raw.items())[:40]}
        out["license"] = parse.parse_license(raw, reply.lines)
    except AxigenError as exc:
        out["warnings"].append(f"лицензия: {exc.message}")
    try:
        reply = cli.run("LIST Domains")
        table = parse.parse_table(reply.lines)
        name_col = table.column("name", "domain") or (table.columns[0] if table.columns else "name")
        for row in table.rows[:500]:
            name = (row.get(name_col) or "").split()[0] if (row.get(name_col) or "").strip() else ""
            if not name:
                continue
            item = {"name": name.lower(), "fields": {table.titles[i] if i < len(table.titles) else k: v
                                                     for i, (k, v) in enumerate(row.items()) if k != name_col}}
            out["domains"].append(item)
    except AxigenError as exc:
        out["warnings"].append(f"домены: {exc.message}")
    try:
        cli.to_root()
        cli.run("ENTER QUEUE")
        reply = cli.run("LIST")
        out["queue"] = count_queue(reply.lines)
    except AxigenError as exc:
        out["warnings"].append(f"очередь: {exc.message}")
    finally:
        try:
            cli.to_root()
        except AxigenError:
            pass
    return out


def count_queue(lines: List[str]) -> int:
    """Число писем в ответе ``LIST`` очереди (строки таблицы; «Total: N», если сервер его пишет)."""
    for ln in lines:
        low = ln.strip().lower()
        if low.startswith(("total", "всего")) and any(ch.isdigit() for ch in low):
            number = parse.parse_int(low.split(":", 1)[-1].strip().split()[0]) if ":" in low else None
            if number is not None:
                return number
    table = parse.parse_table(lines)
    rows = [r for r in table.raw_rows if r.strip() and not r.strip().lower().startswith(("total", "всего",
                                                                                          "no ", "queue is empty"))]
    if len(table.columns) == 1:
        rows = [r for r in rows if not r.strip().endswith(":")]
    return len(rows)


def refresh(svc, *, username: str = "system", scheduled: bool = False) -> Dict:
    """Прочитать состояние сервера и сохранить его (с итогом, удалась ли проверка)."""
    from .service import logged_session
    st = load_settings(svc)
    prev = cached(svc)
    now = datetime.now(timezone.utc).isoformat()
    try:
        st.require()
        with logged_session(svc, username=username, kind="monitor" if scheduled else "read",
                            summary="Проверка состояния сервера", st=st) as cli:
            status = read_status(cli)
        status["ok"] = True
        status["checked_at"] = now
        status["failures"] = 0
        status["last_ok"] = now
    except AxigenError as exc:
        status = dict(prev) if prev else {}
        status["ok"] = False
        status["error"] = exc.message
        status["hint"] = exc.hint
        status["checked_at"] = now
        status["failures"] = int(prev.get("failures") or 0) + 1 if prev else 1
    svc.db.set_meta(STATUS_KEY, json.dumps(status, ensure_ascii=False, default=str))
    return status


def cached(svc) -> Dict:
    try:
        return json.loads(svc.db.get_meta(STATUS_KEY) or "{}")
    except (TypeError, ValueError):
        return {}


def license_days_left(status: Dict, today: Optional[date] = None) -> Optional[int]:
    expires = (status.get("license") or {}).get("expires")
    if not expires:
        return None
    try:
        day = date.fromisoformat(str(expires)[:10])
    except ValueError:
        return None
    return (day - (today or date.today())).days


def problems(svc, st: Optional[AxigenSettings] = None, status: Optional[Dict] = None) -> List[Dict]:
    """Что требует внимания: [{key, level, text}] — по состоянию сервера и последней сверке."""
    from .directory import is_axigen_mailbox, match
    st = st or load_settings(svc)
    if not st.enabled:
        return []
    status = status if status is not None else cached(svc)
    out: List[Dict] = []

    def add(key: str, level: str, text: str) -> None:
        out.append({"key": key, "level": level, "text": text})

    unverified = tls_unverified(st)
    if unverified:
        add("tls_unverified", "warning",
            f"Сертификат сервера Axigen не проверяется ({', '.join(unverified)}): соединение шифруется, но подмену "
            f"сервера никто не заметит — пароль администратора получил бы тот, кто встал посередине. Включите "
            f"«Проверять сертификат сервера» или доверьтесь сертификату по отпечатку («Проверить подключение»).")
    if status and not status.get("ok") and int(status.get("failures") or 0) >= UNREACHABLE_AFTER:
        add("unreachable", "error", f"Сервер Axigen недоступен ({status.get('failures')} проверок подряд): "
                                    f"{status.get('error') or 'нет ответа'}.")
    queue = status.get("queue") if status else None
    try:
        limit = int(svc.rt("axigen", "alert_queue") or 0)
    except (TypeError, ValueError):
        limit = 0
    if queue is not None and limit and int(queue) >= limit:
        add("queue", "warning", f"В очереди Axigen {queue} писем (порог {limit}) — возможно, почта не уходит.")
    days = license_days_left(status or {})
    try:
        warn_days = int(svc.rt("axigen", "alert_license_days") or 0)
    except (TypeError, ValueError):
        warn_days = 0
    if days is not None and warn_days and days <= warn_days:
        add("license", "error" if days <= 7 else "warning",
            f"Лицензия Axigen {'истекла' if days < 0 else f'истекает через {days} дн.'} "
            f"({(status.get('license') or {}).get('expires')}).")
    rows = axstore.list_accounts(svc.db)
    try:
        percent = float(svc.rt("axigen", "alert_quota_percent") or 0)
    except (TypeError, ValueError):
        percent = 0
    if percent:
        full = []
        for row in rows:
            if row.get("deleted_at") or not row.get("quota_kb") or row.get("used_bytes") is None:
                continue
            if 100.0 * row["used_bytes"] / (row["quota_kb"] * 1024) >= percent:
                full.append(row["email"])
        if full:
            add("quota", "warning", f"Ящики у предела квоты на сервере (≥ {percent:g} %): {len(full)} — "
                                    f"{', '.join(full[:10])}{' …' if len(full) > 10 else ''}.")
    if rows:
        accounts = svc.db.list_accounts()
        matched = match(rows, accounts, st)
        live = [r for r in rows if not r.get("deleted_at")]
        missing = [r["email"] for r in live if r["email"] not in matched]
        if missing:
            add("unarchived", "warning", f"Учётные записи Axigen без копии в архиве: {len(missing)} — "
                                         f"{', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}.")
        gone = [r["email"] for r in rows if r.get("deleted_at") and r["email"] in matched
                and matched[r["email"]].enabled]
        if gone:
            add("gone", "warning", f"Удалены на сервере, а копирование ящика включено: {len(gone)} — "
                                   f"{', '.join(gone[:10])}. Выключите копирование (архив сохранится).")
        stuck = [r["email"] for r in live if r.get("login_blocked") and r["email"] in matched
                 and matched[r["email"]].enabled and matched[r["email"]].auth_type not in ("axigen_ftp",)
                 and is_axigen_mailbox(matched[r["email"]], st)]
        if stuck:
            add("blocked_imap", "warning",
                f"Вход заблокирован на сервере, а ящик копируется по IMAP — копирование не пройдёт: {len(stuck)} "
                f"— {', '.join(stuck[:10])}. Переведите их на копирование через FTP Backup.")
    return out


def tick(svc) -> Optional[Dict]:
    """Проверка по расписанию: обновить состояние и разослать письма о новых проблемах."""
    st = load_settings(svc)
    if not st.enabled or not bool(svc.rt("axigen", "monitor_enabled")):
        return None
    if not st.configured:
        return None
    status = refresh(svc, scheduled=True)
    found = problems(svc, st, status)
    notify_new(svc, found)
    return status


def notify_new(svc, found: List[Dict]) -> List[str]:
    """Письмо о проблемах, о которых ещё не сообщали (или сообщали больше суток назад)."""
    try:
        alerted = json.loads(svc.db.get_meta(ALERTED_KEY) or "{}")
    except (TypeError, ValueError):
        alerted = {}
    now = datetime.now(timezone.utc)
    fresh = []
    for item in found:
        last = alerted.get(item["key"])
        try:
            when = datetime.fromisoformat(last) if last else None
        except ValueError:
            when = None
        if when is None or now - when >= timedelta(hours=ALERT_REPEAT_HOURS):
            fresh.append(item)
    keys = {item["key"] for item in found}
    alerted = {k: v for k, v in alerted.items() if k in keys}       # проблема ушла — забыть
    sent: List[str] = []
    if fresh and svc.notifier.enabled():
        body = "Сервер Axigen требует внимания:\n\n" + "\n".join(f"• {i['text']}" for i in fresh) + \
               "\n\nПодробности — в разделе «Сервер Axigen» MailArchiver."
        try:
            svc.notifier.send("MailArchiver: сервер Axigen требует внимания", body)
            sent = [i["key"] for i in fresh]
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить письмо о состоянии Axigen: %s", exc)
    for key in sent:
        alerted[key] = now.isoformat()
    svc.db.set_meta(ALERTED_KEY, json.dumps(alerted))
    return sent


def metrics(svc) -> List[tuple]:
    """Метрики для /metrics: (имя, описание, вид, [(метки, значение)])."""
    st = load_settings(svc)
    if not st.enabled:
        return []
    status = cached(svc)
    rows = axstore.list_accounts(svc.db)
    live = [r for r in rows if not r.get("deleted_at")]
    out = [
        ("mailarchiver_axigen_up", "Последняя проверка сервера Axigen удалась (1/0)", "gauge",
         [({}, 1 if status.get("ok") else 0)] if status else []),
        ("mailarchiver_axigen_queue_messages", "Писем в очереди Axigen", "gauge",
         [({}, status.get("queue"))] if status.get("queue") is not None else []),
        ("mailarchiver_axigen_license_days_left", "Дней до окончания лицензии Axigen", "gauge",
         [({}, license_days_left(status))] if license_days_left(status) is not None else []),
        ("mailarchiver_axigen_accounts", "Учётные записи Axigen по последней сверке", "gauge",
         [({"state": "total"}, len(live)),
          ({"state": "blocked"}, sum(1 for r in live if r.get("login_blocked"))),
          ({"state": "deleted"}, sum(1 for r in rows if r.get("deleted_at")))]),
    ]
    probs = {p["key"]: p for p in problems(svc, st, status)}
    out.append(("mailarchiver_axigen_problems", "Что требует внимания на сервере Axigen (1 — есть)", "gauge",
                [({"problem": key}, 1 if key in probs else 0)
                 for key in ("unreachable", "queue", "license", "quota", "unarchived", "gone", "blocked_imap",
                             "tls_unverified")]))
    return out


def summary_lines(svc) -> List[str]:
    """Строки для еженедельной сводки администраторам."""
    st = load_settings(svc)
    if not st.enabled:
        return []
    status = cached(svc)
    lines = []
    if status:
        when = status.get("checked_at") or ""
        state = "доступен" if status.get("ok") else f"НЕДОСТУПЕН ({status.get('error') or 'нет ответа'})"
        version = status.get("version") or ""
        lines.append(f"Сервер Axigen {st.host}: {state}{f', версия {version}' if version else ''}"
                     f"{f', проверен {when[:16].replace(chr(84), chr(32))} UTC' if when else ''}.")
    for item in problems(svc, st, status):
        lines.append(f"  • {item['text']}")
    return lines
