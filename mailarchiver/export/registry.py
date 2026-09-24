"""
Реестр движков экспорта и выбор движка.
"""
from __future__ import annotations

from typing import Dict, List

from ..errors import ExportError
from .base import ExportEngine
from .eml_engine import EmlExportEngine
from .mbox_engine import MboxExportEngine
from .pst_aspose import AsposePstExportEngine
from .pst_native import NativePstExportEngine

_ENGINES = {
    "eml": EmlExportEngine,
    "mbox": MboxExportEngine,
    "aspose": AsposePstExportEngine,
    "native": NativePstExportEngine,
}

# Человекочитаемые описания для интерфейса
ENGINE_META = {
    "eml": {"fmt": "eml", "title": "EML (каталог .eml)", "reliable": True,
            "desc": "По файлу .eml на письмо, структура папок сохраняется. Надёжно, без потерь."},
    "mbox": {"fmt": "mbox", "title": "MBOX (.zip)", "reliable": True,
             "desc": "По одному mbox-файлу на папку, упаковка в .zip. Импортируется Thunderbird и конвертерами."},
    "aspose": {"fmt": "pst", "title": "PST через Aspose", "reliable": True,
               "desc": "Надёжный .pst для Outlook (ANSI/Unicode). Требуется Aspose.Email и лицензия для продакшена."},
    "native": {"fmt": "pst", "title": "PST встроенный (эксперим.)", "reliable": False,
               "desc": "Встроенный генератор .pst без зависимостей: папки, тема, адреса, дата и текст писем — "
                       "БЕЗ вложений. ЭКСПЕРИМЕНТАЛЬНО — проверяйте в своём Outlook; полная выгрузка с "
                       "вложениями — через Aspose или в EML/MBOX."},
}


def resolve_engine(name: str, fmt: str = "") -> ExportEngine:
    """
    Вернуть экземпляр движка по имени. Поддерживает 'auto':
      * auto + формат pst  -> aspose (если доступен), иначе native;
      * auto без формата   -> eml (надёжно и без зависимостей).
    """
    name = (name or "auto").lower()
    fmt = (fmt or "").lower()

    if name == "auto":
        if fmt == "pst":
            ok, _ = AsposePstExportEngine.available()
            name = "aspose" if ok else "native"
        elif fmt in ("mbox",):
            name = "mbox"
        else:
            name = "eml"

    cls = _ENGINES.get(name)
    if cls is None:
        raise ExportError(f"Неизвестный движок экспорта: {name}",
                          hint=f"Доступны: {', '.join(_ENGINES)} или 'auto'.")
    ok, reason = cls.available()
    if not ok:
        raise ExportError(f"Движок экспорта «{name}» недоступен: {reason}",
                          hint="Выберите другой движок (eml/mbox/native) или установите нужную зависимость.")
    return cls()


def list_engines() -> List[Dict]:
    out = []
    for key, cls in _ENGINES.items():
        ok, reason = cls.available()
        meta = ENGINE_META.get(key, {})
        out.append({
            "name": key,
            "fmt": meta.get("fmt", cls.fmt),
            "title": meta.get("title", key),
            "desc": meta.get("desc", ""),
            "reliable": meta.get("reliable", not cls.experimental),
            "experimental": cls.experimental,
            "available": ok,
            "reason": reason,
        })
    return out


def engine_availability() -> Dict[str, bool]:
    return {k: cls.available()[0] for k, cls in _ENGINES.items()}
