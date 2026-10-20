"""Download jobs: queue, progress, cancel, cleanup. Everything is in memory"""

import asyncio
import logging
import math
import re
import secrets
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.netguard import redact_text, redact_url
from app.services.errors import AppError, error_message
from app.services.formats import DownloadOption
from app.services.media import Analysis, WorkerRunner, run_worker_process

log = logging.getLogger(__name__)

JOB_DIR_PREFIX = "job-"
TERMINAL = {"completed", "cancelled", "failed", "expired"}
ACTIVE = {"created", "queued", "running", "processing"}
PROCESSING_STAGES = {"merging", "converting", "finalizing"}
FORGET_AFTER_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 30

MEDIA_TYPES = {
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska",
    "mov": "video/quicktime", "flv": "video/x-flv", "3gp": "video/3gpp", "ts": "video/mp2t",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "opus": "audio/ogg",
    "ogg": "audio/ogg", "oga": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav", "weba": "audio/webm",
}


def safe_filename(title: str, ext: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFC", title or "")
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        name = "media"
    name = name[:max_len].rstrip(" .")
    ext = re.sub(r"[^a-z0-9]", "", (ext or "").lower())[:8] or "bin"
    return f"{name}.{ext}"


@dataclass
class Job:
    id: str
    session: str
    url: str
    title: str
    option: DownloadOption
    dir: Path
    status: str = "created"
    stage: str = "created"
    fraction: float | None = None
    downloaded: int | None = None
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    part: int | None = None
    parts: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    file_path: Path | None = None
    file_name: str | None = None
    file_size: int | None = None
    file_ext: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    expires_at: float | None = None
    cancel_requested: bool = False
    version: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def touch(self) -> None:
        self.version += 1
        old, self._changed = self._changed, asyncio.Event()
        old.set()

    async def wait_for_change(self, seen_version: int, timeout: float) -> bool:
        if self.version != seen_version:
            return True
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "version": self.version,
            "title": self.title,
            "option": {"label": self.option.label, "detail": self.option.detail, "kind": self.option.kind,
                       "container": self.option.container},
            "progress": {
                "fraction": self.fraction,
                "downloaded": self.downloaded,
                "total": self.total,
                "speed": self.speed,
                "eta": self.eta,
                "part": self.part,
                "parts": self.parts,
            },
            "error": {"code": self.error_code, "message": self.error_message} if self.error_code else None,
            "file": None,
            "created_at": self.created_at,
        }
        if self.status == "completed" and self.file_name:
            data["file"] = {
                "name": self.file_name,
                "size": self.file_size,
                "ext": self.file_ext,
                "url": f"/api/jobs/{self.id}/file",
                "expires_in": max(0, int((self.expires_at or now) - now)),
            }
        return data


async def remove_dir(path: Path, attempts: int = 10) -> bool:
    # on windows a killed ffmpeg keeps the file locked for a while
    for i in range(attempts):
        if not path.exists():
            return True
        try:
            await asyncio.to_thread(shutil.rmtree, path)
            return True
        except OSError:
            await asyncio.sleep(0.2 * (i + 1))
    log.warning("could not remove %s", path.name)
    return False


