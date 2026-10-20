"""Manual smoke test with a real link

python scripts/e2e.py http://127.0.0.1:8000 <url> [option-id]
"""

import json
import sys
import time

import httpx


def main() -> int:
    base, url = sys.argv[1], sys.argv[2]
    wanted = sys.argv[3] if len(sys.argv) > 3 else None
    with httpx.Client(base_url=base, timeout=120) as client:
        client.get("/").raise_for_status()
        t0 = time.monotonic()
        res = client.post("/api/analyze", json={"url": url})
        data = res.json()
        print(f"analyze: HTTP {res.status_code} in {time.monotonic() - t0:.1f}s")
        if not data.get("ok"):
            print("  error:", data["error"])
            return 1
        print("  title:", data["media"]["title"], "| source:", data["media"]["source"],
              "| duration:", data["media"]["duration"])
        for opt in data["options"]:
            print(f"  option {opt['id']:<22} {opt['label']:<24} {opt['detail']:<28} size={opt['size']}")
        option = wanted or data["options"][0]["id"]
        res = client.post("/api/jobs", json={"analysis_id": data["analysis_id"], "option_id": option})
        job = res.json()["job"]
        print(f"job: HTTP {res.status_code} id={job['id'][:8]}… option={option}")

        last_stage, events = None, 0
        with client.stream("GET", f"/api/jobs/{job['id']}/events", timeout=None) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                snap = json.loads(line[6:])
                if "status" not in snap or "progress" not in snap:
                    continue
                events += 1
                p = snap["progress"]
                if snap["stage"] != last_stage or events % 20 == 0:
                    frac = f"{p['fraction'] * 100:.1f}%" if p["fraction"] is not None else "-"
                    print(f"  sse: status={snap['status']:<10} stage={snap['stage']:<12} {frac:>7} "
                          f"bytes={p['downloaded']} speed={p['speed']}")
                    last_stage = snap["stage"]
                if snap["status"] in {"completed", "failed", "cancelled"}:
                    job = snap
                    break
        print(f"sse events received: {events}")
        if job["status"] != "completed":
            print("  job failed:", job.get("error"))
            return 1
        res = client.get(job["file"]["url"])
        print(f"file: HTTP {res.status_code} {len(res.content)} bytes, type={res.headers['content-type']}")
        print("  disposition:", res.headers["content-disposition"])
        assert len(res.content) == job["file"]["size"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
