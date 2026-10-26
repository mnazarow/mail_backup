"""
REST API раздела «Почтовые серверы» (только администратор): подключения к Microsoft 365, Google Workspace,
Яндекс 360, Zimbra и другим серверам — настройка, проверка, сверка учётных записей, журнал обращений к API.

Логика — в :mod:`mailarchiver.mailservers`. Секреты подключения (секрет приложения, ключ, пароль
администратора) в ответах не отдаются — только «задан»; пустое поле при правке оставляет прежний секрет.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..errors import ValidationError
from . import auth as auth_mod

router = APIRouter(prefix="/api/mailservers")

MAX_VALUE = 20000


def _svc(request: Request):
    return request.app.state.services


def _id(server_id: int) -> int:
    if server_id < 1 or server_id > 2 ** 31:
        raise HTTPException(404, "Подключение не найдено")
    return int(server_id)


def _conn(request: Request, server_id: int):
    from ..mailservers import store
    conn = store.get_server(_svc(request).db, _id(server_id))
    if conn is None:
        raise HTTPException(404, "Подключение не найдено")
    return conn


def _view(svc, conn, problems=None, counts=None, running=None) -> Dict[str, Any]:
    from ..mailservers import service as mss
    data = conn.public()
    try:
        cls = mss.driver_class(conn.type)
        data.update({"label": cls.label, "caps": sorted(cls.caps), "cloud": cls.cloud, "known": True})
    except ValidationError:
        data.update({"label": conn.type, "caps": [], "cloud": False, "known": False})
    c = (counts or {}).get(conn.id) or {}
    data["accounts"] = {"total": c.get("live", 0), "blocked": c.get("blocked", 0),
                        "deleted": c.get("total", 0) - c.get("live", 0)}
    data["problems"] = [p for p in (problems or []) if p["server_id"] == conn.id]
    data["sync_running"] = conn.id in (running or set())
    mailboxes = [a for a in svc.db.list_accounts() if getattr(a, "server_id", None) == conn.id]
    data["mailboxes"] = len(mailboxes)
    return data


def _running(svc) -> set:
    from ..models import JobType
    out = set()
    for job in svc.db.active_jobs():
        if job["type"] == JobType.SERVER_SYNC:
            try:
                import json
                out.add(int((json.loads(job["params"] or "{}") or {}).get("server_id") or 0))
            except (TypeError, ValueError, KeyError, IndexError):
                pass
    return out


@router.get("/types")
def mailserver_types(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Какие серверы можно подключить: поля формы, что умеет, что настроить на стороне сервера."""
    from ..mailservers.service import describe_types
    return {"types": describe_types()}


