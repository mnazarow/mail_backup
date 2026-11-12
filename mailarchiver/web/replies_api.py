"""
REST API раздела «Ответы на письма» (1.16.0).

Администратор видит все ящики (и отбор по отделу), сотрудник — только ящики своей карточки
(отбор чужого ящика молча сужается до своих). Оператору раздел недоступен: в списках — темы и
отправители писем, а писем оператор не читает. Пересобрать индекс может только администратор.

Логика — в :mod:`mailarchiver.replies`.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import replies as rp
from ..errors import ValidationError
from ..models import JobType
from . import auth as auth_mod

router = APIRouter(prefix="/api/replies")

PERIOD_MAX_DAYS = 3660


def _svc(request: Request):
    return request.app.state.services


def _user(request: Request) -> dict:
    """Администратор или сотрудник (вход по ящику); оператору — 403."""
    user = auth_mod.require_user(request)
    if user.get("role") not in (auth_mod.ROLE_ADMIN, auth_mod.ROLE_MAILBOX):
        raise HTTPException(403, "Раздел «Ответы на письма» доступен администратору и сотрудникам (по своим ящикам)")
    return user


def _is_mailbox(user: dict) -> bool:
    return user.get("role") == auth_mod.ROLE_MAILBOX


def _scope(svc, user: dict, account_id: Optional[int], department: str) -> Optional[List[int]]:
    """Какие ящики считать: None — все (администратор без отбора)."""
    if _is_mailbox(user):
        own = auth_mod.mailbox_ids(user)
        if account_id is not None:
            return [int(account_id)] if int(account_id) in own else own
        return own
    if account_id is not None:
        return [int(account_id)]
    dep = (department or "").strip()
    if dep:
        if dep == "Без отдела":
            rows = svc.db.query("SELECT a.id FROM accounts a LEFT JOIN employees e ON e.id = a.employee_id "
                                "WHERE e.id IS NULL OR TRIM(COALESCE(e.department, '')) = ''")
        else:
            rows = svc.db.query("SELECT a.id FROM accounts a JOIN employees e ON e.id = a.employee_id "
                                "WHERE TRIM(e.department) = ?", (dep,))
        return [int(r["id"]) for r in rows]
    return None


def _period(svc, days: Optional[int], date_from: str, date_to: str) -> Tuple[Optional[int], int]:
    """Период расчёта: последние ``days`` дней (с полуночи по местному времени) или даты «с» и «по»."""
    now = int(time.time())
    if date_from or date_to:
        try:
            start, end = svc.day_bounds_utc(date_from or None, date_to or None)
        except ValidationError as exc:
            raise HTTPException(400, exc.message) from None
        except ValueError:
            raise HTTPException(400, "Даты периода — в виде ГГГГ-ММ-ДД.") from None
        since = int(datetime.fromisoformat(start).timestamp()) if start else None
        until = int(datetime.fromisoformat(end).timestamp()) if end else now + 1
        return since, until
    if days is None or days == 0:
        return None, now + 1                                   # всё время
    if days < 0 or days > PERIOD_MAX_DAYS:
        raise HTTPException(400, f"Период — от 1 до {PERIOD_MAX_DAYS} дней (0 — всё время).")
    tz = svc.local_tz()
    today = datetime.now(tz).date()
    first = today - timedelta(days=int(days) - 1)
    since = int(datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(timezone.utc).timestamp())
    return since, now + 1


def _kinds(text: str) -> Set[int]:
    if not (text or "").strip():
        return {rp.PERSONAL}
    try:
        kinds = {int(x) for x in text.split(",") if x.strip()}
    except ValueError:
        raise HTTPException(400, "Типы писем — номера через запятую (0 — личные).") from None
    if not kinds or not kinds <= set(rp.KIND_LABELS):
        raise HTTPException(400, "Неизвестный тип писем.")
    return kinds


def _sla(sla: Optional[float]) -> Optional[float]:
    if sla is None:
        return None
    if not (0.25 <= float(sla) <= 8760):
        raise HTTPException(400, "Срок ответа — от 0,25 до 8760 часов.")
    return float(sla)


class _Q:
    """Общие параметры отбора (сводка, списки, выгрузка)."""

    def __init__(self, days: Optional[int] = 30, date_from: str = "", date_to: str = "",
                 account_id: Optional[int] = None, department: str = "", kinds: str = "0", scope: str = "all",
                 addressed: str = "all", incoming: str = "all", sla: Optional[float] = None,
                 sla_business: Optional[bool] = None):
        self.days, self.date_from, self.date_to = days, date_from, date_to
        self.account_id, self.department = account_id, department
        self.kinds, self.scope, self.addressed, self.incoming = kinds, scope, addressed, incoming
        self.sla, self.sla_business = sla, sla_business

    def resolve(self, svc, user) -> dict:
        since, until = _period(svc, self.days, self.date_from, self.date_to)
        return {"account_ids": _scope(svc, user, self.account_id, self.department), "since": since, "until": until,
                "kinds": _kinds(self.kinds), "scope": self.scope, "addressed": self.addressed,
                "incoming": self.incoming, "sla_hours": _sla(self.sla), "sla_business": self.sla_business}


@router.get("/status")
def replies_status(request: Request):
    user = _user(request)
    svc = _svc(request)
    st = rp.status(svc, auth_mod.mailbox_ids(user) if _is_mailbox(user) else None)
    if _is_mailbox(user):
        st.pop("job_id", None)
    return st


@router.get("/meta")
def replies_meta(request: Request):
    """Что нужно фильтрам раздела: ящики, отделы, типы писем, действующие настройки."""
    user = _user(request)
    svc = _svc(request)
    opts = rp.calc_settings(svc)
    if _is_mailbox(user):
        ids = auth_mod.mailbox_ids(user)
        marks = ",".join("?" * len(ids)) or "NULL"
        accounts = [{"id": int(r["id"]), "name": r["name"]} for r in
                    svc.db.query(f"SELECT id, name FROM accounts WHERE id IN ({marks}) ORDER BY name", tuple(ids))]
        departments: List[str] = []
    else:
        accounts = [{"id": int(r["id"]), "name": r["name"]} for r in
                    svc.db.query("SELECT id, name FROM accounts ORDER BY name")]
        departments = [r["d"] for r in svc.db.query(
            "SELECT DISTINCT TRIM(e.department) AS d FROM employees e JOIN accounts a ON a.employee_id = e.id "
            "WHERE TRIM(COALESCE(e.department, '')) <> '' ORDER BY d")]
        if svc.db.scalar("SELECT 1 FROM accounts a LEFT JOIN employees e ON e.id = a.employee_id "
                         "WHERE e.id IS NULL OR TRIM(COALESCE(e.department, '')) = '' LIMIT 1"):
            departments.append("Без отдела")
    return {"accounts": accounts, "departments": departments,
            "kinds": [{"id": k, "label": v} for k, v in rp.KIND_LABELS.items()],
            "lists": [{"id": k, "label": v} for k, v in rp.LIST_MODES.items()],
            "settings": {"sla_hours": opts["sla_hours"], "sla_business": opts["sla_business"],
                         "work_days": sorted(opts["work_days"]),
                         "work_hours": f"{opts['work_start'] // 60:02d}:{opts['work_start'] % 60:02d}-"
                                       f"{opts['work_end'] // 60:02d}:{opts['work_end'] % 60:02d}",
                         "timezone": str(svc.local_tz()), "enabled": bool(svc.rt("replies", "enabled"))},
            "is_admin": not _is_mailbox(user)}


@router.get("/summary")
def replies_summary(request: Request, q: _Q = Depends()):
    user = _user(request)
    svc = _svc(request)
    p = q.resolve(svc, user)
    data = rp.analyze(svc, **p)
    data["index"] = rp.status(svc, p["account_ids"] if _is_mailbox(user) else None)
    if _is_mailbox(user):
        data["index"].pop("job_id", None)
    return data


@router.get("/list")
def replies_list(request: Request, mode: str = "unanswered", text: str = "", order: str = "old", offset: int = 0,
                 limit: int = 100, q: _Q = Depends()):
    user = _user(request)
    svc = _svc(request)
    if mode not in rp.LIST_MODES:
        raise HTTPException(400, "Неизвестный список писем.")
    p = q.resolve(svc, user)
    return rp.list_messages(svc, mode=mode, q=text[:200], order="new" if order == "new" else "old",
                            offset=max(0, offset), limit=max(1, min(500, limit)), **p)


@router.get("/list.csv")
def replies_list_csv(request: Request, mode: str = "unanswered", text: str = "", order: str = "old",
                     q: _Q = Depends()):
    user = _user(request)
    svc = _svc(request)
    if mode not in rp.LIST_MODES:
        raise HTTPException(400, "Неизвестный список писем.")
    p = q.resolve(svc, user)
    listing = rp.list_messages(svc, mode=mode, q=text[:200], order="new" if order == "new" else "old",
                               offset=0, limit=rp.LIST_MAX, **p)
    scope = "все ящики" if p["account_ids"] is None else f"ящики {','.join(map(str, p['account_ids'][:20]))}"
    svc.db.add_audit(user["username"], "replies_export",
                     f"{rp.LIST_MODES[mode]}: {len(listing['items'])} писем; {scope}")
    body = rp.list_csv(svc, svc.local_tz(), listing)
    stamp = datetime.now(svc.local_tz()).strftime("%Y-%m-%d")
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="replies-{mode}-{stamp}.csv"',
                             "Cache-Control": "no-store"})


def _enqueue(svc, user: dict) -> int:
    job = next((j for j in svc.db.active_jobs() if j["type"] == JobType.REPLY_INDEX), None)
    if job:
        return int(job["id"])
    return int(svc.queue.enqueue(JobType.REPLY_INDEX, None, {}, priority=7, created_by=user["username"]))


@router.post("/update")
def replies_update(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Разобрать новые письма сейчас, не дожидаясь планировщика."""
    svc = _svc(request)
    return {"job_id": _enqueue(svc, user)}


@router.post("/rebuild")
def replies_rebuild(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Пересобрать индекс целиком: после смены адресов ящиков и сотрудников (направление писем)."""
    svc = _svc(request)
    rp.reset_index(svc.db)
    svc.db.add_audit(user["username"], "replies_rebuild", "индекс ответов на письма пересобирается")
    return {"job_id": _enqueue(svc, user)}