class JobManager:
    def __init__(self, settings: Settings, runner: WorkerRunner | None = None, worker_base: dict | None = None):
        self.settings = settings
        self.runner = runner or run_worker_process
        self.worker_base = worker_base or {}
        self.jobs: dict[str, Job] = {}
        self.root = settings.download_dir
        self._slots = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # leftovers from the previous run
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith(JOB_DIR_PREFIX):
                await remove_dir(child)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="job-cleanup")

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in list(self.jobs.values()):
            await remove_dir(job.dir)

    def get(self, job_id: str, session: str) -> Job:
        job = self.jobs.get(job_id)
        # someone else's job -> 404 too
        if job is None or job.session != session:
            raise AppError("not_found")
        return job

    def active_count(self, session: str | None = None) -> int:
        return sum(1 for j in self.jobs.values() if j.status in ACTIVE and (session is None or j.session == session))

    def queued_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status in {"created", "queued"})

    async def create(self, analysis: Analysis, option_id: str, session: str) -> Job:
        option = analysis.option(option_id)
        if option is None:
            raise AppError("format_unavailable")
        if self.active_count(session) >= self.settings.max_active_jobs_per_session:
            raise AppError("too_many_jobs")
        running = sum(1 for j in self.jobs.values() if j.status in {"running", "processing"})
        if running >= self.settings.max_concurrent_downloads and self.queued_count() >= self.settings.max_queued_jobs:
            raise AppError("busy")
        self.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.root).free
        if free < self.settings.min_free_disk_mb * 1024 * 1024:
            raise AppError("disk_full")

        job_id = secrets.token_urlsafe(18)
        job = Job(id=job_id, session=session, url=analysis.url, title=analysis.title, option=option,
                  dir=self.root / f"{JOB_DIR_PREFIX}{job_id}")
        self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job), name=f"job-{job_id[:6]}")
        log.info("job %s created option=%s url=%s", job_id[:8], option.id, redact_url(job.url))
        return job

    async def cancel(self, job: Job) -> Job:
        if job.terminal:
            return job
        job.cancel_requested = True
        task = job.task
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except (asyncio.CancelledError, TimeoutError):
                pass
        if not job.terminal:
            await remove_dir(job.dir)
            self._finish(job, "cancelled", stage="cancelled")
        return job

    def _set(self, job: Job, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(job, key, value)
        job.touch()

    def _finish(self, job: Job, status: str, *, stage: str | None = None, code: str | None = None,
                message: str | None = None) -> None:
        changes: dict[str, Any] = {"status": status, "stage": stage or status, "finished_at": time.time(),
                                   "speed": None, "eta": None}
        if code:
            changes.update(error_code=code, error_message=message or error_message(code))
        if status == "cancelled":
            changes.update(error_code="cancelled", error_message="Загрузка отменена.")
        self._set(job, **changes)

    async def _run(self, job: Job) -> None:
        # on every exit path the dir is removed before the final status is published
        try:
            self._set(job, status="queued", stage="queued")
            async with self._slots:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                self._set(job, status="running", stage="starting", started_at=time.time())
                await self._execute(job)
        except asyncio.CancelledError:
            await remove_dir(job.dir)
            if not job.terminal:
                self._finish(job, "cancelled", stage="cancelled")
            log.info("job %s cancelled", job.id[:8])
        except AppError as exc:
            await remove_dir(job.dir)
            self._finish(job, "failed", stage="failed", code=exc.code, message=exc.message)
            log.info("job %s failed code=%s", job.id[:8], exc.code)
        except Exception:
            log.exception("job %s crashed", job.id[:8])
            await remove_dir(job.dir)
            self._finish(job, "failed", stage="failed", code="internal")

    def _on_worker_event(self, job: Job, result: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "progress":
            self._set(
                job,
                stage="downloading" if job.stage in {"starting", "downloading"} else job.stage,
                fraction=_frac(event.get("fraction")),
                downloaded=_int(event.get("downloaded")),
                total=_int(event.get("total")),
                speed=_float(event.get("speed")),
                eta=_float(event.get("eta")),
                part=_int(event.get("part")),
                parts=_int(event.get("parts")),
            )
        elif kind == "stage":
            stage = event.get("stage")
            if stage in {"downloading", "merging", "converting", "finalizing"}:
                status = "processing" if stage in PROCESSING_STAGES else "running"
                # ffmpeg doesn't report progress, reset the numbers
                extra = {"fraction": None, "speed": None, "eta": None} if status == "processing" else {}
                self._set(job, stage=stage, status=status, **extra)
        elif kind in {"done", "error"}:
            result.setdefault("event", event)

    async def _execute(self, job: Job) -> None:
        s = self.settings
        result: dict[str, Any] = {}
        request = {
            **self.worker_base,
            "action": "download",
            "url": job.url,
            "job_dir": str(job.dir),
            "recipe": job.option.recipe,
            "max_filesize": s.max_filesize_bytes,
            "max_duration": s.max_duration_seconds,
        }
        try:
            await self.runner(request, lambda e: self._on_worker_event(job, result, e),
                              s.job_timeout_seconds, s.max_filesize_bytes)
        except TimeoutError as exc:
            raise AppError("timeout", "Загрузка заняла слишком много времени и была остановлена.") from exc

        event = result.get("event")
        if event is None:
            raise AppError("internal")
        if event["type"] == "error":
            log.info("job %s worker error code=%s detail=%s", job.id[:8], event.get("code"), redact_text(event.get("detail")))
            raise AppError(event.get("code") or "internal")

        path = self._verify_output(job, event.get("filepath"))
        ext = path.suffix.lstrip(".").lower()
        size = path.stat().st_size
        if size > s.max_filesize_bytes:
            raise AppError("too_large")
        now = time.time()
        self._set(
            job, status="completed", stage="completed", fraction=1.0, speed=None, eta=None,
            file_path=path, file_ext=ext, file_size=size, file_name=safe_filename(job.title, ext),
            finished_at=now, expires_at=now + s.file_ttl_seconds,
        )
        log.info("job %s completed size=%d ext=%s", job.id[:8], size, ext)

    @staticmethod
    def _verify_output(job: Job, filepath: Any) -> Path:
        if not isinstance(filepath, str):
            raise AppError("internal")
        path = Path(filepath).resolve()
        if job.dir.resolve() not in path.parents or not path.is_file():
            raise AppError("internal")
        return path

    def file_for_download(self, job: Job) -> Path:
        if job.status == "expired":
            raise AppError("expired")
        if job.status != "completed":
            raise AppError("not_ready")
        if job.expires_at and time.time() > job.expires_at:
            raise AppError("expired")
        path = job.file_path
        if path is None or not path.is_file() or job.dir.resolve() not in path.resolve().parents:
            raise AppError("expired")
        return path

    async def cleanup_once(self) -> None:
        now = time.time()
        forget_after = max(FORGET_AFTER_SECONDS, self.settings.file_ttl_seconds)
        for job in list(self.jobs.values()):
            if job.status == "completed" and job.expires_at and now > job.expires_at:
                self._set(job, status="expired", stage="expired", file_path=None)
                await remove_dir(job.dir)
            elif job.terminal and job.status != "completed" and job.dir.exists():
                await remove_dir(job.dir)
            if job.terminal and job.status != "completed" and (job.finished_at or now) + forget_after < now:
                self.jobs.pop(job.id, None)
        # dirs that failed to delete earlier
        known = {j.dir.name for j in self.jobs.values()}
        if self.root.exists():
            for child in list(self.root.iterdir()):
                if child.is_dir() and child.name.startswith(JOB_DIR_PREFIX) and child.name not in known:
                    await remove_dir(child)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                await self.cleanup_once()
            except Exception:
                log.exception("cleanup failed")


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _frac(value: Any) -> float | None:
    number = _float(value)
    return min(1.0, number) if number is not None else None
