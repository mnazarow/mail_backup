"""API: первичная настройка, вход (пароль, единый вход OpenID Connect, 2FA), «о себе».

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..errors import ValidationError
from ..version import __version__
from . import auth as auth_mod

from .api_common import LoginBody, PASSWORD_MAX, SetupBody, _own_ids, log, svc_dep

router = APIRouter(prefix="/api")

# =====================================================================
#  Аутентификация
# =====================================================================
@router.get("/needs-setup")
def needs_setup(request: Request):
    from .. import oidc
    svc = svc_dep(request)
    return {"needs_setup": svc.db.count_users() == 0, "auth_enabled": bool(svc.rt("security", "auth_enabled")),
            "oidc": {"enabled": oidc.enabled(svc),
                     "button": str(svc.rt("oidc", "button_text") or "Войти через единый вход")}}


# ---- единый вход (OpenID Connect, 1.22.0) --------------------------------
def _sso_fail(svc, request: Request, text: str) -> RedirectResponse:
    from .. import oidc
    ip = auth_mod.client_ip(request)
    log.warning("Единый вход не удался (%s): %s", ip, text)
    svc.db.add_audit("-", "login_oidc_error", f"{ip}: {text}"[:500])
    resp = RedirectResponse("/?sso_error=1#/", status_code=303)
    resp.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH)
    return resp


@router.get("/auth/oidc/start", name="oidc_start")
def oidc_start(request: Request):
    from .. import oidc
    svc = svc_dep(request)
    if not oidc.enabled(svc):
        raise HTTPException(404, "Единый вход не настроен")
    try:
        url, state_cookie = oidc.start(svc, request)
    except oidc.OidcError as exc:
        return _sso_fail(svc, request, str(exc))
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(oidc.COOKIE, state_cookie, max_age=oidc.STATE_TTL_S, path=oidc.COOKIE_PATH, httponly=True,
                     samesite="lax", secure=str(request.url.scheme).lower() == "https")
    return resp


@router.get("/auth/oidc/callback", name="oidc_callback")
def oidc_callback(request: Request):
    """Возврат с поставщика: код → проверенный id_token → сессия архива."""
    from .. import ldapauth, oidc
    svc = svc_dep(request)
    if not oidc.enabled(svc):
        raise HTTPException(404, "Единый вход не настроен")
    ip = auth_mod.client_ip(request)
    try:
        info = oidc.finish(svc, dict(request.query_params), request.cookies.get(oidc.COOKIE, ""), request)
        resp = RedirectResponse("/#/", status_code=303)
        resp.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH)
        if info["role"] in ("admin", "operator"):
            user = oidc.ensure_user(svc, info)
            svc.db.set_last_login(int(user["id"]))
            svc.db.add_audit(info["username"], "login", f"{ip} (OIDC)")
            auth_mod.create_session(svc, resp, int(user["id"]), request, role=user["role"])
            return resp
        oidc.ensure_user(svc, info)                    # прав на архив нет — учётную запись из OIDC отключить
        acc = ldapauth.mailbox_for(svc, info["email"]) if info["role"] == "mailbox" else None
        if acc is None:
            raise oidc.OidcError(f"{info['username']}: нет прав на архив" +
                                 (" и ящика с адресом " + info["email"] if info["role"] == "mailbox" else ""))
        svc.db.add_audit(info["username"], "login_mailbox", f"{ip} account={acc.id} (OIDC)")
        auth_mod.create_session(svc, resp, 0, request, role="mailbox", account_id=acc.id)
        return resp
    except oidc.OidcError as exc:
        return _sso_fail(svc, request, str(exc))


@router.post("/oidc/check")
def oidc_check(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверка настроек единого входа: описание поставщика и ключи подписи."""
    from .. import oidc
    svc = svc_dep(request)
    res = oidc.check(svc)
    res["redirect_uri"] = oidc.redirect_uri(svc, request)
    return res


@router.post("/setup")
def setup(request: Request, body: SetupBody):
    svc = svc_dep(request)
    user = auth_mod.create_first_admin(svc, body.username, body.password)
    return {"ok": True, "user": user}


