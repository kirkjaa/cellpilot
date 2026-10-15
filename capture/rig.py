"""The rig as the bridge sees it: the camera links, the controller link, takes and jobs.

A take goes arming -> armed -> recording -> stopped (or failed). The controller's `cmd.fire`
arms every camera and sends the RF trigger; the cameras then tell us `evt.recording_started`
when they consumed the edge and `evt.recording_stopped` when the clip is closed. Once every
clip has stopped, the bridge looks for the files in the offload folder, reads the sidecars and
runs the offset check. "Make session" converts one take into a session folder in a thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import convert, sidecar
from .client import Link, LinkError
from .config import Config
from .protocol import (CMD_CONFIGURE, CMD_FIRE, CMD_LIST_NODES, CMD_START_PREVIEW, CMD_STOP, CMD_STOP_PREVIEW,
                       EVT_RECORDING_STARTED, EVT_RECORDING_STOPPED, EVT_RF_EDGE, Envelope)

log = logging.getLogger("capture.rig")

FIRE_TIMEOUT_S = 30.0          # the controller stops, arms and fires inside this
START_GRACE_S = 6.0            # from the fire ack to the first evt.recording_started
STOP_GRACE_S = 15.0            # after max_seconds, before we send cmd.stop ourselves
OFFLOAD_WAIT_S = 20.0          # how long we keep looking for the files after the stop
NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
MIN_SECONDS, MAX_SECONDS = 3.0, 900.0


def clean_name(name: str, default: str = "cellpilot") -> str:
    s = NAME_RE.sub("-", (name or "").strip()).strip("-_")[:40]
    return s or default


@dataclass
class Take:
    id: str
    name: str
    session: str
    clip: str
    seconds: float
    state: str = "arming"                 # arming | armed | recording | stopped | failed
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    fired_at: Optional[float] = None
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    edge_id: Optional[int] = None
    rf_acked: Optional[bool] = None
    armed: list[str] = field(default_factory=list)
    not_armed: dict[str, str] = field(default_factory=dict)
    cameras: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, int] = field(default_factory=dict)   # node id -> edge mono ns (evt.rf_edge)
    offloaded: bool = False
    folder: Optional[str] = None
    check: Optional[dict] = None
    check_error: Optional[str] = None

    def fail(self, why: str) -> None:
        self.state = "failed"
        self.error = why
        self.stopped_at = self.stopped_at or time.time()

    @property
    def started(self) -> list[str]:
        return [n for n, c in self.cameras.items() if c.get("started")]

    @property
    def all_stopped(self) -> bool:
        return bool(self.started) and all(c.get("stopped") for n, c in self.cameras.items() if c.get("started"))

    def to_dict(self) -> dict:
        now = time.time()
        elapsed = None
        if self.started_at is not None:
            elapsed = round((self.stopped_at or now) - self.started_at, 1)
        return {"id": self.id, "name": self.name, "session": self.session, "clip": self.clip,
                "seconds": self.seconds, "state": self.state, "error": self.error,
                "created_at": self.created_at, "fired_at": self.fired_at, "started_at": self.started_at,
                "stopped_at": self.stopped_at, "elapsed_s": elapsed, "edge_id": self.edge_id,
                "rf_acked": self.rf_acked, "armed": list(self.armed), "not_armed": dict(self.not_armed),
                "cameras": {n: dict(c) for n, c in self.cameras.items()}, "offloaded": self.offloaded,
                "folder": self.folder, "check": self.check, "check_error": self.check_error}


class Rig:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.nodes: dict[str, Link] = {c.id: Link(c.id, c.url, on_event=self._node_event, on_connect=self._node_connect)
                                       for c in cfg.cameras}
        self.controller = Link("controller", cfg.controller_url, kind="controller", on_event=self._controller_event)
        self.takes: dict[str, Take] = {}
        self.jobs: dict[str, dict] = {}
        self.preview: dict = {"on": False, "fps": 4, "max_width": 640}
        self._take_tasks: set[asyncio.Task] = set()
        self._counter = 0

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        for link in self.nodes.values():
            link.start()
        self.controller.start()

    async def stop(self) -> None:
        for t in list(self._take_tasks):
            t.cancel()
        await asyncio.gather(*[link.stop() for link in [*self.nodes.values(), self.controller]], return_exceptions=True)

    # ---------------------------------------------------------------- cameras

    def camera_status(self, link: Link) -> dict:
        st = link.last_state or {}
        cap = st.get("capture") or {}
        return {
            "id": link.node_id, "url": link.url, "online": link.online, "error": None if link.online else link.error,
            "state": st.get("state"), "preset": st.get("preset"), "armed": st.get("armed"),
            "battery": st.get("battery"), "chrony_offset_ms": st.get("chrony_offset_ms"),
            "rf_ok": st.get("rf_ok"), "simulated": bool(st.get("simulated")),
            "capture": {k: cap.get(k) for k in ("dequeued", "dropped", "expected_per_sec", "last_frame_age_s", "pump_alive")},
            "heartbeat_age_s": link.heartbeat_age_s(), "preview_age_s": link.preview_age_s(),
            "preview_frames": link.preview_count, "connects": link.connects,
        }

    def cameras(self) -> list[dict]:
        return [self.camera_status(link) for link in self.nodes.values()]

    async def ping_all(self) -> dict[str, dict]:
        async def one(link: Link) -> dict:
            try:
                return await link.ping()
            except LinkError as e:
                return {"error": str(e)}
        results = await asyncio.gather(*[one(l) for l in self.nodes.values()])
        return dict(zip(self.nodes, results))

    async def fan_out(self, type_: str, timeout: float, **payload) -> dict[str, dict]:
        """Send one command to every camera; {id: {"ok": bool, "reason"?, "payload"}}."""
        async def one(link: Link) -> dict:
            try:
                r = await link.cmd(type_, timeout=timeout, **payload)
            except LinkError as e:
                return {"ok": False, "reason": str(e)}
            return {"ok": r.ok, "reason": r.reason or None, "type": r.type, "payload": r.payload}
        results = await asyncio.gather(*[one(l) for l in self.nodes.values()])
        return dict(zip(self.nodes, results))

    async def configure(self, preset: str) -> dict[str, dict]:
        return await self.fan_out(CMD_CONFIGURE, 20.0, preset=preset)

    async def set_preview(self, on: bool, fps: float = 4.0, max_width: int = 640) -> dict[str, dict]:
        self.preview = {"on": on, "fps": fps, "max_width": max_width}
        if on:
            return await self.fan_out(CMD_START_PREVIEW, 4.0, fps=fps, gray=False, max_width=max_width)
        return await self.fan_out(CMD_STOP_PREVIEW, 4.0)

    async def _node_connect(self, link: Link) -> None:
        """A camera that (re)connected while the page wants previews gets the preview request again."""
        if self.preview.get("on"):
            try:
                await link.cmd(CMD_START_PREVIEW, timeout=4.0, fps=self.preview["fps"], gray=False,
                               max_width=self.preview["max_width"])
            except LinkError as e:
                log.warning("%s: start_preview after connect failed: %s", link.node_id, e)

    async def controller_status(self) -> dict:
        out = {"url": self.controller.url, "online": self.controller.online,
               "error": None if self.controller.online else self.controller.error, "nodes": None}
        if self.controller.online:
            try:
                r = await self.controller.cmd(CMD_LIST_NODES, timeout=4.0)
                out["nodes"] = r.payload.get("nodes") if r.ok else None
                if not r.ok:
                    out["error"] = r.reason
            except LinkError as e:
                out["error"] = str(e)
        return out

    # ---------------------------------------------------------------- events

    def _take_for(self, session: Optional[str], clip: Optional[str]) -> Optional[Take]:
        for t in reversed(list(self.takes.values())):
            if t.state in ("arming", "armed", "recording") and t.clip == clip and (session is None or t.session == session):
                return t
        return None

    async def _node_event(self, link: Link, env: Envelope) -> None:
        p = env.payload
        nid = str(p.get("node_id") or link.node_id)
        if env.type == EVT_RECORDING_STARTED:
            take = self._take_for(p.get("session"), p.get("clip"))
            if take is None:
                return
            cam = take.cameras.setdefault(nid, {})
            cam.update({"started": True, "started_at": time.time(), "edge_id": p.get("edge_id")})
            if take.state in ("arming", "armed"):
                take.state = "recording"
                take.started_at = time.time()
        elif env.type == EVT_RECORDING_STOPPED:
            take = self._take_for(None, p.get("clip"))
            if take is None or not p.get("clip"):
                return
            cam = take.cameras.setdefault(nid, {})
            cam.update({"stopped": True, "stopped_at": time.time(), "path": p.get("path"), "frames": p.get("frames"),
                        "duration_s": p.get("duration_s"), "bytes": p.get("bytes")})
            if take.all_stopped and take.state == "recording":
                take.state = "stopped"
                take.stopped_at = time.time()
        elif env.type == EVT_RF_EDGE:
            for t in reversed(list(self.takes.values())):
                if t.state in ("armed", "recording") and t.edge_id is not None and p.get("edge_id") == t.edge_id:
                    t.edges[nid] = int(p.get("mono_ns") or 0)
                    break

    async def _controller_event(self, link: Link, env: Envelope) -> None:
        # The controller relays every node event to its clients; a camera we are not linked to
        # directly still reaches us this way.
        if env.type in (EVT_RECORDING_STARTED, EVT_RECORDING_STOPPED, EVT_RF_EDGE):
            nid = env.payload.get("node_id")
            if nid and nid not in self.nodes:
                await self._node_event(link, env)

    # ---------------------------------------------------------------- takes

    def _next_clip(self, session: str) -> str:
        """take_NNN, continuing after any take already in the offload folder for this session."""
        used: set[int] = set()
        folder = self.cfg.offload_dir / session
        if folder.is_dir():
            for p in folder.glob("take_*.jsonl"):
                m = re.match(r"take_(\d{3})_", p.name)
                if m:
                    used.add(int(m.group(1)))
        for t in self.takes.values():
            m = re.match(r"take_(\d{3})$", t.clip)
            if t.session == session and m:
                used.add(int(m.group(1)))
        return f"take_{(max(used) + 1) if used else 1:03d}"

    async def start_take(self, name: str, seconds: float) -> Take:
        if any(t.state in ("arming", "armed", "recording") for t in self.takes.values()):
            raise LinkError("a take is already in progress")
        if not self.controller.online:
            raise LinkError(f"the controller is offline ({self.controller.error or 'not connected'}): it owns the RF trigger")
        seconds = float(seconds)
        cap = min(MAX_SECONDS, getattr(self.cfg.console, "max_take_seconds", MAX_SECONDS) or MAX_SECONDS)
        if not MIN_SECONDS <= seconds <= cap:
            raise LinkError(f"seconds must be between {MIN_SECONDS:g} and {cap:g}")
        session = clean_name(name)
        clip = self._next_clip(session)
        self._counter += 1
        take = Take(id=f"t-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}", name=name.strip() or session,
                    session=session, clip=clip, seconds=seconds)
        self.takes[take.id] = take
        task = asyncio.create_task(self._run_take(take), name=f"take-{take.id}")
        self._take_tasks.add(task)
        task.add_done_callback(self._take_tasks.discard)
        return take

    async def _run_take(self, take: Take) -> None:
        try:
            await self._fire(take)
            if take.state == "failed":
                return
            await self._watch(take)
            if take.state == "stopped":
                await self._offload_check(take)
        except asyncio.CancelledError:
            if take.state in ("arming", "armed", "recording"):
                take.fail("the bridge stopped")
            raise
        except Exception as e:
            log.exception("take %s failed", take.id)
            take.fail(f"{type(e).__name__}: {e}")

    async def _fire(self, take: Take) -> None:
        try:
            r = await self.controller.cmd(CMD_FIRE, timeout=FIRE_TIMEOUT_S, session=take.session, clip=take.clip,
                                          max_seconds=take.seconds)
        except LinkError as e:
            take.fail(f"fire failed: {e}")
            return
        p = r.payload
        take.fired_at = time.time()
        take.not_armed = {str(k): str(v) for k, v in (p.get("not_armed") or {}).items()}
        if not r.ok:
            take.fail(f"the controller refused to fire: {r.reason}"
                      + (f" (not armed: {', '.join(f'{k}: {v}' for k, v in take.not_armed.items())})" if take.not_armed else ""))
            return
        take.edge_id = p.get("edge_id")
        take.rf_acked = p.get("rf_acked")
        take.armed = [str(n) for n in (p.get("armed") or [])]
        for nid in take.armed:
            take.cameras.setdefault(nid, {"started": False, "stopped": False})
        if take.state == "arming":  # a fast camera may already have reported recording_started
            take.state = "armed"

    async def _watch(self, take: Take) -> None:
        # The trigger is immediate: a camera that heard it starts within a frame or two.
        deadline = time.monotonic() + START_GRACE_S
        while time.monotonic() < deadline and not take.started:
            await asyncio.sleep(0.1)
        if not take.started:
            take.fail("no camera started recording after the trigger"
                      + ("" if take.rf_acked else " (the dongle did not acknowledge the RF send)"))
            await self._stop_all()
            return
        missing = [n for n in take.armed if n not in take.started]
        if missing:
            log.warning("take %s: armed but not recording: %s", take.id, missing)
        deadline = time.monotonic() + take.seconds + STOP_GRACE_S
        while time.monotonic() < deadline and take.state == "recording":
            await asyncio.sleep(0.2)
        if take.state == "recording":
            log.warning("take %s: clips did not stop by themselves; sending stop", take.id)
            await self._stop_all()
            deadline = time.monotonic() + STOP_GRACE_S
            while time.monotonic() < deadline and take.state == "recording":
                await asyncio.sleep(0.2)
            if take.state == "recording":
                take.fail("the cameras did not report the end of the clip")

    async def _stop_all(self) -> None:
        try:
            if self.controller.online:
                await self.controller.cmd(CMD_STOP, timeout=15.0)
                return
        except LinkError as e:
            log.warning("stop through the controller failed: %s", e)
        await self.fan_out(CMD_STOP, 8.0)

    async def stop_take(self, take: Take) -> Take:
        if take.state not in ("arming", "armed", "recording"):
            return take
        await self._stop_all()
        if take.state in ("arming", "armed"):
            take.fail("stopped before any camera recorded")
        return take

    # ---------------------------------------------------------------- after the take

    def _candidate_folders(self, take: Take) -> list[Path]:
        out: list[Path] = []
        for cam in take.cameras.values():
            p = cam.get("path")
            if p:
                try:
                    parent = Path(str(p)).parent
                    if parent.is_dir():
                        out.append(parent)
                except (OSError, ValueError):
                    pass
        out.append(self.cfg.offload_dir / take.session)
        out.append(self.cfg.offload_dir)
        seen, uniq = set(), []
        for p in out:
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return uniq

    def find_take_folder(self, take: Take) -> Optional[Path]:
        """The folder on this PC holding the take's clips and sidecars, once offloaded."""
        want = set(take.started) or set(take.armed)
        for folder in self._candidate_folders(take):
            if not folder.is_dir():
                continue
            try:
                _clip, sidecars = sidecar.find_clips(folder, take.clip)
            except sidecar.SidecarError:
                continue
            have = {n for n, s in sidecars.items() if s.with_suffix(".mp4").exists()}
            if want and want <= have:
                return folder
        return None

    def check_take(self, take: Take, *, final: bool = True) -> dict:
        """Run (again) the offset check on the take's files and store the result on the take.
        A failure is recorded on the take only when `final`: the wait loop after a take keeps
        transient misses (files still on their way) to itself, so a poll never sees a stale
        error. The take is marked offloaded only together with its check."""
        folder = self.find_take_folder(take)
        if folder is None:
            msg = (f"the clips of {take.clip} are not in {self.cfg.offload_dir} yet "
                   "(offload them from the cameras first)")
            if final:
                take.check_error = msg
            raise sidecar.SidecarError(msg)
        try:
            chk = sidecar.check_session(folder, take.clip, expected=take.armed or take.started)
        except sidecar.SidecarError as e:
            if final:
                take.folder, take.check_error = str(folder), str(e)
            raise
        take.check = chk.to_dict()
        take.check_error = None
        take.folder = str(folder)
        take.offloaded = True  # last, so "offloaded" always comes with a check
        return take.check

    async def _offload_check(self, take: Take) -> None:
        deadline = time.monotonic() + OFFLOAD_WAIT_S
        while True:
            final = time.monotonic() + 1.0 >= deadline
            try:
                await asyncio.to_thread(self.check_take, take, final=final)
                return
            except sidecar.SidecarError:
                if final:
                    return
                await asyncio.sleep(1.0)

    # ---------------------------------------------------------------- make session

    def start_job(self, folder: Path, clip: Optional[str], sid: str, name: str, ref: Optional[str],
                  overwrite: bool, take_id: Optional[str] = None, scenario: str = "", brief: str = "") -> dict:
        out_dir = self.cfg.samples_dir / sid
        if (out_dir / "session.json").exists() and not overwrite:
            raise convert.ConvertError(f"session {sid!r} already exists under {self.cfg.samples_dir}; choose another id")
        job = {"id": f"j-{uuid.uuid4().hex[:8]}", "status": "queued", "sid": sid, "name": name, "folder": str(folder),
               "clip": clip, "take_id": take_id, "scenario": scenario, "brief": brief,
               "out_dir": str(out_dir), "error": None, "log": [], "session": None,
               "url": f"#/s/{sid}", "started_at": time.time(), "finished_at": None}
        self.jobs[job["id"]] = job
        asyncio.create_task(self._run_job(job, folder, clip, ref), name=f"job-{job['id']}")
        return job

    async def _run_job(self, job: dict, folder: Path, clip: Optional[str], ref: Optional[str]) -> None:
        job["status"] = "running"
        try:
            session = await asyncio.to_thread(
                convert.make_session, folder, Path(job["out_dir"]), job["sid"], job["name"], clip=clip, ref=ref,
                scenario=job.get("scenario", ""), brief=job.get("brief", ""),
                progress=lambda msg: job["log"].append(msg))
            job["session"], job["status"] = session, "done"
        except convert.ConvertError as e:
            job["status"], job["error"] = "failed", str(e)
        except Exception as e:
            log.exception("job %s failed", job["id"])
            job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
        job["finished_at"] = time.time()
