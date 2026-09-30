"""
Cron-выражения в привычной нотации crontab → триггеры APScheduler.

APScheduler (3.x) в ``CronTrigger.from_crontab`` передаёт поле «день недели»
как есть, но считает 0 понедельником, тогда как в crontab 0 (и 7) —
воскресенье. Из-за этого «0 8 * * 1» (сводка «по понедельникам») срабатывала во
вторник, а «по будням» (1-5) — со вторника по субботу. Здесь поле дня недели
переводится в имена дней (mon, tue…), которые APScheduler понимает однозначно;
остальные поля совпадают у обоих.
"""
from __future__ import annotations

from typing import Optional, Set

#: Дни недели в нумерации crontab: 0 — воскресенье, 1 — понедельник … 6 — суббота (7 — тоже воскресенье).
_CRON_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
#: Имена для APScheduler — в его порядке (понедельник первым).
_APS_ORDER = (1, 2, 3, 4, 5, 6, 0)


def _day(token: str, *, range_end: bool = False) -> int:
    """Номер дня (0–6, 0 — воскресенье) из числа или имени; 7 — воскресенье.

    В конце диапазона воскресенье считается седьмым днём: «1-7» и «mon-sun» —
    вся неделя, а не пустой диапазон.
    """
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        if not 0 <= value <= 7:
            raise ValueError(f"день недели «{token}» вне 0–7")
    elif token[:3] in _CRON_NAMES and (len(token) == 3 or token in ("monday", "tuesday", "wednesday", "thursday",
                                                                     "friday", "saturday", "sunday")):
        value = _CRON_NAMES.index(token[:3])
    else:
        raise ValueError(f"непонятный день недели «{token}»")
    if value == 7 or (value == 0 and range_end):
        return 7
    return value


def crontab_days(field: str) -> Set[int]:
    """Множество дней (0–6, 0 — воскресенье), заданных полем «день недели» crontab."""
    field = (field or "").strip().lower()
    days: Set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("пустой элемент в дне недели")
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ValueError(f"неверный шаг «{raw_step}»")
            step = int(raw_step)
        if part in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in part:
            a, b = part.split("-", 1)
            lo, hi = _day(a), _day(b, range_end=True)
            if lo == 7:
                lo = 0
            if lo > hi:
                raise ValueError(f"диапазон дней «{part}» задан наоборот")
        else:
            lo = _day(part)
            if lo == 7:
                lo = 0
            hi = 6 if step > 1 else lo          # «a/n» — от a до конца недели с шагом
        for value in range(lo, hi + 1, step):
            days.add(value % 7)
    return days


def apscheduler_day_of_week(field: str) -> str:
    """Поле «день недели» crontab → значение day_of_week для APScheduler."""
    field = (field or "").strip()
    if field in ("*", "?"):
        return "*"
    days = crontab_days(field)
    if len(days) == 7:
        return "*"
    return ",".join(_CRON_NAMES[d] for d in _APS_ORDER if d in days)


def crontab_trigger(expr: str, timezone=None):
    """CronTrigger по выражению crontab из пяти полей (с правильными днями недели)."""
    from apscheduler.triggers.cron import CronTrigger
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("нужно 5 полей: минуты часы день месяц день_недели")
    minute, hour, day, month, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=apscheduler_day_of_week(dow), timezone=timezone)


_DAY_DATIVE = ("воскресеньям", "понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам")


def describe_cron(expr: str) -> str:
    """«ежедневно в 02:00», «по будням в 23:30» … для простых выражений; иначе — само выражение."""
    parts = (expr or "").split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2] == "*" and parts[3] == "*":
        minute, hour, dow = int(parts[0]), int(parts[1]), parts[4]
        if minute < 60 and hour < 24:
            days = {"*": "ежедневно", "1-5": "по будням", "2-6": "со вторника по субботу",
                    "0,6": "по выходным", "6,0": "по выходным"}.get(dow)
            if days is None and dow.isdigit() and int(dow) <= 7:
                days = "по " + _DAY_DATIVE[int(dow) % 7]
            if days:
                return f"{days} в {hour:02d}:{minute:02d}"
    return f"cron «{(expr or '').strip()}»"


def describe_days(field: str) -> Optional[str]:
    """«по будням», «по понедельникам» … — для журналов и подсказок (None — если сложно)."""
    try:
        days = crontab_days(field)
    except ValueError:
        return None
    if len(days) == 7:
        return "ежедневно"
    if days == {1, 2, 3, 4, 5}:
        return "по будням"
    if days == {0, 6}:
        return "по выходным"
    return None
