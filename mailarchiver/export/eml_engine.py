"""
Экспорт в каталог .eml-файлов (по файлу на письмо), с сохранением структуры
папок. Формат .eml — это просто исходный текст письма (RFC 822), поэтому
экспорт полностью без потерь и без внешних зависимостей.

Такой каталог можно:
  * открыть/перетащить в Outlook (классический) по одному письму;
  * импортировать сторонними утилитами;
  * хранить как надёжную «вечную» копию.
"""
from __future__ import annotations

import email
import os
from email.header import decode_header, make_header
from typing import Iterable, Optional

from ..util import ensure_dir, safe_filename
from .base import CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB, folder_to_fs


def extract_subject(raw: bytes) -> str:
    try:
        msg = email.message_from_bytes(raw[:16384])
        subj = msg.get("Subject", "")
        return str(make_header(decode_header(subj))) if subj else ""
    except Exception:  # noqa: BLE001
        return ""


class EmlExportEngine(ExportEngine):
    name = "eml"
    fmt = "eml"

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        ensure_dir(out_path, 0o700)
        result = ExportResult(path=out_path, is_dir=True, engine=self.name, fmt=self.fmt)
        counters: dict = {}
        for item in items:
            if cancel_cb and cancel_cb():
                break
            folder_dir = os.path.join(out_path, folder_to_fs(item.folder))
            ensure_dir(folder_dir, 0o700)
            counters[item.folder] = counters.get(item.folder, 0) + 1
            seq = counters[item.folder]
            subject = extract_subject(item.raw)
            fname = f"{seq:06d}_{safe_filename(subject or 'no_subject', max_len=60)}.eml"
            try:
                with open(os.path.join(folder_dir, fname), "wb") as fh:
                    fh.write(item.raw)
                result.count += 1
                result.bytes_written += len(item.raw)
            except OSError as exc:
                result.errors += 1
                result.error_details.append(f"{item.folder}/{fname}: {exc}")
            if progress_cb and result.count % 25 == 0:
                progress_cb(result.count, total_hint, f"EML: {result.count}")
        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "EML: готово")
        return result
