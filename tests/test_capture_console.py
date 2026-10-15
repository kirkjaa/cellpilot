"""Console mode: the bridge behind NODAL's emulator console, against a fake console on the standard library."""
from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture import config as cfgmod
from capture.client import LinkError
from capture.console import ConsoleHttp, ConsoleRig
from capture.protocol import EVT_HEARTBEAT, EVT_NODE_LEFT, EVT_RECORDING_STARTED, EVT_RECORDING_STOPPED, Envelope, PreviewFrame
from capture.rig import Take

USER, PASSWORD, TOKEN = "cellpilot", "pw-for-tests", "tok123"


class FakeConsole:
    """What the bridge needs from `m1sim console --auth`: a login cookie, the user's rig, sessions and zips."""

    def __init__(self, tmp: Path):
        self.rig_started = False
        self.starts: list[dict] = []
        self.logins = 0
        self.listed = True
        self.sessions_dir = tmp / "recordings"
        s = self.sessions_dir / "kitting"
        (s / "truth").mkdir(parents=True)
        for i, n in enumerate(("rx-cam1", "rx-cam2", "rx-cam3")):
            (s / f"take_001_{n}.mp4").write_bytes(b"\x00" * 64)
            header = {"_": "header", "node_id": n, "clip": "take_001", "edge_mono_ns": 1_000_000_000, "capture": {"fps": 30}}
            frame = {"mono_ns": 1_000_000_000 + 5_000_000 * (i + 1), "seq": 0}
            (s / f"take_001_{n}.jsonl").write_text(json.dumps(header) + "\n" + json.dumps(frame) + "\n", encoding="utf-8")
        (s / "truth" / "cameras.json").write_text("{}", encoding="utf-8")
        (s / "take_002_rx-cam1.mp4").write_bytes(b"other take")
        (s / "take_002_rx-cam1.jsonl").write_text(json.dumps({"_": "header", "node_id": "rx-cam1", "clip": "take_002", "edge_mono_ns": 1}) + "\n", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def zip_of(self, name: str) -> bytes:
        d = self.sessions_dir / name
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=f"{name}/{p.relative_to(d).as_posix()}")
        return buf.getvalue()

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, code, obj, cookie=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                return f"m1sim_session={TOKEN}" in (self.headers.get("Cookie") or "")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/login":
                    fake.logins += 1
                    if body.get("username") == USER and body.get("password") == PASSWORD:
                        return self._json(200, {"ok": True, "user": {"name": USER}}, f"m1sim_session={TOKEN}; Path=/; HttpOnly")
                    return self._json(401, {"error": "wrong username or password"})
                if not self._authed():
                    return self._json(401, {"error": "login required"})
                if self.path == "/api/rig/start":
                    fake.starts.append(body)
                    fake.rig_started = True
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "no"})

            def do_GET(self):
                if not self._authed():
                    return self._json(401, {"error": "login required"})
                if self.path == "/api/state":
                    rig = ({"nodes": [{"node_id": f"rx-cam{i}", "port": 5600 + i} for i in (1, 2, 3)],
                            "spec": {"preset": "1080p30", "scene": "walker", "cameras": 3}}
                           if fake.rig_started else {"running": False})
                    return self._json(200, {"rig": rig, "user": {"name": USER}})
                if self.path == "/api/sessions":
                    sessions = [{"session": "kitting", "clips": ["take_001", "take_002"], "bytes": 1}] if fake.listed else []
                    return self._json(200, {"sessions": sessions, "user": USER})
                if self.path == "/api/sessions/kitting/download":
                    data = fake.zip_of("kitting")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._json(404, {"error": "no"})

        return H


@pytest.fixture
def fake(tmp_path):
    f = FakeConsole(tmp_path)
    yield f
    f.close()


def console_cfg(tmp_path: Path, url: str, **extra) -> cfgmod.Config:
    d = {"console": {"url": url, "username": USER, "password": PASSWORD,
                     "rig": {"cameras": 3, "preset": "1080p30", "scene": "walker"}, **extra},
         "offload_dir": str(tmp_path / "offload"), "samples_dir": str(tmp_path / "samples")}
    return cfgmod.parse(d, base=tmp_path)


