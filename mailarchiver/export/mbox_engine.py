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
from typing import Dict, Iterable, Optional, TextIO

from ..util import ensure_dir
from .base import CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB, folder_to_fs

_FROM_RE = re.compile(rb"^(>*From )", re.MULTILINE)


class MboxExportEngine(ExportEngine):
    name = "mbox"
    fmt = "mbox"

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        ensure_dir(out_path, 0o700)
        result = ExportResult(path=out_path, is_dir=True, engine=self.name, fmt=self.fmt)
        handles: Dict[str, "os.PathLike"] = {}
        open_files: Dict[str, object] = {}
        try:
            for item in items:
                if cancel_cb and cancel_cb():
                    break
                rel = folder_to_fs(item.folder) + ".mbox"
                path = os.path.join(out_path, rel)
                fh = open_files.get(rel)
                if fh is None:
                    ensure_dir(os.path.dirname(path) or out_path, 0o700)
                    fh = open(path, "ab")
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
            for fh in open_files.values():
                try:
                    fh.close()
                except OSError:
                    pass
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
        if not body.endswith(b"\n"):
            body += b"\n"
        return header + body + b"\n"