@router.get("/brief")
def mailservers_brief(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Коротко о подключениях — для карточки ящика («через подключение к почтовому серверу»)."""
    from ..mailservers import service as mss
    from ..mailservers import store
    from ..mailservers.base import Cap
    svc = _svc(request)
    out = []
    for conn in store.list_servers(svc.db):
        try:
            cls = mss.driver_class(conn.type)
            host, port, security = mss.driver(svc, conn).imap_server()
        except Exception:  # noqa: BLE001 — неизвестный вид или неполные настройки
            continue
        out.append({"id": conn.id, "name": conn.name, "type": conn.type, "label": cls.label, "enabled": conn.enabled,
                    "imap_admin": cls.can(Cap.IMAP_ADMIN), "imap_host": host, "imap_port": port,
                    "imap_security": security})
    return {"items": out}


@router.get("")
def mailservers_list(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import monitor, store
    svc = _svc(request)
    conns = store.list_servers(svc.db)
    problems = monitor.problems(svc) if conns else []
    counts = store.counts(svc.db) if conns else {}
    running = _running(svc) if conns else set()
    return {"items": [_view(svc, c, problems, counts, running) for c in conns]}


@router.get("/{server_id}")
def mailserver_get(request: Request, server_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import monitor, store
    svc = _svc(request)
    conn = _conn(request, server_id)
    return _view(svc, conn, monitor.problems(svc, conn.id), store.counts(svc.db), _running(svc))


class ServerBody(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


def _clean(values: Dict[str, Any]) -> Dict[str, Any]:
    if len(values) > 100:
        raise ValidationError("Слишком много полей.")
    out = {}
    for key, value in values.items():
        if not isinstance(key, str) or len(key) > 64:
            raise ValidationError("Недопустимое поле.")
        if isinstance(value, str) and len(value) > MAX_VALUE:
            raise ValidationError(f"Слишком длинное значение поля «{key}».")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValidationError(f"Недопустимое значение поля «{key}».")
        out[key] = value
    return out


@router.post("")
def mailserver_create(request: Request, body: ServerBody, user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import service as mss
    server_id, warnings = mss.save_ex(_svc(request), user["username"], _clean(body.values))
    return {"ok": True, "id": server_id, "warnings": warnings}


@router.put("/{server_id}")
def mailserver_update(request: Request, server_id: int, body: ServerBody,
                      user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import service as mss
    conn = _conn(request, server_id)
    _sid, warnings = mss.save_ex(_svc(request), user["username"], _clean(body.values), conn.id)
    return {"ok": True, "id": conn.id, "warnings": warnings}


@router.delete("/{server_id}")
def mailserver_delete(request: Request, server_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import service as mss
    conn = _conn(request, server_id)
    mss.remove(_svc(request), user["username"], conn.id)
    return {"ok": True}


@router.post("/{server_id}/test")
def mailserver_test(request: Request, server_id: int, user: dict = Depends(auth_mod.require_admin)):
    """«Проверить подключение»: связь, права, версия, лицензии (итог — и в карточку подключения)."""
    from ..mailservers import service as mss
    conn = _conn(request, server_id)
    return mss.check(_svc(request), conn.id, user=user["username"])


class TrustBody(BaseModel):
    fingerprint: str = Field("", max_length=200)


@router.post("/{server_id}/trust")
def mailserver_trust(request: Request, server_id: int, body: TrustBody, user: dict = Depends(auth_mod.require_admin)):
    """Доверять самоподписанному сертификату сервера (по отпечатку SHA-256, который показала проверка)."""
    from ..mailservers import service as mss
    conn = _conn(request, server_id)
    mss.trust_certificate(_svc(request), conn.id, body.fingerprint, user["username"])
    return {"ok": True}


@router.post("/{server_id}/sync")
def mailserver_sync(request: Request, server_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Сверить учётные записи сейчас (задание в очереди)."""
    from ..models import JobType
    svc = _svc(request)
    conn = _conn(request, server_id)
    if not conn.enabled:
        raise ValidationError(f"Подключение «{conn.name}» выключено.")
    if conn.id in _running(svc):
        raise ValidationError("Сверка этого подключения уже идёт.")
    job_id = svc.queue.enqueue(JobType.SERVER_SYNC, None, {"server_id": conn.id, "requested_by": user["username"]},
                               priority=4, max_attempts=1, created_by=user["username"])
    return {"ok": True, "job_id": job_id}


@router.get("/{server_id}/accounts")
def mailserver_accounts(request: Request, server_id: int, deleted: bool = True,
                        user: dict = Depends(auth_mod.require_admin)):
    """Учётные записи по последней сверке."""
    from ..mailservers import store
    conn = _conn(request, server_id)
    rows = store.list_accounts(_svc(request).db, conn.id, include_deleted=bool(deleted))
    return {"items": rows[:50000], "total": len(rows)}


@router.get("/{server_id}/log")
def mailserver_log(request: Request, server_id: int, before: int = 0, limit: int = 100, kind: str = "",
                   user: dict = Depends(auth_mod.require_admin)):
    """Журнал обращений к API сервера: кто, когда, что делал (секреты в нём не показываются)."""
    from ..mailservers import store
    conn = _conn(request, server_id)
    if kind not in ("", "people", "check", "monitor", "sync", "read", "write"):
        raise ValidationError("Неизвестный отбор журнала.")
    return {"items": store.list_log(_svc(request).db, conn.id, max(1, min(500, limit)), before=max(0, before),
                                    kind=kind)}


@router.get("/{server_id}/log/{log_id}")
def mailserver_log_item(request: Request, server_id: int, log_id: int, user: dict = Depends(auth_mod.require_admin)):
    from ..mailservers import store
    conn = _conn(request, server_id)
    if log_id < 1 or log_id > 2 ** 63 - 1:
        raise HTTPException(404, "Запись не найдена")
    row = store.get_log(_svc(request).db, log_id)
    if row is None or int(row["server_id"]) != conn.id:
        raise HTTPException(404, "Запись не найдена")
    return row


class MailboxesBody(BaseModel):
    emails: List[str] = Field(default_factory=list)
    enable: bool = True
    preview: bool = True


@router.post("/{server_id}/mailboxes")
def mailserver_mailboxes(request: Request, server_id: int, body: MailboxesBody,
                         user: dict = Depends(auth_mod.require_admin)):
    """Завести в архиве ящики для учётных записей подключения (копирование без паролей сотрудников)."""
    from ..mailservers.mailboxes import add_mailboxes
    conn = _conn(request, server_id)
    if len(body.emails) > 20000:
        raise ValidationError("Слишком длинный список.")
    items = add_mailboxes(_svc(request), conn, body.emails, user=user["username"], enable=body.enable,
                          preview=body.preview)
    return {"items": items, "ok": sum(1 for i in items if i["status"] == "ok"),
            "skipped": sum(1 for i in items if i["status"] != "ok")}
