"""
REST API раздела «Ящики на сервере» (только администратор).

Логика — в :mod:`mailarchiver.serverops`; здесь приём запросов, права и
отдача файлов. Всё, что меняет учётные записи Axigen, дополнительно требует
«Разрешить изменения на сервере» в настройках Axigen; всё, что удаляет письма
или учётные записи, выполняется только при полном архиве (проверяется в самих
действиях).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..errors import ValidationError
from . import auth as auth_mod

router = APIRouter(prefix="/api/server")


def _svc(request: Request):
    return request.app.state.services


@router.get("/targets")
def server_targets(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Ящики на сервере: учётные записи Axigen и ящики архива на других серверах."""
    from ..serverops.targets import overview
    return overview(_svc(request))


class KeysBody(BaseModel):
    keys: Optional[List[str]] = None


@router.post("/targets/export")
def server_targets_export(request: Request, body: Optional[KeysBody] = None,
                          user: dict = Depends(auth_mod.require_admin)):
    """Список ящиков раздела файлом Excel (все или перечисленные)."""
    from ..serverops.report import targets_file
    svc = _svc(request)
    keys = (body.keys if body else None) or None
    if keys is not None and len(keys) > 20000:
        raise ValidationError("Слишком длинный список.")
    data, name, ctype = targets_file(svc, keys)
    svc.db.add_audit(user["username"], "server_export_list", f"ящиков: {len(keys) if keys else 'все'}")
    return _file(data, name, ctype)


@router.get("/actions")
def server_actions(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Какие действия есть и какие у них параметры (для формы в интерфейсе)."""
    from ..serverops.base import describe_actions
    return describe_actions(_svc(request))


class OpBody(BaseModel):
    action: str = Field("", max_length=64)
    keys: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)
    preview: bool = True
    confirm: str = Field("", max_length=20)


@router.post("/ops")
def server_op(request: Request, body: OpBody, user: dict = Depends(auth_mod.require_admin)):
    """Проверить (``preview``) или запустить действие над ящиками."""
    from ..serverops.base import run
    return run(_svc(request), user, body.action, body.keys, body.params, preview=body.preview,
               confirm=body.confirm)


class CreateBody(BaseModel):
    source: str = Field("text", max_length=16)
    text: str = Field("", max_length=2_000_000)
    password_mode: str = Field("random_file", max_length=16)
    password: str = Field("", max_length=128)
    must_change: bool = True
    quota_mb: int = 0
    add_to_archive: bool = True
    preview: bool = True
    #: где создавать: 0 — Axigen, иначе номер подключения к почтовому серверу
    server_id: int = 0


@router.post("/create")
def server_create(request: Request, body: CreateBody, user: dict = Depends(auth_mod.require_admin)):
    """Создать учётные записи списком — на Axigen или на подключённом почтовом сервере (проверка — ``preview``)."""
    from ..serverops import create
    return create.run(_svc(request), user, body.model_dump(), preview=body.preview)


@router.get("/history")
def server_history(request: Request, limit: int = 50, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops.base import history
    return {"items": history(_svc(request), limit)}


def _item(request: Request, op_id: int) -> dict:
    from ..serverops.base import history_item
    if op_id < 0 or op_id > 2 ** 63 - 1:
        raise HTTPException(404, "Не найдено")
    item = history_item(_svc(request), op_id)
    if item is None:
        raise HTTPException(404, "Операция не найдена")
    return item


@router.get("/history/{op_id}")
def server_history_item(request: Request, op_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops.base import history_item_full
    _item(request, op_id)
    return history_item_full(_svc(request), op_id)


@router.post("/history/{op_id}/cancel")
def server_history_cancel(request: Request, op_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops.base import cancel_op
    _item(request, op_id)
    svc = _svc(request)
    cancelled = cancel_op(svc, op_id)
    svc.db.add_audit(user["username"], "server_cancel", f"операция №{op_id}: отменено заданий {cancelled}")
    return {"ok": True, "cancelled": cancelled}


@router.get("/history/{op_id}/retry")
def server_history_retry(request: Request, op_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Что запустить заново: то же действие для ящиков с ошибкой и не обработанных (запуск — через проверку)."""
    from ..serverops.base import retry_info
    _item(request, op_id)
    return retry_info(_svc(request), op_id)


@router.post("/history/{op_id}/report")
def server_history_report(request: Request, op_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Отчёт о ящиках (действие «Отчёт») — файлом Excel."""
    from ..serverops.report import report_file
    _item(request, op_id)
    data, name, ctype = report_file(_svc(request), op_id)
    return _file(data, name, ctype)


@router.post("/history/{op_id}/passwords")
def server_history_passwords(request: Request, op_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Новые пароли операции — файлом Excel, один раз (после выдачи они стираются)."""
    from ..serverops.report import passwords_file
    _item(request, op_id)
    svc = _svc(request)
    data, name, ctype = passwords_file(svc, op_id)
    svc.db.add_audit(user["username"], "server_passwords_download", f"операция №{op_id}")
    return _file(data, name, ctype)


# ---------------------------------------------------------------------------
#  Расписания (только по явному согласию)
# ---------------------------------------------------------------------------
class ScheduleCreateBody(BaseModel):
    action: str = Field("", max_length=64)
    keys: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)
    when: Dict[str, Any] = Field(default_factory=dict)
    consent: bool = False
    confirm: str = Field("", max_length=20)


class ScheduleToggleBody(BaseModel):
    enabled: bool


@router.get("/schedules")
def server_schedules(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops import schedules
    svc = _svc(request)
    return {"items": schedules.list_all(svc), "scheduler_enabled": bool(svc.rt("scheduler", "enabled"))}


@router.post("/schedules")
def server_schedule_create(request: Request, body: ScheduleCreateBody, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops import schedules
    return schedules.create(_svc(request), user, action_key=body.action, keys=body.keys, params=body.params,
                            when=body.when, consent=body.consent, confirm=body.confirm)


@router.put("/schedules/{schedule_id}")
def server_schedule_toggle(request: Request, schedule_id: int, body: ScheduleToggleBody,
                           user: dict = Depends(auth_mod.require_admin)):
    from ..serverops import schedules
    return schedules.set_enabled(_svc(request), user, schedule_id, body.enabled)


@router.delete("/schedules/{schedule_id}")
def server_schedule_delete(request: Request, schedule_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..serverops import schedules
    schedules.delete(_svc(request), user, schedule_id)
    return {"ok": True}


def _file(data: bytes, name: str, ctype: str) -> Response:
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
