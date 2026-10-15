"""FastAPI app: the API under /api/, clips under /media/, and the page from web/ at /.

    uvicorn app.main:app --port 8080        or        python -m app.main   (binds $PORT, default 8080)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .limits import DailyLimit
from .schemas import SignoffIn
from .store import ID_RE, ROOT, Samples, Store, analysis_key, latest_key, make_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cellpilot")

INTERRUPTED = "Interrupted: the server restarted before this analysis finished."


CAPTURE_BRIDGE_PATH = "/capture-bridge"


def capture_bridge_app():
    """The capture bridge mounted inside this service when CELLPILOT_CAPTURE_CONFIG names a config
    (the cloud demo: a simulated M1 rig on NODAL's emulator console). None on a plain server."""
    path = os.environ.get("CELLPILOT_CAPTURE_CONFIG")
    if not path:
        return None
    from capture import bridge as bridgemod
    from capture import config as capcfg
    return bridgemod.create_app(capcfg.load(path))


def web_dir() -> Path:
    """web/dist when the page has a build step, otherwise web/ itself (CELLPILOT_WEB_DIR overrides)."""
    if os.environ.get("CELLPILOT_WEB_DIR"):
        return Path(os.environ["CELLPILOT_WEB_DIR"])
    dist = ROOT / "web" / "dist"
    return dist if (dist / "index.html").is_file() else ROOT / "web"


def create_app(store: Optional[Store] = None, samples: Optional[Samples] = None,
               limit: Optional[DailyLimit] = None) -> FastAPI:
    store = store or make_store()
    samples = samples or Samples()
    limit = limit or DailyLimit(store)
    edit_lock = threading.Lock()
    start_lock = threading.Lock()
    max_running = int(os.environ.get("CELLPILOT_MAX_RUNNING", "1"))
    log.info("store: %s; samples: %s; daily live runs: %d", store.describe(), samples.root, limit.cap)
    bridge_app = capture_bridge_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A mounted app's own lifespan does not run by itself: enter the bridge's here so its rig connects.
        if bridge_app is None:
            yield
            return
        async with bridge_app.router.lifespan_context(bridge_app):
            yield

    app = FastAPI(title="CellPilot", version="0.1.0", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.samples, app.state.limit = store, samples, limit
    origins = [o.strip() for o in os.environ.get(
        "CELLPILOT_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:  # only for a page served by a separate dev server; production is same-origin
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])

    # ---------------------------------------------------------------- helpers

    def session_or_404(sid: str) -> dict:
        session = samples.session(sid)
        if session is None:
            raise HTTPException(404, "Session not found")
        return session

    def find_analysis(sid: str, aid: str) -> Optional[dict]:
        if not ID_RE.match(aid):
            return None
        return store.read_json(analysis_key(sid, aid)) or samples.analysis(sid, aid)

    def analysis_or_404(sid: str, aid: str) -> dict:
        rec = find_analysis(sid, aid)
        if rec is None:
            raise HTTPException(404, "Analysis not found")
        return rec

    def settle(rec: dict) -> dict:
        """A live run left queued or running by a previous process will never finish: mark it failed."""
        if rec.get("live") and rec.get("status") in ("queued", "running") and not pipeline.is_active(rec["id"]):
            key = analysis_key(rec["session_id"], rec["id"])
            fresh = store.read_json(key) or rec  # re-read: the run may have just finished
            if fresh.get("status") in ("queued", "running"):
                fresh = {**fresh, "status": "failed", "error": INTERRUPTED, "finished_at": pipeline.now_iso()}
                store.write_json(key, fresh)
            return fresh
        return rec

    def latest(sid: str) -> Optional[dict]:
        """The sample's stored analysis (its signed-off copy lives in the store); for a session without
        one, the newest finished live run. A visitor's live run is shown to that visitor by its id, so a
        stray live result never replaces the curated sample for everyone who opens it afterwards."""
        stored = [store.read_json(analysis_key(sid, a["id"])) or a
                  for a in samples.analyses(sid) if a.get("status") == "done"]
        if stored:
            return max(stored, key=lambda a: (a.get("finished_at") or "", a["id"]))
        ptr = store.read_json(latest_key(sid))
        if ptr and ID_RE.match(ptr.get("id", "")):
            rec = store.read_json(analysis_key(sid, ptr["id"]))
            if rec and rec.get("status") == "done":
                return rec
        return None

    # --------------------------------------------------------------------- API

    @app.get("/api/health")
    def health() -> dict:
        out: dict = {"ok": True}
        if bridge_app is not None:
            out["capture_bridge"] = CAPTURE_BRIDGE_PATH
        return out

    @app.get("/api/sessions")
    def list_sessions() -> list[dict]:
        out = []
        for sid in samples.ids():
            s = samples.session(sid)
            cam0 = next((c for c in s["clips"] if c["view"] == "cam0"), None)
            out.append({"id": sid, "name": s["name"], "kind": s["kind"], "synthetic": s["synthetic"],
                        "scenario": s.get("scenario", ""),
                        "clips": len(s["clips"]), "duration_s": cam0["duration_s"] if cam0 else 0.0})
        return out

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str) -> dict:
        session = session_or_404(sid)
        return {"session": session,
                "clip_urls": {c["view"]: f"/media/{sid}/{c['file']}" for c in session["clips"]},
                "latest": latest(sid), "live_runs_left": limit.left(), "live_runs_per_day": limit.cap}

    @app.post("/api/sessions/{sid}/analyses", status_code=202)
    def start_analysis(sid: str):
        session_or_404(sid)
        with start_lock:  # check, count and start as one step
            running = pipeline.active_runs()
            same = [aid for aid, s in running.items() if s == sid]
            if same:  # join the run in progress instead of paying for another
                return {"id": same[0]}
            if len(running) >= max_running:
                return JSONResponse({"detail": "Another live run is in progress; try again in a few minutes"},
                                    status_code=429)
            if not limit.take():
                return JSONResponse({"detail": "Daily limit reached"}, status_code=429)
            return {"id": pipeline.start(store, samples, sid)}

    @app.get("/api/sessions/{sid}/analyses/{aid}")
    def get_analysis(sid: str, aid: str) -> dict:
        session_or_404(sid)
        return settle(analysis_or_404(sid, aid))

    @app.put("/api/sessions/{sid}/analyses/{aid}/signoffs/{step_id}")
    def put_signoff(sid: str, aid: str, step_id: str, body: SignoffIn) -> dict:
        session_or_404(sid)
        with edit_lock:
            rec = analysis_or_404(sid, aid)
            if rec.get("status") != "done" or not rec.get("result"):
                raise HTTPException(409, "Analysis is not finished")
            if step_id not in {s["id"] for s in rec["result"]["steps"]}:
                raise HTTPException(404, "Step not found")
            rec.setdefault("signoffs", {})[step_id] = {
                "decision": body.decision, "note": body.note.strip(), "engineer": body.engineer.strip(),
                "at": pipeline.now_iso()}
            store.write_json(analysis_key(sid, aid), rec)  # stored sample analyses get a copy in the store
        return rec

    @app.get("/api/sessions/{sid}/analyses/{aid}/export")
    def export(sid: str, aid: str) -> Response:
        session = session_or_404(sid)
        rec = analysis_or_404(sid, aid)
        if rec.get("status") != "done":
            raise HTTPException(409, "Analysis is not finished")
        body = {
            "exported_at": pipeline.now_iso(),
            "session": session,
            "analysis": {k: rec.get(k) for k in ("id", "session_id", "model", "live", "started_at",
                                                 "finished_at", "usage")},
            "result": rec["result"],
            "signoffs": rec.get("signoffs", {}),
        }
        return Response(json.dumps(body, indent=2, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="cellpilot-{sid}-{aid}.json"'})

    @app.api_route("/media/{sid}/{file}", methods=["GET", "HEAD"])
    def media(sid: str, file: str) -> FileResponse:
        path = samples.clip_path(sid, file)
        if path is None:
            raise HTTPException(404, "Clip not found")
        # Starlette's FileResponse answers Range requests with 206, so the page can seek.
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/dev/{path:path}", include_in_schema=False)
    @app.get("/{name}.test.mjs", include_in_schema=False)
    def development_files(path: str = "", name: str = ""):
        """Mock server, fixture and tests: left out of the image, and never served from a local web/ either."""
        raise HTTPException(404, "Not found")

    if bridge_app is not None:
        app.mount(CAPTURE_BRIDGE_PATH, bridge_app, name="capture")
    # Registered last so /api and /media win; check_dir=False lets the server start before web/ exists.
    app.mount("/", StaticFiles(directory=web_dir(), html=True, check_dir=False), name="web")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
