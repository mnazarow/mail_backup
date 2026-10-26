"""
Задание «Действия с ящиками на сервере» (``server_action``).

Обходит ящики по одному. Перед каждым ящиком данные о нём берутся заново, а
условия действия проверяются ещё раз (``check``): между проверкой в окне и
заданием ящик могли выключить, учётную запись — удалить, вход — открыть. Итог
по каждому ящику — строка в результате задания (окно задания показывает их
таблицей) и событие в журнале; что изменено на сервере — ещё и в аудите.

После перезапуска службы задание само не продолжается (см.
``JobType.NOT_RESUMABLE``): за время простоя ящики могли поменять вручную.
"""
from __future__ import annotations

from typing import Dict, List

from ..bulk import OK
from ..errors import DiskSpaceError, JobCancelled, MailArchiverError, ValidationError
from ..logging_setup import get_logger
from ..models import JobStatus
from . import base, progress
from . import targets as tg

log = get_logger("serverops.jobs")

_ITEM_STATUS = {"ok": "success", "skip": "skipped", "fail": "failed"}


def handle_job(ctx) -> Dict:
    svc = ctx.services
    params = ctx.params
    from . import create
    if params.get("action") == create.KEY:
        return create.handle_job(ctx)
    action = base.get(params.get("action"))
    who = params.get("requested_by") or "admin"
    options = dict(params.get("options") or {})
    keys = tg.clean_keys(params.get("keys") or [])
    op = base.Ctx(svc, who, options, preview=False, job=ctx)
    op.data["op_id"] = params.get("op_id")
    known = tg.resolve(svc, keys)
    if params.get("schedule_id") and not params.get("op_id"):
        # запуск по расписанию — своя запись в истории раздела, как у ручного запуска
        sid = params["schedule_id"]
        from .schedules import SCHEDULABLE
        consent = (params.get("schedule") or {}).get("consent_by")
        if action.key not in SCHEDULABLE or not consent:
            # расписание из старой или испорченной записи: без согласия и не для своих действий не выполняется
            raise ValidationError(f"Расписание №{sid}: действие «{action.label}» по расписанию не выполняется "
                                  f"или нет отметки согласия администратора.",
                                  hint="Удалите расписание и создайте заново в «Ящики на сервере».")
        op_id = svc.db.create_bulk_op(f"расписание №{sid}", base.HISTORY_PREFIX + action.key, action.label,
                                      base.history_params(options), len(keys))
        op.data["op_id"] = op_id
        progress.started(svc.db, op_id)             # до prepare: его отказ — «ни один ящик не тронут»

        def _who(k: str):
            t = known.get(k)
            return ((t.name or t.email) if t else k.split(":", 1)[-1]), ((t.email if t else "") or k.split(":", 1)[-1])
        launch = [{"id": k, "key": k, "name": _who(k)[0], "username": _who(k)[1], "status": OK,
                   "detail": f"в задании по расписанию №{sid}", "job_id": ctx.job_id} for k in keys]
        svc.db.finish_bulk_op(op_id, ok=len(keys), skipped=0, failed=0, jobs=1, results=launch,
                              summary=f"{action.label}: по расписанию №{sid}, ящиков {len(keys)}. "
                                      f"Задание №{ctx.job_id}.")
        ctx.event("INFO", f"Запуск по расписанию №{sid} (согласие на выполнение без проверки перед запуском: "
                          f"{(params.get('schedule') or {}).get('consent_by') or '—'}).")
    try:
        action.prepare(op)
    except ValidationError as exc:
        # настройки поменяли после запуска (например, запретили изменения на сервере)
        ctx.event("ERROR", f"{action.label}: {exc.message}")
        raise
    ctx.event("INFO", f"{action.label}: ящиков {len(keys)}.")
    op_id = op.data.get("op_id")
    progress.started(svc.db, op_id)
    results: List[Dict] = []
    stats = {"ok": 0, "skip": 0, "fail": 0}
    action.job_started(op)

    def record(t: tg.Target, status: str, text: str) -> None:
        stats[status] += 1
        item = {"name": t.email or t.name, "key": t.key, "status": _ITEM_STATUS[status], "detail": text[:1000]}
        if t.mailbox is not None:
            item["account_id"] = t.mailbox.id
        extra = op.data.get("item_extra", {}).pop(t.key, None)
        if extra:
            item.update(extra)
        results.append(item)
        progress.mark(svc.db, op_id, t.key, item["status"], (extra or {}).get("next_action", ""))
        level = {"ok": "INFO", "skip": "WARNING", "fail": "ERROR"}[status]
        ctx.event(level, f"{t.email or t.name}: {text}"[:600])

    cancelled_at = None
    for index, key in enumerate(keys):
        t = None
        try:
            if ctx.is_cancelled():
                raise JobCancelled("Задание отменено.")
            ctx.progress(index, len(keys), f"{index + 1}/{len(keys)}")
            # данные ящика — заново перед каждым: предыдущие шаги задания шли минуты
            t = tg.refresh(svc, known[key]) if key in known else None
            if t is None:
                stats["skip"] += 1
                results.append({"name": key.split(":", 1)[-1], "key": key, "status": "skipped",
                                "detail": "ящика больше нет (удалён или пропал из сверки)"})
                progress.mark(svc.db, op_id, key, progress.SKIPPED)
                continue
            try:
                reason = action.target_check(op, t)
            except MailArchiverError as exc:
                reason = exc.message
            if reason:
                record(t, "skip", reason)
                continue
            # до изменения: если служба упадёт посреди ящика, «Повторить» предупредит о нём
            progress.mark(svc.db, op_id, t.key, progress.RUNNING)
            try:
                status, text = action.target_execute(op, t)
            except (JobCancelled, DiskSpaceError):
                raise
            except MailArchiverError as exc:
                status, text = "fail", exc.message + (f" ({exc.hint})" if exc.hint else "")
            except Exception as exc:  # noqa: BLE001
                log.exception("Действие «%s» с ящиком %s упало", action.key, key)
                status, text = "fail", f"{type(exc).__name__}: {exc}"
        except JobCancelled:
            # Остановка службы — не отмена: очередь сама отметит задание (оно не продолжается после
            # перезапуска). Отмена администратором — итог по уже обработанным ящикам сохраняется.
            if not svc.db.is_cancel_requested(ctx.job_id):
                raise
            cancelled_at = index
            if t is not None:
                record(t, "fail", "задание отменено во время обработки этого ящика — часть изменений могла "
                                  "выполниться (подробности — в журнале задания)")
                # для «Повторить»: этот ящик — «прерван посреди работы», а не просто «ошибка»
                progress.mark(svc.db, op_id, t.key, progress.RUNNING)
                cancelled_at += 1
            break
        record(t, status, text)
        if status == "ok" and not getattr(action, "read_only", False) and t.key not in op.data.get("audited", ()):
            svc.db.add_audit(who, base.HISTORY_PREFIX + action.key, f"{t.email}: {text}"[:300])
    if cancelled_at is not None:
        rest = len(keys) - cancelled_at
        summary = (f"{action.label}: отменено — сделано {stats['ok']}, пропущено {stats['skip']}, ошибок "
                   f"{stats['fail']}; не обработано {rest} (из {len(keys)}).")
        ctx.event("WARNING", summary)
        result = {"final_status": JobStatus.CANCELLED, "summary": summary, "ok": stats["ok"],
                  "skipped": stats["skip"], "failed": stats["fail"], "items": results[:5000]}
        action.job_finished(op, result)
        return result
    ctx.progress(len(keys), len(keys), "Готово")
    summary = (f"{action.label}: сделано {stats['ok']}, пропущено {stats['skip']}, ошибок {stats['fail']} "
               f"(из {len(keys)}).")
    final = JobStatus.SUCCESS
    if stats["fail"]:
        final = JobStatus.PARTIAL if stats["ok"] else JobStatus.FAILED
    result = {"final_status": final, "summary": summary, "ok": stats["ok"], "skipped": stats["skip"],
              "failed": stats["fail"], "items": results[:5000]}
    action.job_finished(op, result)
    return result
