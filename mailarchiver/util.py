"""
Вспомогательные функции общего назначения.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from functools import wraps
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Время
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """Текущее время в UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def ts() -> float:
    return time.time()


def human_duration(seconds: float) -> str:
    """Человекочитаемая длительность: 3725 -> '1 ч 2 мин 5 с'."""
    seconds = int(max(0, seconds))
    if seconds < 1:
        return "меньше секунды"
    parts = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds and not days:
        parts.append(f"{seconds} с")
    return " ".join(parts) if parts else "0 с"


def human_size(num_bytes: float) -> str:
    """Человекочитаемый размер: 1536 -> '1.5 КБ'."""
    num = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"):
        if abs(num) < 1024.0:
            if unit == "Б":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} ЭБ"


# ---------------------------------------------------------------------------
# Файлы
# ---------------------------------------------------------------------------

def ensure_dir(path: str, mode: int = 0o700) -> str:
    """Создать директорию (со всеми родителями), если её нет."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def fsync_dir(path: str) -> None:
    """Сбросить на диск сам каталог (чтобы переименование пережило потерю питания).

    На части файловых систем и на Windows это не поддерживается — такой сбой
    не должен ронять уже выполненную запись, поэтому ошибки игнорируются.
    """
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600, fsync: bool = True) -> None:
    """Атомарная запись файла: сначала во временный, затем rename.

    При ``fsync=True`` на диск сбрасывается и содержимое файла, и запись
    каталога — иначе при потере питания переименование может не сохраниться.
    """
    directory = os.path.dirname(path) or "."
    ensure_dir(directory, 0o700)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        if fsync:
            fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, mode: int = 0o600, fsync: bool = True) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode, fsync=fsync)


_slug_re = re.compile(r"[^\w.\- ]+", re.UNICODE)


def safe_filename(name: str, max_len: int = 120, default: str = "item") -> str:
    """Превратить произвольную строку в безопасное имя файла."""
    name = unicodedata.normalize("NFKC", name or "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    name = _slug_re.sub("_", name)
    name = re.sub(r"_{2,}", "_", name).strip("_. ")
    if not name:
        name = default
    return name[:max_len]


def sanitize_folder_component(name: str) -> str:
    """Безопасное имя для компонента пути папки почты (сохраняем читаемость)."""
    name = (name or "").replace("\x00", "")
    name = name.replace("/", "⧸")  # визуально похожий слэш, чтобы не ломать путь
    for ch in ("\\", ":", "*", "?", '"', "<", ">", "|"):
        name = name.replace(ch, "_")
    name = name.strip()
    # Срезаем ведущие/хвостовые точки и пробелы: имена «.», «..» (и вида «..  »)
    # с сервера увели бы запись за пределы каталога ящика (path traversal).
    # Внутренние точки сохраняем — «Sent.2024» остаётся как есть.
    stripped = name.strip(". ")
    if not stripped or set(stripped) <= {"."}:
        return "_" if name else "INBOX"
    return stripped


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def disk_free_bytes(path: str) -> int:
    """Свободно байт на файловой системе, где лежит path."""
    target = path
    while target and not os.path.exists(target):
        target = os.path.dirname(target)
    if not target:
        target = "/"
    st = os.statvfs(target)
    return st.f_bavail * st.f_frsize


# ---------------------------------------------------------------------------
# Повторные попытки с экспоненциальной задержкой
# ---------------------------------------------------------------------------

def retry(
    attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
):
    """
    Декоратор повторных попыток с экспоненциальной задержкой.

    :param attempts:   максимальное число попыток;
    :param delay:      начальная задержка (сек);
    :param backoff:    множитель роста задержки;
    :param max_delay:  верхняя граница задержки;
    :param exceptions: какие исключения считать временными;
    :param on_retry:   колбэк (номер_попытки, ошибка, следующая_задержка).
    """

    # attempts=0 (или отрицательное) дало бы «ноль попыток» и падение на assert —
    # выполняем функцию хотя бы один раз.
    attempts = max(1, int(attempts))

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            cur_delay = delay
            last_exc: Optional[BaseException] = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt >= attempts:
                        break
                    if on_retry:
                        on_retry(attempt, exc, cur_delay)
                    time.sleep(cur_delay)
                    cur_delay = min(cur_delay * backoff, max_delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


def chunked(iterable: Iterable[T], size: int) -> Iterable[list]:
    """Разбить последовательность на куски по size элементов."""
    chunk: list = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def clamp(value, low, high):
    return max(low, min(high, value))


def parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да", "y"}
