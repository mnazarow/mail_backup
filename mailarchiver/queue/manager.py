"""
Менеджер очереди заданий: пул воркеров, приоритеты, лимит одновременных
заданий, взаимоисключение работы с локальной копией одного ящика, отмена,
повторные попытки, уведомления.

Устройство:
  * поток-поллер регулярно берёт из БД следующее подходящее задание и отдаёт
    его в пул потоков (ThreadPoolExecutor);
  * каждое задание выполняется синхронно в своём потоке (IMAP-библиотека
    синхронна) и сообщает прогресс в БД;
  * отложенный повтор хранится в БД (``jobs.run_after``): задание ждёт в
    статусе «в очереди», отмена на нём работает, а перезапуск службы его не
    теряет;
  * состояние выполняющихся заданий также хранится в памяти для быстрого
    отображения «текущих операций» в веб-интерфейсе.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..errors import JobCancelled, MailArchiverError
from ..logging_setup import get_logger
from ..models import JobStatus, JobType
from ..util import clamp

log = get_logger("queue")

#: Задания, работающие с локальной копией ящика. Два таких задания по одному
#: ящику одновременно не запускаются: бэкап, очистка и перешифровка иначе
#: работали бы с одними и теми же файлами и строками индекса наперегонки
#: (удалённое письмо оставалось сиротой, бэкап качал всё дважды).
LOCAL_JOB_TYPES = (JobType.BACKUP, JobType.RETENTION, JobType.STORAGE_CONVERT, JobType.VERIFY,
                   JobType.EXPORT, JobType.ANALYZE, JobType.RESTORE)
#: Задания, которые не запускаются параллельно сами с собой.
SINGLETON_JOB_TYPES = (JobType.SYNC_EMPLOYEES, JobType.CHECK_LOGINS, JobType.REPLICATE,
                       JobType.DB_SNAPSHOT, JobType.SEARCH_INDEX, JobType.DEDUP_REPORT)
#: О каких заданиях не слать уведомления (служебные, по кнопке).
_QUIET_TYPES = (JobType.TEST, JobType.CHECK_LOGINS, JobType.SEARCH_INDEX, JobType.DEDUP_REPORT)


class QueueManager:
    def __init__(self, services) -> None:
        self.services = services
        self.db = services.db
        self._stop = threading.Event()
        self._shutting_down = threading.Event()
        self._poller: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._pool_size = 0                   # фактический размер пула (настройка могла измениться)
        self._running: Dict[int, dict] = {}   # job_id -> {account_id, type, started}
        self._to_requeue: Dict[int, dict] = {}  # job_id -> как вернуть в очередь после снятия с учёта
        self._lock = threading.Lock()
        self._wake = threading.Event()

    # -- параметры (устойчивы к некорректным значениям настроек) -------------
    def max_workers(self) -> int:
        try:
            return int(clamp(int(self.services.rt("backup", "max_concurrent_jobs") or 2), 1, 16))
        except (ValueError, TypeError):
            return 2

    def per_account_limit(self) -> int:
        """Сохранено для совместимости: с локальной копией ящика всегда работает одно задание."""
        return 1

    def is_shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    # -- жизненный цикл ------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._shutting_down.clear()
        workers = self.max_workers()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._pool_size = workers
        self._poller = threading.Thread(target=self._poll_loop, name="queue-poller", daemon=True)
        self._poller.start()
        log.info("Очередь запущена (воркеров: %s).", workers)

    def stop(self, timeout: float = 20.0) -> None:
        """Остановить очередь при остановке службы.

        Выполняющимся заданиям подаётся сигнал «служба останавливается» — НЕ
        отмена: задание, прерванное так, возвращается в очередь и продолжится
        после запуска. Раньше остановка ставила тот же флаг, что кнопка
        «Отмена», и ночные бэкапы после обновления значились «отменёнными
        пользователем» и не возобновлялись.
        """
        self._shutting_down.set()
        self._stop.set()
        self._wake.set()
        if self._poller:
            self._poller.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            deadline = time.time() + max(0.0, timeout)
            while time.time() < deadline:
                with self._lock:
                    if not self._running:
                        break
                time.sleep(0.2)
            with self._lock:
                stuck = list(self._running.keys())
            if stuck:
                log.warning("Задания не успели остановиться за %s с: %s — они продолжатся после "
                            "запуска службы.", timeout, stuck)
        log.info("Очередь остановлена.")

    # -- публичный API -------------------------------------------------------
    def enqueue(self, job_type: str, account_id: Optional[int], params: Optional[dict] = None,
                priority: int = 5, max_attempts: int = 1, created_by: str = "") -> int:
        job_id = self.db.enqueue_job(job_type, account_id, params or {}, priority, max_attempts, created_by)
        self._wake.set()
        log.info("Задание #%s (%s) поставлено в очередь.", job_id, job_type)
        return job_id

    def cancel(self, job_id: int) -> None:
        self.db.request_cancel(job_id)
        row = self.db.get_job(job_id)
        with self._lock:
            active = job_id in self._running
        if row is not None and row["status"] == JobStatus.QUEUED and not active:
            # Задание ещё (или снова — ждёт повтора) в очереди: обработчика,
            # который увидел бы флаг отмены, нет — завершаем его сразу.
            self.db.finish_job(job_id, JobStatus.CANCELLED, error="Отменено пользователем")
            self.db.add_job_event(job_id, "WARNING", "Задание отменено до начала выполнения.")
            log.info("Задание #%s отменено (стояло в очереди).", job_id)
            return
        log.info("Запрошена отмена задания #%s.", job_id)

    def retry(self, job_id: int) -> None:
        """Ручной «Повторить»: снова все попытки и снятый флаг отмены."""
        self.db.requeue_job(job_id, reset_attempts=True, clear_cancel=True)
        self._wake.set()

    def running_ids(self) -> List[int]:
        with self._lock:
            return list(self._running.keys())

    def running_snapshot(self) -> List[dict]:
        with self._lock:
            return [dict(id=jid, **info) for jid, info in self._running.items()]

    # -- цикл поллера --------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            submitted = False
            try:
                self._maybe_resize_pool()
                while not self._stop.is_set():
                    with self._lock:
                        # свободные слоты считаем по ФАКТИЧЕСКОМУ размеру пула
                        free = self._pool_size - len(self._running)
                        busy = sorted({info["account_id"] for info in self._running.values()
                                       if info.get("account_id") is not None
                                       and info.get("type") in LOCAL_JOB_TYPES})
                        running_types = {info.get("type") for info in self._running.values()}
                    if free <= 0:
                        break
                    job = self.db.claim_next_job_filtered(
                        "worker", busy_accounts=busy, local_types=LOCAL_JOB_TYPES,
                        skip_types=[t for t in SINGLETON_JOB_TYPES if t in running_types])
                    if job is None:
                        break
                    self._submit(job)
                    submitted = True
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле поллера очереди")
            if not submitted:
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    def _maybe_resize_pool(self) -> None:
        """Привести размер пула к текущей настройке (только когда он свободен)."""
        desired = self.max_workers()
        with self._lock:
            if desired == self._pool_size or self._running:
                return
        old = self._executor
        self._executor = ThreadPoolExecutor(max_workers=desired, thread_name_prefix="job")
        with self._lock:
            self._pool_size = desired
        if old is not None:
            old.shutdown(wait=False)
        log.info("Размер пула воркеров изменён: %s.", desired)

    def _submit(self, job) -> None:
        job_id = job["id"]
        info = {"account_id": job["account_id"], "type": job["type"], "started": time.time()}
        with self._lock:
            self._running[job_id] = info
        job_dict = {k: job[k] for k in job.keys()}
        future = self._executor.submit(self._run_job, job_dict)
        future.add_done_callback(lambda f, jid=job_id: self._on_done(jid, f))

    def _on_done(self, job_id: int, future) -> None:
        # Снимаем задание с учёта и ТОЛЬКО ПОТОМ возвращаем его в очередь —
        # иначе поллер мог бы захватить его вторым воркером до снятия.
        with self._lock:
            self._running.pop(job_id, None)
            requeue = self._to_requeue.pop(job_id, None)
        if requeue is not None:
            try:
                self.db.requeue_job(job_id, run_after=requeue.get("run_after"),
                                    refund_attempt=bool(requeue.get("refund")))
            except Exception:  # noqa: BLE001
                log.exception("Не удалось вернуть задание #%s в очередь", job_id)
        exc = future.exception()
        if exc:
            log.error("Задание #%s завершилось необработанной ошибкой: %s", job_id, exc)
        self._wake.set()

    # -- выполнение задания --------------------------------------------------
    def _run_job(self, job: dict) -> None:
        from .jobs import HANDLERS, JobContext  # локальный импорт против циклов
        job_id = job["id"]
        job_type = job["type"]
        try:
            params = json.loads(job["params"] or "{}")
        except json.JSONDecodeError:
            params = {}
        ctx = JobContext(self.services, job_id, job_type, job["account_id"], params)
        handler = HANDLERS.get(job_type)
        if handler is None:
            self.db.finish_job(job_id, JobStatus.FAILED, error=f"Неизвестный тип задания: {job_type}")
            return
        try:
            result = handler(ctx)
            final = result.get("final_status", JobStatus.SUCCESS)
            self.db.finish_job(job_id, final, result)
            log.info("Задание #%s (%s) завершено: %s", job_id, job_type, final)
            self._after_final(job, final, result.get("summary") or "", result.get("hint"))
        except JobCancelled as exc:
            if self._shutting_down.is_set() and not self.db.is_cancel_requested(job_id):
                # Не отмена пользователем, а остановка службы: задание вернётся
                # в очередь (без траты попытки) и продолжится после запуска.
                self.db.add_job_event(job_id, "WARNING", "Служба останавливается — задание прервано и "
                                                         "продолжится после её запуска.")
                with self._lock:
                    self._to_requeue[job_id] = {"refund": True}
                log.info("Задание #%s прервано остановкой службы и вернётся в очередь.", job_id)
                return
            self.db.add_job_event(job_id, "WARNING", str(exc.message if hasattr(exc, "message") else exc))
            self.db.finish_job(job_id, JobStatus.CANCELLED, error=exc.message)
            log.info("Задание #%s отменено.", job_id)
        except MailArchiverError as exc:
            self._handle_failure(job_id, job, exc, exc.retryable)
        except Exception as exc:  # noqa: BLE001
            log.exception("Задание #%s упало с ошибкой", job_id)
            self._handle_failure(job_id, job, exc, _is_transient(exc))

    def _handle_failure(self, job_id: int, job: dict, exc: BaseException, retryable: bool) -> None:
        attempts = int(job.get("attempts") or 1)
        max_attempts = int(job.get("max_attempts") or 1)
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        hint = getattr(exc, "hint", None)
        self.db.add_job_event(job_id, "ERROR", message + (f" | {hint}" if hint else ""))
        if self.db.is_cancel_requested(job_id):
            self.db.finish_job(job_id, JobStatus.CANCELLED, error="Отменено пользователем")
            return
        if self._shutting_down.is_set():
            with self._lock:
                self._to_requeue[job_id] = {"refund": True}
            return
        if retryable and attempts < max_attempts:
            # Пауза растёт в retry_backoff раз с каждой попыткой (2 → 4 → 8 с …).
            try:
                initial = float(self.services.rt("backup", "retry_initial_delay_s") or 2)
                backoff = float(self.services.rt("backup", "retry_backoff") or 2.0)
                delay = int(min(initial * (max(1.0, backoff) ** max(0, attempts - 1)), 600))
            except (ValueError, TypeError):
                delay = 2 * attempts
            run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            self.db.add_job_event(job_id, "WARNING",
                                  f"Повторная попытка через {delay} с (попытка {attempts + 1}/{max_attempts}). "
                                  f"Пока задание ждёт, его можно отменить.")
            with self._lock:
                self._to_requeue[job_id] = {"run_after": run_after}
            return
        self.db.finish_job(job_id, JobStatus.FAILED, error=message)
        log.error("Задание #%s провалено: %s", job_id, message)
        self._after_final(job, JobStatus.FAILED, message, hint)

    # -- итог задания: статистика и уведомления ------------------------------
    def _after_final(self, job: dict, status: str, summary: str, hint: Optional[str]) -> None:
        job_type = job.get("type")
        account_id = job.get("account_id")
        if status == JobStatus.FAILED and job_type == JobType.BACKUP and account_id is not None:
            # Провал бэкапа (сервер недоступен, сменился пароль) тоже должен быть
            # виден в «Активности»: раньше такая ночь показывала 0 заданий.
            try:
                self.db.bump_daily_stats(int(account_id), jobs=1, errors=1)
            except Exception:  # noqa: BLE001
                log.debug("Не удалось учесть провал в статистике", exc_info=True)
        if job_type in _QUIET_TYPES or status == JobStatus.CANCELLED:
            return
        notifier = getattr(self.services, "notifier", None)
        if notifier is None:
            return
        label = JobType.LABELS.get(job_type, job_type)
        where = ""
        if account_id is not None:
            try:
                acc = self.db.get_account(int(account_id))
                where = f" «{acc.name}»" if acc else ""
            except Exception:  # noqa: BLE001
                where = ""
        status_label = JobStatus.LABELS.get(status, status)
        body = f"{label}{where}: {status_label}.\n\n{summary}"
        if hint:
            body += f"\n\nЧто сделать: {hint}"
        body += f"\n\nЗадание №{job.get('id')}."
        notifier.notify_job_async(job_type, status, f"[MailArchiver] {label}{where}: {status_label}", body)


def _is_transient(exc: BaseException) -> bool:
    """Сбой вне иерархии приложения, который стоит повторить (а не провалить сразу)."""
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text
    return isinstance(exc, (ConnectionError, TimeoutError))
