"""
Место на почтовом сервере во времени: сколько занимал ящик в каждый день, когда его читали.

Точки записываются при сверке с Axigen (все учётные записи), при «Перечитать с
сервера» и при «Отчёте» (учётные записи Axigen и ящики других серверов, если
сервер сообщает занятое место). По ним раздел показывает, на сколько ящик
растёт в месяц и через сколько дней при таком росте упрётся в квоту.

Одна точка в день на ящик (последнее значение дня); точки старше
:data:`KEEP_DAYS` удаляет служебное обслуживание.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

#: Сколько дней хранить точки.
KEEP_DAYS = 400
#: Рост считается по точкам не старше этого срока…
WINDOW_DAYS = 90
#: …и только если между первой и последней точкой не меньше стольких дней.
MIN_SPAN_DAYS = 7
#: «Скоро упрётся в квоту» — меньше стольких дней при нынешнем росте.
SOON_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_usage (
    key        TEXT NOT NULL,              -- ax:<адрес> или mb:<номер ящика архива>
    day        TEXT NOT NULL,              -- ГГГГ-ММ-ДД, день по часовому поясу расписаний
    used_bytes INTEGER,
    quota_bytes INTEGER,
    messages   INTEGER,
    PRIMARY KEY (key, day)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def today(svc) -> str:
    """Сегодня по часовому поясу пользователя (scheduler.timezone) — граница дня, как у остальных дат."""
    try:
        return datetime.now(svc.local_tz()).date().isoformat()
    except Exception:  # noqa: BLE001
        return _today()


#: Прогноз дальше этого срока не показывается («до квоты ~9 000 000 дн.» ничего не говорит).
MAX_FORECAST_DAYS = 3650
_version = 0                       # меняется при каждой записи — кэш прогнозов устаревает
_cache: Dict = {}


def _stamp(db):
    """Отпечаток таблицы: каждая запись (INSERT OR REPLACE) даёт строке новый rowid — кэш видит и записи
    в обход этого модуля."""
    row = db.query_one("SELECT MAX(rowid) AS m FROM server_usage")
    return row["m"] if row is not None else None


def _changed() -> None:
    global _version
    _version += 1
    _cache.clear()


def record(db, key: str, used_bytes: Optional[int], quota_bytes: Optional[int] = None,
           messages: Optional[int] = None, day: Optional[str] = None) -> None:
    """Точка «сколько занято» (повторная за тот же день заменяет прежнюю)."""
    if used_bytes is None or not key:
        return
    db.execute("INSERT OR REPLACE INTO server_usage(key, day, used_bytes, quota_bytes, messages) VALUES(?,?,?,?,?)",
               (key, day or _today(), int(used_bytes), int(quota_bytes) if quota_bytes else None,
                int(messages) if messages is not None else None))
    _changed()


def record_axigen_rows(db, rows: Iterable[Dict], day: Optional[str] = None) -> int:
    """Точки по строкам сверки с Axigen (одной транзакцией)."""
    day = day or _today()
    payload = []
    for row in rows:
        used = row.get("used_bytes")
        if used is None or row.get("deleted_at") or not row.get("email"):
            continue
        quota = row.get("quota_kb")
        payload.append((f"ax:{str(row['email']).strip().lower()}", day, int(used), int(quota) * 1024 if quota else None,
                        row.get("messages")))
    if payload:
        db.executemany("INSERT OR REPLACE INTO server_usage(key, day, used_bytes, quota_bytes, messages) "
                       "VALUES(?,?,?,?,?)", payload)
        _changed()
    return len(payload)


def purge_old(db, keep_days: int = KEEP_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=keep_days)).isoformat()
    cur = db.execute("DELETE FROM server_usage WHERE day < ?", (cutoff,))
    _changed()
    return cur.rowcount or 0


def history(db, key: str, days: int = KEEP_DAYS) -> List[Dict]:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    return [dict(r) for r in db.query("SELECT day, used_bytes, quota_bytes, messages FROM server_usage "
                                      "WHERE key=? AND day>=? ORDER BY day", (key, cutoff))]


def _forecast(per_day: float, used: int, quota: Optional[int]) -> Optional[int]:
    """Дней до квоты: занято не меньше квоты — 0; не растёт или дольше MAX_FORECAST_DAYS — None."""
    if not quota:
        return None
    left = int(quota) - int(used)
    if left <= 0:
        return 0
    if per_day <= 0:
        return None
    days = int(left / per_day)
    return days if days <= MAX_FORECAST_DAYS else None


def _all_trends(db) -> Dict[str, Dict]:
    """Рост по всем ящикам — суммами в SQL (одним проходом), наклон прямой — точной целочисленной арифметикой.

    Результат кэшируется до следующей записи точек (или до смены дня): его читают мониторинг (каждый
    опрос /metrics), дашборд и раздел.
    """
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=WINDOW_DAYS)).isoformat()
    key = (id(db), cutoff, _version, _stamp(db))
    hit = _cache.get("trends")
    if hit is not None and hit[0] == key:
        return hit[1]
    rows = db.query(
        "SELECT key, COUNT(*) AS n, MIN(day) AS first_day, MAX(day) AS last_day, "
        "SUM(CAST(julianday(day) - julianday(?) AS INTEGER)) AS sx, SUM(used_bytes) AS sy, "
        "SUM(CAST(julianday(day) - julianday(?) AS INTEGER) * used_bytes) AS sxy, "
        "SUM(CAST(julianday(day) - julianday(?) AS INTEGER) * CAST(julianday(day) - julianday(?) AS INTEGER)) "
        "AS sxx FROM server_usage WHERE day >= ? GROUP BY key HAVING n >= 2",
        (cutoff, cutoff, cutoff, cutoff, cutoff))
    last = {r["key"]: r for r in db.query(
        "SELECT u.key, u.day, u.used_bytes, u.quota_bytes FROM server_usage u JOIN "
        "(SELECT key, MAX(day) AS day FROM server_usage WHERE day >= ? GROUP BY key) m "
        "ON u.key = m.key AND u.day = m.day", (cutoff,))}
    out: Dict[str, Dict] = {}
    for r in rows:
        span = (date.fromisoformat(r["last_day"]) - date.fromisoformat(r["first_day"])).days
        if span < MIN_SPAN_DAYS:
            continue
        n, sx, sy, sxy, sxx = int(r["n"]), int(r["sx"]), int(r["sy"]), int(r["sxy"]), int(r["sxx"])
        den = n * sxx - sx * sx
        if den <= 0:
            continue
        per_day = (n * sxy - sx * sy) / den
        lr = last[r["key"]]
        used, quota = int(lr["used_bytes"]), lr["quota_bytes"]
        out[r["key"]] = {"per_day": per_day, "per_month": per_day * 30, "span_days": span, "since": r["first_day"],
                         "last_day": lr["day"], "used": used, "quota": quota,
                         "days_to_quota": _forecast(per_day, used, quota)}
    _cache["trends"] = (key, out)
    return out


def _trend(points: Sequence[Dict]) -> Optional[Dict]:
    """Рост по точкам (день, занято): наклон прямой наименьших квадратов, байт в день (для проверок)."""
    if len(points) < 2:
        return None
    days = [date.fromisoformat(p["day"]) for p in points]
    span = (days[-1] - days[0]).days
    if span < MIN_SPAN_DAYS:
        return None
    xs = [(d - days[0]).days for d in days]
    ys = [int(p["used_bytes"]) for p in points]
    n, sx, sy = len(xs), sum(xs), sum(ys)
    sxy, sxx = sum(x * y for x, y in zip(xs, ys)), sum(x * x for x in xs)
    den = n * sxx - sx * sx
    if den <= 0:
        return None
    per_day = (n * sxy - sx * sy) / den
    last = points[-1]
    return {"per_day": per_day, "per_month": per_day * 30, "span_days": span, "since": points[0]["day"],
            "last_day": last["day"], "used": int(last["used_bytes"]), "quota": last.get("quota_bytes"),
            "days_to_quota": _forecast(per_day, int(last["used_bytes"]), last.get("quota_bytes"))}


def trends(db, keys: Optional[Sequence[str]] = None) -> Dict[str, Dict]:
    """Рост и прогноз до квоты по ящикам (ключ → данные; нет данных — ключа нет)."""
    allt = _all_trends(db)
    if keys is None:
        return allt
    return {k: allt[k] for k in keys if k in allt}


def latest(db) -> Dict[str, Dict]:
    """Последний замер каждого ящика (за срок хранения): ключ → {day, used, quota}."""
    hit = _cache.get("latest")
    key = (id(db), _version, _stamp(db))
    if hit is not None and hit[0] == key:
        return hit[1]
    out = {r["key"]: {"day": r["day"], "used": r["used_bytes"], "quota": r["quota_bytes"]} for r in db.query(
        "SELECT u.key, u.day, u.used_bytes, u.quota_bytes FROM server_usage u JOIN "
        "(SELECT key, MAX(day) AS day FROM server_usage GROUP BY key) m ON u.key = m.key AND u.day = m.day")}
    _cache["latest"] = (key, out)
    return out


def soon_full(db, days: int = SOON_DAYS) -> List[str]:
    """Ящики, которые при нынешнем росте упрутся в квоту за ``days`` дней."""
    return sorted(k for k, t in trends(db).items() if t.get("days_to_quota") is not None and t["days_to_quota"] <= days)
