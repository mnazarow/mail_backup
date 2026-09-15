# -*- coding: utf-8 -*-
"""
Аналитика MailArchiver.

Два независимых набора показателей:

  * :func:`system_analytics` — «Аналитика» по всем параметрам системы: хранилище,
    задания, прогоны, экспорт/восстановление, расписания, пользователи, безопасность,
    активность по дням. Считается из агрегатов БД — быстро, без чтения писем.

  * :func:`mail_analytics` — «Аналитика писем» по содержанию (метаданным) писем:
    отправители и домены, распределение по времени (год/месяц/день недели/час),
    гистограмма размеров, прочитанные/непрочитанные, вложения, папки, частотные
    слова темы, крупнейшие письма, тепловая карта «день недели × час».
    Считается из индекса писем (тема, отправитель, размер, дата, флаги) — тоже
    быстро и не требует распаковки .eml.

  * :func:`deep_scan` — тяжёлый разбор самих писем (.eml): типы вложений, домены
    получателей, соотношение текст/HTML, длина тела, эвристика языка, частотные
    слова тела. Запускается как фоновое задание (JobType.ANALYZE) и кэшируется
    в таблице settings, потому что читает каждое письмо с диска.

Все функции возвращают обычные словари/списки, готовые к сериализации в JSON.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from typing import Callable, Dict, List, Optional

from .models import JobStatus, JobType
from .util import human_size

# --- Русскоязычные подписи ---------------------------------------------------
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Небольшой список стоп-слов (рус./англ.) для частотного анализа темы/тела.
STOPWORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если
уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей
может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом
один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец
два об другой хоть после над больше тот через эти нас про всего них какая много
разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой
им более всегда конечно всю между
the a an and or but for nor of to in on at by with from as is are was were be been
being this that these those it its it's you your yours we our ours they their them
he she his her i me my mine will would can could should have has had do does did not
no yes re fw fwd if then else so than too very just about into over under out up
""".split())

# Границы гистограммы размеров писем (в байтах) и их подписи.
_SIZE_EDGES = [
    (0, 10 * 1024, "< 10 КБ"),
    (10 * 1024, 50 * 1024, "10–50 КБ"),
    (50 * 1024, 100 * 1024, "50–100 КБ"),
    (100 * 1024, 500 * 1024, "100–500 КБ"),
    (500 * 1024, 1024 * 1024, "0,5–1 МБ"),
    (1024 * 1024, 5 * 1024 * 1024, "1–5 МБ"),
    (5 * 1024 * 1024, 10 * 1024 * 1024, "5–10 МБ"),
    (10 * 1024 * 1024, float("inf"), "> 10 МБ"),
]

# Размер страницы при постраничном обходе индекса в глубоком анализе.
_SCAN_PAGE_SIZE = 2000
# Верхняя граница словаря частот: на большом архиве Counter со ВСЕМИ уникальными
# словами всех писем вырастает до сотен МБ, хотя наружу отдаётся только топ-60.
_WORDS_SOFT_LIMIT = 50_000
_WORDS_KEEP = 5_000

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")
_SUBJ_PREFIX_RE = re.compile(r"^\s*(re|fwd?|отв|переслать|пересл)\s*(\[\d+\])?\s*:", re.IGNORECASE)


# ---------------------------------------------------------------------------
#  Вспомогательные
# ---------------------------------------------------------------------------
def _parse_dt(s) -> Optional[datetime]:
    """Разобрать ISO-дату из индекса (с учётом смещения) → aware datetime UTC.

    Значение приходит из БД и может оказаться не строкой (например числом) —
    тогда ``s.replace`` падал с AttributeError, поэтому приводим к строке.
    """
    if not s:
        return None
    if not isinstance(s, str):
        s = str(s)
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError, AttributeError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _domain_of(addr: str) -> str:
    _, email_addr = parseaddr(addr or "")
    if "@" in email_addr:
        return email_addr.rsplit("@", 1)[1].lower().strip(">").strip()
    return ""


