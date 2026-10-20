import asyncio
import dataclasses
import ipaddress
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import SESSION_COOKIE
from app.main import create_app

# roughly what youtube returns
SAMPLE_FORMATS = [
    {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "protocol": "mhtml", "format_note": "storyboard"},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5, "filesize": 3_400_000, "protocol": "https"},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135.2, "filesize": 3_500_000, "protocol": "https"},
    {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "height": 360, "tbr": 500, "filesize_approx": 12_000_000, "protocol": "https"},
    {"format_id": "134", "ext": "mp4", "vcodec": "avc1.4d401e", "acodec": "none", "height": 360, "tbr": 300, "filesize": 8_000_000, "protocol": "https"},
    {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "height": 720, "tbr": 1500, "filesize": 40_000_000, "protocol": "https"},
    {"format_id": "247", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 720, "tbr": 1300, "filesize": 35_000_000, "protocol": "https"},
    {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "height": 1080, "tbr": 3000, "filesize": 90_000_000, "protocol": "https"},
    {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 1080, "tbr": 2600, "protocol": "https"},
    # rtmp - should be skipped
    {"format_id": "rtmp1", "ext": "flv", "vcodec": "h264", "acodec": "aac", "height": 2160, "protocol": "rtmp"},
]

SAMPLE_INFO = {
    "id": "abc123",
    "title": "Тестовое видео: <b>жирный</b> / слэш",
    "uploader": "Channel",
    "duration": 212,
    "thumbnail": "https://img.example.com/t.jpg",
    "webpage_url": "https://video.example.com/watch?v=abc123",
    "extractor": "Youtube",
    "site": "video.example.com",
    "is_live": False,
    "formats": SAMPLE_FORMATS,
}


class FakeRunner:
    # instead of the worker process. download_script steps: event dict,
    # ("file", name, bytes), ("partial", name), ("sleep", s) or "gate"

    def __init__(self) -> None:
        self.analyze_result: dict[str, Any] = {"type": "info", "data": SAMPLE_INFO}
        self.download_script: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        self.gate = threading.Event()
        self.started = 0
        self.cancelled = 0
        self.raise_timeout = False

    async def __call__(self, request, on_event, timeout, file_limit=None) -> int:
        self.requests.append(json.loads(json.dumps(request)))
        if self.raise_timeout:
            raise TimeoutError
        if request["action"] == "analyze":
            await asyncio.sleep(0)
            on_event(self.analyze_result)
            return 0 if self.analyze_result["type"] == "info" else 1
        self.started += 1
        try:
            job_dir = Path(request["job_dir"])
            job_dir.mkdir(parents=True, exist_ok=True)
            for step in self.download_script:
                if step == "gate":
                    while not self.gate.is_set():
                        await asyncio.sleep(0.01)
                elif isinstance(step, tuple) and step[0] == "sleep":
                    await asyncio.sleep(step[1])
                elif isinstance(step, tuple) and step[0] == "file":
                    path = job_dir / step[1]
                    path.write_bytes(step[2])
                    on_event({"type": "done", "filepath": str(path), "ext": path.suffix[1:], "size": len(step[2])})
                elif isinstance(step, tuple) and step[0] == "partial":
                    (job_dir / step[1]).write_bytes(b"partial")
                else:
                    on_event(step)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return 0


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = Settings(
        download_dir=tmp_path / "downloads",
        min_free_disk_mb=0,
        max_concurrent_downloads=1,
        max_queued_jobs=5,
        max_active_jobs_per_session=5,
        analyze_rate_per_minute=1000,
        jobs_rate_per_hour=1000,
    )
    return dataclasses.replace(base, **overrides)


def wait_for_status(client: TestClient, job_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    job = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {job.get('status')}/{job.get('stage')}")


def analyze_and_start(client: TestClient, option_id: str = "video-best-mp4") -> dict:
    res = client.post("/api/analyze", json={"url": "https://video.example.com/watch?v=abc123"})
    assert res.status_code == 200, res.text
    res = client.post("/api/jobs", json={"analysis_id": res.json()["analysis_id"], "option_id": option_id})
    assert res.status_code == 201, res.text
    return res.json()["job"]


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def make_client(tmp_path, runner):
    clients: list[TestClient] = []

    def factory(has_ffmpeg: bool = True, **overrides: Any) -> TestClient:
        settings = make_settings(tmp_path, **overrides)
        app = create_app(settings, runner=runner, has_ffmpeg=has_ffmpeg)
        client = TestClient(app, base_url="http://testserver")
        client.__enter__()
        clients.append(client)
        client.get("/")  # session cookie
        assert client.cookies.get(SESSION_COOKIE)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    # no real dns in tests
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in {"rebind.example.com", "internal.example.com"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port or 443))]
        if host == "mixed.example.com":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443)),
            ]
        if host == "nxdomain.example.com":
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        try:
            ipaddress.ip_address(host)
            return real(host, port, *args, **kwargs)
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr("app.core.netguard.socket.getaddrinfo", fake)
