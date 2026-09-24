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

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..errors import MailArchiverError
from ..logging_setup import get_logger, setup_logging
from ..service import Services
from ..version import __version__, APP_TITLE
from . import auth as auth_mod
from .api import router as api_router
from .proxy import ProxyHeadersMiddleware, SecurityHeadersMiddleware
from .ws import router as ws_router

log = get_logger("web")


def _asset_tag() -> str:
    """Отпечаток файлов интерфейса — для ?v=… в ссылках на них (сброс кеша)."""
    import hashlib
    digest = hashlib.sha256()
    for rel in ("js/app.js", "css/app.css"):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", rel), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(rel.encode())
    return digest.hexdigest()[:12]

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_HERE, "static")
TEMPLATES_DIR = os.path.join(_HERE, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _starlette_request_first() -> bool:
    """Определяет, требует ли установленный Starlette request первым аргументом.

    Начиная со Starlette 0.29 у Jinja2Templates.TemplateResponse изменилась
    сигнатура: request передаётся первым (TemplateResponse(request, name, context)).
    В более старых версиях первым идёт имя шаблона, а request кладут в контекст.
    """
    try:
        import starlette
        major, minor = (int(p) for p in starlette.__version__.split(".")[:2])
        return (major, minor) >= (0, 29)
    except Exception:  # noqa: BLE001
        return True


_REQUEST_FIRST = _starlette_request_first()


def render_template(request: Request, name: str, context: dict):
    """Отрисовать HTML-шаблон, поддерживая обе сигнатуры Starlette.

    Без этого на новых версиях Starlette вызов вида
    ``TemplateResponse(name, {"request": ...})`` трактуется как
    ``TemplateResponse(request=name, name={...})`` — именем шаблона становится
    словарь контекста, и Jinja2 падает с «unhashable type: dict».
    """
    if _REQUEST_FIRST:
        return templates.TemplateResponse(request, name, context)
    ctx = dict(context)
    ctx.setdefault("request", request)
    return templates.TemplateResponse(name, ctx)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.environ.get("MAILARCHIVER_CONFIG"))
    setup_logging(cfg.log_dir, level=str(cfg.logging_cfg.get("level", "INFO")),
                  to_stdout=bool(cfg.logging_cfg.get("to_stdout", True)))
    services = Services(cfg)
    # ключ шифрования (если шифрование включено в config.yaml, а ключа ещё нет)
    # создаёт только сама служба — от своего пользователя
    services.setup(generate_key=True)
    services.start()
    app.state.services = services
    for warning in getattr(cfg, "warnings", []) or []:
        log.warning("Конфигурация: %s", warning)
    log.info("%s %s готов к работе (data_dir=%s)", APP_TITLE, __version__, cfg.data_dir)
    try:
        yield
    finally:
        services.stop()


