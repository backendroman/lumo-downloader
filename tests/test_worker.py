"""Real worker process against a local http server"""

import asyncio
import functools
import http.server
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.services.errors import classify_ytdlp_error
from app.services.runner import WorkerProcess

FFMPEG = shutil.which("ffmpeg")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(root: Path):
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def media_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    (root / "clip.mp3").write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 4000)
    server, base = serve(root)
    yield base
    server.shutdown()


def run(request: dict, timeout: float = 60) -> tuple[int, list[dict]]:
    events: list[dict] = []

    async def go():
        return await WorkerProcess(request).run(events.append, timeout)

    return asyncio.run(go()), events


def test_worker_blocks_private_targets_at_socket_level(media_server):
    code, events = run({"action": "analyze", "url": f"{media_server}/clip.mp3"})
    assert code != 0
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "forbidden_target"


def test_worker_analyze_and_download_local_file(media_server, tmp_path):
    url = f"{media_server}/clip.mp3"
    code, events = run({"action": "analyze", "url": url, "allow_private": True})
    assert code == 0, events
    info = events[-1]["data"]
    assert info["formats"] and info["formats"][0]["ext"] == "mp3"

    job_dir = tmp_path / "job-x"
    code, events = run({
        "action": "download", "url": url, "allow_private": True, "job_dir": str(job_dir),
        "recipe": {"format": "ba/b"}, "max_filesize": 10_000_000, "max_duration": 3600,
    })
    assert code == 0, events
    kinds = [e["type"] for e in events]
    assert "progress" in kinds and kinds[-1] == "done"
    done = events[-1]
    path = Path(done["filepath"])
    assert path.is_file() and path.parent == job_dir.resolve()
    assert done["size"] == path.stat().st_size > 0
    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["fraction"] == 1.0


def test_worker_enforces_size_limit(media_server, tmp_path):
    code, events = run({
        "action": "download", "url": f"{media_server}/clip.mp3", "allow_private": True,
        "job_dir": str(tmp_path / "job-y"), "recipe": {"format": "ba/b"},
        "max_filesize": 1000, "max_duration": 3600,
    })
    assert code != 0
    assert events[-1]["type"] == "error" and events[-1]["code"] == "too_large"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_worker_mp3_conversion_with_real_ffmpeg(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    str(root / "tone.wav")], check=True)
    server, base = serve(root)
    try:
        code, events = run({
            "action": "download", "url": f"{base}/tone.wav", "allow_private": True,
            "job_dir": str(tmp_path / "job-z"), "ffmpeg_location": FFMPEG,
            "recipe": {"format": "ba/b", "extract_audio": "mp3", "audio_quality": "192"},
            "max_filesize": 50_000_000, "max_duration": 3600,
        }, timeout=120)
    finally:
        server.shutdown()
    assert code == 0, events
    assert {"type": "stage", "stage": "converting"} in events
    done = events[-1]
    assert done["type"] == "done" and done["ext"] == "mp3"
    head = Path(done["filepath"]).read_bytes()[:3]
    assert head == b"ID3" or head[:2] == b"\xff\xfb"


def _unreachable_request(job_dir: Path) -> dict:
    # non-routable, connect just hangs
    return {"action": "download", "url": "http://10.255.255.1/x", "allow_private": True,
            "job_dir": str(job_dir), "recipe": {"format": "b"}}


def test_runner_timeout_kills_process(tmp_path):
    proc = WorkerProcess(_unreachable_request(tmp_path / "job-t"))

    async def go():
        return await proc.run(lambda e: None, timeout=1.0)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(go())
    assert time.monotonic() - started < 20
    proc.proc.wait(timeout=10)
    assert proc.proc.poll() is not None


def test_runner_cancellation_kills_process(tmp_path):
    proc = WorkerProcess(_unreachable_request(tmp_path / "job-c"))

    async def go():
        task = asyncio.create_task(proc.run(lambda e: None, timeout=60))
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    proc.proc.wait(timeout=10)
    assert proc.proc.poll() is not None


@pytest.mark.parametrize("message,code", [
    ("ERROR: Unsupported URL: https://example.com", "unsupported"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "source_blocked"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "auth_required"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: This video is DRM protected", "drm"),
    ("ERROR: Postprocessing: Conversion failed!", "ffmpeg_error"),
    ("ERROR: ffmpeg not found. Please install", "ffmpeg_missing"),
    ("ERROR: Requested format is not available", "format_unavailable"),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate_limited_source"),
    ("ERROR: The read operation timed out", "timeout"),
    ("ERROR: [Errno 11001] blocked: non-public address", "forbidden_target"),
    ("something completely unexpected", "internal"),
])
def test_error_classification(message, code):
    assert classify_ytdlp_error(message) == code
