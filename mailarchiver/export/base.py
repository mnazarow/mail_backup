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


def zip_directory(dir_path: str, zip_path: str, *, arc_root: Optional[str] = None) -> int:
    """Упаковать каталог в zip. Возвращает размер архива в байтах."""
    root = arc_root or os.path.basename(dir_path.rstrip("/"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, _dirs, files in os.walk(dir_path):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, dir_path)
                zf.write(full, os.path.join(root, rel))
    return os.path.getsize(zip_path)


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
