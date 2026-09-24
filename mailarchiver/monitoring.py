"""
Мониторинг: метрики для Prometheus/Zabbix и еженедельная сводка администраторам.

``/metrics`` отдаёт состояние в текстовом формате Prometheus (его же понимает
Zabbix: элемент «HTTP-агент» + предобработка «Prometheus pattern»). Доступ —
по токену (``Authorization: Bearer …``) и/или с разрешённых адресов; без
настройки — только с самого сервера.

Сводка (``notifications.summary_cron``, по умолчанию по понедельникам в 8:00)
приходит письмом: что с ящиками (неверные пароли, давно не копировались,
ошибки), сколько скопировано за неделю, место на диске и прогноз, копия вне
сервера, события безопасности. Ящики «требуют внимания» по тем же правилам,
что и на дашборде.
"""
from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .logging_setup import get_logger
from .models import account_has_credentials
from .util import human_size
from .version import __version__

log = get_logger("monitoring")

#: Через сколько часов без удачной копии включённый ящик считается «давно не копировался»
#: (как фильтр «Копия старше 72 ч» в разделе «Почтовые ящики»).
STALE_HOURS = 72
#: Как часто пересчитывать тяжёлую часть (итоги по ящикам): Prometheus опрашивает
#: раз в 15–60 секунд, а полный подсчёт писем на большом архиве занимает секунды.
HEAVY_TTL_S = 300
#: Лёгкие метрики (очередь, диск) — не чаще раза в 10 секунд.
LIGHT_TTL_S = 10

_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, object]] = {}


