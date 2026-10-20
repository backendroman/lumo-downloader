"""App with a fake worker for working on the UI offline

python scripts/ui_server.py [port] [download_seconds]
urls containing "fail" -> error, "long" -> long title
"""

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.conftest import SAMPLE_INFO  # noqa: E402

HOLD = {"seconds": 60.0}


async def fake_runner(request, on_event, timeout, file_limit=None):
    if request["action"] == "analyze":
        await asyncio.sleep(1.2)
        url = request["url"]
        if "fail" in url:
            on_event({"type": "error", "code": "unsupported", "detail": "Unsupported URL"})
            return 1
        data = dict(SAMPLE_INFO)
        if "long" in url:
            data["title"] = ("Очень длинное название видео, которое не должно ломать интерфейс " * 4).strip()
        on_event({"type": "info", "data": data})
        return 0
    job_dir = Path(request["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)
    total = 134_000_000
    on_event({"type": "stage", "stage": "downloading"})
    steps = 200
    for i in range(1, steps + 1):
        frac = i / steps * 0.62
        on_event({"type": "progress", "status": "downloading", "downloaded": int(total * frac), "total": total,
                  "fraction": frac, "speed": 6_400_000.0, "eta": (0.62 - frac) * 40, "part": 1, "parts": 2})
        await asyncio.sleep(HOLD["seconds"] / steps)
    on_event({"type": "stage", "stage": "merging"})
    await asyncio.sleep(3)
    path = job_dir / "media.mp4"
    path.write_bytes(b"\x00" * 1024)
    on_event({"type": "done", "filepath": str(path), "ext": "mp4", "size": 1024})
    return 0


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    HOLD["seconds"] = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    # own download dir: the startup/cleanup sweep would wipe job dirs of a real server sharing it
    settings = dataclasses.replace(get_settings(), download_dir=Path(tempfile.mkdtemp(prefix="lumo-ui-")),
                                   analyze_rate_per_minute=1000, jobs_rate_per_hour=1000,
                                   max_active_jobs_per_session=50, allow_private_networks=True)
    app = create_app(settings, runner=fake_runner, has_ffmpeg=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
