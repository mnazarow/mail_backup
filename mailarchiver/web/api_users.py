"""API: пользователи интерфейса, роли, API-токены.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..errors import ValidationError
from .. import roles
from . import auth as auth_mod

from .api_common import PasswordBody, UserBody, UserRoleBody, _bounded_id, svc_dep
from .api_employees import _row_get

router = APIRouter(prefix="/api")

# =====================================================================
#  Пользователи (только администратор)
# =====================================================================
class TokenBody(BaseModel):
    name: str = Field("", max_length=100)
    role: str = Field("operator", max_length=20)
    days: int = Field(365, ge=0, le=3650)
    user_id: Optional[int] = None


@router.get("/tokens")
def list_api_tokens(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..apitokens import list_tokens
    return {"items": list_tokens(svc_dep(request))}


@router.post("/tokens")
def create_api_token(request: Request, body: TokenBody, user: dict = Depends(auth_mod.require_admin)):
    """Выпустить API-токен: показывается ОДИН раз, в базе — только его SHA-256."""
    from ..apitokens import create
    return create(svc_dep(request), user, name=body.name, role=body.role, days=body.days, user_id=body.user_id)


@router.delete("/tokens/{token_id}")
def revoke_api_token(request: Request, token_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..apitokens import revoke
    try:
        revoke(svc_dep(request), user, token_id)
    except ValidationError as exc:
        if exc.code == "not_found":
            raise HTTPException(404, exc.message) from None
        raise
    return {"ok": True}


@router.get("/users")
def list_users(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    return [{"id": r["id"], "username": r["username"], "role": r["role"], "created_at": r["created_at"],
             "last_login": r["last_login"], "disabled": bool(r["disabled"]),
             "totp_enabled": bool(r["totp_enabled"]), "auth_source": _row_get(r, "auth_source") or "local",
             "disabled_by": _row_get(r, "disabled_by") or ""}
            for r in svc.db.list_users()]


@router.post("/users/{user_id}/2fa/reset")
def reset_user_2fa(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Сбросить 2FA другого пользователя (потерял телефон). Сеансы завершаются."""
    svc = svc_dep(request)
    if user_id == user["id"]:
        raise ValidationError("Свой двухфакторный вход отключается в профиле (нужны пароль и код).")
    target = svc.db.get_user_by_id(user_id)
    if target is None:
        raise ValidationError("Пользователь не найден.")
    svc.db.disable_totp(user_id)
    svc.db.delete_user_sessions(user_id)
    svc.db.add_audit(user["username"], "2fa_reset", target["username"])
    return {"ok": True}


@router.post("/users")
def create_user(request: Request, body: UserBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    role = _staff_role(body.role or roles.ROLE_OPERATOR)
    # Сначала strip, потом проверки: логин « adm» раньше проходил проверку
    # уникальности и падал в БД с ошибкой 500, а «   » создавал пустое имя.
    username = (body.username or "").strip()
    if not username:
        raise ValidationError("Укажите имя пользователя.")
    if svc.db.get_user_by_name(username):
        raise ValidationError("Пользователь с таким именем уже существует.")
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    try:
        uid = svc.db.create_user(username, hash_password(body.password), role)
    except sqlite3.IntegrityError:
        raise ValidationError("Пользователь с таким именем уже существует.")
    svc.db.add_audit(user["username"], "user_create", f"{username} ({roles.ROLE_LABELS[role]})")
    return {"ok": True, "id": uid}


#: Отказ, если после действия не осталось бы ни одного включённого администратора.
_LAST_ADMIN_REFUSAL = ("Нельзя: в системе не останется ни одного включённого администратора.")


def _staff_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if role not in roles.STAFF_ROLES:
        # вход по ящику (role=mailbox) выполняется по email и паролю ящика,
        # отдельная запись пользователя для этого не создаётся и не работает
        raise ValidationError("Роль пользователя — «admin» (администратор) или «operator» (оператор).",
                              hint="Для доступа к своему ящику сотрудник входит по email и паролю ящика.")
    return role


@router.post("/users/{user_id}/role")
def set_user_role(request: Request, user_id: int, body: UserRoleBody,
                  user: dict = Depends(auth_mod.require_admin)):
    """Сменить роль пользователя (администратор ↔ оператор). Сеансы завершаются."""
    svc = svc_dep(request)
    role = _staff_role(body.role)
    if user_id == user["id"]:
        # так в системе всегда остаётся хотя бы один администратор — тот, кто меняет роли
        raise ValidationError("Свою роль поменять нельзя.",
                              hint="Попросите другого администратора.")
    target = svc.db.get_user_by_id(_bounded_id(user_id))
    if target is None:
        raise ValidationError("Пользователь не найден.")
    if (target["role"] or roles.ROLE_ADMIN) == role:
        return {"ok": True, "changed": False}
    if not svc.db.set_user_role(user_id, role):
        raise ValidationError(_LAST_ADMIN_REFUSAL)
    # права берутся из сессии при каждом запросе, но открытые вкладки с прежним
    # набором разделов лучше закрыть явно
    svc.db.delete_user_sessions(user_id)
    svc.db.add_audit(user["username"], "user_role", f"{target['username']}: {roles.ROLE_LABELS[role]}")
    return {"ok": True, "changed": True}


@router.put("/users/{user_id}/password")
def set_user_password(request: Request, user_id: int, response: Response, body: PasswordBody,
                      user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    from ..security import check_password_policy, hash_password
    policy = check_password_policy(body.password, int(svc.rt("security", "min_password_length") or 8))
    if policy:
        raise ValidationError(policy)
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    # set_user_password завершает все сеансы пользователя (иначе старая cookie
    # продолжала бы работать после смены пароля). Если администратор меняет
    # пароль СЕБЕ, тут же выдаём новую сессию — иначе его выбрасывало бы из
    # интерфейса сразу после сохранения.
    svc.db.set_user_password(user_id, hash_password(body.password))
    target = svc.db.get_user_by_id(user_id)
    svc.db.add_audit(user["username"], "user_password", (target["username"] if target else str(user_id)))
    if user_id == user["id"]:
        auth_mod.create_session(svc, response, user_id, request, role=user.get("role", "admin"))
        return {"ok": True, "sessions_closed": True, "self": True}
    return {"ok": True, "sessions_closed": True}


@router.post("/users/{user_id}/logout-all")
def logout_all_sessions(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Завершить все открытые сеансы пользователя."""
    svc = svc_dep(request)
    target = svc.db.get_user_by_id(user_id)
    if not target:
        raise ValidationError("Пользователь не найден.")
    closed = svc.db.delete_user_sessions(user_id)
    svc.db.add_audit(user["username"], "user_logout_all", target["username"])
    return {"ok": True, "closed": closed}


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int, disabled: bool = True, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    if user_id == user["id"] and disabled:
        raise ValidationError("Нельзя отключить самого себя.")
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    if not svc.db.set_user_disabled(user_id, disabled):
        raise ValidationError(_LAST_ADMIN_REFUSAL)
    svc.db.add_audit(user["username"], "user_disable" if disabled else "user_enable", str(user_id))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: int, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    if user_id == user["id"]:
        raise ValidationError("Нельзя удалить самого себя.")
    if svc.db.count_users() <= 1:
        raise ValidationError("Нельзя удалить последнего пользователя.")
    if svc.db.get_user_by_id(user_id) is None:
        raise ValidationError("Пользователь не найден.")
    if not svc.db.delete_user(user_id):
        raise ValidationError(_LAST_ADMIN_REFUSAL)
    svc.db.add_audit(user["username"], "user_delete", str(user_id))
    return {"ok": True}