def _cached(name: str, ttl: float, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[name] = (now, value)
    return value


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
#  Сбор состояния
# ---------------------------------------------------------------------------
def account_problems(svc) -> Dict:
    """Ящики по состояниям — по тем же правилам, что дашборд и отборы ящиков."""
    def compute():
        now = datetime.now(timezone.utc)
        totals = svc.db.message_totals_by_account()
        last_runs = svc.db.last_runs_by_account()
        rows = []
        out = {"total": 0, "enabled": 0, "badpw": [], "nobackup": [], "stale": [], "failed": [],
               "nopassword": [], "accounts": rows, "messages": 0, "bytes": 0}
        for acc in svc.db.list_accounts():
            t = totals.get(acc.id) or {}
            msgs, size = int(t.get("messages", 0)), int(t.get("bytes", 0))
            out["messages"] += msgs
            out["bytes"] += size
            out["total"] += 1
            last_ok = _parse_iso(acc.last_backup_at)
            has_secret = account_has_credentials(acc)
            run = last_runs.get(acc.id)
            item = {"id": acc.id, "name": acc.name, "username": acc.username, "enabled": bool(acc.enabled),
                    "messages": msgs, "bytes": size, "last_backup_at": acc.last_backup_at or "",
                    "login_ok": acc.login_status == "ok", "login_status": acc.login_status or ""}
            rows.append(item)
            if not acc.enabled:
                continue
            out["enabled"] += 1
            if acc.secret_broken or acc.login_status in ("auth_error", "secret_broken"):
                out["badpw"].append(item)
            elif not has_secret:
                out["nopassword"].append(item)
            if not acc.last_backup_at and not msgs:
                out["nobackup"].append(item)
            elif last_ok is None or now - last_ok > timedelta(hours=STALE_HOURS):
                out["stale"].append(item)
            status = run["status"] if run is not None else (acc.last_backup_status or "")
            if status == "failed":
                out["failed"].append(item)
        return out
    return _cached("accounts", HEAVY_TTL_S, compute)


def collect(svc) -> Dict:
    """Всё состояние для метрик и сводки."""
    acc = account_problems(svc)

    def light():
        counts = svc.db.count_jobs_by_status()
        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        failed_24h = int(svc.db.scalar("SELECT COUNT(*) FROM jobs WHERE status='failed' AND finished_at>=?",
                                       (since_24h,)) or 0)
        try:
            usage = shutil.disk_usage(svc.cfg.mail_root)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            disk = {"free": 0, "total": 0}
        since_1h = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        failures = {r["kind"] or "password": int(r["c"]) for r in svc.db.query(
            "SELECT kind, COUNT(*) AS c FROM login_attempts WHERE success=0 AND ts>=? GROUP BY kind", (since_1h,))}
        return {"jobs": {"queued": int(counts.get("queued", 0)), "running": int(counts.get("running", 0)),
                         "failed_24h": failed_24h},
                "disk": disk, "login_failures_1h": failures}
    state = _cached("light", LIGHT_TTL_S, light)
    from .replica import runner as replica_runner
    from .replica import snapshots
    rep = replica_runner.status(svc)
    snaps = snapshots.list_snapshots(svc.cfg)
    return {"accounts": acc, **state,
            "encryption": {"active": bool(svc.store.encrypt), "blocked": bool(svc.store.encryption_blocked)},
            "scheduler_running": bool(svc.scheduler.running()),
            "replica": rep, "snapshot": snaps[0] if snaps else None}


# ---------------------------------------------------------------------------
#  Prometheus
# ---------------------------------------------------------------------------
def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _ts(value) -> Optional[float]:
    dt = _parse_iso(value)
    return dt.timestamp() if dt else None


class _Out:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def metric(self, name: str, help_text: str, kind: str, samples) -> None:
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if value is None:
                continue
            lab = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) if labels else ""
            num = int(value) if isinstance(value, bool) else value
            text = repr(float(num)) if isinstance(num, float) else str(int(num))
            self.lines.append(f"{name}{{{lab}}} {text}" if lab else f"{name} {text}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render_prometheus(svc, *, per_account: bool = True) -> str:
    data = collect(svc)
    acc = data["accounts"]
    out = _Out()
    out.metric("mailarchiver_info", "Версия службы", "gauge", [({"version": __version__}, 1)])
    out.metric("mailarchiver_accounts", "Ящики по состояниям (как на дашборде)", "gauge", [
        ({"state": "total"}, acc["total"]), ({"state": "enabled"}, acc["enabled"]),
        ({"state": "bad_password"}, len(acc["badpw"])), ({"state": "no_backup"}, len(acc["nobackup"])),
        ({"state": "stale"}, len(acc["stale"])), ({"state": "last_failed"}, len(acc["failed"])),
        ({"state": "no_password"}, len(acc["nopassword"]))])
    out.metric("mailarchiver_messages", "Писем в архиве", "gauge", [({}, acc["messages"])])
    out.metric("mailarchiver_messages_bytes", "Объём писем в архиве, байт", "gauge", [({}, acc["bytes"])])
    jobs = data["jobs"]
    out.metric("mailarchiver_jobs", "Задания в очереди и выполняющиеся", "gauge",
               [({"status": "queued"}, jobs["queued"]), ({"status": "running"}, jobs["running"])])
    out.metric("mailarchiver_jobs_failed_24h", "Заданий с ошибкой за сутки", "gauge", [({}, jobs["failed_24h"])])
    out.metric("mailarchiver_disk_free_bytes", "Свободно на диске с письмами, байт", "gauge",
               [({}, data["disk"]["free"])])
    out.metric("mailarchiver_disk_total_bytes", "Размер диска с письмами, байт", "gauge",
               [({}, data["disk"]["total"])])
    out.metric("mailarchiver_encryption_blocked", "Шифрование включено, а ключа нет (копирование стоит)", "gauge",
               [({}, data["encryption"]["blocked"])])
    out.metric("mailarchiver_scheduler_running", "Работает ли планировщик расписаний", "gauge",
               [({}, data["scheduler_running"])])
    rep = data["replica"]
    last = rep.get("last_run") or {}
    out.metric("mailarchiver_replica_enabled", "Включена ли копия вне сервера", "gauge", [({}, rep["enabled"])])
    out.metric("mailarchiver_replica_last_success_timestamp_seconds", "Время последней удачной копии вне сервера",
               "gauge", [({}, _ts(rep.get("last_ok")))])
    out.metric("mailarchiver_replica_last_run_ok", "Последний прогон копии вне сервера без ошибок (1/0)", "gauge",
               [({}, last.get("status") == "success")] if last else [])
    snap = data.get("snapshot")
    out.metric("mailarchiver_db_snapshot_timestamp_seconds", "Время самого свежего снимка базы", "gauge",
               [({}, _ts(snap["created_at"]))] if snap else [])
    out.metric("mailarchiver_login_failures_1h", "Неудачных входов за час по видам", "gauge",
               [({"kind": kind}, n) for kind, n in sorted(data["login_failures_1h"].items())]
               or [({"kind": "password"}, 0)])
    oldest = None
    now = datetime.now(timezone.utc)
    for item in acc["accounts"]:
        if not item["enabled"]:
            continue
        dt = _parse_iso(item["last_backup_at"])
        age = (now - dt).total_seconds() if dt else None
        if age is not None and (oldest is None or age > oldest):
            oldest = age
    out.metric("mailarchiver_oldest_backup_age_seconds",
               "Сколько секунд назад была последняя удачная копия у самого «отстающего» включённого ящика",
               "gauge", [({}, oldest)])
    if per_account:
        rows = acc["accounts"]

        def labels(item):
            return {"account_id": item["id"], "account": item["name"], "username": item["username"]}
        out.metric("mailarchiver_account_enabled", "Ящик включён в копирование", "gauge",
                   [(labels(i), i["enabled"]) for i in rows])
        out.metric("mailarchiver_account_last_backup_timestamp_seconds", "Последняя удачная копия ящика", "gauge",
                   [(labels(i), _ts(i["last_backup_at"])) for i in rows])
        out.metric("mailarchiver_account_login_ok", "Последний вход в ящик удался (1/0)", "gauge",
                   [(labels(i), i["login_ok"]) for i in rows if i["login_status"]])
        out.metric("mailarchiver_account_messages", "Писем ящика в архиве", "gauge",
                   [(labels(i), i["messages"]) for i in rows])
        out.metric("mailarchiver_account_bytes", "Объём писем ящика в архиве, байт", "gauge",
                   [(labels(i), i["bytes"]) for i in rows])
    return out.text()


# ---------------------------------------------------------------------------
#  Еженедельная сводка
# ---------------------------------------------------------------------------
def _names(items: List[Dict], limit: int = 20) -> str:
    shown = [f"{i['name']} ({i['username']})" if i.get("username") and i["username"] != i["name"] else i["name"]
             for i in items[:limit]]
    more = len(items) - limit
    return "; ".join(shown) + (f" и ещё {more}" if more > 0 else "")


def weekly_summary(svc, days: int = 7) -> Tuple[str, str]:
    """Тема и текст письма-сводки за последние ``days`` дней."""
    reset_cache()
    data = collect(svc)
    acc = data["accounts"]
    tz = svc.local_tz()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = since.isoformat()
    runs = svc.db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status IN ('success','partial') THEN 1 ELSE 0 END),0) AS ok, "
        "COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS bad, "
        "COALESCE(SUM(messages_new),0) AS msgs, COALESCE(SUM(bytes_new),0) AS bytes "
        "FROM runs WHERE type='backup' AND started_at>=?", (since_iso,))
    failed_jobs = int(svc.db.scalar("SELECT COUNT(*) FROM jobs WHERE status='failed' AND finished_at>=?",
                                    (since_iso,)) or 0)
    sec = svc.db.query_one(
        "SELECT COALESCE(SUM(CASE WHEN COALESCE(kind,'')='' THEN 1 ELSE 0 END),0) AS pw, "
        "COALESCE(SUM(CASE WHEN kind='otp' THEN 1 ELSE 0 END),0) AS otp, "
        "COALESCE(SUM(CASE WHEN kind='imap' THEN 1 ELSE 0 END),0) AS imap "
        "FROM login_attempts WHERE success=0 AND ts>=?", (since_iso,))
    problems = len(acc["badpw"]) + len(acc["nobackup"]) + len(acc["stale"]) + len(acc["failed"])
    rep = data["replica"]
    rep_problem = False
    period = f"{since.astimezone(tz):%d.%m}–{now.astimezone(tz):%d.%m.%Y}"
    lines = [f"MailArchiver {__version__}: сводка за {period}", ""]
    lines.append(f"Ящиков: {acc['total']} (копируются: {acc['enabled']}).")
    if runs is not None:
        lines.append(f"Копирование за неделю: прогонов {runs['n']}, удачных {runs['ok']}, с ошибкой {runs['bad']}; "
                     f"новых писем {int(runs['msgs']):,} ({human_size(int(runs['bytes']))}).".replace(",", " "))
    lines.append(f"Архив: писем {acc['messages']:,}, объём {human_size(acc['bytes'])}.".replace(",", " "))
    disk = data["disk"]
    if disk["total"]:
        text = f"Диск с письмами: свободно {human_size(disk['free'])} из {human_size(disk['total'])}"
        grow = _daily_growth(svc, 28)
        if grow > 0:
            days_left = int(disk["free"] / grow)
            text += f"; при нынешнем приросте (~{human_size(grow)} в день) места хватит примерно на {days_left} дн."
            if days_left < 60:
                problems += 1
                text += " ⚠ пора расширять диск или сокращать сроки хранения"
        lines.append(text + ".")
    lines.append("")
    if problems:
        lines.append("ТРЕБУЮТ ВНИМАНИЯ")
        for key, title in (("badpw", "Неверный пароль (копирование не идёт)"),
                           ("failed", "Последнее копирование с ошибкой"),
                           ("stale", f"Нет удачной копии дольше {STALE_HOURS} ч"),
                           ("nobackup", "Ни одной резервной копии")):
            if acc[key]:
                lines.append(f"• {title} — {len(acc[key])}: {_names(acc[key])}")
        lines.append("")
    else:
        lines.append("Все включённые ящики копируются без замечаний.")
        lines.append("")
    if acc["nopassword"]:
        lines.append(f"Включены, но без пароля — {len(acc['nopassword'])}: {_names(acc['nopassword'], 10)}")
    if failed_jobs:
        lines.append(f"Заданий с ошибкой за неделю: {failed_jobs} (подробно — «Очередь и задания»).")
    if rep["enabled"]:
        last = rep.get("last_run") or {}
        last_ok = _parse_iso(rep.get("last_ok"))
        if last_ok is None or now - last_ok > timedelta(hours=48):
            rep_problem = True
            lines.append("⚠ Копия вне сервера: удачного прогона не было больше 48 ч"
                         + (f" (последний: {last.get('status')} — {last.get('message', '')[:300]})" if last else "") + ".")
        else:
            lines.append(f"Копия вне сервера: последняя удачная {last_ok.astimezone(tz):%d.%m %H:%M}"
                         + (" — С ЗАМЕЧАНИЯМИ: " + last.get("message", "")[:300] if last.get("status") == "partial" else "")
                         + ".")
    else:
        lines.append("Копия вне сервера не настроена: архив хранится в одном экземпляре "
                     "(«Настройки → Копия вне сервера и снимки базы»).")
    snap = data.get("snapshot")
    if snap:
        created = _parse_iso(snap["created_at"])
        lines.append(f"Снимок базы: {created.astimezone(tz):%d.%m %H:%M}, {human_size(snap['size'])}.")
    else:
        lines.append("Снимков базы нет.")
    if data["encryption"]["blocked"]:
        problems += 1
        lines.append("⛔ Шифрование включено, а ключа нет — новые письма не сохраняются!")
    if sec is not None and (sec["pw"] or sec["otp"] or sec["imap"]):
        lines.append(f"Безопасность за неделю: неудачных входов {sec['pw']}, неверных кодов 2FA {sec['otp']}, "
                     f"неудачных проверок паролей ящиков {sec['imap']}.")
    lines.append("")
    base = str(svc.rt("server", "public_url") or "").rstrip("/")
    if base:
        lines.append(f"Открыть MailArchiver: {base}/")
    lines.append("Письмо отправлено службой MailArchiver (настройка «Еженедельная сводка» в разделе «Уведомления»).")
    total_problems = problems + (1 if rep_problem else 0)
    subject = (f"MailArchiver: сводка за неделю — требуют внимания: {total_problems}" if total_problems
               else "MailArchiver: сводка за неделю — всё в порядке")
    return subject, "\n".join(lines) + "\n"


