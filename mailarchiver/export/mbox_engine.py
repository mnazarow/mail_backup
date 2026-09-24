"""
Экспорт в формат mbox (по одному файлу на папку).

mbox — текстовый формат, где письма идут подряд, разделённые строкой,
начинающейся с ``From ``. Используется вариант mboxrd (экранирование строк
``From `` в теле как ``>From ``) — он наиболее совместим.

Итоговый каталог с *.mbox затем упаковывается в .zip уровнем выше (см. задание
экспорта). mbox напрямую импортируют Thunderbird, а также многие утилиты
конвертации в Outlook.
"""
from __future__ import annotations

import email.utils
import os
import re
import time
from collections import OrderedDict
from typing import Iterable, Optional

from ..errors import ExportError
from ..util import ensure_dir
from .base import (CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB, folder_to_fs,
                   safe_export_path)

_FROM_RE = re.compile(rb"^(>*From )", re.MULTILINE)


#: Сколько mbox-файлов держим открытыми одновременно.
MAX_OPEN_MBOX_FILES = 64


def _status_headers(flags) -> bytes:
    """Флаги IMAP → заголовки mbox ``Status``/``X-Status``.

    Status: R — прочитано, O — «старое» (не новое в ящике).
    X-Status: A — отвечено, F — помечено, D — удалено, T — черновик.
    """
    names = {str(f).lstrip("\\").lower() for f in (flags or [])}
    status = ("R" if "seen" in names else "") + "O"
    x = ""
    if "answered" in names:
        x += "A"
    if "flagged" in names:
        x += "F"
    if "deleted" in names:
        x += "D"
    if "draft" in names:
        x += "T"
    out = f"Status: {status}\n"
    if x:
        out += f"X-Status: {x}\n"
    return out.encode("ascii", "replace")


class MboxExportEngine(ExportEngine):
    name = "mbox"
    fmt = "mbox"

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        ensure_dir(out_path, 0o700)
        result = ExportResult(path=out_path, is_dir=True, engine=self.name, fmt=self.fmt)
        # OrderedDict: вытесняем ДАВНО не использованный файл, а не первый
        # открытый. Письма приходят вперемешку по дате, и при FIFO кэш
        # вырождался — на 100 папок было 5000 открытий вместо 100.
        open_files: "OrderedDict[str, object]" = OrderedDict()
        try:
            for item in items:
                if cancel_cb and cancel_cb():
                    break
                rel = folder_to_fs(item.folder) + ".mbox"
                fh = open_files.get(rel)
                if fh is not None:
                    open_files.move_to_end(rel)
                if fh is None and len(open_files) >= MAX_OPEN_MBOX_FILES:
                    # На ящике с сотнями папок держать по дескриптору на каждую
                    # — верный путь упереться в лимит открытых файлов. Закрываем
                    # самый давний: файлы открываются в режиме дозаписи.
                    old_rel, old_fh = next(iter(open_files.items()))
                    try:
                        old_fh.flush()
                        old_fh.close()
                    except (OSError, ValueError) as exc:
                        result.errors += 1
                        result.error_details.append(f"{old_rel}: {exc}")
                    open_files.pop(old_rel, None)
                if fh is None:
                    try:
                        # защита «в глубину»: путь обязан остаться внутри каталога экспорта
                        path = safe_export_path(out_path, rel)
                        ensure_dir(os.path.dirname(path) or out_path, 0o700)
                        fh = open(path, "ab")
                    except (ExportError, OSError) as exc:
                        result.errors += 1
                        result.error_details.append(f"{rel}: {exc}")
                        continue
                    open_files[rel] = fh
                try:
                    fh.write(self._mbox_record(item))
                    result.count += 1
                    result.bytes_written += len(item.raw)
                except OSError as exc:
                    result.errors += 1
                    result.error_details.append(f"{rel}: {exc}")
                if progress_cb and result.count % 25 == 0:
                    progress_cb(result.count, total_hint, f"mbox: {result.count}")
        finally:
            # Ошибки close() глотать нельзя: именно на flush/close вылезает
            # «кончилось место», и обрезанный mbox иначе уехал бы как успешный.
            for rel, fh in open_files.items():
                problem = ""
                try:
                    fh.flush()
                except (OSError, ValueError) as exc:
                    problem = f"не удалось записать данные на диск: {exc}"
                try:
                    fh.close()
                except (OSError, ValueError) as exc:
                    if not problem:
                        problem = f"ошибка закрытия файла: {exc}"
                if problem:
                    result.errors += 1
                    result.error_details.append(f"{rel}: {problem}")
        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "mbox: готово")
        return result

    @staticmethod
    def _mbox_record(item: MailItem) -> bytes:
        # Заголовок-разделитель "From sender date"
        sender = "MAILER-DAEMON"
        try:
            import email as _email
            m = _email.message_from_bytes(item.raw[:8192])
            frm = m.get("From", "")
            addr = email.utils.parseaddr(frm)[1]
            if addr:
                sender = addr
        except Exception:  # noqa: BLE001
            pass
        when = time.gmtime(item.internaldate or time.time())
        date_str = time.strftime("%a %b %d %H:%M:%S %Y", when)
        header = f"From {sender} {date_str}\n".encode("utf-8", "replace")
        # mboxrd: экранируем строки, начинающиеся с (>*)From
        body = _FROM_RE.sub(rb">\1", item.raw)
        status = _status_headers(item.flags)
        if status:
            # Status/X-Status — стандартный способ mbox хранить флаги письма.
            # Без них «прочитано», «отвечено» и «помечено» терялись при экспорте.
            body = status + body
        if not body.endswith(b"\n"):
            body += b"\n"
        return header + body + b"\n"
