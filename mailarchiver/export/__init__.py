"""
Экспорт локальных копий писем в разные форматы.

Движки:
  * eml   — каталог .eml-файлов (надёжно, без зависимостей);
  * mbox  — по одному mbox-файлу на папку, упаковка в .zip (надёжно);
  * pst   — .pst для Microsoft Outlook. Два движка:
              - aspose  (требует Aspose.Email + лицензию; ANSI и Unicode),
              - native  (встроенный, без зависимостей: Unicode PST с вложениями);
  * msg   — .msg-файлы Outlook (через Aspose).

Выбор движка — см. :func:`registry.resolve_engine`.
"""
from .base import MailItem, ExportEngine, ExportResult
from .registry import resolve_engine, list_engines, engine_availability

__all__ = [
    "MailItem",
    "ExportEngine",
    "ExportResult",
    "resolve_engine",
    "list_engines",
    "engine_availability",
]