def _email_of(addr: str) -> str:
    _, email_addr = parseaddr(addr or "")
    return (email_addr or addr or "").lower().strip()


def _top(counter: Counter, n: int) -> List[Dict]:
    return [{"label": k, "value": int(v)} for k, v in counter.most_common(n)]


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# --- Размер архива на диске --------------------------------------------------
# Обход всего архива (отдельный файл на каждое письмо) на больших архивах
# занимает минуты, поэтому в обработчике запроса его делать нельзя. Показываем
# последнее сохранённое значение (кэш в settings), а сам обход выполняем в
# фоновом потоке не чаще раза в сутки.
_ARCHIVE_DISK_KEY = "analytics.archive_disk"
_ARCHIVE_DISK_TTL_S = 24 * 3600
_archive_scan_lock = threading.Lock()
_archive_scanning = False


def archive_disk_size(svc) -> int:
    """Размер архива на диске: из кэша; пересчёт — в фоне, не чаще раза в сутки."""
    cached = svc.db.get_setting(_ARCHIVE_DISK_KEY, None)
    value, stamp = 0, 0.0
    if isinstance(cached, dict):
        try:
            value = int(cached.get("bytes") or 0)
            stamp = float(cached.get("ts") or 0)
        except (ValueError, TypeError):
            value, stamp = 0, 0.0
    if time.time() - stamp > _ARCHIVE_DISK_TTL_S:
        _start_archive_scan(svc)
    if not value:
        # кэша ещё нет — показываем логический объём из индекса (без обхода ФС)
        value = int(svc.db.sum_message_bytes() or 0)
    return value


def _start_archive_scan(svc) -> None:
    """Запустить фоновый пересчёт размера архива (не более одного за раз)."""
    global _archive_scanning
    with _archive_scan_lock:
        if _archive_scanning:
            return
        _archive_scanning = True

    def _run() -> None:
        global _archive_scanning
        try:
            size = _dir_size(svc.cfg.mail_root)
            svc.db.set_setting(_ARCHIVE_DISK_KEY, {"bytes": size, "ts": time.time()})
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _archive_scan_lock:
                _archive_scanning = False

    threading.Thread(target=_run, name="archive-disk-scan", daemon=True).start()


