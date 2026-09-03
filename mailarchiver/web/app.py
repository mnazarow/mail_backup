"""
Сборка ASGI-приложения FastAPI: инициализация сервиса, статика, шаблоны,
маршруты API/WebSocket, обработка ошибок.

Запуск в проде — через uvicorn:
    uvicorn mailarchiver.web.app:app --host 127.0.0.1 --port 8493
Путь к конфигурации берётся из переменной окружения MAILARCHIVER_CONFIG.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..errors import MailArchiverError
from ..logging_setup import get_logger, setup_logging
from ..service import Services
from ..version import __version__, APP_TITLE
from .api import router as api_router
from .ws import router as ws_router

log = get_logger("web")

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_HERE, "static")
TEMPLATES_DIR = os.path.join(_HERE, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.environ.get("MAILARCHIVER_CONFIG"))
    setup_logging(cfg.log_dir, level=str(cfg.logging_cfg.get("level", "INFO")),
                  to_stdout=bool(cfg.logging_cfg.get("to_stdout", True)))
    services = Services(cfg)
    services.setup()
    services.start()
    app.state.services = services
    log.info("%s %s готов к работе (data_dir=%s)", APP_TITLE, __version__, cfg.data_dir)
    try:
        yield
    finally:
        services.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(api_router)
    app.include_router(ws_router)

    # --- обработчики ошибок ---
    @app.exception_handler(MailArchiverError)
    async def _domain_error(request: Request, exc: MailArchiverError):
        return JSONResponse(status_code=400, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Необработанная ошибка запроса %s", request.url.path)
        return JSONResponse(status_code=500,
                            content={"error": True, "code": "internal",
                                     "message": "Внутренняя ошибка сервера. Подробности в логах.",
                                     "hint": None})

    # --- корневые маршруты ---
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request, "version": __version__,
                                                          "title": APP_TITLE})

    @app.get("/health")
    async def health(request: Request):
        svc = getattr(request.app.state, "services", None)
        return {"status": "ok", "version": __version__,
                "scheduler": bool(svc and svc.scheduler.running()) if svc else False}

    @app.get("/favicon.ico")
    async def favicon():
        path = os.path.join(STATIC_DIR, "favicon.svg")
        if os.path.exists(path):
            from fastapi.responses import FileResponse
            return FileResponse(path, media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    return app


app = create_app()
