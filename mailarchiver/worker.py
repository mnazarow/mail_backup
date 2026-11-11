"""
Очередь заданий и планировщик в отдельном процессе (``server.worker_process: true``).

Зачем. Копирование, выгрузки, импорт .pst и прочие задания выполняются потоками Python. В одном процессе с
веб-сервером они делят с ним GIL: пока идут четыре копии ящиков, каждый запрос интерфейса ждёт своей
очереди к интерпретатору, и интерфейс заметно тормозит. В отдельном процессе у заданий свой интерпретатор.

Как устроено. Служба (``mailarchiver serve``) по-прежнему одна — один unit systemd, один контейнер. При
запуске веб-процесс сам запускает дочерний процесс «mailarchiver-worker» (multiprocessing, способ spawn),
и в нём работают очередь и планировщик. Общее у процессов — база SQLite и каталог данных: задания
ставятся в очередь записью в базу (её дочерний процесс опрашивает раз в секунду, а по уведомлению —
сразу), ход и итог заданий тоже читаются из базы. То, что живёт только в памяти очереди (какие задания
выполняются прямо сейчас, какие ящики заняты, когда сработают расписания), веб-процесс спрашивает у
дочернего по каналу (multiprocessing.Pipe) — см. :class:`QueueProxy` и :class:`SchedulerProxy`, которые
подменяют ``services.queue`` и ``services.scheduler`` в веб-процессе.

Журнал. Записи дочернего процесса передаются в веб-процесс (QueueHandler → QueueListener) и попадают в те
же обработчики: файл, stdout, буфер раздела «Логи». Файл журнала пишет только веб-процесс — два процесса,
вращающие один файл, теряли бы записи.

Сбой. Если дочерний процесс завершился сам (нехватка памяти, ошибка в библиотеке), веб-процесс пишет об
этом в журнал, возвращает его задания в очередь и запускает его снова (через 5 с, при частых падениях —
через минуту). Остановка службы останавливает дочерний процесс так же мягко, как прежде очередь: идущие
задания возвращаются в очередь и продолжатся после запуска.
"""
from __future__ import annotations

import itertools
import logging
import logging.handlers
import multiprocessing
import os
import signal
import threading
import time
from typing import Any, Dict, List, Optional

from .errors import MailArchiverError, ValidationError
from .logging_setup import JobTypeFilter, get_logger

log = get_logger("worker")

#: Сколько ждать готовности дочернего процесса при запуске (секунды): первый запуск новой версии
#: может обновлять базу — это делает веб-процесс до запуска дочернего, так что хватает и минуты.
START_TIMEOUT_S = 120.0
#: Сколько ждать мягкой остановки дочернего процесса (очередь ждёт задания до 20 с).
STOP_TIMEOUT_S = 25.0
#: После вызова без ответа следующие вызовы столько секунд сразу получают «недоступен»: зависший процесс
#: не должен задерживать каждый запрос интерфейса на весь срок ожидания.
UNRESPONSIVE_HOLD_S = 10.0


#: Переменная окружения, которая важнее ``server.worker_process`` (в Docker конфигурации обычно нет).
WORKER_ENV = "MAILARCHIVER_WORKER_PROCESS"


