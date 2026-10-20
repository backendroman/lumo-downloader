import json
import logging
import shutil
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.security import client_ip, session_from_request
from app.services.errors import AppError
from app.services.jobs import MEDIA_TYPES, Job

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_CONNECTION_SECONDS = 600  # browser reconnects by itself
SSE_RETRY_MS = 2000


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    option_id: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9-]+$")


def require_session(request: Request) -> str:
    session = session_from_request(request)
    if session is None:
        raise AppError("forbidden", "Сессия не найдена. Обновите страницу.")
    return session


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return  # curl, old browsers
    host = request.headers.get("host", "")
    if origin.split("://", 1)[-1] != host:
        raise AppError("forbidden")


def ok(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, **data}, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    try:
        free_mb = shutil.disk_usage(settings.download_dir).free // (1024 * 1024)
    except OSError:
        free_mb = None
    return ok({
        "status": "ok",
        "ffmpeg": state.media.has_ffmpeg,
        "yt_dlp": state.ytdlp_version,
        "active_jobs": state.jobs.active_count(),
        "max_concurrent_downloads": settings.max_concurrent_downloads,
        "disk_free_mb": free_mb,
    })


@router.post("/analyze", dependencies=[Depends(require_same_origin)])
async def analyze(body: AnalyzeRequest, request: Request, session: str = Depends(require_session)) -> JSONResponse:
    state = request.app.state
    if not state.limiter.allow(f"analyze:{client_ip(request)}", state.settings.analyze_rate_per_minute, 60):
        raise AppError("rate_limited")
    analysis = await state.media.analyze(body.url, session)
    return ok(state.media.public(analysis))


@router.post("/jobs", dependencies=[Depends(require_same_origin)])
async def create_job(body: CreateJobRequest, request: Request, session: str = Depends(require_session)) -> JSONResponse:
    state = request.app.state
    analysis = state.media.get(body.analysis_id, session)
    if not state.limiter.allow(f"jobs:{client_ip(request)}", state.settings.jobs_rate_per_hour, 3600):
        raise AppError("rate_limited")
    job = await state.jobs.create(analysis, body.option_id, session)
    return ok({"job": job.snapshot()}, status=201)


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request, session: str = Depends(require_session)) -> JSONResponse:
    job = request.app.state.jobs.get(job_id, session)
    return ok({"job": job.snapshot()})


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_same_origin)])
async def cancel_job(job_id: str, request: Request, session: str = Depends(require_session)) -> JSONResponse:
    manager = request.app.state.jobs
    job = manager.get(job_id, session)
    job = await manager.cancel(job)
    return ok({"job": job.snapshot()})


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def job_event_stream(job: Job, request: Request):
    # always send the full snapshot first, so a reconnect never misses the final state
    yield f"retry: {SSE_RETRY_MS}\n\n".encode()
    seen = job.version
    yield _sse("state", job.snapshot(), seen)
    if job.terminal:
        yield _sse("end", {"status": job.status})
        return
    started = time.monotonic()
    while time.monotonic() - started < SSE_MAX_CONNECTION_SECONDS:
        changed = await job.wait_for_change(seen, SSE_HEARTBEAT_SECONDS)
        if await request.is_disconnected():
            return
        if not changed:
            yield b": heartbeat\n\n"
            continue
        seen = job.version
        yield _sse("state", job.snapshot(), seen)
        if job.terminal:
            yield _sse("end", {"status": job.status})
            return


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, session: str = Depends(require_session)) -> StreamingResponse:
    job = request.app.state.jobs.get(job_id, session)
    return StreamingResponse(
        job_event_stream(job, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/jobs/{job_id}/file")
async def job_file(job_id: str, request: Request, session: str = Depends(require_session)) -> FileResponse:
    manager = request.app.state.jobs
    job = manager.get(job_id, session)
    path = manager.file_for_download(job)
    name = job.file_name or f"media.{job.file_ext or 'bin'}"
    ascii_name = name.encode("ascii", "ignore").decode() or f"media.{job.file_ext or 'bin'}"
    ascii_name = ascii_name.replace('"', "").replace("\\", "")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(job.file_ext or "", "application/octet-stream"),
        headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store"},
    )