def create_app() -> FastAPI:
    # Встроенные маршруты документации отключены: /api/docs, /api/openapi.json и
    # /redoc отдавали полную схему API кому угодно без входа. Ниже они заведены
    # заново — под правами администратора.
    app = FastAPI(title=APP_TITLE, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Промежуточные слои. Порядок важен: сначала подставляется настоящий адрес
    # клиента (его читают аудит и защита от подбора пароля), затем проверяется
    # источник запроса и навешиваются защитные заголовки.
    _cfg = load_config(os.environ.get("MAILARCHIVER_CONFIG"))

    def _rt(key: str, default):
        """Значение параметра сервера: из БД, если задано, иначе из config.yaml."""
        svc = getattr(app.state, "services", None)
        if svc is not None:
            try:
                return svc.rt("server", key)
            except Exception:  # noqa: BLE001
                pass
        return _cfg.server.get(key, default)

    def _public_url() -> str:
        return str(_rt("public_url", "") or "")

    def _proxy_settings():
        return (_rt("trusted_proxies", "127.0.0.1, ::1"), bool(_rt("behind_proxy", False)))

    def _auth_enabled() -> bool:
        svc = getattr(app.state, "services", None)
        if svc is not None:
            try:
                return bool(svc.rt("security", "auth_enabled"))
            except Exception:  # noqa: BLE001
                pass
        return bool(_cfg.get("security", "auth_enabled", True))

    app.add_middleware(SecurityHeadersMiddleware, public_url_getter=_public_url,
                       auth_enabled_getter=_auth_enabled)
    app.add_middleware(ProxyHeadersMiddleware, settings_getter=_proxy_settings,
                       trusted=_cfg.server.get("trusted_proxies", "127.0.0.1, ::1"),
                       enabled=bool(_cfg.server.get("behind_proxy", False)))

    app.include_router(api_router)
    app.include_router(ws_router)

    # --- документация API (только администратор) ---
    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(user: dict = Depends(auth_mod.require_admin)):
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(user: dict = Depends(auth_mod.require_admin)):
        return get_swagger_ui_html(openapi_url="/api/openapi.json",
                                   title=f"{APP_TITLE} — API")

    # --- обработчики ошибок ---
    @app.exception_handler(MailArchiverError)
    async def _domain_error(request: Request, exc: MailArchiverError):
        return JSONResponse(status_code=400, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError):
        # Ответ валидации FastAPI — английский массив; интерфейс показывал из
        # него только «Ошибка 422». Отдаём понятный текст (и детали для API).
        parts = []
        for err in list(exc.errors())[:5]:
            loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
            kind = str(err.get("type", ""))
            if "too_long" in kind or "max_length" in kind:
                text = "слишком длинное значение"
            elif "missing" in kind:
                text = "не указано"
            elif kind.startswith(("int", "float", "bool")) or "parsing" in kind:
                text = "неверный тип значения"
            else:
                text = "некорректное значение"
            parts.append(f"{loc or 'запрос'} — {text}")
        return JSONResponse(status_code=422, content={
            "error": True, "code": "validation_error",
            "message": "Некорректные данные запроса: " + "; ".join(parts) + ".",
            "hint": None, "detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(OverflowError)
    async def _overflow(request: Request, exc: OverflowError):
        # Число вне 64 бит (например, /api/jobs/100000000000000000000) — ошибка
        # запроса, а не сервера: раньше это давало 500 и трассировку в журнале.
        return JSONResponse(status_code=400, content={
            "error": True, "code": "validation_error",
            "message": "Слишком большое число в запросе.", "hint": None})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Необработанная ошибка запроса %s", request.url.path)
        return JSONResponse(status_code=500,
                            content={"error": True, "code": "internal",
                                     "message": "Внутренняя ошибка сервера. Подробности в логах.",
                                     "hint": None})

    # --- корневые маршруты ---
    asset_tag = _asset_tag()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        # Версию анониму не отдаём (как и /health): для сброса кеша браузера
        # хватает отпечатка самих файлов интерфейса.
        return render_template(request, "index.html", {"asset": asset_tag, "title": APP_TITLE})

    @app.get("/health")
    async def health(request: Request):
        """Проба живости для systemd/Docker/балансировщика.

        Версия сервиса и состояние планировщика отдаются только тому, кто
        вошёл: раньше любой, кто дотянулся до порта, узнавал точную версию
        (и, значит, список известных для неё уязвимостей).
        """
        svc = getattr(request.app.state, "services", None)
        try:
            user = auth_mod.current_user(request)
        except Exception:  # noqa: BLE001
            user = None
        if user is None:
            return {"status": "ok"}
        return {"status": "ok", "version": __version__,
                "scheduler": bool(svc and svc.scheduler.running()) if svc else False}

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        """Метрики для Prometheus/Zabbix (monitoring.*). Выключено — 404."""
        from fastapi.responses import PlainTextResponse
        from ..monitoring import metrics_access, render_prometheus
        svc = request.app.state.services
        ip = request.client.host if request.client else ""
        allowed, code = metrics_access(svc, ip, request.headers.get("authorization", ""))
        if not allowed:
            if code == 404:
                return PlainTextResponse("Not Found\n", status_code=404)
            headers = {"WWW-Authenticate": 'Bearer realm="mailarchiver"'} if code == 401 else None
            return PlainTextResponse("Доступ к метрикам запрещён\n", status_code=code, headers=headers)
        text = render_prometheus(svc, per_account=bool(svc.rt("monitoring", "per_account_metrics")))
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    async def favicon():
        path = os.path.join(STATIC_DIR, "favicon.svg")
        if os.path.exists(path):
            from fastapi.responses import FileResponse
            return FileResponse(path, media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    return app


app = create_app()
