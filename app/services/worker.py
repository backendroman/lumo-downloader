"""yt-dlp worker: python -m app.services.worker

Reads one JSON request from stdin, writes JSON lines to stdout:
info / progress / stage / done / error
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yt_dlp.utils import DownloadCancelled

from app.core.netguard import install_socket_guard
from app.services.errors import classify_ytdlp_error

PROGRESS_INTERVAL = 0.25
# +10% for container overhead when merging
FILE_LIMIT_SLACK = 1.1
FILE_LIMIT_EXTRA = 64 * 1024 * 1024

PP_STAGES = {
    "Merger": "merging",
    "ExtractAudio": "converting",
    "VideoRemuxer": "converting",
    "VideoConvertor": "converting",
}


class Emitter:
    def __init__(self) -> None:
        self._out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
        # anything printed by libs goes to stderr
        sys.stdout = sys.stderr

    def emit(self, event: dict[str, Any]) -> None:
        self._out.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self._out.flush()


class QuietLogger:
    def __init__(self) -> None:
        self.last_error: str | None = None

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.last_error = msg


def _base_params(req: dict[str, Any], logger: QuietLogger) -> dict[str, Any]:
    params: dict[str, Any] = {
        "logger": logger,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 1,
        "hls_prefer_native": True,
        "check_formats": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "color": "never",
        "windowsfilenames": True,
    }
    if req.get("ffmpeg_location"):
        params["ffmpeg_location"] = req["ffmpeg_location"]
    if req.get("proxy"):
        params["proxy"] = req["proxy"]
    return params


def _disable_network_ffmpeg() -> None:
    # ffmpeg downloading by itself would bypass the socket guard
    from yt_dlp.downloader import external
    from yt_dlp.utils import DownloadError

    def refuse(self, *args, **kwargs):
        raise DownloadError("requested format is not available: ffmpeg network download disabled")

    external.FFmpegFD.real_download = refuse


def _summarise(info: dict[str, Any]) -> dict[str, Any]:
    thumb = info.get("thumbnail")
    if not thumb:
        thumbs = [t for t in info.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
        if thumbs:
            thumbs.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
            thumb = thumbs[-1]["url"]
    desc = info.get("description")
    formats = info.get("formats") or ([info] if info.get("url") else [])
    return {
        "id": info.get("id"),
        "title": info.get("title") or info.get("fulltitle"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator") or info.get("artist"),
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "site": info.get("webpage_url_domain"),
        "description": desc[:600] if isinstance(desc, str) else None,
        "is_live": bool(info.get("is_live")),
        "type": info.get("_type") or "video",
        "formats": [
            {k: f.get(k) for k in (
                "format_id", "ext", "vcodec", "acodec", "height", "tbr", "abr",
                "filesize", "filesize_approx", "protocol", "format_note", "has_drm",
            )}
            for f in formats
            if isinstance(f, dict)
        ],
    }


def _fail(out: Emitter, logger: QuietLogger, exc: BaseException | None, fallback: str = "internal") -> int:
    detail = logger.last_error or (str(exc) if exc else "") or fallback
    code = classify_ytdlp_error(detail)
    out.emit({"type": "error", "code": code, "detail": detail[:500]})
    return 1


def run_analyze(req: dict[str, Any], out: Emitter) -> int:
    import yt_dlp

    logger = QuietLogger()
    params = _base_params(req, logger)
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(req["url"], download=False)
    except Exception as exc:
        return _fail(out, logger, exc)
    if not isinstance(info, dict):
        out.emit({"type": "error", "code": "unsupported", "detail": "empty result"})
        return 1
    if info.get("_type") in {"playlist", "multi_video", "url", "url_transparent"}:
        out.emit({"type": "error", "code": "playlist", "detail": "playlist"})
        return 1
    out.emit({"type": "info", "data": _summarise(info)})
    return 0


class SizeLimitExceeded(DownloadCancelled):
    msg = "file is larger than max-filesize"


class ProgressTracker:
    """Overall progress for video+audio downloads

    fraction is sent only if sizes of all streams are known
    """

    def __init__(self, out: Emitter, max_bytes: int | None) -> None:
        self.out = out
        self.max_bytes = max_bytes
        self.parts: dict[str, dict[str, float | None]] = {}
        self.last_emit = 0.0

    def hook(self, d: dict[str, Any]) -> None:
        info = d.get("info_dict") or {}
        key = str(info.get("format_id") or "main")
        requested = info.get("requested_formats") or []

        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "finished":
            total = total or downloaded
            downloaded = total
        self.parts[key] = {"downloaded": downloaded, "total": total}

        so_far = sum(p["downloaded"] or 0 for p in self.parts.values())
        if self.max_bytes and (so_far > self.max_bytes or (total and total > self.max_bytes)):
            raise SizeLimitExceeded()

        now = time.monotonic()
        if d.get("status") == "downloading" and now - self.last_emit < PROGRESS_INTERVAL:
            return
        self.last_emit = now

        fraction, grand_total = None, None
        if requested:
            totals = [
                (self.parts.get(str(f.get("format_id"))) or {}).get("total")
                or f.get("filesize") or f.get("filesize_approx")
                for f in requested
            ]
            if all(totals):
                grand_total = sum(totals)
        elif total:
            grand_total = total
        if grand_total:
            fraction = max(0.0, min(1.0, so_far / grand_total))

        part_index = next((i for i, f in enumerate(requested) if str(f.get("format_id")) == key), 0)
        self.out.emit({
            "type": "progress",
            "status": d.get("status"),
            "downloaded": so_far,
            "total": grand_total,
            "fraction": fraction,
            "speed": d.get("speed"),
            # eta is per stream, only makes sense for the last one
            "eta": d.get("eta") if fraction is not None and part_index + 1 == max(1, len(requested)) else None,
            "part": part_index + 1,
            "parts": max(1, len(requested)),
        })


def _postprocessors(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    pps: list[dict[str, Any]] = []
    if recipe.get("extract_audio"):
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": recipe["extract_audio"]}
        if recipe.get("audio_quality"):
            pp["preferredquality"] = recipe["audio_quality"]
        pps.append(pp)
    if recipe.get("remux"):
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": recipe["remux"]})
    return pps


def _make_match_filter(max_duration: int | None):
    def match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        if info.get("is_live"):
            raise DownloadCancelled("live stream")
        duration = info.get("duration")
        if max_duration and duration and duration > max_duration:
            raise DownloadCancelled("too long")
        return None

    return match_filter


def run_download(req: dict[str, Any], out: Emitter) -> int:
    import yt_dlp

    recipe = req["recipe"]
    job_dir = Path(req["job_dir"]).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    logger = QuietLogger()
    tracker = ProgressTracker(out, req.get("max_filesize"))
    final_paths: list[str] = []

    def pp_hook(d: dict[str, Any]) -> None:
        stage = PP_STAGES.get(str(d.get("postprocessor")))
        if stage and d.get("status") == "started":
            out.emit({"type": "stage", "stage": stage})

    params = _base_params(req, logger)
    params.update({
        "format": recipe["format"],
        "paths": {"home": str(job_dir), "temp": str(job_dir)},
        "outtmpl": {"default": "media.%(ext)s"},
        "overwrites": True,
        "continuedl": False,
        "progress_hooks": [tracker.hook],
        "postprocessor_hooks": [pp_hook],
        "post_hooks": [final_paths.append],
        "postprocessors": _postprocessors(recipe),
        "match_filter": _make_match_filter(req.get("max_duration")),
        "max_filesize": req.get("max_filesize"),
        "keepvideo": False,
    })
    if recipe.get("format_sort"):
        params["format_sort"] = recipe["format_sort"]
    if recipe.get("merge_output_format"):
        params["merge_output_format"] = recipe["merge_output_format"]

    out.emit({"type": "stage", "stage": "downloading"})
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(req["url"], download=True)
    except SizeLimitExceeded:
        out.emit({"type": "error", "code": "too_large", "detail": "size limit"})
        return 1
    except DownloadCancelled as exc:
        code = {"live stream": "live", "too long": "too_long"}.get(str(exc.msg), "internal")
        out.emit({"type": "error", "code": code, "detail": str(exc.msg)})
        return 1
    except Exception as exc:
        return _fail(out, logger, exc)

    # final_paths is filled by post_hooks after all postprocessors (.mp3, not .webm)
    path = Path(final_paths[-1]).resolve() if final_paths else None
    if path is None or not path.is_file() or job_dir not in path.parents:
        if logger.last_error:
            return _fail(out, logger, None)
        # yt-dlp silently skips files over max_filesize
        out.emit({"type": "error", "code": "too_large" if req.get("max_filesize") else "internal",
                  "detail": "no output file"})
        return 1

    out.emit({"type": "stage", "stage": "finalizing"})
    out.emit({
        "type": "done",
        "filepath": str(path),
        "ext": path.suffix.lstrip(".").lower(),
        "size": path.stat().st_size,
        "title": info.get("title") if isinstance(info, dict) else None,
    })
    return 0


def _limit_file_size(max_bytes: Any) -> None:
    # set on ourselves, ffmpeg started later inherits it. no-op on windows
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        return
    try:
        import resource
    except ImportError:
        return
    limit = int(max_bytes * FILE_LIMIT_SLACK) + FILE_LIMIT_EXTRA
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    except (OSError, ValueError):
        pass


def main() -> int:
    out = Emitter()
    try:
        req = json.loads(sys.stdin.readline() or "{}")
    except json.JSONDecodeError:
        out.emit({"type": "error", "code": "internal", "detail": "bad request"})
        return 2
    _limit_file_size(req.get("file_limit"))
    if not req.get("allow_private"):
        proxy_host = urlsplit(req["proxy"]).hostname if req.get("proxy") else None
        install_socket_guard({proxy_host} if proxy_host else None)
    _disable_network_ffmpeg()
    action = req.get("action")
    try:
        if action == "analyze":
            return run_analyze(req, out)
        if action == "download":
            return run_download(req, out)
    except Exception as exc:
        out.emit({"type": "error", "code": "internal", "detail": f"{type(exc).__name__}: {exc}"[:500]})
        return 1
    out.emit({"type": "error", "code": "internal", "detail": "unknown action"})
    return 2


if __name__ == "__main__":
    sys.exit(main())

