"""
Базовые типы движков экспорта.
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Tuple

ProgressCB = Callable[[int, int, str], None]     # (current, total, message)
CancelCB = Callable[[], bool]


@dataclass
class MailItem:
    """Одно письмо, подаваемое на экспорт."""
    folder: str
    raw: bytes
    flags: List[str] = field(default_factory=list)
    internaldate: Optional[float] = None   # epoch-секунды
    message_id: str = ""
    size: int = 0


@dataclass
class ExportResult:
    path: str = ""                 # путь к результату (файл или каталог)
    is_dir: bool = False
    count: int = 0                 # сколько писем экспортировано
    bytes_written: int = 0
    errors: int = 0
    error_details: List[str] = field(default_factory=list)
    engine: str = ""
    fmt: str = ""
    warning: str = ""              # напр. про экспериментальность или eval-режим
    #: если результат разбит на несколько файлов (например .pst по частям) —
    #: их пути; обработчик задания упакует их в один архив
    parts: List[str] = field(default_factory=list)


class ExportEngine:
    """Базовый класс движка экспорта."""

    name: str = "base"
    fmt: str = ""
    experimental: bool = False

    @classmethod
    def available(cls) -> Tuple[bool, str]:
        """(доступен ли движок, причина недоступности)."""
        return True, ""

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None,
               progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None,
               total_hint: int = 0) -> ExportResult:
        raise NotImplementedError


def _zip_entries(entries, zip_path: str, cancel_cb: Optional[CancelCB] = None) -> int:
    """Записать архив атомарно: во временный ``*.part``, затем переименовать.

    Недописанный архив (кончилось место, отмена) не остаётся лежать в каталоге
    выгрузок под «настоящим» именем — ``*.part`` убирается здесь же, а если
    процесс убит — при следующем запуске службы.
    """
    part = zip_path + ".part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
            for full, arcname in entries:
                if cancel_cb and cancel_cb():
                    from ..errors import JobCancelled
                    raise JobCancelled("Экспорт отменён пользователем.")
                zf.write(full, arcname)
        os.replace(part, zip_path)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    return os.path.getsize(zip_path)


def zip_files(paths: List[str], zip_path: str, *, arc_root: str = "",
              cancel_cb: Optional[CancelCB] = None) -> int:
    """Упаковать список файлов в zip (плоско, в каталог arc_root). Размер архива."""
    entries = []
    for full in paths:
        name = os.path.basename(full)
        entries.append((full, os.path.join(arc_root, name) if arc_root else name))
    return _zip_entries(entries, zip_path, cancel_cb)


def zip_directory(dir_path: str, zip_path: str, *, arc_root: Optional[str] = None,
                  cancel_cb: Optional[CancelCB] = None) -> int:
    """Упаковать каталог в zip. Возвращает размер архива в байтах."""
    root = arc_root or os.path.basename(dir_path.rstrip("/"))

    def entries():
        for base, _dirs, files in os.walk(dir_path):
            for fn in sorted(files):
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, dir_path)
                yield full, os.path.join(root, rel)

    return _zip_entries(entries(), zip_path, cancel_cb)


def folder_to_fs(folder: str) -> str:
    """IMAP-имя папки -> безопасный относительный путь для файлов экспорта."""
    from ..util import sanitize_folder_component
    parts = [sanitize_folder_component(p) for p in folder.replace("\\", "/").split("/") if p]
    return os.path.join(*parts) if parts else "INBOX"


def safe_export_path(base_dir: str, *parts: str) -> str:
    """
    Собрать путь внутри каталога экспорта и проверить (защита «в глубину»), что
    он действительно остался внутри него.

    Имя папки приходит с почтового сервера, поэтому одной очистки компонентов
    (sanitize_folder_component) мало: путь может увести наружу через символическую
    ссылку или неожиданную комбинацию разделителей. Сверяем реальные пути
    (realpath) — при выходе за пределы каталога бросаем ExportError, вызывающий
    движок обязан пропустить письмо и учесть ошибку.
    """
    from ..errors import ExportError

    target = os.path.normpath(os.path.join(base_dir, *parts))
    base_real = os.path.realpath(base_dir)
    # realpath самого файла: если он ещё не существует, разыменуются каталоги-родители
    target_real = os.path.realpath(target)
    try:
        inside = os.path.commonpath([base_real, target_real]) == base_real
    except ValueError:  # разные диски/несопоставимые пути
        inside = False
    if not inside:
        raise ExportError(
            f"Путь экспорта выходит за пределы каталога назначения: {target}",
            hint="Проверьте имена папок в ящике и отсутствие символических ссылок в каталоге экспорта.",
        )
    return target
