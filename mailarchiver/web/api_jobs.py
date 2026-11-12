"""API: задания, готовые выгрузки, расписания.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..errors import ValidationError
from ..accountops import validate_cron, validate_restore_options as _validate_restore_options
from ..models import AuthType, JobStatus, JobType, ScheduleKind
from .. import roles
from ..util import human_size
from . import auth as auth_mod
from ..export import list_engines

from .api_common import (
    MIN_SCHEDULE_INTERVAL_S, ScheduleBody, _ensure_full_access, _ensure_job_access, _foreign_target, _full_ids,
    _is_own_job, _mailbox_flood_guard, _operator_job_guard, _own_ids, _own_logins, _require_not_mailbox, svc_dep,
)

router = APIRouter(prefix="/api")

# =====================================================================
#  Задания
# =====================================================================
def visible_author(author: str, viewer: Optional[dict]) -> str:
    """Кто запустил задание — как его видит пользователь.

    Сотруднику (вход по ящику) не показываем логины администраторов: только
    «вы», «расписание» или безличное «администратор».
    """
    author = author or ""
    if not viewer or viewer.get("role") != "mailbox":
        return author
    if author in ("", "scheduler", "system") or author in (viewer.get("logins") or [viewer.get("username") or ""]):
        return author
    return "администратор"


def _job_scope_label(row) -> str:
    """Что обрабатывает задание без своего ящика — для колонки «Ящик»."""
    try:
        params = json.loads(row["params"] or "{}") or {}
    except (TypeError, ValueError):
        params = {}
    if row["type"] == JobType.BACKUP_ALL:
        ids = params.get("account_ids")
        return f"выбранные ящики ({len(ids)}), по очереди" if ids else "все включённые ящики, по очереди"
    if row["type"] == JobType.CLEANUP:
        return f"ящиков: {len(params.get('items') or [])}"
    if row["type"] in (JobType.CHECK_LOGINS, JobType.FOLDERS_CHECK, JobType.SEARCH_REINDEX) \
            and params.get("account_ids"):
        return f"ящиков: {len(params['account_ids'])}"
    return ""


#: Сколько ждущих заданий показывать в живом списке (идущие — все). Когда расписания ящиков ставят в очередь
#: сотни заданий разом, полный список уходил каждой открытой вкладке каждые полторы секунды (сотни
#: килобайт), а дашборд перерисовывал его целиком; весь список — в разделе «Задания».
LIVE_QUEUED_LIMIT = 50


def cap_active_jobs(jobs: list, limit: int = LIVE_QUEUED_LIMIT) -> Tuple[list, int]:
    """Идущие задания — все, ждущие — первые ``limit`` (в порядке очереди); → (список, сколько ждущих скрыто).
    Годится и для строк базы, и для уже готовых словарей."""
    out, shown, hidden = [], 0, 0
    for j in jobs:
        if (j.get("status") if isinstance(j, dict) else j["status"]) == JobStatus.QUEUED:
            if shown >= limit:
                hidden += 1
                continue
            shown += 1
        out.append(j)
    return out, hidden


def serialize_job(row, names: Optional[Dict[int, str]] = None, viewer: Optional[dict] = None) -> dict:
    data = {
        "id": row["id"], "type": row["type"], "type_label": JobType.LABELS.get(row["type"], row["type"]),
        "account_id": row["account_id"],
        # имя ящика — с сервера: после обновления страницы список ящиков в
        # браузере ещё пуст, и колонка «Ящик» показывала «—»
        "account_name": ((names or {}).get(row["account_id"], "") if row["account_id"]
                         else _job_scope_label(row)),
        "status": row["status"],
        "status_label": JobStatus.LABELS.get(row["status"], row["status"]),
        "priority": row["priority"], "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "progress_current": row["progress_current"],
        "progress_total": row["progress_total"], "progress_message": row["progress_message"],
        "bytes_done": row["bytes_done"], "bytes_done_h": human_size(row["bytes_done"] or 0),
        "speed": row["speed"], "speed_h": human_size(row["speed"] or 0) + "/с",
        "error": row["error"], "attempts": row["attempts"], "max_attempts": row["max_attempts"],
        "created_by": visible_author(row["created_by"], viewer),
        "percent": (round(100 * row["progress_current"] / row["progress_total"])
                    if row["progress_total"] else (100 if row["status"] in JobStatus.TERMINAL else 0)),
        # может ли оператор отменить и повторить это задание (кнопки в интерфейсе)
        "operator_can_manage": roles.operator_may_manage(row),
    }
    if row["type"] == JobType.RESTORE:
        # «Восстановление писем (3)» и — если в другой ящик — куда (1.15.0)
        params = roles.job_params(row)
        picked = params.get("message_ids")
        if picked:
            data["type_label"] = f"Восстановление писем ({len(picked)})"
            target = int(params.get("target_account_id") or 0)
            if target:
                data["target_account_id"] = target
            if target and target != row["account_id"]:
                hidden = bool(viewer) and viewer.get("role") == "mailbox" and target not in _own_ids(viewer)
                where = "другой ящик" if hidden else (names or {}).get(target, f"ящик #{target}")
                data["account_name"] = f"{data['account_name']} → {where}"
    return redact_job_for(viewer, data)


def redact_job_for(viewer: Optional[dict], data: dict) -> dict:
    """Задание глазами оператора: у выгрузок, восстановления, анализа писем и
    сравнения прежних копий текст ошибки и хода бывает с именами файлов, папок и
    писем — оператор видит только, что задание идёт или завершилось ошибкой."""
    if not roles.is_operator(viewer) or data.get("type") in roles.OPERATOR_JOB_TYPES:
        return data
    return dict(data, progress_message="",
                error=("ошибка — подробности у администратора" if data.get("error") else ""))


@router.get("/jobs")
def list_jobs(request: Request, status: Optional[str] = None, type: Optional[str] = None,
              account_id: Optional[int] = None, limit: int = 100,
              user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    scope: Any = account_id
    if user.get("role") == "mailbox":
        # свой ящик — он; иначе — все свои ящики (чужой ящик в запросе — не повод показать его)
        own = _own_ids(user)
        scope = account_id if account_id in own else own
    limit = max(1, min(1000, int(limit)))
    rows = svc.db.list_jobs(status=status, job_type=type, account_id=scope, limit=limit)
    if user.get("role") == "mailbox":
        rows = [r for r in rows if not _foreign_target(user, r)]
    names = svc.db.account_names()
    return [serialize_job(r, names, user) for r in rows]


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    data = serialize_job(row, svc.db.account_names(), user)
    try:
        data["result"] = json.loads(row["result"] or "{}")
    except json.JSONDecodeError:
        data["result"] = {}
    data["params"] = json.loads(row["params"] or "{}")
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        # Задание по его ящику запустил администратор или расписание: параметры и подробный итог
        # (пути к файлам на сервере, состав выгрузки) сотруднику не нужны — только итог.
        result = data["result"] if isinstance(data["result"], dict) else {}
        data["result"] = {k: result[k] for k in ("summary", "final_status") if k in result}
        data["params"] = {}
    if roles.is_operator(user) and not roles.operator_sees_job(row):
        # Выгрузки, восстановление, анализ писем, сравнение прежних копий: в
        # их итогах бывают темы и отправители писем — оператору только итог.
        result = data["result"] if isinstance(data["result"], dict) else {}
        data["result"] = {k: result[k] for k in ("summary", "final_status") if k in result}
        data["params"] = {}
        data["restricted"] = True
    elif roles.is_operator(user) and isinstance(data["result"], dict):
        data["result"].pop("rebuild_info", None)     # пути прежних копий на диске сервера
    return data


@router.get("/jobs/{job_id}/events")
def job_events(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    if roles.is_operator(user) and not roles.operator_sees_job(row):
        return []
    rows = svc.db.list_job_events(job_id, limit=500)
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        # Задание по его ящику запустил администратор или расписание: как и в подробностях задания
        # (get_job), сотруднику — только предупреждения и ошибки (почему копия не прошла), без хода работы.
        rows = [r for r in rows if (r["level"] or "").upper() in ("WARNING", "ERROR", "CRITICAL")]
    return [{"ts": r["ts"], "level": r["level"], "message": r["message"]} for r in rows][::-1]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        # Иначе сотрудник мог бы снимать каждое копирование своего ящика по
        # расписанию сразу после старта — и архив перестал бы пополняться.
        raise HTTPException(403, "Отменить можно только задание, запущенное вами")
    if roles.is_operator(user) and not roles.operator_may_manage(row):
        raise HTTPException(403, _OPERATOR_JOB_REFUSAL)
    svc.queue.cancel(job_id)
    if user.get("role") != "mailbox":
        svc.db.add_audit(user["username"], "job_cancel", f"#{job_id} {row['type']}")
    return {"ok": True}


#: Отказ оператору в отмене и повторе задания, которое ему не подвластно.
_OPERATOR_JOB_REFUSAL = ("Оператор отменяет и повторяет только копирование и проверки "
                         "(не «Скопировать с нуля» и не последнюю копию перед выключением ящика)")


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    row = svc.db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "Задание не найдено")
    _ensure_job_access(user, row)
    if user.get("role") == "mailbox" and not _is_own_job(user, row):
        raise HTTPException(403, "Повторить можно только задание, запущенное вами")
    if roles.is_operator(user) and not roles.operator_may_manage(row):
        raise HTTPException(403, _OPERATOR_JOB_REFUSAL)
    if row["status"] not in JobStatus.TERMINAL:
        raise ValidationError("Повторить можно только завершённое задание.")
    if row["type"] in JobType.NOT_RESUMABLE:
        raise ValidationError(JobType.NO_RETRY_TEXT,
                              hint=("«Ящики на сервере» → «История» → «Подробнее» → «Повторить для неудачных» отметит "
                                    "необработанные ящики — с проверкой перед запуском."
                                    if row["type"] == JobType.SERVER_ACTION else
                                    "История операций раздела → «Подробнее» покажет, что уже сделано."))
    _operator_job_guard(svc, user, row["type"], row["account_id"], need_enabled=row["type"] == JobType.BACKUP,
                        rebuild=str(roles.job_params(row).get("rebuild") or ""))
    if user.get("role") == "mailbox":
        # те же ограничения, что и при запуске: иначе «выгрузить → отменить →
        # повторить» давало десятки одновременных выгрузок одного ящика
        _mailbox_flood_guard(svc, user, row["type"], row["account_id"])
    # Ручной повтор: снова все попытки и снятый флаг отмены.
    svc.queue.retry(job_id)
    svc.db.add_audit(user["username"], "job_retry", f"#{job_id} {row['type']}")
    return {"ok": True}


# =====================================================================
#  Экспорты
# =====================================================================
_EXPORT_STATUS = {"pending": "готовится", "running": "готовится", "success": "готово",
                  "partial": "готово частично", "failed": "ошибка", "cancelled": "отменён"}


@router.get("/exports")
def list_exports(request: Request, account_id: Optional[int] = None, limit: int = 200,
                 before_id: Optional[int] = None, user: dict = Depends(auth_mod.require_user)):
    """Выгрузки, новые первыми: страница до 1000, следующая — с ``before_id`` (id последней показанной)."""
    svc = svc_dep(request)
    scope: Any = account_id
    if user.get("role") == "mailbox":
        own = _full_ids(user)                 # выгрузки ящиков «только чтение» не показываются
        scope = account_id if account_id in own else own
    rows = svc.db.list_exports(scope, limit=max(1, min(1000, int(limit))), before_id=before_id)
    mailbox = user.get("role") == "mailbox"
    own_logins = {x for x in _own_logins(user) if x} if mailbox else set()
    authors = {}
    job_ids = [r["job_id"] for r in rows if r["job_id"]]
    if job_ids:
        marks = ",".join("?" * len(job_ids))
        for j in svc.db.query(f"SELECT id, created_by FROM jobs WHERE id IN ({marks})", tuple(job_ids)):
            authors[j["id"]] = j["created_by"] or ""
    out = []
    names = svc.db.account_names()
    for r in rows:
        author = authors.get(r["job_id"], "") or (r["created_by"] if "created_by" in r.keys() else "") or ""
        item = {"id": r["id"], "account_id": r["account_id"], "account_name": names.get(r["account_id"], ""),
                "engine": r["engine"], "format": r["format"],
                "filename": os.path.basename(r["path"] or ""), "size": r["size"],
                "size_h": human_size(r["size"] or 0), "status": r["status"],
                "status_label": _EXPORT_STATUS.get(r["status"], r["status"]), "error": r["error"],
                "created_at": r["created_at"], "created_by": visible_author(author, user),
                "exists": bool(r["path"] and os.path.exists(r["path"])),
                "can_delete": (not mailbox) or author in own_logins}
        if not mailbox:
            # полный путь на сервере — только администратору
            item["path"] = r["path"]
        out.append(item)
    return out


@router.get("/exports/{export_id}/download")
def download_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    # Проверка доступа — сразу после получения записи: если сначала проверять
    # наличие файла, перебором id можно отличить чужой существующий экспорт.
    if r is None:
        raise HTTPException(404, "Файл экспорта не найден")
    _ensure_full_access(user, r["account_id"])
    if not r["path"] or not os.path.exists(r["path"]):
        raise HTTPException(404, "Файл экспорта не найден")
    return FileResponse(r["path"], filename=os.path.basename(r["path"]), media_type="application/octet-stream")


@router.delete("/exports/{export_id}")
def delete_export(request: Request, export_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    r = svc.db.get_export(export_id)
    if r is None:
        raise HTTPException(404, "Экспорт не найден")
    _ensure_full_access(user, r["account_id"])
    if user.get("role") == "mailbox":
        job = svc.db.get_job(r["job_id"]) if r["job_id"] else None
        if not _is_own_job(user, job):
            # выгрузку мог сделать администратор (например, для юристов) —
            # сотрудник не должен её удалять
            raise HTTPException(403, "Удалить можно только выгрузку, сделанную вами")
    if r["path"] and os.path.exists(r["path"]):
        try:
            os.unlink(r["path"])
        except OSError:
            pass
    svc.db.delete_export(export_id)
    svc.db.add_audit(user["username"], "export_delete", str(export_id))
    return {"ok": True}


@router.get("/export/engines")
def export_engines(request: Request, user: dict = Depends(auth_mod.require_user)):
    return list_engines()


@router.get("/export/defaults")
def export_defaults(request: Request, user: dict = Depends(auth_mod.require_user)):
    """Значения по умолчанию для диалога экспорта (из раздела «Настройки»).

    Раньше «Движок экспорта по умолчанию», «Формат PST» и «Целевая версия
    Outlook» в настройках сохранялись, но диалог их не учитывал.
    """
    svc = svc_dep(request)
    return {"engine": str(svc.rt("export", "default_engine") or "auto"),
            "pst_format": str(svc.rt("export", "pst_format") or "unicode"),
            "outlook_target": str(svc.rt("export", "outlook_target") or "2016+"),
            "pst_split_size_mb": int(svc.rt("export", "pst_split_size_mb") or 0)}


# =====================================================================
#  Расписания
# =====================================================================
@router.get("/schedules")
def list_schedules(request: Request, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    mailbox_aids = _own_ids(user) if user.get("role") == "mailbox" else None
    out = []
    for r in svc.db.list_schedules(account_id=mailbox_aids):
        if r["job_type"] == JobType.SERVER_ACTION:
            continue                    # расписания раздела «Ящики на сервере» — только там
        out.append({"id": r["id"], "account_id": r["account_id"], "kind": r["kind"], "job_type": r["job_type"],
                    "cron_expr": r["cron_expr"], "interval_seconds": r["interval_seconds"],
                    "enabled": bool(r["enabled"]), "last_run": r["last_run"],
                    "next_run": svc.scheduler.next_run_for(r["id"]) or r["next_run"],
                    "options": json.loads(r["options"] or "{}")})
    return out


def _validate_cron(expr: str) -> None:
    """Проверить cron-выражение (общая проверка — в :mod:`mailarchiver.accountops`)."""
    validate_cron(expr)


#: Проверка параметров восстановления — общая с групповыми действиями и
#: расписаниями (см. :func:`mailarchiver.accountops.validate_restore_options`).
validate_restore_options = _validate_restore_options


#: Что можно запускать по расписанию. Остальное (выгрузка, перешифровка,
#: импорт .pst, пересоздание копии) — только вручную: раньше API принимал любой
#: тип и любые параметры, например backup {"rebuild": "full"} в обход проверок
#: ручного запуска или выгрузку каждую минуту.
SCHEDULE_JOB_TYPES = (JobType.BACKUP, JobType.RETENTION, JobType.VERIFY, JobType.RESTORE,
                      JobType.BACKUP_ALL)
#: Расписания без ящика: задание само проходит по всем включённым ящикам.
GLOBAL_SCHEDULE_JOB_TYPES = (JobType.BACKUP_ALL,)


def backup_all_options(raw: Optional[dict]) -> Dict[str, Any]:
    """Параметры копирования всех ящиков по очереди: пауза, «не начинать после», порядок."""
    raw = dict(raw or {})
    out: Dict[str, Any] = {}
    try:
        pause = int(raw.get("pause_seconds") or 0)
    except (TypeError, ValueError):
        raise ValidationError("Пауза между ящиками — целое число секунд.")
    if not 0 <= pause <= 3600:
        raise ValidationError("Пауза между ящиками — от 0 до 3600 секунд.")
    out["pause_seconds"] = pause
    stop_at = str(raw.get("stop_at") or "").strip()
    if stop_at:
        import re as _re
        m = _re.fullmatch(r"(\d{1,2}):(\d{2})", stop_at)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValidationError(f"Время «не начинать новые ящики после» указано неверно: «{stop_at}».",
                                  hint="Формат ЧЧ:ММ, например 07:00. Пусто — без ограничения.")
        stop_at = f"{int(m.group(1)):02d}:{m.group(2)}"
    out["stop_at"] = stop_at
    order = str(raw.get("order") or "oldest").strip()
    if order not in ("oldest", "name"):
        raise ValidationError("Порядок ящиков — oldest (давно не копировавшиеся первыми) или name (по алфавиту).")
    out["order"] = order
    return out
#: Самый длинный интервал расписания — год.
MAX_SCHEDULE_INTERVAL_S = 366 * 86400


def _validate_schedule(body: ScheduleBody, existing_options: Optional[dict] = None) -> dict:
    """Проверить расписание ДО записи в БД; вернуть итоговые параметры задания.

    Иначе некорректное расписание (кривой cron, слишком маленький интервал)
    принимается, а планировщик молча не может построить триггер — задание
    никогда не срабатывает.
    """
    if body.job_type not in SCHEDULE_JOB_TYPES:
        raise ValidationError(f"По расписанию нельзя запускать задание «{body.job_type}».",
                              hint="Допустимо: копирование (backup), очистка (retention), "
                                   "проверка целостности (verify), восстановление (restore).")
    options = body.options if body.options is not None else dict(existing_options or {})
    if not isinstance(options, dict):
        raise ValidationError("Параметры задания должны быть объектом.")
    if body.job_type == JobType.RESTORE:
        # Без этой проверки расписание «восстановление раз в сутки» уходило в
        # обработчик с умолчанием target_mode="original" и регулярно заливало
        # весь архив прямо в рабочие папки живого ящика.
        options = validate_restore_options(options)
    elif body.job_type == JobType.BACKUP_ALL:
        options = backup_all_options(options)
    else:
        # у копирования, очистки и проверки по расписанию параметров нет
        options = {}
    if body.kind == ScheduleKind.CRON:
        _validate_cron(body.cron_expr)
    elif body.kind == ScheduleKind.INTERVAL:
        if int(body.interval_seconds or 0) < MIN_SCHEDULE_INTERVAL_S:
            raise ValidationError(f"Интервал не может быть меньше {MIN_SCHEDULE_INTERVAL_S // 60} мин.",
                                  hint="Укажите интервал от 1 минуты.")
        if int(body.interval_seconds) > MAX_SCHEDULE_INTERVAL_S:
            raise ValidationError("Интервал не может быть больше года.")
    else:
        raise ValidationError(f"Неизвестный тип расписания: {body.kind}",
                              hint="Допустимые значения: «cron» или «interval».")
    return options


def _schedule_target(svc, body: ScheduleBody) -> None:
    """Проверить, к чему относится расписание: к ящику или ко всем ящикам сразу."""
    if body.job_type in GLOBAL_SCHEDULE_JOB_TYPES:
        body.account_id = None            # проход сам берёт все включённые ящики
        if not svc.db.schedules_allow_global():
            raise ValidationError("База данных не обновлена для общих расписаний.",
                                  hint="Перезапустите службу: таблица расписаний обновляется при запуске "
                                       "(подробности — в журнале службы).")
        return
    if body.account_id is None:
        raise ValidationError("Выберите ящик для расписания.",
                              hint="Для копирования всех включённых ящиков выберите задание "
                                   "«Копирование всех ящиков по очереди».")
    acc = svc.require_account(body.account_id)
    if body.job_type == JobType.RESTORE and acc.auth_type == AuthType.AXIGEN_FTP:
        from ..queue.jobs import restore_via_ftp
        if restore_via_ftp(acc, dict(body.options or {})):
            raise ValidationError(
                "Восстановление через FTP Backup Axigen по расписанию не делается: оно меняет почтовый сервер и "
                "запускается только кнопкой администратора.",
                hint="Для расписания выберите «Как заливать: по IMAP» (у ящика должен быть пароль).")


@router.post("/schedules")
def create_schedule(request: Request, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    # Расписаниями определяется, копируется ли ящик вообще и что чистится —
    # у сотрудника доступ к ним только на просмотр.
    _require_not_mailbox(user, "Изменение расписаний")
    _schedule_target(svc, body)
    options = _validate_schedule(body)
    sid = svc.db.create_schedule(body.account_id, body.kind, body.job_type, body.cron_expr.strip(),
                                 body.interval_seconds, body.enabled, options)
    svc.db.add_audit(user["username"], "schedule_create", f"id={sid} account={body.account_id} type={body.job_type}")
    svc.scheduler.reload()
    return {"ok": True, "id": sid}


@router.put("/schedules/{schedule_id}")
def update_schedule(request: Request, schedule_id: int, body: ScheduleBody, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user, "Изменение расписаний")
    existing = svc.db.get_schedule(schedule_id)
    if existing is None or existing["job_type"] == JobType.SERVER_ACTION:
        # расписание раздела «Ящики на сервере» меняется только там (и только администратором)
        raise HTTPException(404, "Расписание не найдено")
    _schedule_target(svc, body)
    try:
        old_options = json.loads(existing["options"] or "{}")
    except (TypeError, ValueError):
        old_options = {}
    options = _validate_schedule(body, old_options if body.job_type == existing["job_type"] else {})
    # account_id тоже сохраняется: раньше смена ящика в форме молча игнорировалась
    svc.db.update_schedule(schedule_id, account_id=body.account_id, kind=body.kind, job_type=body.job_type,
                           cron_expr=body.cron_expr.strip(), interval_seconds=body.interval_seconds,
                           enabled=body.enabled, options=options)
    # Правка пишется в аудит, как создание и удаление: иначе выключить
    # копирование ящика можно было без следа.
    changes = []
    for key, new_val in (("account_id", body.account_id), ("job_type", body.job_type), ("kind", body.kind),
                         ("cron_expr", body.cron_expr.strip()), ("interval_seconds", body.interval_seconds),
                         ("enabled", 1 if body.enabled else 0)):
        if existing[key] != new_val and not (key == "interval_seconds" and body.kind == ScheduleKind.CRON):
            changes.append(f"{key}: {existing[key]} → {new_val}")
    svc.db.add_audit(user["username"], "schedule_update",
                     f"id={schedule_id} account={body.account_id}" + (" " + "; ".join(changes) if changes else ""))
    svc.scheduler.reload()
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(request: Request, schedule_id: int, user: dict = Depends(auth_mod.require_user)):
    svc = svc_dep(request)
    _require_not_mailbox(user, "Изменение расписаний")
    existing = svc.db.get_schedule(schedule_id)
    if existing is None or existing["job_type"] == JobType.SERVER_ACTION:
        raise HTTPException(404, "Расписание не найдено")
    svc.db.delete_schedule(schedule_id)
    svc.db.add_audit(user["username"], "schedule_delete", f"id={schedule_id} account={existing['account_id']}")
    svc.scheduler.reload()
    return {"ok": True}
