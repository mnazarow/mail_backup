"""API: мониторинг и сводка, логи и статистика, аудит, аналитика.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..errors import ValidationError
from ..models import JobStatus, JobType
from .. import roles
from . import auth as auth_mod

from .api_common import _own_ids, svc_dep
from .api_jobs import serialize_job
from .api_settings import _active_job
from .api_employees import _row_get

router = APIRouter(prefix="/api")

# =====================================================================
#  Мониторинг и сводка
# =====================================================================
@router.get("/monitoring/summary")
def monitoring_summary_preview(request: Request, user: dict = Depends(auth_mod.require_staff)):
    from ..monitoring import weekly_summary
    svc = svc_dep(request)
    subject, body = weekly_summary(svc, fresh=False, security=user.get("role") == roles.ROLE_ADMIN)
    return {"subject": subject, "body": body, "last_sent": svc.db.get_meta("summary_last_sent") or "",
            "next_run": svc.scheduler.next_run_of("weekly_summary")}


class LdapTestBody(BaseModel):
    username: str = Field("", max_length=320)
    password: str = Field("", max_length=1000)


@router.post("/ldap/test")
def ldap_test(request: Request, body: LdapTestBody, user: dict = Depends(auth_mod.require_admin)):
    """Проверить подключение к каталогу: служебная учётная запись, поиск пользователя, его группы и роль."""
    from .. import ldapauth
    svc = svc_dep(request)
    if not str(svc.rt("ldap", "url") or "").strip():
        raise ValidationError("Не указан сервер LDAP.")
    try:
        info = ldapauth.lookup(svc, body.username or "", body.password or None) if body.username.strip() else None
        if info is None:
            conn = ldapauth._connect(svc)  # noqa: SLF001
            try:
                dn = str(svc.rt("ldap", "bind_dn") or "")
                conn.bind(dn, str(svc.rt("ldap", "bind_password") or "") if dn else "")
            finally:
                conn.close()
            info = {"ok": True, "bind_only": True}
    except (ldapauth.LdapError, OSError) as exc:
        svc.db.add_audit(user["username"], "ldap_test", f"ошибка: {exc}"[:300])
        return {"ok": False, "error": str(exc)}
    svc.db.add_audit(user["username"], "ldap_test", f"{body.username or 'служебная учётная запись'}: "
                     + ("найден, роль " + (info.get("role") or "нет") if info.get("ok") and not info.get("bind_only")
                        else "подключение работает" if info.get("ok") else info.get("reason", "не найден")))
    return {"ok": bool(info.get("ok")), **{k: v for k, v in info.items() if k != "ok"}}


@router.get("/journal/status")
def journal_status(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Приём журналирования по SMTP: работает ли, сколько писем принято и сохранено."""
    from ..journal import status
    return status(svc_dep(request))