# ===========================================================================
#  СИСТЕМНАЯ АНАЛИТИКА
# ===========================================================================
def system_analytics(svc, account_id: Optional[int] = None, *, days: int = 90) -> Dict:
    """Системная аналитика; при заданном account_id — строго по одному ящику.

    Раньше при фильтре скоупились только письма/объём/папки/прогоны, а задания,
    экспорты, восстановления, расписания, активность, безопасность и аудит
    считались по ВСЕЙ системе — на карточке «Аналитика ящика N» чужие задания и
    экспорты выглядели как свои. Теперь по ящику фильтруется всё, у чего есть
    привязка к ящику. Блоки, у которых её нет по смыслу (пользователи
    веб-интерфейса), при фильтрации не считаются: ключи остаются на месте, но
    значения пустые — см. ниже «Пользователи и безопасность».
    """
    db = svc.db
    accounts = db.list_accounts()
    if account_id is not None:
        accounts = [a for a in accounts if a.id == account_id]
    # ящик, по которому идёт разрез (None — разрез по всей системе)
    scoped = accounts[0] if (account_id is not None and accounts) else None

    # --- Хранилище и ящики ---
    total_messages = db.count_messages(account_id)
    total_bytes = db.sum_message_bytes(account_id)
    folders = db.distinct_folder_count(account_id)
    enabled = sum(1 for a in accounts if a.enabled)
    per_account = []
    for a in accounts:
        cnt = db.count_messages(a.id)
        by = db.sum_message_bytes(a.id)
        runs = db.list_runs(a.id, limit=1)
        last = runs[0] if runs else None
        per_account.append({
            "id": a.id, "name": a.name, "username": a.username, "enabled": a.enabled,
            "messages": cnt, "bytes": by, "bytes_h": human_size(by),
            "folders": db.distinct_folder_count(a.id),
            "retention_days": a.retention_days,
            "last_run": ({"status": last["status"], "finished_at": last["finished_at"],
                          "messages_new": last["messages_new"]} if last else None),
        })
    per_account.sort(key=lambda x: x["messages"], reverse=True)

    # --- Задания ---
    jobs_by_status: Counter = Counter()
    jobs_by_type: Counter = Counter()
    jobs_type_status: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in db.jobs_type_status_counts(account_id):
        jobs_by_status[r["status"]] += r["c"]
        jobs_by_type[r["type"]] += r["c"]
        jobs_type_status[r["type"]][r["status"]] += r["c"]
    total_jobs = sum(jobs_by_status.values())
    terminal = sum(jobs_by_status.get(s, 0) for s in JobStatus.TERMINAL)
    succeeded = jobs_by_status.get(JobStatus.SUCCESS, 0)
    success_rate = round(100.0 * succeeded / terminal, 1) if terminal else None
    durations = [{"type": r["type"], "type_label": JobType.LABELS.get(r["type"], r["type"]),
                  "count": r["c"], "avg_s": round(r["avg_s"] or 0, 1), "max_s": round(r["max_s"] or 0, 1)}
                 for r in db.jobs_duration_by_type(account_id)]
    durations.sort(key=lambda x: x["count"], reverse=True)

    # --- Прогоны (backup/restore/…): суммарно и по типам ---
    rt = db.runs_totals(account_id)
    runs_type_status: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in db.runs_type_status_counts(account_id):
        runs_type_status[r["type"]][r["status"]] += r["c"]

    # --- Экспорты / восстановления ---
    exp_by_format: Counter = Counter()
    exp_by_engine: Counter = Counter()
    exp_by_status: Counter = Counter()
    exp_bytes = 0
    exp_total = 0
    for r in db.exports_stats(account_id):
        exp_by_format[r["format"]] += r["c"]
        exp_by_engine[r["engine"]] += r["c"]
        exp_by_status[r["status"]] += r["c"]
        exp_bytes += r["bytes"] or 0
        exp_total += r["c"]
    restores = db.restores_totals(account_id)

    # --- Расписания ---
    schedules = db.list_schedules(account_id)
    sched_by_type: Counter = Counter()
    sched_enabled = 0
    upcoming = []
    for s in schedules:
        sched_by_type[s["job_type"]] += 1
        if s["enabled"]:
            sched_enabled += 1
        nxt = None
        try:
            nxt = svc.scheduler.next_run_for(s["id"]) or s["next_run"]
        except Exception:  # noqa: BLE001
            nxt = s["next_run"]
        if s["enabled"] and nxt:
            upcoming.append({"account_id": s["account_id"], "job_type": s["job_type"], "next_run": nxt})
    upcoming.sort(key=lambda x: x["next_run"] or "")

    # --- Активность по дням ---
    series = db.daily_series(days=days, account_id=account_id)
    activity = [{"day": r["day"], "messages": r["messages"] or 0, "bytes": r["bytes"] or 0,
                 "jobs": r["jobs"] or 0, "errors": r["errors"] or 0} for r in series][::-1]

    # --- Пользователи и безопасность ---
    # Сессии и неудачные входы у ящика свои: сессии с его account_id и попытки
    # входа под его логином. А пользователи веб-интерфейса к ящику отношения не
    # имеют — в разрезе по ящику список остаётся пустым (ключи не убираем, чтобы
    # не ломать структуру ответа).
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    if account_id is None:
        users_roles = {r["role"]: r["c"] for r in db.users_by_role()}
        login_failures_24h = db.count_login_failures_since(since)
    else:
        users_roles = {}
        login_failures_24h = db.count_login_failures_since(since, username=scoped.username) if scoped else 0
    active_sessions = db.count_active_sessions(account_id)

    # --- Аудит: топ действий ---
    audit_actions = [{"label": r["action"], "value": r["c"]}
                     for r in db.audit_action_counts(15, account_id=account_id,
                                                     account_name=(scoped.name if scoped else None))]

    # --- Диск / размеры на диске ---
    disk_free = 0
    db_size = 0
    archive_disk = 0
    try:
        from .util import disk_free_bytes
        disk_free = disk_free_bytes(svc.cfg.mail_root)
    except Exception:  # noqa: BLE001
        disk_free = 0
    try:
        db_size = os.path.getsize(svc.cfg.db_path)
    except OSError:
        db_size = 0
    if account_id is None:
        # без обхода ФС в обработчике запроса: кэш или логический объём индекса
        archive_disk = archive_disk_size(svc)

    return {
        "scope": {"account_id": account_id,
                  "account_name": (scoped.name if scoped else None)},
        "overview": {
            "accounts_total": len(accounts), "accounts_enabled": enabled,
            "messages": total_messages, "bytes": total_bytes, "bytes_h": human_size(total_bytes),
            "avg_message": (total_bytes // total_messages) if total_messages else 0,
            "avg_message_h": human_size(total_bytes // total_messages) if total_messages else "—",
            "folders": folders,
            "jobs_total": total_jobs, "jobs_success_rate": success_rate,
            "jobs_active": jobs_by_status.get("running", 0) + jobs_by_status.get("queued", 0),
            "exports": exp_total, "restores": restores["c"] if restores else 0,
            "schedules": len(schedules), "schedules_enabled": sched_enabled,
            "disk_free": disk_free, "disk_free_h": human_size(disk_free),
            "db_size": db_size, "db_size_h": human_size(db_size),
            "archive_disk": archive_disk, "archive_disk_h": human_size(archive_disk),
            "users": sum(users_roles.values()), "active_sessions": active_sessions,
            "scheduler_running": svc.scheduler.running(), "workers": svc.queue.max_workers(),
        },
        "accounts": per_account,
        "jobs": {
            "by_status": [{"label": JobStatus.LABELS.get(k, k), "key": k, "value": v}
                          for k, v in jobs_by_status.most_common()],
            "by_type": [{"label": JobType.LABELS.get(k, k), "key": k, "value": v}
                        for k, v in jobs_by_type.most_common()],
            "durations": durations,
            "success_rate": success_rate, "total": total_jobs,
        },
        "runs": {
            "total": rt["runs"] if rt else 0,
            "messages_new": rt["msgs"] if rt else 0,
            "bytes_new": rt["bytes"] if rt else 0,
            "bytes_new_h": human_size(rt["bytes"] if rt else 0),
            "errors": rt["errors"] if rt else 0,
            "by_type": {t: dict(v) for t, v in runs_type_status.items()},
        },
        "exports": {
            "total": exp_total, "bytes": exp_bytes, "bytes_h": human_size(exp_bytes),
            "by_format": [{"label": k, "value": v} for k, v in exp_by_format.most_common()],
            "by_engine": [{"label": k, "value": v} for k, v in exp_by_engine.most_common()],
            "by_status": [{"label": k, "value": v} for k, v in exp_by_status.most_common()],
        },
        "restores": {"total": restores["c"] if restores else 0,
                     "restored": restores["restored"] if restores else 0,
                     "errors": restores["errors"] if restores else 0},
        "schedules": {"total": len(schedules), "enabled": sched_enabled,
                      "by_type": [{"label": JobType.LABELS.get(k, k), "value": v}
                                  for k, v in sched_by_type.most_common()],
                      "upcoming": upcoming[:8]},
        "activity": activity,
        "security": {"users_by_role": [{"label": k, "value": v} for k, v in users_roles.items()],
                     "active_sessions": active_sessions, "login_failures_24h": login_failures_24h},
        "audit_top": audit_actions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ===========================================================================
#  АНАЛИТИКА ПИСЕМ (по метаданным индекса)
# ===========================================================================
def mail_analytics(svc, account_id: Optional[int] = None) -> Dict:
    db = svc.db
    rows = db.index_rows_for_analytics(account_id)
    total = len(rows)

    senders: Counter = Counter()
    sender_names: Dict[str, str] = {}
    domains: Counter = Counter()
    by_folder_cnt: Counter = Counter()
    by_folder_bytes: Counter = Counter()
    by_year: Counter = Counter()
    by_month: Counter = Counter()
    by_weekday = [0] * 7
    by_hour = [0] * 24
    heat = [[0] * 24 for _ in range(7)]      # день недели × час
    size_buckets = [0] * len(_SIZE_EDGES)
    subject_words: Counter = Counter()
    total_bytes = 0
    seen = flagged = answered = draft = with_attach = 0
    reply_cnt = forward_cnt = empty_subject = 0
    sizes: List[int] = []
    dates: List[datetime] = []

    for r in rows:
        size = r["size"] or 0
        total_bytes += size
        sizes.append(size)
        # размер → корзина
        for i, (lo, hi, _label) in enumerate(_SIZE_EDGES):
            if lo <= size < hi:
                size_buckets[i] += 1
                break
        # флаги
        fl = (r["flags"] or "").split(",")
        if "\\Seen" in fl:
            seen += 1
        if "\\Flagged" in fl:
            flagged += 1
        if "\\Answered" in fl:
            answered += 1
        if "\\Draft" in fl:
            draft += 1
        if r["has_attach"]:
            with_attach += 1
        # папка
        fld = r["folder"] or "—"
        by_folder_cnt[fld] += 1
        by_folder_bytes[fld] += size
        # отправитель / домен
        frm = r["from_addr"] or ""
        em = _email_of(frm)
        if em:
            senders[em] += 1
            name, _addr = parseaddr(frm)
            if name and em not in sender_names:
                sender_names[em] = name
        dom = _domain_of(frm)
        if dom:
            domains[dom] += 1
        # дата → год/месяц/день недели/час/тепловая карта
        dt = _parse_dt(r["internaldate"])
        if dt:
            dates.append(dt)
            by_year[dt.strftime("%Y")] += 1
            by_month[dt.strftime("%Y-%m")] += 1
            wd = dt.weekday()
            by_weekday[wd] += 1
            by_hour[dt.hour] += 1
            heat[wd][dt.hour] += 1
        # тема → префиксы (Re/Fwd) и слова
        subj = (r["subject"] or "").strip()
        if not subj or subj == "(без темы)":
            empty_subject += 1
        else:
            if _SUBJ_PREFIX_RE.match(subj):
                if re.match(r"^\s*(fwd?|переслать|пересл)", subj, re.IGNORECASE):
                    forward_cnt += 1
                else:
                    reply_cnt += 1
            for w in _WORD_RE.findall(subj.lower()):
                if w in STOPWORDS or w.isdigit():
                    continue
                subject_words[w] += 1

    # Полный помесячный ряд (без «дыр») между первым и последним месяцем.
    month_series: List[Dict] = []
    if by_month:
        keys = sorted(by_month.keys())
        y0, m0 = int(keys[0][:4]), int(keys[0][5:7])
        y1, m1 = int(keys[-1][:4]), int(keys[-1][5:7])
        y, m = y0, m0
        guard = 0
        while (y, m) <= (y1, m1) and guard < 400:
            key = f"{y:04d}-{m:02d}"
            month_series.append({"label": key, "value": int(by_month.get(key, 0))})
            m += 1
            if m > 12:
                m = 1
                y += 1
            guard += 1

    sizes.sort()
    median = sizes[len(sizes) // 2] if sizes else 0
    dmin = min(dates).isoformat() if dates else None
    dmax = max(dates).isoformat() if dates else None
    span_days = ((max(dates) - min(dates)).days + 1) if dates else 0
    avg_per_day = round(total / span_days, 1) if span_days else 0

    largest = [{"subject": r["subject"] or "(без темы)", "from": r["from_addr"] or "",
                "folder": r["folder"], "size": r["size"] or 0, "size_h": human_size(r["size"] or 0),
                "date": r["internaldate"]}
               for r in db.largest_messages(account_id, limit=10)]

    return {
        "scope": {"account_id": account_id},
        "overview": {
            "messages": total, "bytes": total_bytes, "bytes_h": human_size(total_bytes),
            "avg_size": (total_bytes // total) if total else 0,
            "avg_size_h": human_size(total_bytes // total) if total else "—",
            "median_size": median, "median_size_h": human_size(median),
            "unique_senders": len(senders), "unique_domains": len(domains),
            "with_attach": with_attach,
            "with_attach_pct": round(100.0 * with_attach / total, 1) if total else 0,
            "seen": seen, "unseen": total - seen,
            "unseen_pct": round(100.0 * (total - seen) / total, 1) if total else 0,
            "flagged": flagged, "answered": answered, "draft": draft,
            "reply": reply_cnt, "forward": forward_cnt, "empty_subject": empty_subject,
            "date_from": dmin, "date_to": dmax, "span_days": span_days, "avg_per_day": avg_per_day,
            "folders": len(by_folder_cnt),
        },
        "top_senders": [{"label": (sender_names.get(k) and f"{sender_names[k]} <{k}>") or k,
                         "email": k, "value": int(v)} for k, v in senders.most_common(20)],
        "top_domains": _top(domains, 15),
        "folders": [{"label": k, "value": int(v), "bytes": int(by_folder_bytes[k]),
                     "bytes_h": human_size(by_folder_bytes[k])}
                    for k, v in by_folder_cnt.most_common(20)],
        "by_year": [{"label": k, "value": int(v)} for k, v in sorted(by_year.items())],
        "by_month": month_series,
        "by_weekday": [{"label": WEEKDAYS_RU[i], "value": by_weekday[i]} for i in range(7)],
        "by_hour": [{"label": f"{i:02d}", "value": by_hour[i]} for i in range(24)],
        "heatmap": {"rows": WEEKDAYS_RU, "matrix": heat,
                    "max": max((max(r) for r in heat), default=0)},
        "size_hist": [{"label": _SIZE_EDGES[i][2], "value": size_buckets[i]}
                      for i in range(len(_SIZE_EDGES))],
        "subject_words": _top(subject_words, 40),
        "largest": largest,
        "read_state": [{"label": "Прочитанные", "value": seen},
                       {"label": "Непрочитанные", "value": total - seen}],
        "attach_state": [{"label": "С вложениями", "value": with_attach},
                         {"label": "Без вложений", "value": total - with_attach}],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ===========================================================================
#  ГЛУБОКИЙ АНАЛИЗ (чтение .eml) — фоновое задание + кэш
# ===========================================================================
def _deep_key(account_id: Optional[int]) -> str:
    return f"analytics.deep.{account_id if account_id is not None else 'all'}"


def load_deep(svc, account_id: Optional[int]) -> Optional[Dict]:
    return svc.db.get_setting(_deep_key(account_id), None)


def save_deep(svc, account_id: Optional[int], data: Dict) -> None:
    svc.db.set_setting(_deep_key(account_id), data)


def _ext_of(filename: str) -> str:
    base = os.path.basename(filename or "")
    if "." in base:
        return base.rsplit(".", 1)[1].lower()[:8]
    return "(без расш.)"


def _lang_bucket(text: str) -> Optional[str]:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return None
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    ratio = cyr / len(letters)
    if ratio > 0.6:
        return "Русский"
    if ratio < 0.2:
        return "Латиница"
    return "Смешанный"


def deep_scan(svc, account_id: Optional[int] = None,
              progress_cb: Optional[Callable] = None,
              cancel_cb: Optional[Callable] = None,
              event_cb: Optional[Callable] = None) -> Dict:
    """Тяжёлый разбор писем: типы вложений, домены получателей, тело, язык, слова."""
    from .mailview import parse_message

    db = svc.db
    # Обходим индекс ПОСТРАНИЧНО. Единым запросом с limit=1_000_000 брать нельзя:
    # list_messages сортирует по дате (ORDER BY internaldate DESC), поэтому на
    # больших ящиках в выборку попадали бы только самые новые письма, а самые
    # старые молча выпадали бы из анализа. Заодно не держим весь индекс в памяти.
    account_ids = [account_id] if account_id is not None else [a.id for a in db.list_accounts()]
    total = sum(db.count_messages(aid) for aid in account_ids)

    def _iter_rows():
        for aid in account_ids:
            offset = 0
            while True:
                page = db.list_messages(aid, limit=_SCAN_PAGE_SIZE, offset=offset)
                if not page:
                    break
                for row in page:
                    yield row
                if len(page) < _SCAN_PAGE_SIZE:
                    break
                offset += _SCAN_PAGE_SIZE

    ext_counter: Counter = Counter()
    ctype_counter: Counter = Counter()
    to_domains: Counter = Counter()
    body_words: Counter = Counter()
    lang_counter: Counter = Counter()
    body_kind = {"text": 0, "html": 0, "both": 0, "empty": 0}
    attach_bytes = 0
    attach_count = 0
    msgs_with_attach = 0
    text_len_sum = 0
    text_len_n = 0
    scanned = 0
    errors = 0

    for i, row in enumerate(_iter_rows(), 1):
        if cancel_cb and cancel_cb():
            from .errors import JobCancelled
            raise JobCancelled("Глубокий анализ отменён пользователем.")
        try:
            raw = svc.store.read_message(row["account_id"], row["stored_path"])
            parsed = parse_message(raw)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        scanned += 1
        atts = parsed.get("attachments") or []
        if atts:
            msgs_with_attach += 1
        for a in atts:
            attach_count += 1
            attach_bytes += a.get("size") or 0
            ext_counter[_ext_of(a.get("filename", ""))] += 1
            ctype_counter[(a.get("content_type") or "").split(";")[0].lower() or "(неизв.)"] += 1
        # получатели
        to_field = (parsed.get("headers") or {}).get("to", "")
        cc_field = (parsed.get("headers") or {}).get("cc", "")
        for _n, addr in getaddresses([to_field, cc_field]):
            d = _domain_of(addr)
            if d:
                to_domains[d] += 1
        # тело
        text = (parsed.get("text") or "").strip()
        html = (parsed.get("html") or "").strip()
        if text and html:
            body_kind["both"] += 1
        elif text:
            body_kind["text"] += 1
        elif html:
            body_kind["html"] += 1
        else:
            body_kind["empty"] += 1
        if text:
            text_len_sum += len(text)
            text_len_n += 1
            lang = _lang_bucket(text)
            if lang:
                lang_counter[lang] += 1
            for w in _WORD_RE.findall(text.lower()):
                if w in STOPWORDS or w.isdigit():
                    continue
                body_words[w] += 1
            # периодически усекаем словарь частот, чтобы он не рос бесконечно
            if len(body_words) > _WORDS_SOFT_LIMIT:
                body_words = Counter(dict(body_words.most_common(_WORDS_KEEP)))
        if progress_cb and (i % 25 == 0 or i == total):
            progress_cb(i, total, f"Анализ письма {i}/{total}")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "scanned": scanned, "total": total, "errors": errors,
        "attachments": {
            "count": attach_count, "bytes": attach_bytes, "bytes_h": human_size(attach_bytes),
            "messages_with_attach": msgs_with_attach,
            "by_ext": _top(ext_counter, 15),
            "by_type": _top(ctype_counter, 15),
        },
        "recipients": {"top_domains": _top(to_domains, 15), "unique_domains": len(to_domains)},
        "body": {
            "kinds": [{"label": "Текст + HTML", "value": body_kind["both"]},
                      {"label": "Только текст", "value": body_kind["text"]},
                      {"label": "Только HTML", "value": body_kind["html"]},
                      {"label": "Пустое тело", "value": body_kind["empty"]}],
            "avg_text_len": round(text_len_sum / text_len_n) if text_len_n else 0,
            "languages": _top(lang_counter, 5),
            "top_words": _top(body_words, 60),
        },
    }
    if event_cb:
        event_cb("INFO", f"Глубокий анализ: разобрано {scanned}/{total}, ошибок {errors}.")
    return result
