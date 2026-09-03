"""
WebSocket для живого обновления дашборда: активные задания с прогрессом,
счётчики очереди и «хвост» лога. Клиент подписывается на /ws и каждые ~1.5 с
получает актуальный снимок. Есть и обычный REST-опрос (/api/state) как запасной
вариант, если WebSocket недоступен.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..logging_setup import get_logger, memory_handler
from .api import serialize_job
from .auth import COOKIE_NAME
from ..security import unsign_value

log = get_logger("ws")
router = APIRouter()


def _ws_authorized(websocket: WebSocket) -> bool:
    services = websocket.app.state.services
    if not bool(services.rt("security", "auth_enabled")):
        return True
    raw = websocket.cookies.get(COOKIE_NAME)
    if not raw:
        return False
    token = unsign_value(raw, services.cfg.secret_key())
    if not token:
        return False
    row = services.db.get_session(token)
    return row is not None


@router.websocket("/ws")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    if not _ws_authorized(websocket):
        await websocket.send_text(json.dumps({"type": "error", "message": "unauthorized"}))
        await websocket.close(code=4401)
        return
    services = websocket.app.state.services
    try:
        while True:
            snapshot = await asyncio.to_thread(_build_snapshot, services)
            await websocket.send_text(json.dumps(snapshot, ensure_ascii=False))
            await asyncio.sleep(1.5)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        log.debug("WebSocket закрыт: %s", exc)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


def _build_snapshot(services) -> dict:
    active = [serialize_job(j) for j in services.db.active_jobs()]
    counts = services.db.count_jobs_by_status()
    logs = memory_handler.tail(limit=40)
    return {
        "type": "live",
        "active_jobs": active,
        "job_counts": counts,
        "logs": logs,
        "scheduler_running": services.scheduler.running(),
    }
