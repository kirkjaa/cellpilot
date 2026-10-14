"""DEVELOPMENT ONLY: a mock of the CellPilot API, for building web/ without the real server.

It serves web/ and answers the API in docs/contract.md from web/dev/fixture-analysis.json,
which is hand-written development data, not a model output. Nothing here calls a model,
and the mock never writes to the fixture: sign-offs and live runs live in memory only.
Python standard library only.

    D:\\cellpilot\\.venv\\Scripts\\python.exe web\\dev\\mock_server.py
    then open http://127.0.0.1:8765/

Options:
    --port 8765            port (binds to 127.0.0.1 unless --host is given)
    --media DIR            folder holding cam0.mp4 cam1.mp4 cam2.mp4 (default: spikes/out)
    --runs 3               live runs allowed before POST answers 429
    --stage-seconds 5      time each mocked live-run stage takes
    --fail-live            mocked live runs end with status "failed"
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEV_DIR = Path(__file__).resolve().parent
WEB_DIR = DEV_DIR.parent
REPO_DIR = WEB_DIR.parent
FIXTURE = DEV_DIR / "fixture-analysis.json"

# Explicit types: on Windows, mimetypes reads the registry and can serve .js as text/plain,
# which browsers refuse for ES modules.
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
}
LIVE_STAGES = ["queued", "steps", "verifier", "layout", "robot", "payback"]
CHUNK = 64 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_summary(s: dict) -> dict:
    clips = s.get("clips", [])
    cam0 = next((c for c in clips if c.get("view") == "cam0"), clips[0] if clips else {})
    return {
        "id": s["id"],
        "name": s["name"],
        "kind": s["kind"],
        "synthetic": bool(s.get("synthetic", False)),
        "clips": len(clips),
        "duration_s": cam0.get("duration_s", 0),
    }


class State:
    def __init__(self, fixture: dict, runs: int, stage_s: float, fail_live: bool):
        self.lock = threading.Lock()
        self.sessions = {s["id"]: s for s in fixture["sessions"]}
        self.stored = dict(fixture.get("analyses", {}))  # session id -> stored analysis (live=false)
        self.live: dict[str, dict] = {}  # analysis id -> {"analysis", "result", "t0"}
        self.runs_left = runs
        self.stage_s = stage_s
        self.fail_live = fail_live
        self.counter = 0
        any_result = next((a["result"] for a in self.stored.values() if a.get("result")), None)
        self.template_result = any_result

    def start_live(self, sid: str) -> str:
        self.counter += 1
        aid = f"a-mock-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{self.counter:04d}"
        stored = self.stored.get(sid)
        result = (stored or {}).get("result") or self.template_result
        self.live[aid] = {
            "t0": time.monotonic(),
            "result": copy.deepcopy(result),
            "analysis": {
                "id": aid,
                "session_id": sid,
                "status": "queued",
                "stage": "queued",
                "live": True,
                "model": "mock-live-run (no model called)",
                "started_at": now_iso(),
                "finished_at": None,
                "error": None,
                "usage": {"prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0},
                "result": None,
                "signoffs": {},
            },
        }
        return aid

    def advance(self, run: dict) -> None:
        a = run["analysis"]
        if a["status"] in ("done", "failed"):
            return
        elapsed = time.monotonic() - run["t0"]
        i = int(elapsed // self.stage_s)
        if i < len(LIVE_STAGES):
            a["stage"] = LIVE_STAGES[i]
            a["status"] = "queued" if i == 0 else "running"
            return
        a["finished_at"] = now_iso()
        a["usage"]["seconds"] = round(elapsed, 1)
        if self.fail_live or run["result"] is None:
            # Like the real server: the stage stays where it stopped and the error says so.
            a["status"] = "failed"
            a["stage"] = "verifier"
            a["error"] = "Failed during verifier: mock failure (--fail-live)."
            return
        a["status"] = "done"
        a["stage"] = "done"
        a["result"] = run["result"]

    def find(self, sid: str, aid: str) -> dict | None:
        stored = self.stored.get(sid)
        if stored and stored["id"] == aid:
            return stored
        run = self.live.get(aid)
        if run and run["analysis"]["session_id"] == sid:
            self.advance(run)
            return run["analysis"]
        return None

    def latest(self, sid: str) -> dict | None:
        runs = [r for r in self.live.values() if r["analysis"]["session_id"] == sid]
        for r in runs:
            self.advance(r)
        done = [r for r in runs if r["analysis"]["status"] == "done"]
        if done:
            return max(done, key=lambda r: r["t0"])["analysis"]
        return self.stored.get(sid)


class Handler(BaseHTTPRequestHandler):
    server_version = "CellPilotMock/1"
    state: State
    media_dir: Path

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("HEAD")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if path.startswith("/api/"):
                self.api(method, path)
            elif path.startswith("/media/"):
                self.media(method, path)
            elif method in ("GET", "HEAD"):
                self.static(method, path)
            else:
                self.send_json({"detail": "Method not allowed"}, 405)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser dropped a video request it no longer needs

    # ---- API (docs/contract.md)

    def api(self, method: str, path: str) -> None:
        parts = path.strip("/").split("/")[1:]
        st = self.state
        with st.lock:
            if parts == ["health"] and method == "GET":
                return self.send_json({"ok": True})
            if parts == ["sessions"] and method == "GET":
                return self.send_json([session_summary(s) for s in st.sessions.values()])
            if len(parts) < 2 or parts[0] != "sessions":
                return self.send_json({"detail": "Not found"}, 404)
            sid, rest = parts[1], parts[2:]
            s = st.sessions.get(sid)
            if s is None:
                return self.send_json({"detail": "Session not found"}, 404)
            if rest == [] and method == "GET":
                return self.send_json({
                    "session": s,
                    "clip_urls": {c["view"]: f"/media/{sid}/{c['file']}" for c in s["clips"]},
                    "latest": st.latest(sid),
                    "live_runs_left": st.runs_left,
                })
            if rest == ["analyses"] and method == "POST":
                self.read_body()
                if st.runs_left <= 0:
                    return self.send_json({"detail": "Daily limit reached"}, 429)
                st.runs_left -= 1
                return self.send_json({"id": st.start_live(sid)}, 202)
            if len(rest) >= 2 and rest[0] == "analyses":
                a = st.find(sid, rest[1])
                if a is None:
                    return self.send_json({"detail": "Analysis not found"}, 404)
                if len(rest) == 2 and method == "GET":
                    return self.send_json(a)
                if len(rest) == 4 and rest[2] == "signoffs" and method == "PUT":
                    return self.signoff(a, rest[3])
                if len(rest) == 3 and rest[2] == "export" and method == "GET":
                    payload = {
                        "_note": "Exported from the development mock server.",
                        "session": s,
                        "analysis_id": a["id"],
                        "model": a.get("model"),
                        "live": a.get("live"),
                        "result": a.get("result"),
                        "signoffs": a.get("signoffs", {}),
                        "exported_at": now_iso(),
                    }
                    disposition = f'attachment; filename="cellpilot-{sid}-{a["id"]}.json"'
                    return self.send_json(payload, headers={"Content-Disposition": disposition})
            return self.send_json({"detail": "Not found"}, 404)

    def signoff(self, a: dict, step_id: str) -> None:
        body = self.read_body()
        if a.get("status") != "done" or not a.get("result"):
            return self.send_json({"detail": "The analysis has no result yet"}, 409)
        if step_id not in {s["id"] for s in a["result"].get("steps", [])}:
            return self.send_json({"detail": "Step not found"}, 404)
        if not isinstance(body, dict) or body.get("decision") not in ("approved", "flagged"):
            return self.send_json({"detail": "decision must be 'approved' or 'flagged'"}, 422)
        a.setdefault("signoffs", {})[step_id] = {
            "decision": body["decision"],
            "note": str(body.get("note") or "")[:500],
            "engineer": str(body.get("engineer") or "")[:80],
            "at": now_iso(),
        }
        self.send_json(a)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # ---- clips and static files

    def media(self, method: str, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            return self.send_json({"detail": "Not found"}, 404)
        _, sid, name = parts
        s = self.state.sessions.get(sid)
        if s is None or name not in {c["file"] for c in s["clips"]}:
            return self.send_json({"detail": "Not found"}, 404)
        f = self.media_dir / name
        if not f.is_file():
            return self.send_json({"detail": f"{name} is not in the media folder"}, 404)
        self.send_file(method, f, "video/mp4")

    def static(self, method: str, path: str) -> None:
        rel = path.lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        segments = rel.replace("\\", "/").split("/")
        # web/dev/ (this server and its fixture) is never served: the page must not depend on it.
        if segments[0] == "dev" or any(p.startswith(".") for p in segments):
            return self.send_json({"detail": "Not found"}, 404)
        f = (WEB_DIR / rel).resolve()
        if not f.is_relative_to(WEB_DIR) or not f.is_file():
            return self.send_json({"detail": "Not found"}, 404)
        self.send_file(method, f, MIME.get(f.suffix.lower(), "application/octet-stream"))

    def send_file(self, method: str, f: Path, ctype: str) -> None:
        size = f.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:  # suffix range: the last n bytes
                    n = int(m[2])
                    start, end = (max(0, size - n), size - 1) if n > 0 else (size, size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            # several ranges or another unit: ignore the header and send the whole file
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if method == "HEAD":
            return
        with f.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def send_json(self, obj, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main() -> None:
    ap = argparse.ArgumentParser(description="CellPilot development mock server (hand-written fixture data).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--media", type=Path, default=REPO_DIR / "spikes" / "out")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--stage-seconds", type=float, default=5.0)
    ap.add_argument("--fail-live", action="store_true")
    args = ap.parse_args()

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    Handler.state = State(fixture, args.runs, max(0.1, args.stage_seconds), args.fail_live)
    Handler.media_dir = args.media.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CellPilot MOCK server, hand-written fixture data: http://{args.host}:{args.port}/")
    print(f"clips from {Handler.media_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