def worker_process_enabled(cfg) -> bool:
    """Включён ли режим отдельного процесса: переменная :data:`WORKER_ENV` (1/0) или ``server.worker_process``
    из config.yaml. Меняется перезапуском службы."""
    env = os.environ.get(WORKER_ENV, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        return bool((cfg.server or {}).get("worker_process", False))
    except AttributeError:
        return False


class WorkerUnavailable(MailArchiverError):
    code = "worker_unavailable"


class WorkerHost:
    """Дочерний процесс с очередью и планировщиком — со стороны веб-процесса."""

    def __init__(self, services) -> None:
        self.services = services
        self._ctx = multiprocessing.get_context("spawn")
        self._proc = None
        self._conn = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._stopping = threading.Event()
        self._supervisor: Optional[threading.Thread] = None
        self._log_queue = None
        self._listener: Optional[logging.handlers.QueueListener] = None
        self.restarts = 0
        self.pid: Optional[int] = None
        self._unresponsive_until = 0.0

    # -- жизненный цикл ----------------------------------------------------------------------------
    def start(self) -> None:
        self._stopping.clear()
        self._log_queue = self._ctx.Queue()
        root = logging.getLogger()
        self._listener = logging.handlers.QueueListener(self._log_queue, *root.handlers,
                                                        respect_handler_level=True)
        self._listener.start()
        self._spawn()
        self._supervisor = threading.Thread(target=self._supervise, name="worker-supervisor", daemon=True)
        self._supervisor.start()

    def _spawn(self) -> None:
        parent, child = self._ctx.Pipe()
        source = getattr(self.services.cfg, "source_path", None)
        proc = self._ctx.Process(target=worker_main, name="mailarchiver-worker",
                                 args=(source, child, self._log_queue, logging.getLogger().level))
        proc.start()
        child.close()
        with self._lock:
            self._proc, self._conn = proc, parent
            self._unresponsive_until = 0.0
        if parent.poll(START_TIMEOUT_S):
            try:
                msg = parent.recv()
            except (EOFError, OSError):
                msg = None
            if msg and msg[0] == "ready":
                self.pid = int(msg[1])
                log.info("Очередь заданий и расписания работают в отдельном процессе (pid %s).", self.pid)
                return
            if msg and msg[0] == "failed":
                log.error("Процесс заданий не запустился: %s", msg[1])
                return
        log.error("Процесс заданий не ответил за %.0f с после запуска.", START_TIMEOUT_S)

    def _supervise(self) -> None:
        recent: List[float] = []
        while not self._stopping.wait(1.0):
            proc = self._proc
            if proc is None or proc.is_alive():
                continue
            code = proc.exitcode
            now = time.monotonic()
            recent = [t for t in recent if now - t < 600] + [now]
            delay = 5.0 if len(recent) <= 3 else 60.0
            if code == 0:
                # остановлен сигналом (systemd шлёт SIGTERM всем процессам службы сразу): если это остановка
                # службы, веб-процесс тоже вот-вот остановится — и перезапуска не будет
                log.warning("Процесс заданий остановлен извне. Если служба продолжит работу, он будет "
                            "запущен снова через %.0f с.", delay)
            else:
                log.error("Процесс заданий завершился сам (код %s). Его задания возвращаются в очередь, "
                          "процесс будет запущен снова через %.0f с.", code, delay)
            try:
                self.services.db.reset_orphan_jobs()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось вернуть в очередь задания упавшего процесса")
            if self._stopping.wait(delay):
                return
            self.restarts += 1
            try:
                self._spawn()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось запустить процесс заданий снова")

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None and proc.is_alive():
            try:
                self.notify("stop")
            except Exception:  # noqa: BLE001
                pass
            proc.join(timeout)
            if proc.is_alive():
                log.warning("Процесс заданий не остановился за %.0f с — завершаю принудительно.", timeout)
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join(2)
        if self._supervisor is not None:
            self._supervisor.join(timeout=2)
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None
        with self._lock:
            conn, self._conn = self._conn, None
        for closer in (getattr(conn, "close", None), getattr(self._log_queue, "close", None),
                       getattr(self._log_queue, "join_thread", None)):
            try:
                if closer is not None:
                    closer()
            except Exception:  # noqa: BLE001
                pass
        self._log_queue = None
        log.info("Процесс заданий остановлен.")

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.is_alive() and not self._stopping.is_set()

    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    # -- обращения к дочернему процессу --------------------------------------------------------------
    def notify(self, name: str, *args: Any) -> None:
        """Сообщение без ответа (разбудить очередь, перечитать расписания и настройки, остановиться)."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.send(("notify", name, args))
            except (OSError, EOFError, ValueError) as exc:
                log.debug("Процесс заданий не принял уведомление %s: %s", name, exc)

    def call(self, name: str, *args: Any, timeout: float = 10.0) -> Any:
        """Вызов с ответом. Нет процесса или он не ответил — :class:`WorkerUnavailable`."""
        if not self.alive():
            raise WorkerUnavailable("Процесс заданий не работает.")
        if time.monotonic() < self._unresponsive_until:
            raise WorkerUnavailable("Процесс заданий не отвечает.")
        # канал один: вызовы идут по очереди, но ждать очереди дольше срока вызова незачем
        if not self._lock.acquire(timeout=timeout):
            raise WorkerUnavailable(f"Процесс заданий не ответил за {timeout:.0f} с.")
        try:
            conn = self._conn
            req = next(self._ids)
            try:
                conn.send(("call", req, name, args))
                deadline = time.monotonic() + timeout
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0 or not conn.poll(left):
                        self._unresponsive_until = time.monotonic() + UNRESPONSIVE_HOLD_S
                        log.warning("Процесс заданий не ответил на запрос %s за %.0f с.", name, timeout)
                        raise WorkerUnavailable(f"Процесс заданий не ответил за {timeout:.0f} с.")
                    msg = conn.recv()
                    if msg[0] == "result" and msg[1] == req:
                        break
                    # ответ на прежний, не дождавшийся вызов — пропускаем
            except (OSError, EOFError) as exc:
                raise WorkerUnavailable(f"Связь с процессом заданий прервалась: {exc}") from exc
        finally:
            self._lock.release()
        _tag, _req, ok, payload = msg
        if ok:
            return payload
        kind, message, hint = payload
        if kind == "ValidationError":
            raise ValidationError(message, hint=hint)
        raise MailArchiverError(message, hint=hint)


class QueueProxy:
    """``services.queue`` веб-процесса, когда очередь работает в отдельном процессе."""

    def __init__(self, host: WorkerHost, services) -> None:
        from .queue.manager import QueueManager
        self.host = host
        self.services = services
        self.db = services.db
        # Не запускается: нужен для того, что считается по базе и настройкам (размер пула, «Повторить»).
        self._local = QueueManager(services)

    def start(self) -> None:          # очередь запускает дочерний процесс
        pass

    def stop(self, timeout: float = 20.0) -> None:
        pass

    def max_workers(self) -> int:
        return self._local.max_workers()

    def per_account_limit(self) -> int:
        return 1

    def is_shutting_down(self) -> bool:
        return self.host.is_stopping()

    def enqueue(self, job_type: str, account_id: Optional[int], params: Optional[dict] = None,
                priority: int = 5, max_attempts: int = 1, created_by: str = "") -> int:
        job_id = self.db.enqueue_job(job_type, account_id, params or {}, priority, max_attempts, created_by)
        self.host.notify("wake")
        log.info("Задание #%s (%s) поставлено в очередь.", job_id, job_type)
        return job_id

    def cancel(self, job_id: int) -> None:
        try:
            self.host.call("cancel", int(job_id))
        except WorkerUnavailable:
            # процесса нет — ничего и не выполняется: ждущее отменяется сразу, «выполняющееся» в базе
            # получает флаг отмены и закроется отменённым при запуске процесса (reset_orphan_jobs)
            self._local.cancel(int(job_id))

    def retry(self, job_id: int) -> None:
        self._local.retry(int(job_id))        # только база
        self.host.notify("wake")

    def held_accounts(self) -> Dict[int, int]:
        try:
            return {int(k): int(v) for k, v in (self.host.call("held_accounts", timeout=5) or {}).items()}
        except WorkerUnavailable:
            # процесс не отвечает — заняты те ящики, по которым в базе идут задания
            return self._busy_from_db()

    def _busy_from_db(self) -> Dict[int, int]:
        return {int(j["account_id"]): int(j["id"]) for j in self.db.active_jobs()
                if j["status"] == "running" and j["account_id"] is not None}

    def acquire_account(self, account_id: int, job_id: int) -> bool:
        try:
            return bool(self.host.call("acquire_account", int(account_id), int(job_id), timeout=5))
        except WorkerUnavailable:
            # процесс заданий не отвечает: свободен ли ящик — по заданиям в базе
            return int(account_id) not in self._busy_from_db()

    def release_account(self, account_id: int, job_id: Optional[int] = None) -> None:
        try:
            self.host.call("release_account", int(account_id), job_id, timeout=5)
        except WorkerUnavailable:
            pass

    def running_ids(self) -> List[int]:
        try:
            return list(self.host.call("running_ids", timeout=5) or [])
        except WorkerUnavailable:
            return []

    def running_snapshot(self) -> List[dict]:
        try:
            return list(self.host.call("running_snapshot", timeout=5) or [])
        except WorkerUnavailable:
            return []


class SchedulerProxy:
    """``services.scheduler`` веб-процесса, когда планировщик работает в отдельном процессе."""

    #: «Планировщик активен» спрашивается живым обновлением каждые полторы секунды — ответ держим 3 с.
    _CACHE_S = 3.0

    def __init__(self, host: WorkerHost) -> None:
        self.host = host
        self._running = (0.0, False)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def running(self) -> bool:
        at, value = self._running
        if time.monotonic() - at < self._CACHE_S:
            return value
        try:
            value = bool(self.host.call("sched_running", timeout=2))
        except WorkerUnavailable:
            value = False
        self._running = (time.monotonic(), value)
        return value

    def reload(self, catch_up: bool = False) -> None:
        # без ответа: на тысячах расписаний перечитывание идёт секунды, а ждать его запросу незачем
        self.host.notify("reload", bool(catch_up))
        self._running = (0.0, False)

    def next_run_of(self, job_id: str) -> Optional[str]:
        try:
            return self.host.call("next_run_of", job_id, timeout=5)
        except WorkerUnavailable:
            return None

    def next_run_for(self, schedule_id: int) -> Optional[str]:
        try:
            return self.host.call("next_run_for", int(schedule_id), timeout=5)
        except WorkerUnavailable:
            return None


# =====================================================================================================
#  Дочерний процесс
# =====================================================================================================
def _setup_child_logging(log_queue, level: int) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.handlers.QueueHandler(log_queue)
    # вид задания, выполняемого потоком, — в саму запись: в веб-процессе потока задания нет
    handler.addFilter(JobTypeFilter())
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("apscheduler.scheduler", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _dispatch_call(svc, name: str, args) -> Any:
    queue, sched = svc.queue, svc.scheduler
    if name == "ping":
        return os.getpid()
    if name == "cancel":
        return queue.cancel(*args)
    if name == "held_accounts":
        return queue.held_accounts()
    if name == "acquire_account":
        return queue.acquire_account(*args)
    if name == "release_account":
        return queue.release_account(*args)
    if name == "running_ids":
        return queue.running_ids()
    if name == "running_snapshot":
        return queue.running_snapshot()
    if name == "sched_running":
        return sched.running()
    if name == "next_run_of":
        return sched.next_run_of(*args)
    if name == "next_run_for":
        return sched.next_run_for(*args)
    raise MailArchiverError(f"Неизвестная команда процесса заданий: {name}")


_background_lock = threading.Lock()


def _in_background(what: str, fn, *args) -> None:
    """Перечитать настройки и расписания — не в цикле команд: на тысячах расписаний это секунды, а
    вопросы веб-процесса («какие ящики заняты») всё это время ждали бы ответа. Друг за другом — под замком."""
    def run():
        with _background_lock:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при обработке команды %s", what)
    threading.Thread(target=run, name=f"worker-{what}", daemon=True).start()


def _reload(svc, catch_up: bool = False) -> None:
    svc.apply_runtime_settings()
    svc.scheduler.reload(catch_up)


def _dispatch_notify(svc, name: str, args) -> bool:
    """→ False, если пора останавливаться."""
    if name == "stop":
        return False
    if name == "wake":
        svc.queue._wake.set()                      # noqa: SLF001 — тот же процесс-владелец очереди
    elif name == "settings":
        _in_background(name, svc.apply_runtime_settings)
    elif name == "reload":
        _in_background(name, _reload, svc, *args)
    return True


def worker_main(config_path: Optional[str], conn, log_queue, level: int) -> None:
    """Точка входа дочернего процесса: очередь и планировщик, команды веб-процесса — по ``conn``."""
    _setup_child_logging(log_queue, level)
    stop = threading.Event()
    # systemd при остановке службы шлёт SIGTERM всем её процессам: останавливаемся мягко, как по команде
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl-C в консоли останавливает веб-процесс, а он — нас
    parent = os.getppid()
    try:
        from .config import load_config
        from .service import Services
        cfg = load_config(config_path)
        svc = Services(cfg)
        svc.role = "worker"
        svc.setup()
        svc.queue.start()
        svc.scheduler.start()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("mailarchiver.worker").exception("Процесс заданий не запустился")
        try:
            conn.send(("failed", str(exc)))
        except (OSError, EOFError):
            pass
        return
    conn.send(("ready", os.getpid()))
    try:
        while not stop.is_set():
            if os.getppid() != parent:
                log.error("Веб-процесс службы завершился — процесс заданий останавливается.")
                break
            if not conn.poll(0.5):
                continue
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            kind = msg[0]
            if kind == "notify":
                try:
                    if not _dispatch_notify(svc, msg[1], msg[2]):
                        break
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка при обработке команды %s", msg[1])
            elif kind == "call":
                _tag, req, name, args = msg
                try:
                    reply = ("result", req, True, _dispatch_call(svc, name, args))
                except MailArchiverError as exc:
                    reply = ("result", req, False, (type(exc).__name__, exc.message, exc.hint))
                except Exception as exc:  # noqa: BLE001
                    log.exception("Ошибка при обработке запроса %s", name)
                    reply = ("result", req, False, ("RuntimeError", str(exc), None))
                try:
                    conn.send(reply)
                except (OSError, EOFError):
                    break
    finally:
        try:
            svc.scheduler.stop()
        finally:
            svc.queue.stop()
