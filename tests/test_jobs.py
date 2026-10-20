import json
import time

from fastapi.testclient import TestClient

from app.services.errors import error_message
from tests.conftest import analyze_and_start, wait_for_status

PROGRESS_1 = {"type": "progress", "status": "downloading", "downloaded": 500, "total": 1000,
              "fraction": 0.5, "speed": 250.0, "eta": 2, "part": 1, "parts": 1}
PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


def test_full_download_flow(client, runner, tmp_path):
    runner.download_script = [
        {"type": "stage", "stage": "downloading"},
        PROGRESS_1,
        {"type": "stage", "stage": "merging"},
        ("file", "media.mp4", PAYLOAD),
    ]
    job = analyze_and_start(client)
    assert job["status"] in {"created", "queued", "running"}
    assert len(job["id"]) >= 24

    done = wait_for_status(client, job["id"], {"completed", "failed"})
    assert done["status"] == "completed", done
    assert done["file"]["ext"] == "mp4"
    assert done["file"]["size"] == len(PAYLOAD)
    assert done["file"]["name"].endswith(".mp4")
    assert "/" not in done["file"]["name"] and "<" not in done["file"]["name"]

    req = runner.requests[-1]
    assert req["action"] == "download" and req["recipe"]["merge_output_format"] == "mp4"

    res = client.get(done["file"]["url"])
    assert res.status_code == 200
    assert res.content == PAYLOAD
    assert res.headers["content-type"] == "video/mp4"
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition


def test_option_must_come_from_analysis(client, runner):
    res = client.post("/api/analyze", json={"url": "https://video.example.com/watch?v=abc123"})
    analysis_id = res.json()["analysis_id"]
    for bad in ["video-2160-mp4", "bestvideo+bestaudio", "audio-flac"]:
        r = client.post("/api/jobs", json={"analysis_id": analysis_id, "option_id": bad})
        assert r.status_code == 422, bad
    r = client.post("/api/jobs", json={"analysis_id": "doesnotexist123", "option_id": "video-best-mp4"})
    assert r.status_code == 410 and r.json()["error"]["code"] == "analysis_expired"
    assert not any(q["action"] == "download" for q in runner.requests)


def test_analysis_bound_to_session(client, runner):
    analysis_id = client.post("/api/analyze", json={"url": "https://video.example.com/w"}).json()["analysis_id"]
    other = TestClient(client.app)
    other.get("/")
    r = other.post("/api/jobs", json={"analysis_id": analysis_id, "option_id": "video-best-mp4"})
    assert r.status_code == 410


