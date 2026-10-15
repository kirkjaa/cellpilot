"""The bridge against a fake rig: three fake camera nodes and a fake controller speaking the
documented protocol over real WebSockets, in a background event loop."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from capture import config, protocol
from capture.bridge import create_app
from capture.protocol import Envelope

from .test_capture_sidecar import EDGE, write_sidecar

TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16 + b"\xff\xd9"
DELAYS_NS = {"rx-cam1": 10_366_000, "rx-cam2": 806_000, "rx-cam3": 30_700_000}


class FakeNode:
    """A camera node: heartbeats, previews, ping/get_state/configure/preview/arm/stop, and a clip
    with a sidecar when an RF edge arrives for the armed edge id."""

    def __init__(self, node_id: str, rec_dir: Path, delay_ns: int, *, deaf: bool = False, clip_seconds: float = 0.4):
        self.node_id, self.rec_dir, self.delay_ns, self.deaf, self.clip_seconds = node_id, rec_dir, delay_ns, deaf, clip_seconds
        self.state, self.preset, self.armed = "live", "1080p30", None
        self.commands: list[tuple[str, dict]] = []
        self.peers: set = set()
        self.server = None
        self.port = 0
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.tasks = [asyncio.create_task(self.heartbeats()), asyncio.create_task(self.previews())]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.server.close()
        await self.server.wait_closed()

    def state_payload(self) -> dict:
        return {"node_id": self.node_id, "state": self.state, "preset": self.preset,
                "capture": {"dequeued": 100, "dropped": 0, "expected_per_sec": 30, "pump_alive": True, "last_frame_age_s": 0.02},
                "armed": self.armed, "chrony_offset_ms": 0.3, "rf_ok": True,
                "battery": {"bars": 3, "bars_raw": 3, "charging": False, "full": False, "age_s": 0.5}, "simulated": True}

    async def broadcast(self, data) -> None:
        for ws in list(self.peers):
            try:
                await ws.send(data)
            except Exception:
                pass

    async def heartbeats(self) -> None:
        while True:
            await self.broadcast(Envelope(protocol.EVT_HEARTBEAT, self.state_payload()).to_json())
            await asyncio.sleep(0.2)

    async def previews(self) -> None:
        seq = 0
        while True:
            seq += 1
            await self.broadcast(protocol.encode_preview(self.node_id, 1_000_000 * seq, seq, TINY_JPEG))
            await asyncio.sleep(0.1)

    async def handler(self, ws) -> None:
        self.peers.add(ws)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                req = Envelope.from_json(raw)
                await ws.send(self.handle(req).to_json())
        except ConnectionClosed:
            pass
        finally:
            self.peers.discard(ws)

    def handle(self, req: Envelope) -> Envelope:
        t, p = req.type, req.payload
        self.commands.append((t, p))
        ack = lambda kind, **payload: Envelope(kind, payload, id=req.id)  # noqa: E731
        if t == protocol.CMD_PING:
            return ack(protocol.ACK_OK, wall=time.time(), mono=time.monotonic(), state=self.state)
        if t == protocol.CMD_GET_STATE:
            return ack(protocol.ACK_OK, **self.state_payload(), net={"ssid": "FAKE", "ip": "127.0.0.1"})
        if t == protocol.CMD_CONFIGURE:
            if self.state == "recording":
                return ack(protocol.ACK_BUSY, reason="recording in progress")
            self.preset = p.get("preset", self.preset)
            return ack(protocol.ACK_OK, **self.state_payload())
        if t == protocol.CMD_START_PREVIEW:
            return ack(protocol.ACK_OK, preview_hz=float(p.get("fps") or 4), gray=bool(p.get("gray")), max_width=int(p.get("max_width") or 0))
        if t == protocol.CMD_STOP_PREVIEW:
            return ack(protocol.ACK_OK, preview_hz=4, streaming=False)
        if t == protocol.CMD_ARM:
            if self.state != "live":
                return ack(protocol.ACK_BUSY, reason=f"state={self.state}")
            if self.armed:
                return ack(protocol.ACK_ERROR, reason="already armed; call stop() first")
            self.armed = {"session": p["session"], "clip": p["clip"], "edge_id": int(p["edge_id"]),
                          "max_seconds": float(p.get("max_seconds", 30)), "base_dir": str(self.rec_dir)}
            return ack(protocol.ACK_OK, armed_edge_id=self.armed["edge_id"])
        if t == protocol.CMD_STOP:
            self.armed = None
            if self.state == "recording":
                asyncio.get_running_loop().create_task(self.finish())
            return ack(protocol.ACK_OK)
        return ack(protocol.ACK_ERROR, reason=f"unknown command '{t}'")

    async def edge(self, edge_id: int, session_id: int) -> None:
        if self.deaf:
            return
        mono = EDGE + 1_000
        await self.broadcast(Envelope(protocol.EVT_RF_EDGE, {"node_id": self.node_id, "edge_id": edge_id, "session_id": session_id,
                                                              "mono_ns": mono, "edge_wall_ns": 1_792_000_000_000_000_000}).to_json())
        spec = self.armed
        if not spec or spec["edge_id"] != edge_id or self.state != "live":
            return
        self.armed = None
        self.state = "recording"
        self.spec = spec
        await self.broadcast(Envelope(protocol.EVT_RECORDING_STARTED, {"node_id": self.node_id, "session": spec["session"],
                                                                        "clip": spec["clip"], "edge_id": edge_id}).to_json())
        asyncio.get_running_loop().call_later(self.clip_seconds, lambda: asyncio.get_running_loop().create_task(self.finish()))

    async def finish(self) -> None:
        if self.state != "recording":
            return
        spec = self.spec
        self.state = "live"
        folder = self.rec_dir / spec["session"]
        side = write_sidecar(folder / f"{spec['clip']}_{self.node_id}.jsonl", self.node_id, spec["clip"], EDGE,
                             EDGE + self.delay_ns, session=spec["session"], n=12)
        mp4 = side.with_suffix(".mp4")
        mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100)
        await self.broadcast(Envelope(protocol.EVT_RECORDING_STOPPED, {
            "node_id": self.node_id, "path": str(mp4), "clip": spec["clip"], "frames": 12, "duration_s": 0.4, "bytes": 108}).to_json())


class FakeController:
    """What the rig controller does for cmd.fire: stop, arm with a fresh edge id, send the RF trigger."""

    def __init__(self, nodes: list[FakeNode], *, refuse: bool = False):
        self.nodes, self.refuse = nodes, refuse
        self.edge = 0
        self.session_id = 1
        self.commands: list[tuple[str, dict]] = []
        self.server = None
        self.port = 0

    async def start(self) -> None:
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def handler(self, ws) -> None:
        await ws.send(Envelope(protocol.EVT_NODE_JOINED, {"kind": "controller", "session_id": 1,
                                                          "nodes": [n.node_id for n in self.nodes]}).to_json())
        try:
            async for raw in ws:
                req = Envelope.from_json(raw)
                self.commands.append((req.type, req.payload))
                await ws.send((await self.handle(req)).to_json())
        except ConnectionClosed:
            pass

    async def handle(self, req: Envelope) -> Envelope:
        t, p = req.type, req.payload
        ack = lambda kind, **payload: Envelope(kind, payload, id=req.id)  # noqa: E731
        if t == protocol.CMD_PING:
            return ack(protocol.ACK_OK, wall=time.time(), mono=time.monotonic(), nodes=[n.node_id for n in self.nodes])
        if t == protocol.CMD_LIST_NODES:
            return ack(protocol.ACK_OK, nodes=[{"node_id": n.node_id, "host": "127.0.0.1", "online": True, "state": n.state_payload()}
                                               for n in self.nodes])
        if t == protocol.CMD_STOP:
            results = {n.node_id: n.handle(Envelope(protocol.CMD_STOP, {})).payload for n in self.nodes}
            return ack(protocol.ACK_OK, nodes=results)
        if t == protocol.CMD_FIRE:
            self.edge += 1
            for n in self.nodes:
                n.handle(Envelope(protocol.CMD_STOP, {}))
            arm = {n.node_id: n.handle(Envelope(protocol.CMD_ARM, {"session": p["session"], "clip": p["clip"], "edge_id": self.edge,
                                                                  "max_seconds": p.get("max_seconds", 30)})) for n in self.nodes}
            armed = [nid for nid, r in arm.items() if r.ok]
            not_armed = {nid: r.reason for nid, r in arm.items() if not r.ok}
            if self.refuse or not armed:
                return ack(protocol.ACK_ERROR, reason="no nodes armed", nodes={k: v.payload for k, v in arm.items()}, not_armed=not_armed)
            for n in self.nodes:
                asyncio.get_running_loop().create_task(n.edge(self.edge, self.session_id))
            return ack(protocol.ACK_OK, edge_id=self.edge, session_id=self.session_id, rf_acked=True, tx_mono_ns=time.monotonic_ns(),
                       armed=armed, not_armed=not_armed, nodes={k: {"type": v.type, **v.payload} for k, v in arm.items()})
        return ack(protocol.ACK_ERROR, reason=f"unknown command '{t}'")


class FakeRig:
    """Runs the fakes in their own thread and event loop, like separate processes would."""

    def __init__(self, rec_dir: Path, *, deaf: tuple[str, ...] = (), refuse: bool = False):
        self.nodes = [FakeNode(nid, rec_dir, d, deaf=nid in deaf) for nid, d in DELAYS_NS.items()]
        self.controller = FakeController(self.nodes, refuse=refuse)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> "FakeRig":
        self.thread.start()
        self.run(self._start())
        return self

    def __exit__(self, *exc) -> None:
        self.run(self._stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)

    async def _start(self) -> None:
        for n in self.nodes:
            await n.start()
        await self.controller.start()

    async def _stop(self) -> None:
        for n in self.nodes:
            await n.stop()
        await self.controller.stop()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)

    def config(self, tmp_path: Path) -> config.Config:
        return config.parse({"cameras": [{"id": n.node_id, "url": f"ws://127.0.0.1:{n.port}"} for n in self.nodes],
                             "controller": {"url": f"ws://127.0.0.1:{self.controller.port}"},
                             "offload_dir": str(tmp_path / "rec"), "samples_dir": str(tmp_path / "samples")})


def wait_for(fn, timeout: float = 8.0, every: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(every)


def wait_online(c: TestClient, n: int = 3) -> dict:
    return wait_for(lambda: (lambda h: h if h["cameras_online"] == n and h["controller_online"] else None)(
        c.get("/api/capture/health").json()))


def test_cameras_status_previews_configure_and_cors(tmp_path: Path):
    with FakeRig(tmp_path / "rec") as rig, TestClient(create_app(rig.config(tmp_path))) as c:
        h = wait_online(c)
        assert h["ok"] and h["cameras"] == 3 and h["bridge"] == "cellpilot-capture"
        cams = wait_for(lambda: (lambda cs: cs if all(x["state"] == "live" and x["preview_frames"] > 0 for x in cs) else None)(
            c.get("/api/capture/cameras").json()))
        cam = {x["id"]: x for x in cams}["rx-cam2"]
        assert cam["online"] and cam["preset"] == "1080p30" and cam["battery"]["bars"] == 3 and cam["simulated"]
        assert cam["heartbeat_age_s"] < 2 and cam["capture"]["expected_per_sec"] == 30 and cam["error"] is None
        assert c.get("/api/capture/cameras").json()[0]["url"].startswith("ws://127.0.0.1:")

        r = c.get("/api/capture/cameras/rx-cam1/preview.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == TINY_JPEG
        assert r.headers["cache-control"] == "no-store" and int(r.headers["x-preview-seq"]) >= 1
        assert c.get("/api/capture/cameras/rx-cam9/preview.jpg").status_code == 404

        r = c.post("/api/capture/preview", json={"on": True, "fps": 8})
        assert r.status_code == 200 and all(v["ok"] for v in r.json()["nodes"].values())
        node = rig.nodes[0]
        assert ("cmd.start_preview", {"fps": 8.0, "gray": False, "max_width": 640}) in node.commands
        assert c.post("/api/capture/preview", json={"on": False}).json()["nodes"]["rx-cam3"]["payload"]["streaming"] is False

        pings = c.post("/api/capture/cameras/ping").json()
        assert set(pings) == set(DELAYS_NS) and all(p["rtt_ms"] >= 0 and p["state"] == "live" for p in pings.values())

        r = c.post("/api/capture/configure", json={"preset": "720p30"})
        assert r.status_code == 200 and r.json()["nodes"]["rx-cam1"]["payload"]["preset"] == "720p30"
        wait_for(lambda: all(x["preset"] == "720p30" for x in c.get("/api/capture/cameras").json()))
        assert c.post("/api/capture/configure", json={"preset": "../x"}).status_code == 422
        assert "1080p30" in c.get("/api/capture/presets").json()

        ctrl = c.get("/api/capture/controller").json()
        assert ctrl["online"] and [n["node_id"] for n in ctrl["nodes"]] == list(DELAYS_NS)
        assert c.get("/api/capture/config").json()["allow_lan"] is False

        # The page runs on another local port: CORS for localhost origins only.
        pre = c.options("/api/capture/takes", headers={"Origin": "http://localhost:8090", "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Private-Network": "true"})
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "http://localhost:8090"
        assert pre.headers.get("access-control-allow-private-network") == "true"
        pre = c.options("/api/capture/takes", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in pre.headers


def test_a_take_is_fired_through_the_controller_then_checked_and_converted(tmp_path: Path, monkeypatch):
    from capture import rig as rigmod

    def fake_make_session(folder, out_dir, sid, name, *, clip=None, ref=None, progress=None, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        session = {"id": sid, "name": name, "kind": "sample", "recorded": "2026-10-15", "synthetic": False,
                   "clips": [{"view": "cam0", "file": "cam0.mp4", "offset_s": 0.0}], "folder": str(folder), "clip": clip}
        (out_dir / "session.json").write_text(json.dumps(session), "utf-8")
        progress("encoded")
        return session

    monkeypatch.setattr(rigmod.convert, "make_session", fake_make_session)
    with FakeRig(tmp_path / "rec") as rig, TestClient(create_app(rig.config(tmp_path))) as c:
        wait_online(c)
        r = c.post("/api/capture/takes", json={"name": "Kitting A", "seconds": 3})
        assert r.status_code == 202, r.text
        take = r.json()
        assert take["session"] == "Kitting-A" and take["clip"] == "take_001" and take["state"] in ("arming", "armed", "recording")
        tid = take["id"]
        assert c.post("/api/capture/takes", json={"name": "x", "seconds": 3}).status_code == 409  # one at a time
        assert c.post("/api/capture/takes", json={"name": "x", "seconds": 1}).status_code == 422

        take = wait_for(lambda: (lambda t: t if t["state"] in ("stopped", "failed") and (t["offloaded"] or t["check_error"]) else None)(
            c.get(f"/api/capture/takes/{tid}").json()))
        assert take["state"] == "stopped", take
        assert take["edge_id"] == 1 and take["rf_acked"] is True and take["armed"] == list(DELAYS_NS) and take["not_armed"] == {}
        assert all(cam["started"] and cam["stopped"] and cam["frames"] == 12 for cam in take["cameras"].values())
        assert take["offloaded"] and Path(take["folder"]) == tmp_path / "rec" / "Kitting-A"
        chk = take["check"]
        assert chk["ref"] == "rx-cam2" and chk["order"] == ["rx-cam2", "rx-cam1", "rx-cam3"] and chk["within_a_frame"]
        assert chk["offsets_s"] == {"rx-cam2": 0.0, "rx-cam1": 0.00956, "rx-cam3": 0.029894} and chk["missing"] == []
        assert take["elapsed_s"] is not None and take["edges"] if "edges" in take else True

        # what went over the wire: the controller got cmd.fire; each node was stopped, then armed with the edge id
        assert ("cmd.fire", {"session": "Kitting-A", "clip": "take_001", "max_seconds": 3.0}) in rig.controller.commands
        kinds = [t for t, _ in rig.nodes[0].commands if t in ("cmd.stop", "cmd.arm")]
        assert kinds[-2:] == ["cmd.stop", "cmd.arm"]
        arm = next(p for t, p in rig.nodes[0].commands if t == "cmd.arm")
        assert arm == {"session": "Kitting-A", "clip": "take_001", "edge_id": 1, "max_seconds": 3.0}

        assert c.post(f"/api/capture/takes/{tid}/check").json()["check"] == chk
        assert c.post(f"/api/capture/takes/{tid}/stop").json()["state"] == "stopped"  # a no-op once stopped
        assert c.get("/api/capture/folders").json()["folders"] == {"Kitting-A": ["take_001"]}
        r = c.post("/api/capture/check", json={"folder": str(tmp_path / "rec" / "Kitting-A")})
        assert r.status_code == 200 and r.json()["check"]["spread_ms"] == 29.894
        assert c.post("/api/capture/check", json={"folder": str(tmp_path / "nowhere")}).status_code == 404

        # make a session from the take, then from the folder; the same id is refused unless overwritten
        r = c.post("/api/capture/sessions", json={"take_id": tid, "sid": "kit-01", "name": "Kitting A"})
        assert r.status_code == 202, r.text
        job = wait_for(lambda: (lambda j: j if j["status"] in ("done", "failed") else None)(c.get(f"/api/capture/sessions/{r.json()['id']}").json()))
        assert job["status"] == "done" and job["session"]["id"] == "kit-01" and job["url"] == "#/s/kit-01"
        assert job["session"]["clip"] == "take_001" and job["log"] == ["encoded"]
        assert c.post("/api/capture/sessions", json={"take_id": tid, "sid": "kit-01", "name": "again"}).status_code == 409
        assert c.post("/api/capture/sessions", json={"take_id": tid, "sid": "kit-01", "name": "again", "overwrite": True}).status_code == 202
        r = c.post("/api/capture/sessions", json={"folder": str(tmp_path / "rec" / "Kitting-A"), "sid": "kit-02", "name": "From folder"})
        assert r.status_code == 202
        assert c.post("/api/capture/sessions", json={"sid": "kit-03", "name": "x"}).status_code == 422
        assert c.post("/api/capture/sessions", json={"sid": "bad id", "name": "x", "folder": "."}).status_code == 422
        assert len(c.get("/api/capture/sessions").json()) == 3

        # a second take of the same session continues the numbering
        t2 = c.post("/api/capture/takes", json={"name": "Kitting A", "seconds": 3}).json()
        assert t2["clip"] == "take_002"
        wait_for(lambda: c.get(f"/api/capture/takes/{t2['id']}").json()["state"] == "stopped")
        assert len(c.get("/api/capture/takes").json()) == 2


def test_a_camera_that_misses_the_trigger_is_reported_missing(tmp_path: Path):
    with FakeRig(tmp_path / "rec", deaf=("rx-cam3",)) as rig, TestClient(create_app(rig.config(tmp_path))) as c:
        wait_online(c)
        tid = c.post("/api/capture/takes", json={"name": "deaf", "seconds": 3}).json()["id"]
        take = wait_for(lambda: (lambda t: t if t["state"] in ("stopped", "failed") and (t["offloaded"] or t["check_error"]) else None)(
            c.get(f"/api/capture/takes/{tid}").json()), timeout=12)
        assert take["state"] == "stopped" and take["offloaded"]
        assert take["cameras"]["rx-cam3"] == {"started": False, "stopped": False}
        assert take["check"]["missing"] == ["rx-cam3"] and sorted(take["check"]["delays_ms"]) == ["rx-cam1", "rx-cam2"]


def test_a_refused_fire_fails_the_take_and_an_offline_controller_is_refused_early(tmp_path: Path):
    with FakeRig(tmp_path / "rec", refuse=True) as rig, TestClient(create_app(rig.config(tmp_path))) as c:
        wait_online(c)
        tid = c.post("/api/capture/takes", json={"name": "nope", "seconds": 3}).json()["id"]
        take = wait_for(lambda: (lambda t: t if t["state"] == "failed" else None)(c.get(f"/api/capture/takes/{tid}").json()))
        assert take["error"] == "the controller refused to fire: no nodes armed"
        assert c.post("/api/capture/sessions", json={"take_id": tid, "sid": "x", "name": "x"}).status_code == 409

    cfg = config.parse({"cameras": [], "controller": {"url": "ws://127.0.0.1:1"}, "offload_dir": str(tmp_path)})
    with TestClient(create_app(cfg)) as c:
        time.sleep(0.3)
        r = c.post("/api/capture/takes", json={"name": "x", "seconds": 5})
        assert r.status_code == 409 and "controller is offline" in r.json()["detail"]
        assert c.get("/api/capture/health").json()["controller_online"] is False
        assert c.get("/api/capture/takes/t-nope").status_code == 404
