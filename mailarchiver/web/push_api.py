"""API уведомлений о новой почте на устройства сотрудника (Web Push, 1.22.0): ``/api/push/...``.

Только для сотрудника, вошедшего паролем своего ящика: подписка устройства, проверка и снятие.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import webpush
from . import auth as auth_mod

router = APIRouter(prefix="/api/push")


def _user(request: Request) -> dict:
    user = auth_mod.require_user(request)
    if user.get("role") != "mailbox":
        raise HTTPException(403, "Уведомления о новой почте включает сам сотрудник в своей «Почте».")
    return user


class EndpointBody(BaseModel):
    endpoint: str = Field("", max_length=1000)


class Keys(BaseModel):
    p256dh: str = Field(..., max_length=200)
    auth: str = Field(..., max_length=100)


class SubscribeBody(BaseModel):
    endpoint: str = Field(..., max_length=1000)
    keys: Keys
    account_ids: Optional[List[int]] = Field(None, max_length=50)


@router.post("/state")
def push_state(body: EndpointBody, request: Request, user: dict = Depends(_user)):
    return webpush.state(request.app.state.services, user, body.endpoint)


@router.post("/subscribe")
def push_subscribe(body: SubscribeBody, request: Request, user: dict = Depends(_user)):
    return webpush.subscribe(request.app.state.services, user, body.endpoint, body.keys.p256dh, body.keys.auth,
                             body.account_ids, request.headers.get("user-agent", ""))


@router.post("/unsubscribe")
def push_unsubscribe(body: EndpointBody, request: Request, user: dict = Depends(_user)):
    return webpush.unsubscribe(request.app.state.services, user, body.endpoint)


@router.post("/test")
def push_test(body: EndpointBody, request: Request, user: dict = Depends(_user)):
    return webpush.send_test(request.app.state.services, user, body.endpoint)


@router.delete("/devices/{sub_id}")
def push_forget(sub_id: int, request: Request, user: dict = Depends(_user)):
    return webpush.forget_device(request.app.state.services, user, sub_id)
