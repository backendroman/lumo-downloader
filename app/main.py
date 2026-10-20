import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router as api_router
from app.core.config import BASE_DIR, Settings, get_settings
from app.core.security import (
    SESSION_COOKIE,
    RateLimiter,
    SecurityMiddleware,
    new_session_id,
    session_from_request,
)
from app.services.errors import AppError, error_message
from app.services.jobs import JobManager
from app.services.media import MediaService, WorkerRunner

log = logging.getLogger("app")


def _ytdlp_version() -> str | None:
    try:
        return version("yt-dlp")
    except PackageNotFoundError:
        return None


def _error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def create_app(
    settings: Settings | None = None,
    *,
    runner: WorkerRunner | None = None,
    has_ffmpeg: bool | None = None,
) -> FastAPI:
    # runner / has_ffmpeg are overridden in tests
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    media = MediaService(settings, runner=runner, has_ffmpeg=has_ffmpeg)
    jobs = JobManager(settings, runner=runner, worker_base=media.worker_base_request())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await jobs.start()
        if not media.has_ffmpeg:
            log.warning("FFmpeg not found: merging, MP4 remux and MP3 options are disabled")
        log.info("%s started (yt-dlp %s)", settings.app_name, app.state.ytdlp_version)
        try:
            yield
        finally:
            await jobs.stop()

    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.media = media
    app.state.jobs = jobs
    app.state.limiter = RateLimiter()
    app.state.ytdlp_version = _ytdlp_version()

    app.add_middleware(SecurityMiddleware, max_body=settings.max_request_body_bytes)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(api_router)
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error_response("invalid_request", error_message("invalid_request"), 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse | HTMLResponse:
        code = {404: "not_found", 405: "invalid_request"}.get(exc.status_code, "internal")
        message = "Страница не найдена." if exc.status_code == 404 else error_message(code)
        return _error_response(code, message, exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _error_response("internal", error_message("internal"), 500)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_name": settings.app_name,
                "has_ffmpeg": media.has_ffmpeg,
                "file_ttl_minutes": settings.file_ttl_seconds // 60,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        if session_from_request(request) is None:
            response.set_cookie(
                SESSION_COOKIE,
                new_session_id(),
                max_age=7 * 24 * 3600,
                httponly=True,
                samesite="strict",
                secure=settings.cookie_secure,
                path="/",
            )
        return response

    return app


app = create_app()
