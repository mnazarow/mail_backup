"""Задание «Сверка с почтовым сервером» (``server_sync``) — одно подключение за раз."""
from __future__ import annotations

from typing import Dict

from ..logging_setup import get_logger
from ..models import JobStatus
from . import service

log = get_logger("mailservers.jobs")


def handle_sync(ctx) -> Dict:
    svc = ctx.services
    server_id = int(ctx.params.get("server_id") or 0)
    who = ctx.params.get("requested_by") or "system"
    conn = service.get(svc, server_id)
    ctx.event("INFO", f"Сверка учётных записей: «{conn.name}»…")
    res = service.sync(svc, server_id, user=who, event=ctx.event)
    changes = res["changes"]
    if changes["gone"]:
        ctx.event("WARNING", "Удалены на сервере (архив их писем сохранён): " + ", ".join(changes["gone"][:30]))
    if changes["new"]:
        ctx.event("INFO", "Новые учётные записи: " + ", ".join(changes["new"][:30]))
    for problem in res["problems"][:20]:
        ctx.event("WARNING", str(problem))
    try:
        from .monitor import notify_new, problems
        notify_new(svc, problems(svc))
    except Exception:  # noqa: BLE001
        log.debug("Не удалось проверить проблемы после сверки", exc_info=True)
    status = JobStatus.PARTIAL if (res["incomplete"] or res["problems"]) else JobStatus.SUCCESS
    return {"final_status": status, "summary": res["summary"], "server_id": server_id, "accounts": res["accounts"],
            "new": len(changes["new"]), "gone": len(changes["gone"]), "added": res["added"],
            "problems": [str(p) for p in res["problems"]][:50]}