@router.post("/login")
def login(request: Request, response: Response, body: LoginBody):
    svc = svc_dep(request)
    result = auth_mod.do_login(svc, request, response, body.username, body.password)
    if result.get("otp_required"):
        # пароль верен, нужен второй шаг — сессии ещё нет
        return {"ok": False, "otp_required": True, "challenge": result["challenge"]}
    return {"ok": True, "user": result}


class OtpLoginBody(BaseModel):
    challenge: str = Field(max_length=4096)
    code: str = Field(max_length=128)


@router.post("/login/otp")
def login_otp(request: Request, response: Response, body: OtpLoginBody):
    svc = svc_dep(request)
    user = auth_mod.do_login_otp(svc, request, response, body.challenge, body.code)
    return {"ok": True, "user": user}


@router.post("/logout")
def logout(request: Request, response: Response):
    svc = svc_dep(request)
    auth_mod.destroy_session(svc, request, response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = auth_mod.current_user(request)
    if user is None:
        raise HTTPException(401, "Не авторизован")
    svc = svc_dep(request)
    out = dict(user)
    out["version"] = __version__
    out["require_2fa"] = bool(svc.rt("security", "require_2fa"))
    out["must_enroll_2fa"] = auth_mod.two_factor_required(svc, user)
    # разделы, которые включаются в настройках (меню показывает их, только когда они включены)
    out["features"] = {"axigen": bool(svc.rt("axigen", "enabled")) if user.get("role") == "admin" else False}
    if user.get("role") == "mailbox":
        # все ящики сотрудника — для переключателя в «Почте» (вошедший — первым)
        names = svc.db.account_names()
        out["accounts"] = [{"id": aid, "name": names.get(aid, ""), "rights": auth_mod.mailbox_rights(user, aid)}
                           for aid in _own_ids(user)]
    if user.get("role") == "admin":
        from ..mailservers import store as msstore
        out["features"]["mailservers"] = len(msstore.list_servers(svc.db))
    return out


# ---------------------------------------------------------------------
#  Двухфакторный вход текущего пользователя
# ---------------------------------------------------------------------
class OtpCodeBody(BaseModel):
    code: str = Field("", max_length=128)


class OtpDisableBody(BaseModel):
    password: str = Field("", max_length=PASSWORD_MAX)
    code: str = Field("", max_length=128)


def _own_web_user(request: Request) -> dict:
    """Текущий пользователь веб-интерфейса (не вход по ящику): администратор или оператор."""
    user = auth_mod.require_login(request)
    if user.get("role") == "mailbox" or not user.get("id"):
        raise HTTPException(403, "Двухфакторный вход настраивается для пользователей веб-интерфейса; "
                                 "при входе по ящику защита — пароль самого ящика.")
    return user


def _check_user_code(svc, request: Request, user: dict, code: str, allow_recovery: bool = True) -> str:
    """Проверить код 2FA пользователя: 'totp' | 'recovery'; иначе ValidationError.

    Неверные коды учитываются так же, как при входе (лимит на учётную запись):
    иначе с чужой открытой сессией можно было бы перебрать код здесь и выпустить
    себе новые резервные коды.
    """
    state = svc.db.totp_state(user["id"]) or {}
    if not state.get("enabled"):
        raise ValidationError("Двухфакторный вход не включён.")
    ip = auth_mod.client_ip(request)
    how, attempt_id = auth_mod.check_second_factor(svc, user["id"], user["username"], ip, code,
                                                   allow_recovery=allow_recovery, state=state)
    if how is None:
        svc.db.add_audit(user["username"], "2fa_code_failed", ip)
        raise ValidationError("Неверный код подтверждения.",
                              hint="Введите 6 цифр из приложения-аутентификатора"
                                   + (" или резервный код." if allow_recovery else "."))
    svc.db.delete_login_attempt(attempt_id)
    return how


def _new_recovery(svc, user_id: int):
    from .. import totp as totp_mod
    from ..security import hash_password
    codes = totp_mod.new_recovery_codes()
    svc.db.set_totp_recovery(user_id, [hash_password(totp_mod.normalize_recovery(c)) for c in codes])
    return codes


@router.get("/me/2fa")
def my_2fa_status(request: Request):
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    return {"enabled": bool(state.get("enabled")), "pending": bool(state.get("pending")),
            "recovery_left": len(state.get("recovery") or []),
            "required": bool(svc.rt("security", "require_2fa"))}


@router.post("/me/2fa/setup")
def my_2fa_setup(request: Request):
    """Начать включение: новый секрет, ссылка otpauth и QR-код (пока не активен)."""
    from .. import qrcode, totp as totp_mod
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    if state.get("enabled"):
        raise ValidationError("Двухфакторный вход уже включён.",
                              hint="Чтобы привязать другой телефон, сначала отключите его.")
    secret = totp_mod.new_secret()
    svc.db.set_totp_pending(user["id"], secret)
    uri = totp_mod.provisioning_uri(secret, user["username"])
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "uri": uri, "qr_svg": qrcode.to_svg(uri, scale=5)}


