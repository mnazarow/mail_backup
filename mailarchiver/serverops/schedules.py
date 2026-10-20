"""
Действия с ящиками на сервере по расписанию — только по явному согласию администратора.

Сам MailArchiver почтовый сервер не меняет. Расписание — исключение, которое
администратор включает сам для конкретного действия и конкретных ящиков:
очистка корзины и спама, удаление старых писем, перенос старых писем, отчёт.
При создании параметры проверяются, как перед ручным запуском (окно заодно
показывает, что было бы при запуске сейчас), удаление подтверждается числом
ящиков, нужна отметка согласия; кто и когда согласился, пишется в расписание
и в аудит. Запуск без отметки согласия или для действия не из списка не
выполняется.

Каждый запуск — обычное задание «Действия с ящиками на сервере»: условия
(«только при полном архиве», свежая копия, сверка каждого письма) проверяются
заново по каждому ящику, итог — в истории раздела. Ящики — список, выбранный
при создании (не отбор): новый ящик сам в расписание не попадёт.

Хранится в общей таблице расписаний (``job_type = server_action``, без ящика);
в разделе «Расписания» не показывается и не меняется — только здесь.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..errors import ValidationError
from ..models import JobType, ScheduleKind
from ..util import utcnow_iso
from . import base
from . import targets as tg

#: Какие действия можно поставить на расписание.
SCHEDULABLE = ("imap_trash", "imap_junk", "imap_purge", "ax_purge", "imap_move", "report")
FREQUENCIES = ("daily", "weekly", "monthly")
DAYS_RU = {1: "понедельник", 2: "вторник", 3: "среда", 4: "четверг", 5: "пятница", 6: "суббота",
           7: "воскресенье"}
#: Сколько ящиков в одном расписании.
MAX_KEYS = 5000


def when_to_cron(when: Dict[str, Any]) -> str:
    """{"freq": daily|weekly|monthly, "time": "ЧЧ:ММ", "dow": 1..7, "day": 1..28} → cron (5 полей)."""
    when = dict(when or {})
    freq = str(when.get("freq") or "")
    if freq not in FREQUENCIES:
        raise ValidationError("Выберите, как часто: каждый день, каждую неделю или каждый месяц.")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(when.get("time") or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValidationError("Время указано неверно.", hint="Формат ЧЧ:ММ, например 02:30.")
    hour, minute = int(m.group(1)), int(m.group(2))
    if freq == "daily":
        return f"{minute} {hour} * * *"
    if freq == "weekly":
        try:
            dow = int(when.get("dow"))
        except (TypeError, ValueError):
            dow = 0
        if not 1 <= dow <= 7:
            raise ValidationError("Выберите день недели.")
        return f"{minute} {hour} * * {dow % 7}"           # cron: 0 — воскресенье, 1 — понедельник
    try:
        day = int(when.get("day"))
    except (TypeError, ValueError):
        day = 0
    if not 1 <= day <= 28:
        raise ValidationError("День месяца — от 1 до 28 (в любом месяце есть такой день).")
    return f"{minute} {hour} {day} * *"


def when_text(when: Dict[str, Any]) -> str:
    when = dict(when or {})
    time = str(when.get("time") or "")
    freq = when.get("freq")
    if freq == "daily":
        return f"каждый день в {time}"
    if freq == "weekly":
        return f"каждую неделю, {DAYS_RU.get(int(when.get('dow') or 0), '?')}, в {time}"
    if freq == "monthly":
        return f"каждый месяц, {when.get('day')}-го числа, в {time}"
    return "—"


def create(svc, user: dict, *, action_key: str, keys, params: Optional[Dict[str, Any]], when: Dict[str, Any],
           consent: bool, confirm: str = "") -> Dict[str, Any]:
    """Создать расписание: параметры — как перед ручным запуском; согласие и (для удаления) число ящиков."""
    from ..accountops import validate_cron
    action = base.get(action_key)
    if action.key not in SCHEDULABLE:
        raise ValidationError(f"Действие «{action.label}» по расписанию не выполняется.",
                              hint="По расписанию можно: очистить корзину и спам, удалить старые письма, перенести "
                                   "старые письма в папку, отчёт.")
    keys = tg.clean_keys(keys)
    if not keys:
        raise ValidationError("Не выбрано ни одного ящика.")
    if len(keys) > MAX_KEYS:
        raise ValidationError(f"В одном расписании — не больше {MAX_KEYS} ящиков.")
    found = tg.resolve(svc, keys)
    missing = [k for k in keys if k not in found]
    if missing:
        raise ValidationError(f"Ящики не найдены: {', '.join(m.split(':', 1)[-1] for m in missing[:10])}.",
                              hint="Обновите страницу и выберите ящики заново.")
    if not consent:
        raise ValidationError("Подтвердите, что разрешаете выполнять это действие по расписанию без вас.")
    if action.danger >= 2 and str(confirm or "").strip() != str(len(keys)):
        raise ValidationError(f"Для подтверждения введите число ящиков в расписании: {len(keys)}.")
    cron = when_to_cron(when)
    validate_cron(cron)
    if not svc.db.schedules_allow_global():
        raise ValidationError("База данных не обновлена для общих расписаний.", hint="Перезапустите службу.")
    username = (user or {}).get("username") or "admin"
    values = base.coerce_params(action, svc, params)
    ctx = base.Ctx(svc, username, values, preview=True)
    action.prepare(ctx)                             # те же проверки параметров, что перед ручным запуском
    options = {"action": action.key, "keys": keys, "options": action.options(ctx), "requested_by": username,
               "schedule": {"label": action.label, "when": dict(when), "consent_by": username,
                            "consent_at": utcnow_iso(), "count": len(keys)}}
    sid = svc.db.create_schedule(None, ScheduleKind.CRON, JobType.SERVER_ACTION, cron, 0, True, options)
    options["schedule_id"] = sid
    svc.db.update_schedule(sid, options=options)
    names = ", ".join(found[k].email for k in keys[:20])
    svc.db.add_audit(username, base.HISTORY_PREFIX + "schedule_create",
                     f"расписание №{sid}: {action.label}, {when_text(when)}, ящиков {len(keys)} "
                     f"(согласие на выполнение без проверки перед запуском): {names}"[:900])
    _reload(svc)
    return {"ok": True, "id": sid, "summary": f"Расписание №{sid}: {action.label}, {when_text(when)}, "
                                              f"ящиков {len(keys)}."}


def _reload(svc) -> None:
    scheduler = getattr(svc, "scheduler", None)
    if scheduler is not None:
        try:
            scheduler.reload()
        except Exception:  # noqa: BLE001 — расписание записано; планировщик подхватит его при запуске
            pass


def _row(svc, row) -> Optional[Dict[str, Any]]:
    import json
    try:
        options = json.loads(row["options"] or "{}")
    except (TypeError, ValueError):
        options = {}
    meta = options.get("schedule") or {}
    keys = options.get("keys") or []
    scheduler = getattr(svc, "scheduler", None)
    next_run = None
    if scheduler is not None and row["enabled"]:
        try:
            next_run = scheduler.next_run_for(row["id"])
        except Exception:  # noqa: BLE001
            next_run = None
    params = {k: v for k, v in (options.get("options") or {}).items() if not k.endswith("_enc")}
    return {"id": row["id"], "action": options.get("action"), "label": meta.get("label") or options.get("action"),
            "when": meta.get("when") or {}, "when_text": when_text(meta.get("when") or {}), "cron": row["cron_expr"],
            "enabled": bool(row["enabled"]), "keys": keys, "count": len(keys),
            "emails": [k.split(":", 1)[-1] for k in keys[:30]], "params": params,
            "consent_by": meta.get("consent_by"), "consent_at": meta.get("consent_at"),
            "last_run": row["last_run"], "next_run": next_run or row["next_run"], "created_at": row["created_at"]}


def list_all(svc) -> List[Dict[str, Any]]:
    return [_row(svc, r) for r in svc.db.list_schedules() if r["job_type"] == JobType.SERVER_ACTION]


def _get(svc, schedule_id: int):
    row = svc.db.get_schedule(schedule_id)
    if row is None or row["job_type"] != JobType.SERVER_ACTION:
        raise ValidationError("Расписание не найдено.")
    return row


def set_enabled(svc, user: dict, schedule_id: int, enabled: bool) -> Dict[str, Any]:
    row = _get(svc, schedule_id)
    svc.db.update_schedule(schedule_id, enabled=bool(enabled))
    svc.db.add_audit((user or {}).get("username") or "admin", base.HISTORY_PREFIX + "schedule_update",
                     f"расписание №{schedule_id}: {'включено' if enabled else 'выключено'}")
    _reload(svc)
    return _row(svc, svc.db.get_schedule(schedule_id) or row)


def delete(svc, user: dict, schedule_id: int) -> None:
    _get(svc, schedule_id)
    svc.db.delete_schedule(schedule_id)
    svc.db.add_audit((user or {}).get("username") or "admin", base.HISTORY_PREFIX + "schedule_delete",
                     f"расписание №{schedule_id} удалено")
    _reload(svc)
