"""Импорт файлов Outlook .pst без внешних программ.

* :mod:`reader` — чтение формата [MS-PST] (пара к ``export.pstformat``);
* :mod:`rtf` — тело письма в сжатом RTF и HTML, спрятанный внутри него;
* :mod:`eml` — сборка письма RFC 822 из свойств MAPI;
* :mod:`importer` — заливка писем на сервер IMAP или в локальную копию ящика.
"""
from .reader import PstReadError, PstReader, PstUnsupportedError  # noqa: F401
