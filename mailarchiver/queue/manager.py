"""
Менеджер очереди заданий: пул воркеров, приоритеты, лимит одновременных
заданий, ограничение параллелизма на аккаунт, отмена, повторные попытки.

Устройство:
  * поток-поллер регулярно берёт из БД следующее подходящее задание и отдаёт
    его в пул потоков (ThreadPoolExecutor);
  * каждое задание выполняется синхронно в своём потоке (IMAP-библиотека
    синхронна) и сообщает прогресс в БД;
  * состояние выполняющихся заданий также хранится в памяти для быстрого
    отображения «текущих операций» в веб-интерфейсе.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from ..errors import JobCancelled, JobError, MailArchiverError
from ..logging_setup import get_logger
from ..models import JobStatus
from ..util import clamp

log = get_logger("queue")


class QueueManager:
    def __init__(self, services) -> None:
        self.services = services
        self.db = services.db
        self._stop = threading.Event()
        self._poller: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running: Dict[int, dict] = {}   # job_id -> {account_id, type, started, worker}
        self._to_requeue: set = set()         # задания, которые нужно поставить в очередь заново (повтор)
        self._lock = threading.Lock()
        self._wake = threading.Event()

    # -- параметры (устойчивы к некорректным значениям настроек) -------------
    def max_workers(self) -> int:
        try:
            return int(clamp(int(self.services.rt("backup", "max_concurrent_jobs") or 2), 1, 16))
        except (ValueError, TypeError):
            return 2

    def per_account_limit(self) -> int:
        try:
            return int(clamp(int(self.services.rt("backup", "per_account_concurrency") or 1), 1, 8))
        except (ValueError, TypeError):
            return 1

    # -- жизненный цикл ------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers(), thread_name_prefix="job")
        self._poller = threading.Thread(target=self._poll_loop, name="queue-poller", daemon=True)
        self._poller.start()
        log.info("Очередь запущена (воркеров: %s, на аккаунт: %s).", self.max_workers(), self.per_account_limit())

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._poller:
            self._poller.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
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
        log.info("Запрошена отмена задания #%s.", job_id)

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
                while not self._stop.is_set():
                    with self._lock:
                        free = self.max_workers() - len(self._running)
                        running_accounts: Dict[int, int] = {}
                        for info in self._running.values():
                            a = info.get("account_id")
                            if a is not None:
                                running_accounts[a] = running_accounts.get(a, 0) + 1
                    if free <= 0:
                        break
                    per = self.per_account_limit()
                    skip = [a for a, c in running_accounts.items() if c >= per]
                    job = self.db.claim_next_job_filtered("worker", skip)
                    if job is None:
                        break
                    self._submit(job)
                    submitted = True
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле поллера очереди")
            # ждать сигнала или таймаута
            if not submitted:
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    def _submit(self, job) -> None:
        job_id = job["id"]
        info = {"account_id": job["account_id"], "type": job["type"], "started": time.time()}
        with self._lock:
            self._running[job_id] = info
        job_dict = {k: job[k] for k in job.keys()}
        future = self._executor.submit(self._run_job, job_dict)
        future.add_done_callback(lambda f, jid=job_id: self._on_done(jid, f))

    def _on_done(self, job_id: int, future) -> None:
        # Снимаем задание с учёта и, если запрошен повтор, ставим его в очередь
        # заново ТОЛЬКО после снятия — иначе поллер мог бы захватить его вторым
        # воркером до снятия и запустить дважды.
        with self._lock:
            self._running.pop(job_id, None)
            do_requeue = job_id in self._to_requeue
            self._to_requeue.discard(job_id)
        if do_requeue:
            self.db.requeue_job(job_id)
            log.warning("Задание #%s возвращено в очередь для повтора.", job_id)
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
        except JobCancelled as exc:
            self.db.add_job_event(job_id, "WARNING", str(exc))
            self.db.finish_job(job_id, JobStatus.CANCELLED, error=str(exc))
            log.info("Задание #%s отменено.", job_id)
        except MailArchiverError as exc:
            self._handle_failure(job_id, job, exc, exc.retryable)
        except Exception as exc:  # noqa: BLE001
            log.exception("Задание #%s упало с ошибкой", job_id)
            self._handle_failure(job_id, job, exc, False)

    def _handle_failure(self, job_id: int, job: dict, exc: BaseException, retryable: bool) -> None:
        attempts = int(job.get("attempts") or 1)
        max_attempts = int(job.get("max_attempts") or 1)
        message = getattr(exc, "message", None) or str(exc)
        hint = getattr(exc, "hint", None)
        self.db.add_job_event(job_id, "ERROR", message + (f" | {hint}" if hint else ""))
        if retryable and attempts < max_attempts:
            try:
                delay = min(int(self.services.rt("backup", "retry_initial_delay_s") or 2) * attempts, 60)
            except (ValueError, TypeError):
                delay = 2 * attempts
            self.db.add_job_event(job_id, "WARNING",
                                  f"Повторная попытка через {delay} с (попытка {attempts + 1}/{max_attempts}).")
            time.sleep(delay)
            # НЕ ставим в очередь здесь — это сделает _on_done после снятия с учёта
            with self._lock:
                self._to_requeue.add(job_id)
        else:
            self.db.finish_job(job_id, JobStatus.FAILED, error=message)
            log.error("Задание #%s провалено: %s", job_id, message)
