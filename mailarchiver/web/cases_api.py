"""
REST API раздела «Дела» (eDiscovery, 1.20.0) — только администратору.

Оператор писем не читает, сотрудник не должен видеть, что его переписка собрана для проверки, —
поэтому раздел и все его ответы доступны одному администратору. Логика — :mod:`mailarchiver.cases`.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import cases as cs
from ..errors import ValidationError
from . import auth as auth_mod

router = APIRouter(prefix="/api/cases")


def _svc(request: Request):
    return request.app.state.services


def _get(svc, case_id: int):
    try:
        return cs.get_case(svc, case_id)
    except ValidationError as exc:
        raise HTTPException(404, exc.message) from None


class CaseBody(BaseModel):
    name: str = Field("", max_length=cs.NAME_MAX)
    description: str = Field("", max_length=cs.DESC_MAX)
    query: str = Field("", max_length=cs.QUERY_MAX)
    date_from: str = Field("", max_length=20)
    date_to: str = Field("", max_length=20)
    hold: bool = True
    account_ids: List[int] = Field(default_factory=list, max_length=20000)


class CaseUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=cs.NAME_MAX)
    description: Optional[str] = Field(None, max_length=cs.DESC_MAX)
    query: Optional[str] = Field(None, max_length=cs.QUERY_MAX)
    date_from: Optional[str] = Field(None, max_length=20)
    date_to: Optional[str] = Field(None, max_length=20)
    hold: Optional[bool] = None


class CustodiansBody(BaseModel):
    add: List[int] = Field(default_factory=list, max_length=20000)
    remove: List[int] = Field(default_factory=list, max_length=20000)


@router.get("")
def list_cases(request: Request, user: dict = Depends(auth_mod.require_admin)):
    return {"items": cs.list_cases(_svc(request))}


@router.post("")
def create_case(request: Request, body: CaseBody, user: dict = Depends(auth_mod.require_admin)):
    case_id = cs.create_case(_svc(request), user["username"], name=body.name, description=body.description,
                             query=body.query, date_from=body.date_from, date_to=body.date_to, hold=body.hold,
                             account_ids=body.account_ids)
    return {"ok": True, "id": case_id}


@router.get("/{case_id}")
def get_case(request: Request, case_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    return cs.case_dict(svc, _get(svc, case_id), full=True)


@router.put("/{case_id}")
def update_case(request: Request, case_id: int, body: CaseUpdate, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    cs.update_case(svc, user["username"], case_id, fields)
    return {"ok": True}


@router.post("/{case_id}/custodians")
def custodians(request: Request, case_id: int, body: CustodiansBody, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    return {"ok": True, **cs.set_custodians(svc, user["username"], case_id, body.add, body.remove)}


@router.get("/{case_id}/messages")
def messages(request: Request, case_id: int, offset: int = 0, limit: int = cs.PAGE,
             user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    if offset == 0:
        svc.db.add_audit(user["username"], "case_view", f"case={int(case_id)} просмотр писем дела")
    return cs.list_messages(svc, case_id, offset, limit)


@router.post("/{case_id}/export")
def export(request: Request, case_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    return {"ok": True, **cs.start_export(svc, user["username"], case_id)}


@router.get("/{case_id}/exports/{export_id}/download")
def download(request: Request, case_id: int, export_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    row = _get(svc, case_id)
    try:
        path, name = cs.export_path(svc, case_id, export_id)
    except ValidationError as exc:
        raise HTTPException(404, exc.message) from None
    svc.db.add_audit(user["username"], "case_download", f"case={int(case_id)} «{row['name']}»: {name}")
    return FileResponse(path, filename=name, media_type="application/zip")


@router.post("/{case_id}/close")
def close(request: Request, case_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    cs.close_case(svc, user["username"], case_id)
    return {"ok": True}


@router.post("/{case_id}/reopen")
def reopen(request: Request, case_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    cs.close_case(svc, user["username"], case_id, reopen=True)
    return {"ok": True}


@router.delete("/{case_id}")
def delete(request: Request, case_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = _svc(request)
    _get(svc, case_id)
    cs.delete_case(svc, user["username"], case_id)
    return {"ok": True}
