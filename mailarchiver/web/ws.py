"""
WebSocket для живого обновления дашборда: активные задания с прогрессом,
счётчики очереди и «хвост» лога. Клиент подписывается на /ws и каждые ~1.5 с
получает актуальный снимок. Есть и обычный REST-опрос (/api/state) как запасной
вариант, если WebSocket недоступен.

Снимок считается ОДИН раз на всех подписчиков (см. :class:`_LiveHub`), и только
потом фильтруется под конкретного пользователя. Раньше каждое соединение
считало снимок само: 50 открытых вкладок давали 50×N запросов к SQLite каждые
1.5 с — в ту же базу, куда в это же время пишет бэкап.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..logging_setup import get_logger, memory_handler
from .api import serialize_job
from . import auth as auth_mod

log = get_logger("ws")
router = APIRouter()

# Как часто перепроверять сессию: соединение живёт долго, и без перепроверки
# поток данных продолжался бы и после выхода пользователя или конца сессии.
_RECHECK_EVERY = 20   # ~30 секунд при интервале 1.5 с

#: Интервал пересчёта общего снимка, секунды.
_INTERVAL_S = 1.5


def _ws_user(websocket: WebSocket):
    """Полноценная проверка сессии (та же, что и у REST) → пользователь или None.

    Раньше здесь проверялось только наличие строки сессии в БД: истёкшая
    сессия, отключённый пользователь или отключённый ящик всё равно получали
    данные.
    """
    services = websocket.app.state.services
    return auth_mod.user_from_cookies(services, websocket.cookies)


class _LiveHub:
    """Единый источник снимков состояния для всех подключённых клиентов.

    Пока есть хотя бы один подписчик, фоновая задача раз в :data:`_INTERVAL_S`
    считает общий снимок (один поход в БД на всех) и будит ожидающие
    соединения. Каждое соединение рассылает подписчику уже готовый снимок,
    применив к нему только персональную фильтрацию по роли/ящику.
    """

    def __init__(self) -> None:
        self._subscribers = 0
        self._task: Optional[asyncio.Task] = None
        self._services = None
        self._version = 0
        self._snapshot: Dict = {}
        self._waiters: List[asyncio.Future] = []

    # -- подписка ----------------------------------------------------------
    def subscribe(self, services) -> None:
        self._subscribers += 1
        # services меняется, если приложение пересоздали в том же процессе
        if self._services is not services:
            self._cancel_task()
            self._services = services
            self._version = 0
            self._snapshot = {}
        self._ensure_task()

    def unsubscribe(self) -> None:
        self._subscribers = max(0, self._subscribers - 1)
        if self._subscribers == 0:
            # некому слушать — перестаём опрашивать БД
            self._cancel_task()
            self._services = None

    async def next_snapshot(self, seen_version: int):
        """Дождаться снимка, которого подписчик ещё не видел → (версия, снимок)."""
        self._ensure_task()
        if self._snapshot and self._version != seen_version:
            return self._version, self._snapshot
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        return self._version, self._snapshot

    # -- внутреннее --------------------------------------------------------
    def _ensure_task(self) -> None:
        if self._services is None or self._subscribers <= 0:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._produce(self._services))

    def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _publish(self) -> None:
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            if not w.done():
                w.set_result(None)

    async def _produce(self, services) -> None:
        try:
            while True:
                try:
                    self._snapshot = await asyncio.to_thread(_collect_snapshot, services)
                    self._version += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.debug("Не удалось собрать снимок состояния: %s", exc)
                self._publish()
                await asyncio.sleep(_INTERVAL_S)
        finally:
            # разбудить ожидающих, иначе после отмены задачи они повисли бы
            self._publish()


_hub = _LiveHub()


@router.websocket("/ws")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    user = await asyncio.to_thread(_ws_user, websocket)
    if user is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "unauthorized"}))
        await websocket.close(code=4401)
        return
    services = websocket.app.state.services
    _hub.subscribe(services)
    tick = 0
    version = 0
    try:
        while True:
            tick += 1
            if tick % _RECHECK_EVERY == 0:
                user = await asyncio.to_thread(_ws_user, websocket)
                if user is None:
                    await websocket.send_text(json.dumps({"type": "error", "message": "unauthorized"}))
                    await websocket.close(code=4401)
                    return
            version, snapshot = await _hub.next_snapshot(version)
            await websocket.send_text(json.dumps(_personal_snapshot(snapshot, user), ensure_ascii=False))
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        log.debug("WebSocket закрыт: %s", exc)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _hub.unsubscribe()


def _collect_snapshot(services) -> dict:
    """Общий снимок состояния — считается один раз на всех подписчиков."""
    return {
        "type": "live",
        "active_jobs": [serialize_job(j) for j in services.db.active_jobs()],
        "job_counts": services.db.count_jobs_by_status(),
        "scheduler_running": services.scheduler.running(),
        "logs": memory_handler.tail(limit=40),
    }


def _personal_snapshot(snapshot: dict, user: dict) -> dict:
    """Персональный вид готового снимка: фильтрация по роли и ящику."""
    out = dict(snapshot)
    if user.get("role") == "mailbox":
        # как и в /api/state: пользователь-ящик видит только свои задания
        aid = user.get("account_id")
        out["active_jobs"] = [j for j in snapshot.get("active_jobs", []) if j["account_id"] == aid]
    if user.get("role") != "admin":
        # Общий лог сервиса (имена и хосты чужих ящиков, ошибки чужих заданий)
        # отдаём только администратору — как и /api/logs.
        out.pop("logs", None)
    return out
