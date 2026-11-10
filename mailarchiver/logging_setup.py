"""
Настройка логирования MailArchiver.

Логи пишутся одновременно:
  * в файл с ротацией (data/logs/mailarchiver.log);
  * в stdout (для journald/systemd и Docker);
  * в кольцевой буфер в памяти (последние N записей — для показа в веб-интерфейсе).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from collections import deque
from contextlib import contextmanager
from typing import Deque, Dict, Iterable, List, Optional

from .util import ensure_dir

_MEMORY_CAPACITY = 2000

#: Вид задания, которое выполняет текущий поток (см. job_log_scope).
_job_scope = threading.local()


@contextmanager
def job_log_scope(job_type: Optional[str]):
    """Всё, что поток запишет в журнал внутри блока, помечается видом задания.

    По этой метке журнал в интерфейсе скрывает от оператора записи заданий, подробности которых ему
    не положены (выгрузки, восстановление, анализ писем…): в них бывают темы писем и пути к файлам.
    """
    previous = getattr(_job_scope, "job_type", None)
    _job_scope.job_type = job_type
    try:
        yield
    finally:
        _job_scope.job_type = previous


def visible_to(records: Iterable[Dict], job_types: Iterable[str]) -> List[Dict]:
    """Записи журнала без тех, что сделаны заданиями не из ``job_types`` (записи вне заданий остаются)."""
    allowed = set(job_types)
    return [r for r in records if not r.get("job_type") or r["job_type"] in allowed]


class MemoryLogHandler(logging.Handler):
    """Хранит последние записи лога в памяти для отображения в интерфейсе."""

    def __init__(self, capacity: int = _MEMORY_CAPACITY) -> None:
        super().__init__()
        self.buffer: Deque[Dict] = deque(maxlen=capacity)
        # Сквозной номер записи: интерфейс дописывает только новые строки, а не
        # перерисовывает весь журнал (иначе выделение текста сбрасывалось каждые
        # полторы секунды и скопировать строку было невозможно).
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        # emit вызывается под блокировкой обработчика (Handler.handle), поэтому
        # счётчик увеличивается без гонок.
        try:
            self._seq += 1
            self.buffer.append(
                {
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": self.format(record),
                    "job_type": getattr(_job_scope, "job_type", None),
                }
            )
        except Exception:  # pragma: no cover - защита от рекурсии
            pass

    def tail(self, limit: int = 200, level: str | None = None,
             job_types: Optional[Iterable[str]] = None) -> List[Dict]:
        """``job_types`` — показать записи только этих видов заданий (и записи вне заданий)."""
        items = list(self.buffer)
        if job_types is not None:
            items = visible_to(items, job_types)
        if level:
            level = level.upper()
            order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
            threshold = order.get(level, 0)
            items = [i for i in items if order.get(i["level"], 0) >= threshold]
        return items[-limit:]


memory_handler = MemoryLogHandler()

_CONFIGURED = False


def setup_logging(log_dir: str, level: str = "INFO", to_stdout: bool = True) -> None:
    """Однократно настроить корневой логгер приложения."""
    global _CONFIGURED
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    # Уровень настроен пользователем — применяем его, иначе в буфер и в файл
    # попадало бы всё подряд, включая DEBUG.
    root.setLevel(numeric_level)

    # Удаляем существующие хендлеры (важно при перезапуске в тестах/reload)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Файл с ротацией
    try:
        ensure_dir(log_dir, 0o700)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "mailarchiver.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # не падаем, если каталог логов недоступен
        sys.stderr.write(f"ВНИМАНИЕ: не удалось открыть файл логов: {exc}\n")

    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(numeric_level)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    memory_handler.setLevel(numeric_level)
    memory_handler.setFormatter(fmt)
    root.addHandler(memory_handler)

    # Приглушаем слишком болтливые сторонние логгеры
    for noisy in ("apscheduler.scheduler", "apscheduler.executors.default", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger("mailarchiver").info("Логирование инициализировано, уровень=%s", level)


def set_level(level: str) -> None:
    """Сменить уровень журнала на лету — и у корневого логгера, и у ОБРАБОТЧИКОВ.

    Раньше менялся только уровень логгера «mailarchiver», а обработчики (файл,
    stdout, буфер для раздела «Логи») оставались на уровне из config.yaml:
    выбор DEBUG в «Настройках» ничего не добавлял в журнал.
    """
    numeric = getattr(logging, str(level or "INFO").upper(), None)
    if not isinstance(numeric, int):
        return
    root = logging.getLogger()
    root.setLevel(numeric)
    logging.getLogger("mailarchiver").setLevel(numeric)
    for handler in root.handlers:
        handler.setLevel(numeric)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("mailarchiver." + name if not name.startswith("mailarchiver") else name)