@router.get("/drill")
def drill_status(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверка восстановления: последний результат и идёт ли сейчас (1.21.0)."""
    svc = svc_dep(request)
    # В 1.21.0 здесь читался несуществующий столбец jobs.summary — раздел «Настройки» получал ошибку 500.
    row = svc.db.query_one("SELECT id, status, finished_at, result, error FROM jobs WHERE type=? "
                           "ORDER BY id DESC LIMIT 1", (JobType.RESTORE_DRILL,))
    active = next((j["id"] for j in svc.db.active_jobs() if j["type"] == JobType.RESTORE_DRILL), None)
    last = None
    if row is not None:
        try:
            res = json.loads(row["result"] or "{}")
        except (TypeError, ValueError):
            res = {}
        summary = (res.get("summary") if isinstance(res, dict) else "") or row["error"] or ""
        last = {"id": int(row["id"]), "status": row["status"], "finished_at": row["finished_at"],
                "summary": str(summary)[:1000]}
    return {"last": last, "active_job": active}


@router.post("/drill/run")
def drill_run(request: Request, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    active = next((j["id"] for j in svc.db.active_jobs() if j["type"] == JobType.RESTORE_DRILL), None)
    if active:
        return {"ok": True, "job_id": active, "already": True}
    job_id = svc.queue.enqueue(JobType.RESTORE_DRILL, None, {}, priority=6, created_by=user["username"])
    return {"ok": True, "job_id": job_id}


@router.get("/audit/forward")
def audit_forward_status(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Пересылка аудита в syslog / SIEM: очередь, отправлено, последняя ошибка (1.21.0)."""
    svc = svc_dep(request)
    fwd = getattr(svc, "audit_forwarder", None)
    return fwd.status() if fwd is not None else {"enabled": False, "queued": 0, "sent": 0, "dropped": 0,
                                                 "last_error": ""}


@router.post("/audit/forward/test")
def audit_forward_test(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Отправить на сервер журналов пробную запись — с сохранёнными настройками."""
    from ..auditlog import test_send
    svc = svc_dep(request)
    res = test_send(svc)
    svc.db.add_audit(user["username"], "audit_syslog_test", "успешно" if res["ok"] else res["error"][:300])
    return res


@router.post("/notifications/test")
def notifications_test(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверочное сообщение во все настроенные каналы (почта, Telegram, вебхук) — итог по каждому."""
    svc = svc_dep(request)
    results = svc.notifier.send_all("MailArchiver: проверка уведомлений",
                                    f"Проверочное сообщение. Отправил: {user['username']}.")
    svc.db.add_audit(user["username"], "notify_test",
                     ", ".join(f"{k}: {'ok' if not v else 'ошибка'}" for k, v in results.items()) or "каналов нет")
    return {"results": results}


@router.post("/monitoring/summary")
def monitoring_summary_send(request: Request, user: dict = Depends(auth_mod.require_admin)):
    from ..monitoring import send_weekly_summary
    svc = svc_dep(request)
    if not svc.notifier.enabled():
        raise ValidationError("Уведомления по e-mail выключены.",
                              hint="Включите их и заполните SMTP в разделе «Уведомления».")
    result = send_weekly_summary(svc)
    svc.db.add_audit(user["username"], "summary_send", "ok" if result["ok"] else result.get("error", "")[:300])
    if not result["ok"]:
        raise ValidationError(f"Сводка не отправлена: {result['error']}",
                              hint="Проверьте настройки SMTP в разделе «Уведомления».")
    return result


# =====================================================================
#  Логи, статистика, аудит
# =====================================================================
@router.get("/logs")
def get_logs(limit: int = 300, level: Optional[str] = None, user: dict = Depends(auth_mod.require_staff)):
    """Общий лог сервиса: содержит имена и хосты всех ящиков и ошибки чужих
    заданий, поэтому доступен только администратору и оператору."""
    from ..logging_setup import memory_handler
    limit = max(1, min(2000, int(limit)))
    # оператору — без записей заданий, подробности которых от него скрыты и в «Очереди и заданиях»
    only = roles.OPERATOR_JOB_TYPES if roles.is_operator(user) else None
    return memory_handler.tail(limit=limit, level=level, job_types=only)


@router.get("/stats")
def get_stats(request: Request, days: int = 30, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    days = max(1, min(365, int(days)))
    if user.get("role") == "mailbox":
        # пользователь-ящик видит статистику только по своим ящикам
        own = _own_ids(user)
        series = svc.db.daily_series(days=days, account_id=own)
        totals = {"messages": svc.db.count_messages(own), "bytes": svc.db.sum_message_bytes(own)}
    else:
        series = svc.db.daily_series(days=days)
        totals = {"messages": svc.db.count_messages(), "bytes": svc.db.sum_message_bytes()}
    return {
        "series": [{"day": r["day"], "messages": r["messages"], "bytes": r["bytes"],
                    "jobs": r["jobs"], "errors": r["errors"]} for r in series][::-1],
        "totals": totals,
    }


@router.get("/audit")
def get_audit(request: Request, limit: int = 200, before_id: Optional[int] = None, q: Optional[str] = None,
              user: dict = Depends(auth_mod.require_admin)):
    """Аудит, новые записи первыми; страница — не больше 1000 записей (``limit=-1`` раньше отдавал всю
    таблицу разом), следующая — с ``before_id`` = id последней показанной; ``q`` — поиск по тексту."""
    svc = svc_dep(request)
    limit = max(1, min(1000, int(limit)))
    rows = svc.db.list_audit(limit, before_id=before_id, query=(q or "")[:200])
    return [{"id": r["id"], "ts": r["ts"], "user": r["user"], "action": r["action"], "detail": r["detail"],
             "signed": bool(_row_get(r, "hash"))} for r in rows]


@router.get("/audit/verify")
def verify_audit(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверить цепочку подписей аудита (1.20.0): изменённые, удалённые и вставленные записи."""
    svc = svc_dep(request)
    res = svc.db.verify_audit()
    svc.db.add_audit(user["username"], "audit_verify",
                     "цепочка цела" if res["ok"] else f"нарушений: {len(res['broken'])}, первое — #{res['broken'][0]['id']}")
    return res


@router.get("/audit/export.csv")
def export_audit(request: Request, q: Optional[str] = None, user: dict = Depends(auth_mod.require_admin)):
    """Аудит в CSV (новые записи первыми) — с подписями цепочки, для передачи проверяющим."""
    import csv
    import io
    svc = svc_dep(request)
    query = (q or "")[:200]
    svc.db.add_audit(user["username"], "audit_export", f"выгрузка аудита в CSV{(' — отбор: ' + query) if query else ''}")

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "ts_utc", "user", "action", "detail", "prev_hash", "hash", "kid"])
        yield "\ufeff" + buf.getvalue()
        before = None
        while True:
            chunk = svc.db.list_audit(1000, before_id=before, query=query)
            if not chunk:
                break
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in chunk:
                w.writerow([r["id"], r["ts"], r["user"], r["action"], r["detail"], _row_get(r, "prev_hash") or "",
                            _row_get(r, "hash") or "", _row_get(r, "kid") or ""])
            yield buf.getvalue()
            before = int(chunk[-1]["id"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return StreamingResponse(rows(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="audit_{stamp}.csv"'})


# =====================================================================
#  Аналитика (только администратор)
# =====================================================================
@router.get("/analytics/system")
def analytics_system(request: Request, account_id: Optional[int] = None, days: int = 90,
                     user: dict = Depends(auth_mod.require_staff)):
    """Аналитика по всем параметрам системы (хранилище, задания, прогоны и т.д.)."""
    svc = svc_dep(request)
    from ..analytics import system_analytics
    if account_id is not None and svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    days = max(7, min(365, int(days)))
    return system_analytics(svc, account_id=account_id, days=days)


#: Сколько расчётов «Аналитики писем» считается одновременно. Расчёт обходит
#: весь индекс; без ограничения несколько одновременно открытых вкладок
#: занимали весь пул потоков и съедали память.
_ANALYTICS_SLOTS = threading.BoundedSemaphore(2)


@router.get("/analytics/mail")
def analytics_mail(request: Request, account_id: Optional[int] = None, refresh: bool = False,
                   user: dict = Depends(auth_mod.require_admin)):
    """Аналитика по содержанию писем (метаданные индекса: тема, отправитель, дата…)."""
    svc = svc_dep(request)
    from ..analytics import mail_analytics
    if account_id is not None and svc.db.get_account(account_id) is None:
        raise HTTPException(404, "Ящик не найден")
    # Ждать нельзя: эндпоинт синхронный и занимает поток из пула (по умолчанию
    # их 40). Ожидание семафора внутри потока подвешивало ВЕСЬ синхронный REST —
    # несколько вкладок «Аналитика писем» с обновлением клали интерфейс на
    # десятки секунд. Отказываем сразу, как это уже сделано для входа по ящику.
    if not _ANALYTICS_SLOTS.acquire(blocking=False):
        raise ValidationError("Сейчас уже считается аналитика писем. Повторите через минуту.",
                              hint="Расчёт обходит весь индекс писем и выполняется не более чем в два потока. "
                                   "Готовый результат кэшируется — обычно ждать не приходится.")
    try:
        return mail_analytics(svc, account_id=account_id, use_cache=not refresh)
    finally:
        _ANALYTICS_SLOTS.release()


@router.get("/analytics/mail/deep")
def analytics_mail_deep(request: Request, account_id: Optional[int] = None,
                        user: dict = Depends(auth_mod.require_admin)):
    """Кэш результата глубокого анализа писем (типы вложений, домены, язык, слова тела)."""
    svc = svc_dep(request)
    from ..analytics import load_deep
    data = load_deep(svc, account_id)
    running = [serialize_job(j) for j in svc.db.list_jobs(job_type=JobType.ANALYZE, limit=5)
               if j["status"] in JobStatus.ACTIVE
               and (account_id is None or j["account_id"] == account_id)]
    return {"available": data is not None, "data": data, "running": running}


@router.post("/analytics/mail/scan")
def analytics_mail_scan(request: Request, account_id: Optional[int] = None,
                        user: dict = Depends(auth_mod.require_admin)):
    """Запустить (пересчитать) глубокий анализ писем как фоновое задание."""
    svc = svc_dep(request)
    if account_id is not None:
        svc.require_account(account_id)
    # не плодим дубли: если такой анализ уже в очереди/работе — вернём его
    for j in svc.db.list_jobs(job_type=JobType.ANALYZE, limit=20):
        if j["status"] in JobStatus.ACTIVE and j["account_id"] == account_id:
            return {"ok": True, "job_id": j["id"], "already_running": True}
    jid = svc.queue.enqueue(JobType.ANALYZE, account_id, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "analytics_scan", f"account={account_id}")
    return {"ok": True, "job_id": jid}


@router.get("/analytics/dedup")
def analytics_dedup(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Отчёт «Одинаковые вложения» (последний сохранённый) и ход подсчёта."""
    svc = svc_dep(request)
    from .. import dedup
    job = _active_job(svc, JobType.DEDUP_REPORT)
    return {"report": dedup.load_report(svc), "status": dedup.status(svc),
            "running": [serialize_job(job)] if job else []}


class DedupScanBody(BaseModel):
    full: bool = False          # забыть прочитанное и прочитать весь архив заново


@router.post("/analytics/dedup/scan")
def analytics_dedup_scan(request: Request, body: DedupScanBody, user: dict = Depends(auth_mod.require_admin)):
    """Посчитать (или досчитать) отчёт об одинаковых вложениях фоновым заданием."""
    svc = svc_dep(request)
    job = _active_job(svc, JobType.DEDUP_REPORT)
    if job:
        return {"ok": True, "job_id": job["id"], "already_running": True}
    jid = svc.queue.enqueue(JobType.DEDUP_REPORT, None, {"full": bool(body.full)}, priority=8,
                            created_by=user["username"])
    svc.db.add_audit(user["username"], "dedup_report", "подсчёт заново" if body.full else "подсчёт")
    return {"ok": True, "job_id": jid}