@router.post("/me/2fa/enable")
def my_2fa_enable(request: Request, body: OtpCodeBody):
    """Подтвердить включение кодом из приложения; выдать резервные коды (один раз)."""
    from .. import totp as totp_mod
    svc = svc_dep(request)
    user = _own_web_user(request)
    state = svc.db.totp_state(user["id"]) or {}
    if state.get("enabled"):
        raise ValidationError("Двухфакторный вход уже включён.")
    pending = state.get("pending")
    if not pending:
        raise ValidationError("Сначала получите QR-код для приложения.")
    step = totp_mod.verify(pending, body.code)
    if step is None:
        raise ValidationError("Код не подошёл.",
                              hint="Проверьте, что время на телефоне и на сервере установлено точно, "
                                   "и введите код, который показывает приложение сейчас.")
    svc.db.enable_totp(user["id"], pending, [], step)
    codes = _new_recovery(svc, user["id"])
    svc.db.add_audit(user["username"], "2fa_enabled", auth_mod.client_ip(request))
    return {"ok": True, "recovery_codes": codes}


@router.post("/me/2fa/disable")
def my_2fa_disable(request: Request, body: OtpDisableBody):
    from ..security import verify_password
    svc = svc_dep(request)
    user = _own_web_user(request)
    if svc.rt("security", "require_2fa"):
        raise ValidationError("Двухфакторный вход обязателен по настройкам безопасности — отключить его нельзя.",
                              hint="Привязать другой телефон поможет администратор: «Пользователи» → «Сбросить 2FA».")
    # Пароль здесь проверяется с тем же учётом неудач, что и при входе: иначе
    # с чужой открытой сессией его можно было бы подбирать без ограничений.
    ip = auth_mod.client_ip(request)
    auth_mod.guard_password_check(svc, user["username"], ip)
    row = svc.db.get_user_by_id(user["id"])
    if row is None or not verify_password(body.password or "", row["password_hash"]):
        svc.db.record_login_attempt(user["username"], False, ip)
        svc.db.add_audit(user["username"], "2fa_disable_bad_password", ip)
        raise ValidationError("Неверный пароль.")
    _check_user_code(svc, request, user, body.code)
    svc.db.disable_totp(user["id"])
    svc.db.add_audit(user["username"], "2fa_disabled", auth_mod.client_ip(request))
    return {"ok": True}


@router.post("/me/2fa/recovery")
def my_2fa_new_recovery(request: Request, body: OtpCodeBody):
    """Выпустить новые резервные коды (старые перестают действовать)."""
    svc = svc_dep(request)
    user = _own_web_user(request)
    _check_user_code(svc, request, user, body.code, allow_recovery=False)
    codes = _new_recovery(svc, user["id"])
    svc.db.add_audit(user["username"], "2fa_recovery_regenerated", auth_mod.client_ip(request))
    return {"ok": True, "recovery_codes": codes}
