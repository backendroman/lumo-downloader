from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE

URL = "https://video.example.com/watch?v=abc123"


def test_index_renders_and_sets_session(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Lumo" in res.text
    assert "Найти" in res.text
    csp = res.headers["content-security-policy"]
    script_src = csp.split("script-src")[1].split(";")[0]
    assert "'self'" in script_src and "unsafe-eval" not in script_src and "unsafe-inline" not in script_src
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"


def test_session_cookie_flags(client):
    fresh = TestClient(client.app)
    cookie = fresh.get("/").headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_static_assets_served(client):
    for path in ("/static/css/style.css", "/static/js/app.js", "/static/js/theme.js",
                 "/static/js/vendor/alpine-csp.min.js", "/static/assets/logo.svg"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert len(res.content) > 100, path


def test_downloads_dir_not_exposed(client):
    assert client.get("/static/../data/downloads/").status_code == 404
    assert client.get("/downloads/").status_code == 404


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["ffmpeg"] is True and body["yt_dlp"]


def test_analyze_success(client, runner):
    res = client.post("/api/analyze", json={"url": URL})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["media"]["title"].startswith("Тестовое видео")
    assert data["media"]["duration"] == 212
    assert data["media"]["thumbnail"] == "https://img.example.com/t.jpg"
    assert data["media"]["source"] == "Youtube"
    ids = [o["id"] for o in data["options"]]
    assert "video-best-mp4" in ids and "audio-mp3" in ids
    assert "recipe" not in res.text and "bv*" not in res.text
    assert runner.requests[-1]["action"] == "analyze"


def test_analyze_hides_missing_metadata(client, runner):
    runner.analyze_result = {"type": "info", "data": {
        "title": None, "thumbnail": "http://insecure.example.com/t.jpg", "duration": None,
        "formats": [{"format_id": "a", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 480, "protocol": "https"}],
    }}
    media = client.post("/api/analyze", json={"url": URL}).json()["media"]
    assert media["title"] == "Без названия"
    assert media["thumbnail"] is None  # http thumbnail
    assert media["duration"] is None and media["uploader"] is None


def test_analyze_invalid_url(client, runner):
    for url, code in [("not a url", "invalid_url"), ("ftp://example.com/x", "unsupported_scheme"),
                      ("http://127.0.0.1/", "forbidden_target"), ("http://169.254.169.254/", "forbidden_target"),
                      ("http://internal.example.com/v", "forbidden_target")]:
        res = client.post("/api/analyze", json={"url": url})
        assert res.status_code == 422, url
        assert res.json()["error"]["code"] == code, url
    assert runner.requests == []


def test_analyze_ytdlp_errors_are_mapped(client, runner):
    cases = {
        "unsupported": "ERROR: Unsupported URL: https://x",
        "auth_required": "ERROR: [vimeo] 1: This video is private. Sign in",
        "unavailable": "ERROR: [youtube] x: Video unavailable",
    }
    for code, detail in cases.items():
        runner.analyze_result = {"type": "error", "code": code, "detail": detail}
        res = client.post("/api/analyze", json={"url": URL})
        assert res.json()["ok"] is False and res.json()["error"]["code"] == code
        assert "ERROR:" not in res.text and "Traceback" not in res.text


def test_analyze_no_formats(client, runner):
    runner.analyze_result = {"type": "info", "data": {"title": "x", "formats": []}}
    res = client.post("/api/analyze", json={"url": URL})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "no_formats"


def test_analyze_live_and_too_long(client, runner):
    base = runner.analyze_result["data"]
    runner.analyze_result = {"type": "info", "data": {**base, "is_live": True}}
    assert client.post("/api/analyze", json={"url": URL}).json()["error"]["code"] == "live"
    runner.analyze_result = {"type": "info", "data": {**base, "duration": 10**7}}
    assert client.post("/api/analyze", json={"url": URL}).json()["error"]["code"] == "too_long"


def test_analyze_timeout(client, runner):
    runner.raise_timeout = True
    res = client.post("/api/analyze", json={"url": URL})
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "timeout"


def test_requires_session_cookie(client):
    fresh = TestClient(client.app)
    assert fresh.post("/api/analyze", json={"url": URL}).status_code == 403


def test_rejects_cross_origin(client):
    res = client.post("/api/analyze", json={"url": URL}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_rejects_extra_fields_and_bad_types(client):
    res = client.post("/api/analyze", json={"url": URL, "ytdlp_args": ["--exec", "rm -rf /"]})
    assert res.status_code == 422 and res.json()["error"]["code"] == "invalid_request"
    assert client.post("/api/analyze", json={"url": 123}).status_code == 422
    bad = client.post("/api/analyze", content=b"garbage", headers={"content-type": "application/json"})
    assert bad.status_code == 422


def test_body_size_limit(client):
    res = client.post("/api/analyze", content=b"{" + b" " * 20000 + b"}", headers={"content-type": "application/json"})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"


def test_rate_limit(make_client):
    c = make_client(analyze_rate_per_minute=2)
    assert c.post("/api/analyze", json={"url": URL}).status_code == 200
    assert c.post("/api/analyze", json={"url": URL}).status_code == 200
    res = c.post("/api/analyze", json={"url": URL})
    assert res.status_code == 429 and res.json()["error"]["code"] == "rate_limited"


def test_unknown_routes_return_json(client):
    res = client.get("/api/nope")
    assert res.status_code == 404 and res.json()["ok"] is False
    assert client.get("/docs").status_code == 404
