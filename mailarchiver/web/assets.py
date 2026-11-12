"""Файлы веб-интерфейса (1.22.0).

Интерфейс — обычные (не модульные) скрипты с общей глобальной областью: страница подключает их по порядку
этого списка, а последний запускает интерфейс. Код, выполняемый при загрузке файла, может обращаться только
к своему и предыдущим файлам; функции, вызываемые позже (по щелчку, при переходе в раздел), — к любым.
"""
from __future__ import annotations

import os

JS_FILES = ("app.js", "admin.js", "bulk.js", "ops.js", "settings.js", "docs.js", "mail.js", "mail-settings.js",
            "analytics.js", "axigen.js", "mailservers.js")
CSS_FILES = ("app.css",)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def bundle_text() -> str:
    """Все файлы интерфейса одним текстом, по порядку (для проверок и линтера)."""
    parts = []
    for name in JS_FILES:
        with open(os.path.join(STATIC_DIR, "js", name), encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n".join(parts)