def _daily_growth(svc, days: int) -> float:
    """Средний прирост архива в байтах в день за последние ``days`` дней."""
    since = (datetime.now(svc.local_tz()).date() - timedelta(days=days)).isoformat()
    total = int(svc.db.scalar("SELECT COALESCE(SUM(bytes),0) FROM stats_daily WHERE day>=?", (since,)) or 0)
    return total / float(days) if total else 0.0


def send_weekly_summary(svc) -> Dict:
    """Отправить сводку сейчас. Не бросает исключений — возвращает итог."""
    subject, body = weekly_summary(svc)
    try:
        svc.notifier.send(subject, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("Сводка не отправлена: %s", exc)
        return {"ok": False, "error": str(exc), "subject": subject}
    svc.db.set_meta("summary_last_sent", datetime.now(timezone.utc).isoformat())
    return {"ok": True, "subject": subject}


# ---------------------------------------------------------------------------
#  Доступ к /metrics
# ---------------------------------------------------------------------------
def metrics_access(svc, client_ip: str, auth_header: str) -> Tuple[bool, int]:
    """(разрешено, http-код при отказе). Токен сравнивается за постоянное время."""
    import hmac
    import ipaddress
    if not bool(svc.rt("monitoring", "metrics_enabled")):
        return False, 404
    token = str(svc.rt("monitoring", "metrics_token") or "")
    if token:
        given = ""
        if auth_header.lower().startswith("bearer "):
            given = auth_header[7:].strip()
        if given and hmac.compare_digest(given.encode(), token.encode()):
            return True, 200
    allowed = svc.rt("monitoring", "metrics_allowed_ips") or []
    if isinstance(allowed, str):
        allowed = [p for p in allowed.replace(";", ",").split(",")]
    try:
        ip = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return False, 401 if token else 403
    for item in allowed:
        item = str(item).strip()
        if not item:
            continue
        try:
            if ip in ipaddress.ip_network(item, strict=False):
                return True, 200
        except ValueError:
            continue
    return False, 401 if token else 403
