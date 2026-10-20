import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.netguard import UnsafeURLError, redact_text, redact_url, validate_public_url
from app.services.errors import AppError
from app.services.formats import DownloadOption, build_options
from app.services.runner import WorkerProcess

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
# (request, on_event, timeout_seconds, file_limit_bytes) -> exit code
WorkerRunner = Callable[[dict[str, Any], EventHandler, float, int | None], Awaitable[int]]


async def run_worker_process(request: dict[str, Any], on_event: EventHandler, timeout: float,
                             file_limit: int | None = None) -> int:
    return await WorkerProcess(request, file_limit=file_limit).run(on_event, timeout)


@dataclass
class Analysis:
    id: str
    session: str
    url: str
    title: str
    info: dict[str, Any]
    options: list[DownloadOption]
    created_at: float = field(default_factory=time.time)

    def option(self, option_id: str) -> DownloadOption | None:
        return next((o for o in self.options if o.id == option_id), None)


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _https_or_none(url: Any) -> str | None:
    if isinstance(url, str) and url.startswith("https://") and len(url) < 2048:
        return url
    return None


def _source_name(info: dict[str, Any], url: str) -> str | None:
    extractor = info.get("extractor")
    if isinstance(extractor, str) and extractor.lower() not in {"generic", "html5mediaembed"}:
        return _clean_text(extractor.split(":")[0], 40)
    site = str(info.get("site") or urlsplit(url).hostname or "").lower()
    return site.removeprefix("www.").removeprefix("m.") or None


class MediaService:
    def __init__(self, settings: Settings, runner: WorkerRunner | None = None, has_ffmpeg: bool | None = None):
        self.settings = settings
        self.runner = runner or run_worker_process
        self.has_ffmpeg = bool(settings.resolved_ffmpeg()) if has_ffmpeg is None else has_ffmpeg
        self.analyses: dict[str, Analysis] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_analyses)
        self._waiting = 0

    def worker_base_request(self) -> dict[str, Any]:
        s = self.settings
        return {"ffmpeg_location": s.ffmpeg_location, "proxy": s.ytdlp_proxy, "allow_private": s.allow_private_networks}

    async def validate_url(self, url: str) -> str:
        try:
            # getaddrinfo blocks
            return await asyncio.to_thread(validate_public_url, url, allow_private=self.settings.allow_private_networks)
        except UnsafeURLError as exc:
            raise AppError(exc.code, str(exc)) from exc

    async def analyze(self, url: str, session: str) -> Analysis:
        url = await self.validate_url(url)
        if self._waiting >= self.settings.max_concurrent_analyses * 3:
            raise AppError("busy")
        self._waiting += 1
        try:
            async with self._semaphore:
                info = await self._extract(url)
        finally:
            self._waiting -= 1
        return self._store(url, session, info)

    async def _extract(self, url: str) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") in {"info", "error"}:
                result.setdefault("event", event)

        request = {**self.worker_base_request(), "action": "analyze", "url": url}
        started = time.monotonic()
        try:
            await self.runner(request, on_event, self.settings.analyze_timeout_seconds, None)
        except TimeoutError as exc:
            log.info("analyze timeout url=%s", redact_url(url))
            raise AppError("timeout") from exc
        event = result.get("event")
        if not event:
            raise AppError("internal")
        if event["type"] == "error":
            code = event.get("code") or "internal"
            log.info("analyze failed url=%s code=%s detail=%s", redact_url(url), code, redact_text(event.get("detail")))
            raise AppError(code)
        log.info("analyze ok url=%s in %.1fs", redact_url(url), time.monotonic() - started)
        data = event.get("data")
        if not isinstance(data, dict):
            raise AppError("internal")
        return data

    def _store(self, url: str, session: str, info: dict[str, Any]) -> Analysis:
        if info.get("is_live"):
            raise AppError("live")
        duration = info.get("duration") if isinstance(info.get("duration"), (int, float)) else None
        if duration and duration > self.settings.max_duration_seconds:
            raise AppError("too_long")
        options = build_options(
            info.get("formats"), has_ffmpeg=self.has_ffmpeg, duration=duration,
            max_filesize=self.settings.max_filesize_bytes,
        )
        if not options:
            raise AppError("no_formats")
        title = _clean_text(info.get("title"), 300) or "Без названия"
        webpage = info.get("webpage_url")
        public = {
            "title": title,
            "uploader": _clean_text(info.get("uploader"), 120),
            "duration": duration,
            "thumbnail": _https_or_none(info.get("thumbnail")),
            "source": _source_name(info, url),
            "webpage_url": webpage if isinstance(webpage, str) and webpage.startswith(("http://", "https://")) else None,
            "description": _clean_text(info.get("description"), 300),
        }
        self._prune()
        analysis = Analysis(id=secrets.token_urlsafe(16), session=session, url=url, title=title,
                            info=public, options=options)
        self.analyses[analysis.id] = analysis
        return analysis

    def get(self, analysis_id: str, session: str) -> Analysis:
        analysis = self.analyses.get(analysis_id)
        if analysis is None or analysis.session != session:
            raise AppError("analysis_expired")
        if time.time() - analysis.created_at > self.settings.analysis_ttl_seconds:
            self.analyses.pop(analysis_id, None)
            raise AppError("analysis_expired")
        return analysis

    def _prune(self) -> None:
        now = time.time()
        ttl = self.settings.analysis_ttl_seconds
        for key in [k for k, a in self.analyses.items() if now - a.created_at > ttl]:
            self.analyses.pop(key, None)
        if len(self.analyses) > 2000:
            for key in sorted(self.analyses, key=lambda k: self.analyses[k].created_at)[:500]:
                self.analyses.pop(key, None)

    @staticmethod
    def public(analysis: Analysis) -> dict[str, Any]:
        return {"analysis_id": analysis.id, "media": analysis.info, "options": [o.public() for o in analysis.options]}
