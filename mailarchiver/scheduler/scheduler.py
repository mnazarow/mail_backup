"""
Планировщик на базе APScheduler.

Расписания хранятся в БД (таблица ``schedules``) и синхронизируются с
APScheduler. Поддерживаются два вида триггеров:
  * cron     — по crontab-выражению (минуты часы день месяц день_недели);
  * interval — каждые N секунд/минут/часов.

При срабатывании расписания в очередь ставится соответствующее задание
(резервное копирование, ретеншн и т.п.). Планировщик также запускает
служебное обслуживание: чистку истёкших сессий и старых заданий.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..errors import ValidationError
from ..logging_setup import get_logger
from ..models import JobType, ScheduleKind

log = get_logger("scheduler")


class SchedulerService:
    def __init__(self, services) -> None:
        self.services = services
        self._sched: Optional[BackgroundScheduler] = None

    def _timezone(self) -> ZoneInfo:
        tzname = self.services.rt("scheduler", "timezone") or "UTC"
        try:
            return ZoneInfo(tzname)
        except Exception:  # noqa: BLE001
            log.warning("Неизвестный часовой пояс «%s», используется UTC.", tzname)
            return ZoneInfo("UTC")

    # -- жизненный цикл ------------------------------------------------------
    def start(self) -> None:
        if not bool(self.services.rt("scheduler", "enabled")):
            log.info("Планировщик отключён в настройках.")
            return
        if self._sched is not None:
            # уже запущен — просто пересинхронизируем расписания
            self.reload()
            return
        self._sched = BackgroundScheduler(timezone=self._timezone())
        self._sched.start()
        self.reload()
        # служебное обслуживание раз в час
        self._sched.add_job(self._maintenance, IntervalTrigger(minutes=60), id="maintenance",
                            replace_existing=True, coalesce=True, misfire_grace_time=600)
        log.info("Планировщик запущен.")

    def stop(self) -> None:
        if self._sched:
            try:
                self._sched.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._sched = None
            log.info("Планировщик остановлен.")

    def running(self) -> bool:
        return self._sched is not None and self._sched.running

    # -- синхронизация расписаний -------------------------------------------
    def reload(self) -> None:
        enabled = bool(self.services.rt("scheduler", "enabled"))
        if not self._sched:
            # Настройку могли включить уже после старта процесса: без этого
            # галочка «Планировщик включён» требовала бы перезапуска сервиса.
            if enabled:
                self.start()
            return
        if not enabled:
            # настройку выключили — останавливаем работающий планировщик
            self.stop()
            return
        # удалить существующие задания расписаний
        for job in self._sched.get_jobs():
            if job.id.startswith("sched_"):
                self._sched.remove_job(job.id)
        for row in self.services.db.list_schedules(only_enabled=True):
            try:
                self._add_job(row)
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось добавить расписание #%s: %s", row["id"], exc)
        self._reload_employees_sync()

    # -- синхронизация сотрудников ------------------------------------------
    #: Идентификатор служебного задания (не из таблицы schedules, поэтому и не
    #: попадает под общую чистку заданий «sched_*» в reload()).
    EMPLOYEES_JOB_ID = "employees_sync"

    def _reload_employees_sync(self) -> None:
        """Пересоздать задание синхронизации сотрудников по настройкам.

        Вызывается из reload(), то есть при каждом сохранении настроек: и
        включение/выключение, и смена cron-выражения применяются сразу, без
        перезапуска сервиса.
        """
        if not self._sched:
            return
        if self._sched.get_job(self.EMPLOYEES_JOB_ID):
            self._sched.remove_job(self.EMPLOYEES_JOB_ID)
        if not bool(self.services.rt("employees", "sync_enabled")):
            return
        expr = str(self.services.rt("employees", "cron") or "").strip()
        if len(expr.split()) != 5:
            log.error("Некорректное cron-выражение синхронизации сотрудников: «%s» (нужно 5 полей).", expr)
            return
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=self._timezone())
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось разобрать cron синхронизации сотрудников «%s»: %s", expr, exc)
            return
        self._sched.add_job(
            self._fire_employees_sync, trigger, id=self.EMPLOYEES_JOB_ID,
            replace_existing=True, coalesce=bool(self.services.rt("scheduler", "coalesce")),
            misfire_grace_time=int(self.services.rt("scheduler", "misfire_grace_time_s") or 3600),
        )
        log.info("Синхронизация сотрудников запланирована: «%s».", expr)

    def _fire_employees_sync(self) -> None:
        """Поставить в очередь общесистемное задание синхронизации сотрудников."""
        try:
            job_id = self.services.queue.enqueue(
                JobType.SYNC_EMPLOYEES, None, {}, created_by="scheduler")
            log.info("Синхронизация сотрудников по расписанию → задание #%s.", job_id)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при постановке синхронизации сотрудников")

    def _add_job(self, row) -> None:
        trigger = self._build_trigger(row)
        job = self._sched.add_job(
            self._fire, trigger, args=[row["id"]], id=f"sched_{row['id']}",
            replace_existing=True, coalesce=bool(self.services.rt("scheduler", "coalesce")),
            misfire_grace_time=int(self.services.rt("scheduler", "misfire_grace_time_s") or 3600),
        )
        if job.next_run_time:
            self.services.db.set_schedule_runtimes(row["id"], next_run=job.next_run_time.isoformat())

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
            # Отсчёт ведём от последнего ФАКТИЧЕСКОГО запуска: reload() вызывается
            # при каждом сохранении настроек, и пересоздание триггера «с нуля»
            # обнуляло бы таймер — шестичасовой бэкап мог не запуститься никогда.
            # APScheduler сам выберет ближайшую будущую точку сетки last_run + N*интервал.
            start = self._interval_start(row, tz)
            if start is not None:
                return IntervalTrigger(seconds=secs, timezone=tz, start_date=start)
            return IntervalTrigger(seconds=secs, timezone=tz)
        raise ValidationError(f"Неизвестный тип расписания: {kind}")

    def _interval_start(self, row, tz) -> Optional[datetime]:
        """Точка отсчёта interval-расписания — время последнего запуска (или None)."""
        try:
            last = row["last_run"]
        except (KeyError, IndexError, TypeError):
            return None
        if not last:
            return None
        try:
            dt = datetime.fromisoformat(str(last))
        except (ValueError, TypeError):
            return None
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)

    # -- срабатывание --------------------------------------------------------
    def _fire(self, schedule_id: int) -> None:
        try:
            row = self.services.db.get_schedule(schedule_id)
            if row is None or not row["enabled"]:
                return
            job_type = row["job_type"] or JobType.BACKUP
            import json
            options = json.loads(row["options"] or "{}")
            max_attempts = int(self.services.rt("backup", "retry_attempts") or 1) if job_type == JobType.BACKUP else 1
            job_id = self.services.queue.enqueue(
                job_type, row["account_id"], options,
                priority=int(options.get("priority", 5)),
                max_attempts=max_attempts, created_by="scheduler",
            )
            now = datetime.now(self._timezone()).isoformat()
            self.services.db.set_schedule_runtimes(schedule_id, last_run=now)
            # обновить next_run
            job = self._sched.get_job(f"sched_{schedule_id}") if self._sched else None
            if job and job.next_run_time:
                self.services.db.set_schedule_runtimes(schedule_id, next_run=job.next_run_time.isoformat())
            log.info("Расписание #%s сработало → задание #%s (%s).", schedule_id, job_id, job_type)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при срабатывании расписания #%s", schedule_id)

    def next_run_for(self, schedule_id: int) -> Optional[str]:
        if not self._sched:
            return None
        job = self._sched.get_job(f"sched_{schedule_id}")
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    # -- обслуживание --------------------------------------------------------
    def _maintenance(self) -> None:
        try:
            self.services.db.purge_expired_sessions()
            keep_jobs = int(self.services.rt("retention", "keep_last_runs") or 30) * 10
            self.services.db.purge_old_jobs(max(keep_jobs, 200))
            # Журнал неудачных входов нужен только для временной блокировки —
            # без очистки он рос бы бесконечно (перебор паролей раздувает БД).
            from datetime import datetime, timedelta, timezone
            lockout_min = int(self.services.rt("security", "lockout_minutes") or 15)
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(lockout_min, 15) * 4)).isoformat()
            self.services.db.purge_old_login_attempts(cutoff)
            log.debug("Служебное обслуживание выполнено.")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка служебного обслуживания")