def test_console_config_derives_the_controller_and_keeps_the_password_private(tmp_path, monkeypatch):
    monkeypatch.setenv("M1SIM_USER", "env-user")
    monkeypatch.setenv("M1SIM_PASSWORD", "env-secret")
    cfg = cfgmod.parse({"console": {"url": "https://m1.example.com/"}}, base=tmp_path)
    assert cfg.console.url == "https://m1.example.com" and cfg.console.username == "env-user"
    assert cfg.console.password == "env-secret" and cfg.console.max_take_seconds == 60
    assert cfg.controller_url == "wss://m1.example.com/ws/controller?preview=640"
    assert cfg.cameras == [] and cfg.allow_lan is False  # a remote console needs no allow_lan
    pub = cfg.public()
    assert pub["console"]["url"] == "https://m1.example.com" and "password" not in json.dumps(pub)
    assert "username" not in pub["console"] and "env-user" not in json.dumps(pub)  # a public endpoint shows no login
    assert "env-secret" not in repr(cfg)
    local = cfgmod.parse({"console": {"url": "http://127.0.0.1:5800", "preview_width": 320, "max_take_seconds": 30}}, base=tmp_path)
    assert local.controller_url == "ws://127.0.0.1:5800/ws/controller?preview=320" and local.console.max_take_seconds == 30
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse({"console": {"url": "ftp://x"}}, base=tmp_path)
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse({"console": {"url": "https://x", "preview_width": "wide"}}, base=tmp_path)
    plain = cfgmod.load(str(Path(__file__).resolve().parents[1] / "capture" / "config.cloud.json"))
    assert plain.console.url == "https://m1.ques8.com" and plain.controller_url.startswith("wss://m1.ques8.com/ws/controller")


def test_console_http_logs_in_once_and_again_after_a_401(fake):
    http = ConsoleHttp(fake.url, USER, PASSWORD)
    state = http.json("GET", "/api/state")  # not logged in yet: a 401, then the login, then the answer
    assert state["rig"] == {"running": False} and fake.logins == 1
    assert http.cookie_header() == {"Cookie": f"m1sim_session={TOKEN}"}
    http.json("GET", "/api/state")
    assert fake.logins == 1  # the cookie is reused
    bad = ConsoleHttp(fake.url, USER, "wrong")
    with pytest.raises(LinkError, match="login failed: HTTP 401"):
        bad.login()
    with pytest.raises(LinkError, match="credentials missing"):
        ConsoleHttp(fake.url, "", "").login()


def test_ensure_rig_starts_the_users_rig_when_it_is_down(fake, tmp_path):
    rig = ConsoleRig(console_cfg(tmp_path, fake.url))
    rig.http.login()
    rig._ensure_rig()
    assert fake.starts == [{"cameras": 3, "preset": "1080p30", "scene": "walker"}]
    assert rig.console_state["rig_running"] is True
    assert rig.console_state["rig"] == {"nodes": ["rx-cam1", "rx-cam2", "rx-cam3"], "preset": "1080p30", "scene": "walker", "cameras": 3}
    assert sorted(rig.nodes) == ["rx-cam1", "rx-cam2", "rx-cam3"]
    rig._ensure_rig()  # already up: no second start
    assert len(fake.starts) == 1


def test_the_controllers_relay_feeds_the_virtual_cameras_and_the_take(fake, tmp_path):
    rig = ConsoleRig(console_cfg(tmp_path, fake.url))

    async def scenario():
        await rig._controller_event(rig.controller, Envelope(type=EVT_HEARTBEAT, payload={
            "node_id": "rx-cam1", "state": "live", "battery": 3, "preset": "1080p30"}))
        cams = {c["id"]: c for c in rig.cameras()}
        assert cams["rx-cam1"]["online"] is True and cams["rx-cam1"]["battery"] == 3 and cams["rx-cam1"]["error"] is None
        await rig._preview_frame(rig.controller, PreviewFrame("rx-cam2", 10, 1, b"jpeg"))
        assert rig.nodes["rx-cam2"].preview_count == 1 and rig.nodes["rx-cam2"].last_preview.jpeg == b"jpeg"
        assert rig.nodes["rx-cam2"].online is False  # a preview is not a heartbeat
        t = Take(id="t1", name="kitting", session="kitting", clip="take_001", seconds=5, state="armed",
                 armed=["rx-cam1", "rx-cam2"])
        t.cameras = {"rx-cam1": {"started": False, "stopped": False}, "rx-cam2": {"started": False, "stopped": False}}
        rig.takes[t.id] = t
        for n in ("rx-cam1", "rx-cam2"):
            await rig._controller_event(rig.controller, Envelope(type=EVT_RECORDING_STARTED, payload={
                "node_id": n, "session": "kitting", "clip": "take_001", "edge_id": 7}))
        assert t.state == "recording" and sorted(t.started) == ["rx-cam1", "rx-cam2"]
        for n in ("rx-cam1", "rx-cam2"):
            await rig._controller_event(rig.controller, Envelope(type=EVT_RECORDING_STOPPED, payload={
                "node_id": n, "clip": "take_001", "path": f"/data/kitting/take_001_{n}.mp4", "frames": 150}))
        assert t.state == "stopped"
        await rig._controller_event(rig.controller, Envelope(type=EVT_NODE_LEFT, payload={"node_id": "rx-cam1"}))
        assert rig.nodes["rx-cam1"].online is False and rig.nodes["rx-cam1"].error == "left the rig"
        # cameras are not addressed directly through a console
        with pytest.raises(LinkError, match="through the console"):
            await rig.nodes["rx-cam1"].cmd("cmd.ping")
        pings = await rig.ping_all()
        assert set(pings) == {"rx-cam1", "rx-cam2"} and pings["rx-cam2"]["via"] == "console"
        # a public page gets capped takes; the controller must look online for the length check to be reached
        rig.controller.online = True
        with pytest.raises(LinkError, match="between 3 and 60"):
            await rig.start_take("too long", 120)

    asyncio.run(scenario())


