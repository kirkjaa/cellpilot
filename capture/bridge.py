"""The capture bridge: a small HTTP service on localhost for the Capture page.

    python -m capture.bridge [--config capture.json] [--port 8095]

Endpoints are listed in docs/contract.md ("Capture bridge"). The page is served by the main app
(another port), so the bridge answers CORS for localhost origins only. It binds 127.0.0.1.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import config as cfgmod
from . import convert, sidecar
from .client import LinkError
from .protocol import PRESETS
from .rig import MAX_SECONDS, MIN_SECONDS, Rig, Take

log = logging.getLogger("capture.bridge")
VERSION = "0.1.0"
DEFAULT_PORT = 8095
LOCAL_ORIGIN = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


class PreviewIn(BaseModel):
    on: bool = True
    fps: float = Field(4.0, ge=1, le=10)
    max_width: int = Field(640, ge=0, le=1920)


class ConfigureIn(BaseModel):
    preset: str


class TakeIn(BaseModel):
    name: str = Field("kitting", max_length=80)
    seconds: float = Field(20.0, ge=MIN_SECONDS, le=MAX_SECONDS)


class CheckIn(BaseModel):
    folder: str
    clip: Optional[str] = None
    ref: Optional[str] = None


class SessionIn(BaseModel):
    sid: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    name: str = Field(..., min_length=1, max_length=120)
    take_id: Optional[str] = None
    folder: Optional[str] = None
    clip: Optional[str] = None
    ref: Optional[str] = None
    overwrite: bool = False
    scenario: str = Field("", pattern=r"^[a-z0-9-]{0,40}$")
    brief: str = Field("", max_length=600)


def create_app(cfg: Optional[cfgmod.Config] = None) -> FastAPI:
    cfg = cfg or cfgmod.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .console import ConsoleRig  # here so a LAN-only bridge never imports the console code
        rig = ConsoleRig(cfg) if cfg.console else Rig(cfg)
        app.state.rig = rig
        await rig.start()
        log.info("bridge up: %d cameras, controller %s, offload %s, samples %s", len(cfg.cameras),
                 cfg.controller_url, cfg.offload_dir, cfg.samples_dir)
        try:
            yield
        finally:
            await rig.stop()

    app = FastAPI(title="CellPilot capture bridge", version=VERSION, lifespan=lifespan,
                  docs_url="/api/capture/docs", openapi_url="/api/capture/openapi.json", redoc_url=None)
    # The page runs on another local port (the main app), so every call is cross-origin; Chrome
    # also sends a private-network preflight for a localhost target.
    app.add_middleware(CORSMiddleware, allow_origin_regex=LOCAL_ORIGIN, allow_methods=["GET", "POST"],
                       allow_headers=["*"], allow_private_network=True, max_age=600)

    def rig() -> Rig:
        return app.state.rig

    def take_or_404(tid: str) -> Take:
        t = rig().takes.get(tid)
        if t is None:
            raise HTTPException(404, "Take not found")
        return t

    def node_or_404(nid: str):
        link = rig().nodes.get(nid)
        if link is None:
            raise HTTPException(404, "Camera not found")
        return link

    # ---------------------------------------------------------------- status

    @app.get("/api/capture/health")
    async def health() -> dict:
        r = rig()
        return {"ok": True, "bridge": "cellpilot-capture", "version": VERSION, "cameras": len(r.nodes),
                "cameras_online": sum(1 for l in r.nodes.values() if l.online),
                "controller_online": r.controller.online, "config": str(cfg.path) if cfg.path else None,
                "ffmpeg": convert.ffmpeg_available(), "console": getattr(r, "console_state", None)}

    @app.get("/api/capture/config")
    async def get_config() -> dict:
        return cfg.public()

    @app.get("/api/capture/cameras")
    async def cameras() -> list[dict]:
        return rig().cameras()

    @app.post("/api/capture/cameras/ping")
    async def ping() -> dict:
        return await rig().ping_all()

    @app.get("/api/capture/controller")
    async def controller() -> dict:
        return await rig().controller_status()

    @app.post("/api/capture/preview")
    async def preview(body: PreviewIn) -> dict:
        return {"preview": rig().preview | {"on": body.on}, "nodes": await rig().set_preview(body.on, body.fps, body.max_width)}

    @app.post("/api/capture/configure")
    async def configure(body: ConfigureIn) -> dict:
        if not re.match(r"^[A-Za-z0-9]{1,16}$", body.preset):
            raise HTTPException(422, "preset: letters and digits")
        results = await rig().configure(body.preset)
        return {"preset": body.preset, "nodes": results}

    @app.get("/api/capture/presets")
    async def presets() -> list[str]:
        return list(PRESETS)

    # ---------------------------------------------------------------- previews

    @app.get("/api/capture/cameras/{nid}/preview.jpg")
    async def preview_jpg(nid: str) -> Response:
        link = node_or_404(nid)
        if link.last_preview is None:
            raise HTTPException(404, "No preview frame yet")
        f = link.last_preview
        return Response(f.jpeg, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Preview-Seq": str(f.seq), "X-Preview-Mono-Ns": str(f.mono_ns)})

    @app.get("/api/capture/cameras/{nid}/preview.mjpeg")
    async def preview_mjpeg(nid: str) -> StreamingResponse:
        link = node_or_404(nid)
        boundary = "cellpilotframe"

        async def frames():
            last = -1
            while True:
                if link.preview_count != last and link.last_preview is not None:
                    last = link.preview_count
                    jpeg = link.last_preview.jpeg
                    yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"
                cond = link.preview_cond
                if cond is None:
                    await asyncio.sleep(0.25)
                    continue
                try:
                    async with cond:
                        await asyncio.wait_for(cond.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass  # no frame for a while: keep the connection, try again
        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={boundary}",
                                 headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- takes

    @app.get("/api/capture/takes")
    async def takes() -> list[dict]:
        return [t.to_dict() for t in rig().takes.values()]

    @app.post("/api/capture/takes", status_code=202)
    async def start_take(body: TakeIn):
        try:
            take = await rig().start_take(body.name, body.seconds)
        except LinkError as e:
            return JSONResponse({"detail": str(e)}, status_code=409)
        return take.to_dict()

    @app.get("/api/capture/takes/{tid}")
    async def get_take(tid: str) -> dict:
        return take_or_404(tid).to_dict()

    @app.post("/api/capture/takes/{tid}/stop")
    async def stop_take(tid: str) -> dict:
        return (await rig().stop_take(take_or_404(tid))).to_dict()

    @app.post("/api/capture/takes/{tid}/check")
    async def check_take(tid: str) -> dict:
        take = take_or_404(tid)
        try:
            await asyncio.to_thread(rig().check_take, take)
        except sidecar.SidecarError as e:
            raise HTTPException(409, str(e)) from None
        return take.to_dict()

    @app.post("/api/capture/check")
    async def check_folder(body: CheckIn) -> dict:
        folder = Path(body.folder).expanduser()
        if not folder.is_dir():
            raise HTTPException(404, f"folder not found: {folder}")
        try:
            chk = await asyncio.to_thread(sidecar.check_session, folder, body.clip, body.ref)
        except sidecar.SidecarError as e:
            raise HTTPException(409, str(e)) from None
        return {"folder": str(folder), "check": chk.to_dict()}

    @app.get("/api/capture/folders")
    async def folders() -> dict:
        """Takes already on this PC: {session folder: [clip names]} under the offload dir."""
        out: dict[str, list[str]] = {}
        root = cfg.offload_dir
        if root.is_dir():
            for sub in sorted(p for p in root.iterdir() if p.is_dir()):
                clips = sorted({m.group(1) for p in sub.glob("*.jsonl") for m in [re.match(r"^(.+)_[^_]+\.jsonl$", p.name)] if m})
                if clips:
                    out[sub.name] = clips
        return {"offload_dir": str(root), "folders": out}

    # ---------------------------------------------------------------- make session

    @app.post("/api/capture/sessions", status_code=202)
    async def make_session(body: SessionIn):
        r = rig()
        folder: Optional[Path] = None
        clip = body.clip
        if body.take_id:
            take = take_or_404(body.take_id)
            if take.state != "stopped":
                raise HTTPException(409, f"the take is {take.state}, not stopped")
            f = r.find_take_folder(take)
            if f is None:
                raise HTTPException(409, f"the clips of {take.clip} are not in {cfg.offload_dir} yet")
            folder, clip = f, take.clip
        elif body.folder:
            folder = Path(body.folder).expanduser()
            if not folder.is_dir():
                raise HTTPException(404, f"folder not found: {folder}")
        else:
            raise HTTPException(422, "give a take_id or a folder")
        try:
            job = r.start_job(folder, clip, body.sid, body.name, body.ref, body.overwrite, body.take_id,
                              scenario=body.scenario, brief=body.brief)
        except convert.ConvertError as e:
            raise HTTPException(409, str(e)) from None
        return job

    @app.get("/api/capture/sessions/{jid}")
    async def get_job(jid: str) -> dict:
        job = rig().jobs.get(jid)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.get("/api/capture/sessions")
    async def jobs() -> list[dict]:
        return list(rig().jobs.values())

    return app


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="CellPilot capture bridge (localhost only).")
    ap.add_argument("--config", default=None, help="capture.json (default: ./capture.json or $CELLPILOT_CAPTURE_CONFIG)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CELLPILOT_CAPTURE_PORT", DEFAULT_PORT)))
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = cfgmod.load(a.config)
    except (cfgmod.ConfigError, ValueError, OSError) as e:
        raise SystemExit(f"config: {e}")
    if not cfg.cameras:
        log.warning("no cameras configured (%s): the page will show no camera cards",
                    cfg.path or cfgmod.DEFAULT_PATH)
    import uvicorn

    uvicorn.run(create_app(cfg), host="127.0.0.1", port=a.port, log_level=a.log_level)


if __name__ == "__main__":
    main()
