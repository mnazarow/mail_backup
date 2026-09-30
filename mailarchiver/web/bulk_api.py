"""
REST API групповых действий над почтовыми ящиками (раздел «Почтовые ящики →
Групповые действия»). Вся логика — в :mod:`mailarchiver.bulk`; здесь только
приём запросов, права и отдача файлов. Всё доступно администратору; оператору —
только копирование и проверки (:data:`mailarchiver.roles.OPERATOR_BULK_ACTIONS`)
и выгрузка списка ящиков.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import bulk, roles
from ..errors import ValidationError
from . import auth as auth_mod

router = APIRouter(prefix="/api")


def _svc(request: Request):
    return request.app.state.services


class BulkBody(BaseModel):
    action: str = Field("", max_length=64)
    ids: List[int] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)
    #: True — только проверить и показать, что будет сделано (ничего не меняет)
    preview: bool = True
    #: для опасных действий — число ящиков, которые будут обработаны
    confirm: str = Field("", max_length=20)


class BulkCreateBody(BaseModel):
    #: строки «адрес[;пароль]» — пароли не сохраняются нигде, кроме самих ящиков
    text: str = Field("", max_length=2_000_000)
    host: str = Field("", max_length=255)
    port: int = 993
    security: str = Field("ssl", max_length=16)
    auth_type: str = Field("password", max_length=16)
    enable: bool = True
    schedule_time: str = Field("", max_length=5)
    preview: bool = True


class ExportListBody(BaseModel):
    ids: Optional[List[int]] = None
    format: str = Field("xlsx", max_length=8)


def _operator_action(user: dict, action: str) -> bool:
    return not roles.is_operator(user) or (action or "") in roles.OPERATOR_BULK_ACTIONS


_OPERATOR_REFUSAL = "Оператору доступны только копирование и проверки ящиков"


@router.get("/accounts/bulk/actions")
def bulk_actions(request: Request, user: dict = Depends(auth_mod.require_staff)):
    """Какие групповые действия есть и какие у них параметры (для формы в интерфейсе)."""
    out = bulk.describe_actions(_svc(request))
    if roles.is_operator(user):
        out["actions"] = [a for a in out["actions"] if _operator_action(user, a.get("key"))]
        used = {a.get("group") for a in out["actions"]}
        out["groups"] = [g for g in out["groups"] if g["key"] in used]
        out.pop("create_defaults", None)
    return out


@router.post("/accounts/bulk")
def bulk_run(request: Request, body: BulkBody, user: dict = Depends(auth_mod.require_staff)):
    """Проверить (``preview``) или выполнить групповое действие над ящиками."""
    if not _operator_action(user, body.action):
        raise HTTPException(403, _OPERATOR_REFUSAL)
    return bulk.run(_svc(request), user, body.action, body.ids, body.params,
                    preview=body.preview, confirm=body.confirm)


@router.post("/accounts/bulk-create")
def bulk_create_accounts(request: Request, body: BulkCreateBody, user: dict = Depends(auth_mod.require_admin)):
    """Завести много ящиков сразу из списка адресов (предпросмотр — ``preview``)."""
    return bulk.bulk_create(_svc(request), user, body.text, host=body.host, port=body.port,
                            security=body.security, auth_type=body.auth_type, enable=body.enable,
                            schedule_time=body.schedule_time, preview=body.preview)


@router.get("/accounts/bulk/history")
def bulk_history(request: Request, limit: int = 50, user: dict = Depends(auth_mod.require_staff)):
    items = bulk.history(_svc(request), limit)
    if roles.is_operator(user):
        # оператор видит только операции копирования и проверок — в параметрах
        # остальных бывают заметки, адреса серверов и сроки хранения
        items = [i for i in items if _operator_action(user, i.get("action"))]
    return {"items": items}


def _history_item_for(request: Request, op_id: int, user: dict) -> dict:
    if op_id < 0 or op_id > 2 ** 63 - 1:
        raise HTTPException(404, "Не найдено")
    item = bulk.history_item(_svc(request), op_id)
    if item is None or not _operator_action(user, item.get("action")):
        raise HTTPException(404, "Групповая операция не найдена")
    return item


@router.get("/accounts/bulk/history/{op_id}")
def bulk_history_item(request: Request, op_id: int, user: dict = Depends(auth_mod.require_staff)):
    return _history_item_for(request, op_id, user)


@router.post("/accounts/bulk/history/{op_id}/cancel")
def bulk_history_cancel(request: Request, op_id: int, user: dict = Depends(auth_mod.require_staff)):
    """Отменить незавершённые задания, поставленные групповой операцией."""
    _history_item_for(request, op_id, user)
    svc = _svc(request)
    cancelled = bulk.cancel_op_jobs(svc, op_id, only_operator=roles.is_operator(user))
    svc.db.add_audit(user["username"], "bulk_cancel", f"операция №{op_id}: отменено заданий {cancelled}")
    return {"ok": True, "cancelled": cancelled}


@router.post("/accounts/export-list")
def export_account_list(request: Request, body: Optional[ExportListBody] = None,
                        user: dict = Depends(auth_mod.require_staff)):
    """Список ящиков файлом Excel (или CSV): все или перечисленные."""
    svc = _svc(request)
    body = body or ExportListBody()
    fmt = (body.format or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        raise ValidationError("Формат списка — xlsx или csv.")
    data, filename, ctype = bulk.export_list(svc, body.ids, fmt, with_notes=not roles.is_operator(user))
    svc.db.add_audit(user["username"], "accounts_export_list",
                     f"{fmt}: ящиков {len(body.ids) if body.ids else 'все'}")
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})
