"""
Планировщик на базе APScheduler.

Расписания хранятся в БД (таблица ``schedules``) и синхронизируются с
APScheduler. Поддерживаются два вида триггеров:
  * cron     — по crontab-выражению (минуты часы день месяц день_недели);
  * interval — каждые N секунд.

При срабатывании расписания в очередь ставится соответствующее задание
(резервное копирование, ретеншн и т.п.). Кроме расписаний из таблицы
планировщик ведёт служебные задания:
  * ежедневную очистку по срокам хранения (retention.cron);
  * синхронизацию сотрудников (employees.cron);
  * обслуживание БД и каталогов — раз в час, ВСЕГДА, даже при выключенном
    планировщике: иначе при scheduler.enabled=false переставали чиститься
    сессии, журнал заданий и попытки входа.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..errors import ValidationError
from ..logging_setup import get_logger
from ..models import AuthType, JobType, ScheduleKind, account_has_credentials

log = get_logger("scheduler")

#: Типы заданий, которым нужен вход на почтовый сервер.
_IMAP_JOB_TYPES = (JobType.BACKUP, JobType.RESTORE)


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SchedulerService:
    def __init__(self, services) -> None:
        self.services = services
        self._sched: Optional[BackgroundScheduler] = None
        self._enabled = False

    def _timezone(self) -> ZoneInfo:
        tzname = self.services.rt("scheduler", "timezone") or "UTC"
        try:
            return ZoneInfo(tzname)
        except Exception:  # noqa: BLE001
            log.warning("Неизвестный часовой пояс «%s», используется UTC.", tzname)
            return ZoneInfo("UTC")

    def _job_kwargs(self) -> dict:
        return {"replace_existing": True,
                "coalesce": bool(self.services.rt("scheduler", "coalesce")),
                "misfire_grace_time": int(self.services.rt("scheduler", "misfire_grace_time_s") or 3600)}

    # -- жизненный цикл ------------------------------------------------------
    def start(self) -> None:
        if self._sched is None:
            self._sched = BackgroundScheduler(timezone=self._timezone())
            self._sched.start()
            # служебное обслуживание раз в час — независимо от scheduler.enabled
            self._sched.add_job(self._maintenance, IntervalTrigger(minutes=60), id="maintenance",
                                replace_existing=True, coalesce=True, misfire_grace_time=600,
                                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
            # индексация новых писем для поиска — каждые 10 минут, если есть что индексировать
            self._sched.add_job(self._search_index_tick, IntervalTrigger(minutes=10), id="search_index",
                                replace_existing=True, coalesce=True, misfire_grace_time=600,
                                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))
        self.reload(catch_up=True)

    def stop(self) -> None:
        if self._sched:
            try:
                self._sched.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._sched = None
            self._enabled = False
            log.info("Планировщик остановлен.")

    def running(self) -> bool:
        """Работают ли расписания (служебное обслуживание идёт всегда)."""
        return self._sched is not None and self._sched.running and self._enabled

    # -- синхронизация расписаний -------------------------------------------
    def reload(self, catch_up: bool = False) -> None:
        if self._sched is None:
            return
        enabled = bool(self.services.rt("scheduler", "enabled"))
        # Часовой пояс могли сменить в «Настройках» — триггеры строятся с ним.
        for job in self._sched.get_jobs():
            if job.id not in ("maintenance", "search_index"):
                self._sched.remove_job(job.id)
        was_enabled, self._enabled = self._enabled, enabled
        if not enabled:
            if was_enabled or catch_up:
                log.info("Планировщик отключён в настройках: расписания не выполняются.")
            return
        for row in self.services.db.list_schedules(only_enabled=True):
            try:
                self._add_job(row, catch_up=catch_up or not was_enabled)
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось добавить расписание #%s: %s", row["id"], exc)
        self._reload_employees_sync()
        self._reload_retention_sweep()
        self._reload_replica()
        self._reload_summary()
        if not was_enabled:
            log.info("Планировщик запущен.")

    # -- синхронизация сотрудников ------------------------------------------
    EMPLOYEES_JOB_ID = "employees_sync"
    RETENTION_JOB_ID = "retention_sweep"

    def _cron_trigger(self, expr: str, what: str):
        expr = str(expr or "").strip()
        if len(expr.split()) != 5:
            log.error("Некорректное cron-выражение %s: «%s» (нужно 5 полей).", what, expr)
            return None
        try:
            return CronTrigger.from_crontab(expr, timezone=self._timezone())
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось разобрать cron %s «%s»: %s", what, expr, exc)
            return None

    def _reload_employees_sync(self) -> None:
        """Пересоздать задание синхронизации сотрудников по настройкам."""
        if not bool(self.services.rt("employees", "sync_enabled")):
            return
        trigger = self._cron_trigger(self.services.rt("employees", "cron"), "синхронизации сотрудников")
        if trigger is None:
            return
        self._sched.add_job(self._fire_employees_sync, trigger, id=self.EMPLOYEES_JOB_ID,
                            **self._job_kwargs())

    def _fire_employees_sync(self) -> None:
        """Поставить в очередь общесистемное задание синхронизации сотрудников."""
        try:
            busy = any(job["type"] == JobType.SYNC_EMPLOYEES for job in self.services.db.active_jobs())
            if busy:
                log.warning("Синхронизация сотрудников по расписанию пропущена: предыдущая ещё идёт.")
                return
            job_id = self.services.queue.enqueue(
                JobType.SYNC_EMPLOYEES, None, {}, created_by="scheduler")
            log.info("Синхронизация сотрудников по расписанию → задание #%s.", job_id)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при постановке синхронизации сотрудников")

    # -- ежедневная очистка по срокам хранения -------------------------------
    def _reload_retention_sweep(self) -> None:
        trigger = self._cron_trigger(self.services.rt("retention", "cron") or "30 4 * * *",
                                     "очистки по срокам хранения")
        if trigger is None:
            return
        self._sched.add_job(self._fire_retention_sweep, trigger, id=self.RETENTION_JOB_ID,
                            **self._job_kwargs())

    def _fire_retention_sweep(self) -> list:
        """Поставить задания очистки ящикам, у которых есть что удалить по сроку.

        По заданию на ящик (а не одно общее): так очистка ящика не пересекается
        с его же бэкапом или перешифровкой — очередь не пускает их одновременно.
        """
        from ..queue.jobs import _count_older_than, effective_retention_days, retention_cutoff_iso
        svc = self.services
        created = []
        try:
            active = {(job["account_id"], job["type"]) for job in svc.db.active_jobs()}
            for acc in svc.db.list_accounts():
                days = effective_retention_days(svc, acc)
                if days <= 0 or (acc.id, JobType.RETENTION) in active:
                    continue
                if not _count_older_than(svc.db, acc.id, retention_cutoff_iso(days)):
                    continue
                created.append(svc.queue.enqueue(JobType.RETENTION, acc.id, {}, priority=7,
                                                 created_by="scheduler"))
            if created:
                log.info("Очистка по срокам хранения: поставлено заданий %d.", len(created))
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при постановке ежедневной очистки")
        return created

    # -- копия вне сервера и снимок базы ------------------------------------
    REPLICA_JOB_ID = "replica_sync"

    def _reload_replica(self) -> None:
        """Ежедневно в replica.cron: копия вне сервера (со снимком базы) или только снимок базы."""
        svc = self.services
        try:
            keep = int(svc.rt("replica", "db_snapshot_keep") or 0)
        except (TypeError, ValueError):
            keep = 0
        if not bool(svc.rt("replica", "enabled")) and keep <= 0:
            return
        trigger = self._cron_trigger(svc.rt("replica", "cron") or "0 6 * * *", "копии вне сервера")
        if trigger is None:
            return
        self._sched.add_job(self._fire_replica, trigger, id=self.REPLICA_JOB_ID, **self._job_kwargs())

    def _fire_replica(self) -> Optional[int]:
        svc = self.services
        try:
            active = {job["type"] for job in svc.db.active_jobs()}
            if bool(svc.rt("replica", "enabled")):
                if JobType.REPLICATE in active:
                    log.warning("Копия вне сервера по расписанию пропущена: предыдущая ещё идёт.")
                    return None
                job_id = svc.queue.enqueue(JobType.REPLICATE, None, {}, priority=6, created_by="scheduler")
                log.info("Копия вне сервера по расписанию → задание #%s.", job_id)
                return job_id
            if JobType.DB_SNAPSHOT in active:
                return None
            from ..replica import snapshots
            age = snapshots.newest_age_s(svc.cfg)
            if age is not None and age < 20 * 3600:
                return None
            return svc.queue.enqueue(JobType.DB_SNAPSHOT, None, {}, priority=6, created_by="scheduler")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при постановке копии вне сервера")
            return None

    # -- еженедельная сводка ----------------------------------------------
    SUMMARY_JOB_ID = "weekly_summary"

    def _reload_summary(self) -> None:
        svc = self.services
        if not bool(svc.rt("notifications", "enabled")) or not bool(svc.rt("notifications", "summary_enabled")):
            return
        trigger = self._cron_trigger(svc.rt("notifications", "summary_cron") or "0 8 * * 1", "сводки")
        if trigger is None:
            return
        self._sched.add_job(self._fire_summary, trigger, id=self.SUMMARY_JOB_ID, **self._job_kwargs())

    def _fire_summary(self) -> None:
        try:
            from ..monitoring import send_weekly_summary
            result = send_weekly_summary(self.services)
            if result["ok"]:
                log.info("Сводка отправлена: %s", result["subject"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при отправке сводки")

    # -- расписания из таблицы ----------------------------------------------
    def _add_job(self, row, catch_up: bool = False) -> None:
        trigger = self._build_trigger(row)
        job = self._sched.add_job(self._fire, trigger, args=[row["id"]], id=f"sched_{row['id']}",
                                  **self._job_kwargs())
        if job.next_run_time:
            self.services.db.set_schedule_runtimes(row["id"], next_run=job.next_run_time.isoformat())
        if catch_up:
            self._catch_up(row, trigger)

    def _catch_up(self, row, trigger) -> None:
        """Догнать запуск, пропущенный, пока служба была выключена.

        Расписания живут в памяти процесса, и после перезагрузки сервера в
        02:05 ночное копирование на 02:00 раньше просто пропадало до следующей
        ночи — хотя настройка «Допуск на пропуск запуска» обещала обратное.
        Запускаем ОДИН раз, если пропущенное время укладывается в допуск и
        расписание с тех пор не срабатывало.
        """
        grace = int(self.services.rt("scheduler", "misfire_grace_time_s") or 3600)
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=grace)
        last_run = _parse_iso(row["last_run"])
        created = _parse_iso(row["created_at"]) if "created_at" in row.keys() else None
        try:
            missed = trigger.get_next_fire_time(None, since)
        except Exception:  # noqa: BLE001
            return
        if missed is None or missed > now:
            return
        if last_run is not None and last_run >= missed:
            return
        if last_run is None and created is not None and created >= missed:
            # расписание заведено уже после этого времени — пропуска не было
            return
        log.warning("Расписание #%s: запуск на %s пропущен (служба не работала) — выполняем сейчас.",
                    row["id"], missed.astimezone(self._timezone()).strftime("%d.%m %H:%M"))
        self._fire(row["id"])

    def _build_trigger(self, row):
        kind = row["kind"]
        tz = self._timezone()
        if kind == ScheduleKind.CRON:
            expr = (row["cron_expr"] or "").strip()
            parts = expr.split()
            if len(parts) != 5:
                raise ValidationError(f"Некорректное cron-выражение: «{expr}» (нужно 5 полей).",
                                      hint="Пример: «0 3 * * *» — каждый день в 03:00.")
            return CronTrigger.from_crontab(expr, timezone=tz)
        if kind == ScheduleKind.INTERVAL:
            secs = int(row["interval_seconds"] or 0)
            if secs < 60:
                raise ValidationError("Интервал не может быть меньше 60 секунд.")
            # Отсчёт ведём от последнего ФАКТИЧЕСКОГО запуска, а у нового
            # расписания — от времени его создания: reload() вызывается при
            # каждом сохранении настроек, и триггер «с нуля» обнулял бы таймер —
            # суточное расписание при ежедневных правках не срабатывало бы никогда.
            start = self._interval_start(row, tz)
            if start is not None:
                return IntervalTrigger(seconds=secs, timezone=tz, start_date=start)
            return IntervalTrigger(seconds=secs, timezone=tz)
        raise ValidationError(f"Неизвестный тип расписания: {kind}")

    def _interval_start(self, row, tz) -> Optional[datetime]:
        """Точка отсчёта interval-расписания: последний запуск или создание."""
        keys = row.keys() if hasattr(row, "keys") else ()
        for name in ("last_run", "created_at"):
            if name in keys:
                dt = _parse_iso(row[name])
                if dt is not None:
                    return dt.astimezone(tz)
        return None

    # -- срабатывание --------------------------------------------------------
    def _fire(self, schedule_id: int) -> None:
        svc = self.services
        try:
            row = svc.db.get_schedule(schedule_id)
            if row is None or not row["enabled"]:
                return
            job_type = row["job_type"] or JobType.BACKUP
            try:
                options = json.loads(row["options"] or "{}")
            except (TypeError, ValueError):
                options = {}
            now = datetime.now(self._timezone()).isoformat()
            account_id = row["account_id"]
            if account_id is not None:
                acc = svc.db.get_account(account_id)
                if acc is None:
                    return
                if job_type in _IMAP_JOB_TYPES:
                    reason = ""
                    if not acc.enabled:
                        reason = "копирование ящика выключено"
                    elif acc.auth_type == AuthType.OAUTH2 and not acc.oauth_refresh_token:
                        reason = "не задан refresh-токен OAuth2"
                    elif not account_has_credentials(acc):
                        reason = "у ящика не задан пароль"
                    if reason:
                        # Каждую ночь входить на сервер с пустым паролем — это
                        # неудачный LOGIN с адреса архива, и fail2ban почтового
                        # сервера рано или поздно забанит его целиком.
                        log.info("Расписание #%s пропущено: %s («%s»).", schedule_id, reason, acc.name)
                        svc.db.set_schedule_runtimes(schedule_id, last_run=now)
                        return
                # Не ставим второе такое же задание, если предыдущее ещё в очереди
                # или выполняется: на 500 ящиках при двух воркерах ночные задания
                # иначе копились быстрее, чем выполнялись.
                busy = any(job["account_id"] == account_id and job["type"] == job_type
                           for job in svc.db.active_jobs())
                if busy:
                    log.warning("Расписание #%s пропущено: задание «%s» по этому ящику уже "
                                "в очереди или выполняется.", schedule_id, job_type)
                    svc.db.set_schedule_runtimes(schedule_id, last_run=now)
                    return
            max_attempts = int(svc.rt("backup", "retry_attempts") or 1) if job_type == JobType.BACKUP else 1
            try:
                priority = int(options.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            job_id = svc.queue.enqueue(job_type, account_id, options, priority=priority,
                                       max_attempts=max_attempts, created_by="scheduler")
            svc.db.set_schedule_runtimes(schedule_id, last_run=now)
            job = self._sched.get_job(f"sched_{schedule_id}") if self._sched else None
            if job and job.next_run_time:
                svc.db.set_schedule_runtimes(schedule_id, next_run=job.next_run_time.isoformat())
            log.info("Расписание #%s сработало → задание #%s (%s).", schedule_id, job_id, job_type)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при срабатывании расписания #%s", schedule_id)

    def next_run_of(self, job_id: str) -> Optional[str]:
        """Когда сработает служебное задание планировщика (retention_sweep, replica_sync …)."""
        if not self._sched or not self._enabled:
            return None
        job = self._sched.get_job(job_id)
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    def next_run_for(self, schedule_id: int) -> Optional[str]:
        if not self._sched or not self._enabled:
            return None
        job = self._sched.get_job(f"sched_{schedule_id}")
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    def _search_index_tick(self) -> Optional[int]:
        svc = self.services
        try:
            if not bool(svc.rt("search", "enabled")):
                return None
            from ..search import fts_available, pending_count
            if not fts_available(svc.db) or pending_count(svc.db) <= 0:
                return None
            if any(job["type"] == JobType.SEARCH_INDEX for job in svc.db.active_jobs()):
                return None
            return svc.queue.enqueue(JobType.SEARCH_INDEX, None, {}, priority=8, created_by="scheduler")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при постановке индексации поиска")
            return None

    # -- обслуживание --------------------------------------------------------
    def _maintenance(self) -> None:
        try:
            from ..maintenance import run_maintenance
            run_maintenance(self.services)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка служебного обслуживания")