def test_fetch_take_downloads_the_session_zip_and_unpacks_only_this_take(fake, tmp_path):
    cfg = console_cfg(tmp_path, fake.url)
    rig = ConsoleRig(cfg)
    rig.http.login()
    take = Take(id="t1", name="kitting", session="kitting", clip="take_001", seconds=5, state="stopped",
                armed=["rx-cam1", "rx-cam2", "rx-cam3"])
    rig._fetch_take(take)
    folder = cfg.offload_dir / "kitting"
    for n in ("rx-cam1", "rx-cam2", "rx-cam3"):
        assert (folder / f"take_001_{n}.mp4").read_bytes() == b"\x00" * 64
        assert (folder / f"take_001_{n}.jsonl").is_file()
    assert (folder / "truth" / "cameras.json").is_file()
    assert not (folder / "take_002_rx-cam1.mp4").exists()  # another take stays on the console
    assert not list(cfg.offload_dir.glob("*.zip"))  # the zip is gone once unpacked
    assert rig.find_take_folder(take) == folder
    check = rig.check_take(take)  # the usual offset check runs on the unpacked sidecars
    assert take.offloaded and check["order"][0] == "rx-cam1" and len(check["delays_ms"]) == 3 and check["within_a_frame"] is True
    other = Take(id="t2", name="x", session="kitting", clip="take_009", seconds=5, state="stopped", armed=["rx-cam1"])
    with pytest.raises(LookupError, match="not in the console's recordings yet"):
        rig._fetch_take(other)
    fake.listed = False
    with pytest.raises(LookupError):
        rig._fetch_take(take)


def test_the_app_mounts_the_bridge_for_the_cloud_demo(fake, tmp_path, monkeypatch, store, samples):
    from app.main import create_app

    cfg_path = tmp_path / "capture.cloud.json"
    cfg_path.write_text(json.dumps({"console": {"url": fake.url, "rig": {"cameras": 3}},
                                    "offload_dir": str(tmp_path / "offload"), "samples_dir": str(tmp_path / "samples")}),
                        encoding="utf-8")
    monkeypatch.setenv("CELLPILOT_CAPTURE_CONFIG", str(cfg_path))
    monkeypatch.setenv("M1SIM_USER", USER)
    monkeypatch.setenv("M1SIM_PASSWORD", PASSWORD)
    with TestClient(create_app(store, samples)) as c:
        assert c.get("/api/health").json() == {"ok": True, "capture_bridge": "/capture-bridge"}
        h = c.get("/capture-bridge/api/capture/health").json()
        assert h["bridge"] == "cellpilot-capture" and h["console"]["url"] == fake.url and "user" not in h["console"]
        pub = c.get("/capture-bridge/api/capture/config").json()
        assert pub["console"]["url"] == fake.url and "password" not in json.dumps(pub) and USER not in json.dumps(pub)
        # the bootstrap logs in and starts the rig in the background
        for _ in range(50):
            if fake.rig_started:
                break
            asyncio.run(asyncio.sleep(0.1))
        assert fake.rig_started and fake.logins >= 1
    monkeypatch.delenv("CELLPILOT_CAPTURE_CONFIG")
    plain = create_app(store, samples)
    with TestClient(plain) as c:
        assert c.get("/api/health").json() == {"ok": True}
        assert c.get("/capture-bridge/api/capture/health").status_code == 404


def test_a_rig_the_bridge_started_gets_a_warmup_take(fake, tmp_path):
    rig = ConsoleRig(console_cfg(tmp_path, fake.url))
    fired = []

    async def fake_start_take(name, seconds):
        fired.append((name, seconds))
        return Take(id="w", name=name, session=name, clip="take_001", seconds=seconds)

    rig.start_take = fake_start_take  # type: ignore[method-assign]

    async def scenario():
        # a rig that was already running on the console: no warm-up
        await rig._controller_connect(rig.controller)
        assert rig.warmup_task is None and fired == []
        # the bridge started the rig itself: one warm-up once a camera is online
        rig.started_rig = True
        await rig._controller_event(rig.controller, Envelope(type=EVT_HEARTBEAT, payload={"node_id": "rx-cam1", "state": "live"}))
        await rig._controller_connect(rig.controller)
        assert rig.warmup_task is not None
        await rig.warmup_task
        assert fired == [("cellpilot-warmup", 3.0)]
        await rig._controller_connect(rig.controller)  # a reconnect does not warm up again
        assert len(fired) == 1

    asyncio.run(scenario())
