"""
Служебное обслуживание: чистка журналов, устаревших выгрузок и временных файлов.

Выполняется раз в час планировщиком — ВСЕГДА, даже при выключенных расписаниях
(scheduler.enabled=false): иначе при выключенном планировщике переставали
чиститься сессии, журнал заданий и попытки входа, а таблицы аудита,
статистики, выгрузок и восстановлений не чистились не было никогда вообще.

При запуске службы дополнительно наводится порядок после прерванной работы
(:func:`startup_cleanup`): выгрузки и восстановления, оставшиеся «в работе»,
помечаются прерванными, из каталога временных файлов убираются остатки.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from .logging_setup import get_logger

log = get_logger("maintenance")

#: Сколько хранить журнал заданий: удачные/отменённые и с ошибками.
JOBS_OK_DAYS = 30
JOBS_FAILED_DAYS = 90
JOBS_MAX = 100_000
#: Аудит действий пользователей.
AUDIT_DAYS = 365
#: Дневная статистика (графики «Активность» за годы).
STATS_DAYS = 5 * 365
#: Записи о восстановлениях.
RESTORES_DAYS = 180
#: Журнал попыток входа хранится не меньше суток с запасом: по нему считается
#: суточный лимит неверных кодов двухфакторного входа.
LOGIN_ATTEMPTS_MIN_KEEP_MIN = 25 * 60
#: Остатки во временном каталоге, которые считаются брошенными.
TMP_STALE_HOURS = 48

#: Имена, которые во временном каталоге создаёт сам сервис. Чистим ТОЛЬКО их:
#: каталог временных файлов можно направить и в общий /tmp.
TMP_PREFIXES = ("export_", "pstimport_", "import_", "passwords_", "employees_", "aspose_",
                "pstspool_", "mazip_")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _referenced_tmp_paths(svc) -> Set[str]:
    """Файлы, которые ждут своей очереди (загруженный .pst, список сотрудников)."""
    import json
    paths: Set[str] = set()
    for job in svc.db.active_jobs():
        try:
            params = json.loads(job["params"] or "{}")
        except (TypeError, ValueError):
            continue
        for key in ("pst_path", "path"):
            value = params.get(key)
            if isinstance(value, str) and value:
                paths.add(os.path.abspath(value))
    return paths


def clean_tmp(svc, older_than_s: float) -> int:
    tmp = svc.cfg.tmp_dir
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    keep = _referenced_tmp_paths(svc)
    now = time.time()
    removed = 0
    for name in names:
        if not name.startswith(TMP_PREFIXES):
            continue
        path = os.path.abspath(os.path.join(tmp, name))
        if path in keep:
            continue
        try:
            age = now - os.lstat(path).st_mtime
        except OSError:
            continue
        if age < older_than_s:
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        except OSError as exc:
            log.debug("Не удалось удалить %s: %s", path, exc)
    return removed


def _clean_partial_exports(svc) -> int:
    """Недописанные архивы выгрузок (*.part) в каталоге выгрузок."""
    removed = 0
    try:
        names = os.listdir(svc.cfg.exports_dir)
    except OSError:
        return 0
    for name in names:
        if name.endswith(".part"):
            try:
                os.unlink(os.path.join(svc.cfg.exports_dir, name))
                removed += 1
            except OSError:
                pass
    return removed


def purge_old_exports(svc) -> int:
    """Удалить выгрузки старше export.keep_days (файл и запись)."""
    try:
        days = int(svc.rt("export", "keep_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return 0
    before = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    removed = 0
    for row in svc.db.exports_older_than(before):
        path = row["path"] or ""
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError as exc:
                log.warning("Не удалось удалить старую выгрузку %s: %s", path, exc)
                continue
        svc.db.delete_export(row["id"])
        removed += 1
    if removed:
        svc.db.add_audit("system", "export_cleanup", f"удалено выгрузок старше {days} дн.: {removed}")
    return removed


def run_maintenance(svc) -> Dict[str, int]:
    db = svc.db
    now = datetime.now(timezone.utc)
    out: Dict[str, int] = {}
    db.purge_expired_sessions()
    out["jobs"] = db.purge_jobs_by_age(_iso(now - timedelta(days=JOBS_OK_DAYS)),
                                       _iso(now - timedelta(days=JOBS_FAILED_DAYS)), JOBS_MAX)
    # Журнал неудачных входов нужен только для временной блокировки (и для
    # суточного лимита неверных кодов 2FA — поэтому не короче 25 часов).
    try:
        lockout_min = int(svc.rt("security", "lockout_minutes") or 15)
    except (TypeError, ValueError):
        lockout_min = 15
    keep_min = max(max(lockout_min, 15) * 4, LOGIN_ATTEMPTS_MIN_KEEP_MIN)
    out["login_attempts"] = db.purge_older_login(_iso(now - timedelta(minutes=keep_min)))
    db.purge_expired_otp_challenges()
    out["audit"] = db.purge_older("audit", "ts", _iso(now - timedelta(days=AUDIT_DAYS)))
    out["stats"] = db.purge_older("stats_daily", "day", (now - timedelta(days=STATS_DAYS)).strftime("%Y-%m-%d"))
    out["restores"] = db.purge_older("restores", "created_at", _iso(now - timedelta(days=RESTORES_DAYS)))
    try:
        keep_runs = int(svc.rt("retention", "keep_last_runs") or 30)
    except (TypeError, ValueError):
        keep_runs = 30
    out["runs"] = db.purge_runs(keep_runs)
    out["exports"] = purge_old_exports(svc)
    out["tmp"] = clean_tmp(svc, TMP_STALE_HOURS * 3600)
    if any(out.values()):
        log.info("Обслуживание: %s", ", ".join(f"{k}={v}" for k, v in out.items() if v))
    return out


def startup_cleanup(svc) -> Dict[str, int]:
    """Порядок после прерванной работы — при запуске, ДО старта очереди."""
    out = {"artifacts": svc.db.fail_interrupted_artifacts(),
           "tmp": clean_tmp(svc, 0),
           "partial": _clean_partial_exports(svc)}
    if any(out.values()):
        log.info("Уборка после прошлого запуска: прервано выгрузок/восстановлений %d, удалено "
                 "временных остатков %d, недописанных архивов %d.",
                 out["artifacts"], out["tmp"], out["partial"])
    return out
