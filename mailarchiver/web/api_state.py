"""API: дашборд и живое состояние (/state, /live).

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from ..models import Account
from .. import roles
from ..util import human_size
from ..version import __version__
from . import auth as auth_mod
from ..export import list_engines

from .api_common import _foreign_target, _own_ids, svc_dep
from .api_jobs import cap_active_jobs, serialize_job

router = APIRouter(prefix="/api")

# =====================================================================
#  Дашборд / состояние
# =====================================================================
@router.get("/state")
def state(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    accounts = svc.db.list_accounts()
    if user.get("role") == "mailbox":
        own = set(_own_ids(user))
        accounts = [a for a in accounts if a.id in own]
    # Сводку по всем ящикам берём двумя групповыми запросами: по запросу на
    # ящик дашборд заваливал базу на каждом обновлении (см. /state в ws).
    totals_by_acc = svc.db.message_totals_by_account()
    last_by_acc = svc.db.last_runs_by_account()
    acc_out = []
    for a in accounts:
        stat = totals_by_acc.get(a.id) or {"messages": 0, "bytes": 0}
        cnt, by = stat["messages"], stat["bytes"]
        last = last_by_acc.get(a.id)
        acc_out.append({
            **_account_view(a, user),
            "messages": cnt, "bytes": by, "bytes_h": human_size(by),
            "last_run": ({"status": last["status"], "finished_at": last["finished_at"],
                          "messages_new": last["messages_new"]} if last else None),
        })
    counts = svc.db.count_jobs_by_status(active_only=True)
    names = svc.db.account_names()                     # один раз, а не на каждое задание
    rows = svc.db.active_jobs()
    queued_hidden = 0
    if user.get("role") == "mailbox":
        own = _own_ids(user)
        active = [serialize_job(j, names, user) for j in rows
                  if j["account_id"] in own and not _foreign_target(user, j)]
        # счётчики очереди — общесистемные: показываем только свои задания
        counts = {"running": sum(1 for j in active if j["status"] == "running"),
                  "queued": sum(1 for j in active if j["status"] == "queued")}
        total_msgs = svc.db.count_messages(own)
        total_bytes = svc.db.sum_message_bytes(own)
    else:
        rows, queued_hidden = cap_active_jobs(rows)
        active = [serialize_job(j, names, user) for j in rows]
        total_msgs = svc.db.count_messages()
        total_bytes = svc.db.sum_message_bytes()
    disk_free = _disk_free(svc.cfg.mail_root)
    return {
        "version": __version__,
        "accounts": acc_out,
        "job_counts": counts,
        "active_jobs": active,
        "queued_hidden": queued_hidden,
        "totals": {"accounts": len(accounts), "messages": total_msgs,
                   "bytes": total_bytes, "bytes_h": human_size(total_bytes)},
        "disk": {"free": disk_free, "free_h": human_size(disk_free)},
        "scheduler_running": svc.scheduler.running(),
        "engines": list_engines(),
        "workers": svc.queue.max_workers(),
        # шифрование включено, а ключа нет — копирование писем остановлено
        # (в тексте причины бывает путь к файлу ключа — только администратору;
        # оператор видит сам факт: он следит за копированием)
        "encryption_blocked": _encryption_banner(svc, user),
        "replica_alert": _replica_alert(svc) if user.get("role") in roles.STAFF_ROLES else "",
        # сервер Axigen требует внимания (недоступен, очередь, лицензия…) — только администратору
        "axigen_alerts": _axigen_alerts(svc) if user.get("role") == roles.ROLE_ADMIN else [],
        # подключённые почтовые серверы требуют внимания — тоже только администратору
        "mailserver_alerts": _mailserver_alerts(svc) if user.get("role") == roles.ROLE_ADMIN else [],
    }


def _mailserver_alerts(svc) -> list:
    try:
        from ..mailservers.monitor import problems as ms_problems
        return ms_problems(svc)
    except Exception:  # noqa: BLE001
        return []


def _axigen_alerts(svc) -> list:
    try:
        from ..axigen.monitor import problems as axigen_problems
        return axigen_problems(svc)
    except Exception:  # noqa: BLE001 — дашборд открывается и при сбое раздела Axigen
        return []


def _encryption_banner(svc, user: dict) -> str:
    reason = str(getattr(svc.store, "encryption_blocked", "") or "")
    if not reason or user.get("role") not in roles.STAFF_ROLES:
        return ""
    if user.get("role") == roles.ROLE_ADMIN:
        return reason
    return "Копирование писем остановлено: нет ключа шифрования локальной копии. Сообщите администратору."



#: Через сколько часов без удачного прогона копия вне сервера считается отставшей.
REPLICA_ALERT_HOURS = 48


def _replica_alert(svc) -> str:
    """Текст плашки на дашборде, если копия вне сервера включена и не в порядке."""
    if not bool(svc.rt("replica", "enabled")):
        return ""
    try:
        last = json.loads(svc.db.get_meta("replica_last_run") or "null")
    except ValueError:
        last = None
    if not last:
        return ""
    last_ok = svc.db.get_meta("replica_last_ok") or ""
    stale = True
    if last_ok:
        try:
            stale = datetime.now(timezone.utc) - datetime.fromisoformat(last_ok) > timedelta(hours=REPLICA_ALERT_HOURS)
        except ValueError:
            stale = True
    if last.get("status") == "failed":
        return f"Последний прогон завершился ошибкой: {str(last.get('message') or '')[:400]}"
    if stale:
        return f"Удачного прогона не было больше {REPLICA_ALERT_HOURS} ч."
    if last.get("deletions_blocked"):
        return (f"Удаление в копии приостановлено: прогон собирался удалить {last['deletions_blocked']} файлов. "
                f"Если это ожидаемо — разрешите удаление в настройках копии.")
    return ""


@router.get("/live")
def live(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Лёгкий снимок для живого обновления, когда WebSocket недоступен.

    Раньше запасной опрос каждые 3,5 с запрашивал весь /state (сводку по всем
    ящикам — сотни миллисекунд на большом архиве), а использовал из неё только
    активные задания и счётчики.
    """
    from .ws import collect_live
    return collect_live(svc_dep(request), user)


#: Поля карточки ящика, которые видит только администратор: заметки
#: администратора и технические настройки OAuth2 (сотруднику, вошедшему по
#: ящику, и оператору их не показываем).
_ADMIN_ONLY_ACCOUNT_FIELDS = ("notes", "oauth_client_id", "oauth_token_url", "has_oauth_secret")


def _account_view(acc: Account, user: dict) -> dict:
    data = acc.redacted()
    if user.get("role") != roles.ROLE_ADMIN:
        for key in _ADMIN_ONLY_ACCOUNT_FIELDS:
            data.pop(key, None)
    return data


def _disk_free(path: str) -> int:
    try:
        from ..util import disk_free_bytes
        return disk_free_bytes(path)
    except Exception:  # noqa: BLE001
        return 0