def test_job_access_is_session_scoped(client, runner):
    runner.download_script = [("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    wait_for_status(client, job["id"], {"completed"})
    other = TestClient(client.app)
    other.get("/")
    assert other.get(f"/api/jobs/{job['id']}").status_code == 404
    assert other.get(f"/api/jobs/{job['id']}/file").status_code == 404
    assert other.post(f"/api/jobs/{job['id']}/cancel").status_code == 404
    assert client.get("/api/jobs/guessed-id-000000000000").status_code == 404


def test_sse_streams_progress_and_ends(client, runner):
    runner.gate.clear()
    runner.download_script = [PROGRESS_1, "gate", {"type": "stage", "stage": "merging"}, ("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    wait_for_status(client, job["id"], {"running"})
    runner.gate.set()
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["x-accel-buffering"] == "no"
        body = "".join(res.iter_text())
    assert body.startswith("retry: ")
    events = parse_sse(body)
    states = [d for n, d in events if n == "state"]
    assert states, body
    assert states[-1]["status"] == "completed"
    assert events[-1][0] == "end"
    versions = [s["version"] for s in states]
    assert versions == sorted(versions)


def test_sse_reconnect_gets_current_state(client, runner):
    runner.gate.clear()
    runner.download_script = [PROGRESS_1, "gate", ("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job['id']}").json()["job"]
        if snap["progress"]["fraction"] == 0.5:
            break
        time.sleep(0.02)
    assert snap["progress"]["fraction"] == 0.5
    assert snap["progress"]["speed"] == 250.0
    runner.gate.set()
    wait_for_status(client, job["id"], {"completed"})
    # reconnect after completion
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as res:
        events = parse_sse("".join(res.iter_text()))
    assert events[0][0] == "state" and events[0][1]["status"] == "completed"
    assert events[-1][0] == "end"


def test_cancel_running_job_stops_worker_and_cleans_files(client, runner, tmp_path):
    runner.gate.clear()
    runner.download_script = [PROGRESS_1, ("partial", "media.f137.mp4.part"), "gate", ("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    wait_for_status(client, job["id"], {"running"})
    job_dir = tmp_path / "downloads" / f"job-{job['id']}"
    deadline = time.monotonic() + 5
    while not (job_dir / "media.f137.mp4.part").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job_dir.exists()

    res = client.post(f"/api/jobs/{job['id']}/cancel")
    assert res.status_code == 200
    assert res.json()["job"]["status"] == "cancelled"
    assert runner.cancelled == 1
    assert not job_dir.exists()
    assert client.get(f"/api/jobs/{job['id']}/file").json()["error"]["code"] == "not_ready"
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["job"]["status"] == "cancelled"


def test_cancel_queued_job(make_client, runner):
    client = make_client(max_concurrent_downloads=1)
    runner.gate.clear()
    runner.download_script = ["gate", ("file", "media.mp4", PAYLOAD)]
    first = analyze_and_start(client)
    wait_for_status(client, first["id"], {"running"})
    second = analyze_and_start(client)
    assert wait_for_status(client, second["id"], {"queued"})["stage"] == "queued"
    res = client.post(f"/api/jobs/{second['id']}/cancel")
    assert res.json()["job"]["status"] == "cancelled"
    assert runner.started == 1
    runner.gate.set()
    assert wait_for_status(client, first["id"], {"completed"})["status"] == "completed"


def test_concurrency_limit_queues_jobs(make_client, runner):
    client = make_client(max_concurrent_downloads=1)
    runner.gate.clear()
    runner.download_script = ["gate", ("file", "media.mp4", PAYLOAD)]
    a = analyze_and_start(client)
    wait_for_status(client, a["id"], {"running"})
    b = analyze_and_start(client)
    time.sleep(0.2)
    assert client.get(f"/api/jobs/{b['id']}").json()["job"]["status"] == "queued"
    assert runner.started == 1
    runner.gate.set()
    wait_for_status(client, a["id"], {"completed"})
    wait_for_status(client, b["id"], {"completed"})
    assert runner.started == 2


def test_per_session_and_queue_limits(make_client, runner):
    client = make_client(max_concurrent_downloads=1, max_active_jobs_per_session=2, max_queued_jobs=5)
    runner.gate.clear()
    runner.download_script = ["gate", ("file", "media.mp4", PAYLOAD)]
    analyze_and_start(client)
    analyze_and_start(client)
    res = client.post("/api/analyze", json={"url": "https://video.example.com/x"})
    r = client.post("/api/jobs", json={"analysis_id": res.json()["analysis_id"], "option_id": "video-best-mp4"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_jobs"
    runner.gate.set()


def test_ytdlp_download_error(client, runner, tmp_path):
    runner.download_script = [PROGRESS_1, ("partial", "x.part"),
                              {"type": "error", "code": "unavailable", "detail": "ERROR: HTTP Error 403: Forbidden secret-token"}]
    job = analyze_and_start(client)
    failed = wait_for_status(client, job["id"], {"failed"})
    assert failed["error"]["code"] == "unavailable"
    assert "secret-token" not in json.dumps(failed) and "ERROR" not in failed["error"]["message"]
    assert not (tmp_path / "downloads" / f"job-{job['id']}").exists()


def test_ffmpeg_error(client, runner, tmp_path):
    runner.download_script = [PROGRESS_1, {"type": "stage", "stage": "merging"}, ("partial", "media.f1.mp4"),
                              {"type": "error", "code": "ffmpeg_error", "detail": "Postprocessing: Conversion failed!"}]
    job = analyze_and_start(client)
    failed = wait_for_status(client, job["id"], {"failed"})
    assert failed["error"]["code"] == "ffmpeg_error"
    assert failed["error"]["message"] == error_message("ffmpeg_error")
    assert "Conversion failed" not in json.dumps(failed)
    assert not (tmp_path / "downloads" / f"job-{job['id']}").exists()


def test_worker_exits_without_result(client, runner):
    runner.download_script = [PROGRESS_1]  # no done/error event
    job = analyze_and_start(client)
    assert wait_for_status(client, job["id"], {"failed"})["error"]["code"] == "internal"


def test_job_timeout(client, runner):
    res = client.post("/api/analyze", json={"url": "https://video.example.com/x"})
    runner.raise_timeout = True
    r = client.post("/api/jobs", json={"analysis_id": res.json()["analysis_id"], "option_id": "video-best-mp4"})
    job = r.json()["job"]
    failed = wait_for_status(client, job["id"], {"failed"})
    assert failed["error"]["code"] == "timeout"


def test_output_outside_job_dir_is_rejected(client, runner, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    runner.download_script = [{"type": "done", "filepath": str(outside), "ext": "txt", "size": 6}]
    job = analyze_and_start(client)
    failed = wait_for_status(client, job["id"], {"failed", "completed"})
    assert failed["status"] == "failed"
    assert client.get(f"/api/jobs/{job['id']}/file").status_code != 200


def test_expired_files_are_cleaned_up(client, runner, tmp_path):
    runner.download_script = [("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    wait_for_status(client, job["id"], {"completed"})
    job_dir = tmp_path / "downloads" / f"job-{job['id']}"
    assert job_dir.exists()
    manager = client.app.state.jobs
    manager.jobs[job["id"]].expires_at = time.time() - 1
    client.portal.call(manager.cleanup_once)
    assert not job_dir.exists()
    snap = client.get(f"/api/jobs/{job['id']}").json()["job"]
    assert snap["status"] == "expired"
    res = client.get(f"/api/jobs/{job['id']}/file")
    assert res.status_code == 410 and res.json()["error"]["code"] == "expired"


def test_orphan_dirs_removed_on_startup_and_cleanup(make_client, tmp_path):
    downloads = tmp_path / "downloads"
    stale = downloads / "job-stale-from-previous-run"
    stale.mkdir(parents=True)
    (stale / "media.mp4.part").write_bytes(b"x")
    keep = downloads / "not-a-job-dir"
    keep.mkdir()
    client = make_client()
    assert not stale.exists()
    assert keep.exists()
    orphan = downloads / "job-orphan"
    orphan.mkdir()
    client.portal.call(client.app.state.jobs.cleanup_once)
    assert not orphan.exists()


def test_unknown_job_after_restart_is_404(make_client):
    client = make_client()
    res = client.get("/api/jobs/AAAAAAAAAAAAAAAAAAAAAAAA")
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    assert client.get("/api/jobs/AAAAAAAAAAAAAAAAAAAAAAAA/events").status_code == 404


def test_disk_space_guard(make_client, runner):
    client = make_client(min_free_disk_mb=10**9)
    res = client.post("/api/analyze", json={"url": "https://video.example.com/x"})
    r = client.post("/api/jobs", json={"analysis_id": res.json()["analysis_id"], "option_id": "video-best-mp4"})
    assert r.status_code == 507 and r.json()["error"]["code"] == "disk_full"


def test_progress_values_are_sanitised(client, runner):
    runner.gate.clear()
    runner.download_script = [{"type": "progress", "fraction": 7.5, "speed": "NaN", "eta": -3, "downloaded": "x"}, "gate",
                              ("file", "media.mp4", PAYLOAD)]
    job = analyze_and_start(client)
    deadline = time.monotonic() + 5
    snap = {}
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job['id']}").json()["job"]
        if snap["progress"]["fraction"] is not None:
            break
        time.sleep(0.02)
    assert snap["progress"]["fraction"] == 1.0
    assert snap["progress"]["speed"] is None and snap["progress"]["eta"] is None
    assert snap["progress"]["downloaded"] is None
    runner.gate.set()


def test_processing_stage_has_no_fake_percent(client, runner):
    runner.gate.clear()
    runner.download_script = [PROGRESS_1, {"type": "stage", "stage": "converting"}, "gate", ("file", "media.mp3", b"ID3" + b"0" * 100)]
    job = analyze_and_start(client, "audio-mp3")
    snap = wait_for_status(client, job["id"], {"processing"})
    assert snap["stage"] == "converting"
    assert snap["progress"]["fraction"] is None
    runner.gate.set()
    done = wait_for_status(client, job["id"], {"completed"})
    assert done["file"]["ext"] == "mp3"
    res = client.get(done["file"]["url"])
    assert res.headers["content-type"] == "audio/mpeg"
