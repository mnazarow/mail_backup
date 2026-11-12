"""
REST API веб-интерфейса. Все ответы — JSON. Ошибки предметной области
(:class:`MailArchiverError`) перехватываются глобально (см. app.py) и отдаются
с понятным пользователю сообщением и подсказкой.

С 1.22.0 маршруты разнесены по модулям ``api_*.py`` (по разделам интерфейса); этот модуль собирает их
в один ``router`` — в прежнем порядке, он важен для совпадающих путей — и отдаёт прежние имена
(``from .api import serialize_job`` и т. п.) для совместимости.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import (api_accounts, api_auth, api_common, api_employees, api_jobs, api_monitoring, api_settings,
               api_state, api_users)

#: Модули маршрутов в порядке разделов прежнего api.py.
MODULES = (api_common, api_auth, api_state, api_accounts, api_jobs, api_settings, api_monitoring,
           api_employees, api_users)

router = APIRouter()
for _mod in MODULES:
    if hasattr(_mod, "router"):
        router.include_router(_mod.router)
    # прежние имена модуля api (константы, проверки доступа, сериализация заданий) — как были
    globals().update({k: v for k, v in vars(_mod).items()
                      if not k.startswith("__") and k not in ("router", "annotations")})
del _mod
