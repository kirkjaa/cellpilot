"""End to end against a running NODAL M1 emulator rig (never the real cameras).

Skipped unless CELLPILOT_E2E_CONFIG names a bridge config whose cameras and controller are
localhost addresses of a running `m1sim rig`. From the emulator's checkout:

    .venv\\Scripts\\python -m m1sim rig --cameras 3 --preset 1080p30 --scene walker \\
        --node-ids rx-cam1,rx-cam2,rx-cam3 --controller-port 5600 --node-port-base 5601 \\
        --edge-port-base 6601 --base-dir D:\\cellpilot-verify\\capture\\rec

then here: CELLPILOT_E2E_CONFIG=D:\\cellpilot-verify\\capture\\capture.json python -m pytest tests/test_e2e_emulator.py

The take is 10 s; the whole test takes about a minute, most of it the three ffmpeg encodes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture import config
from capture.bridge import create_app

CONFIG = os.environ.get("CELLPILOT_E2E_CONFIG")
pytestmark = pytest.mark.skipif(not CONFIG, reason="set CELLPILOT_E2E_CONFIG to a bridge config for a running m1sim rig")


def wait_for(fn, timeout: float, every: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(every)


def test_take_on_the_emulator_becomes_a_session_the_app_serves(tmp_path: Path):
    cfg = config.load(CONFIG)
    assert not cfg.allow_lan and all(u.startswith(("ws://127.0.0.1", "ws://localhost")) for u in [c.url for c in cfg.cameras] + [cfg.controller_url])
    cfg.samples_dir = tmp_path / "samples"
    with TestClient(create_app(cfg)) as c:
        h = wait_for(lambda: (lambda x: x if x["cameras_online"] == 3 and x["controller_online"] else None)(
            c.get("/api/capture/health").json()), timeout=20)
        assert h["ffmpeg"], "ffmpeg must be on PATH"
        cams = wait_for(lambda: (lambda cs: cs if all(x["state"] == "live" and x["preview_frames"] > 0 for x in cs) else None)(
            c.get("/api/capture/cameras").json()), timeout=15)
        assert sorted(x["id"] for x in cams) == ["rx-cam1", "rx-cam2", "rx-cam3"]
        assert all(x["battery"]["bars"] >= 1 and x["preset"] == "1080p30" and x["simulated"] for x in cams)
        assert c.get("/api/capture/cameras/rx-cam1/preview.jpg").content[:2] == b"\xff\xd8"

        tid = c.post("/api/capture/takes", json={"name": "e2e", "seconds": 10}).json()["id"]
        take = wait_for(lambda: (lambda t: t if t["state"] in ("stopped", "failed") and (t["offloaded"] or t["check_error"]) else None)(
            c.get(f"/api/capture/takes/{tid}").json()), timeout=60)
        assert take["state"] == "stopped", take
        assert take["rf_acked"] and len(take["armed"]) == 3 and all(cam["stopped"] for cam in take["cameras"].values())
        assert all(280 <= cam["frames"] <= 330 for cam in take["cameras"].values()), take["cameras"]
        chk = take["check"]
        assert chk["missing"] == [] and 0 <= chk["spread_ms"] <= 40 and set(chk["delays_ms"]) == {"rx-cam1", "rx-cam2", "rx-cam3"}
        assert min(chk["delays_ms"].values()) >= 0 and chk["offsets_s"][chk["ref"]] == 0.0
        print("\nemulator take offsets:", json.dumps(chk, indent=1))

        job = c.post("/api/capture/sessions", json={"take_id": tid, "sid": "e2e-emu", "name": "E2E emulator take"}).json()
        job = wait_for(lambda: (lambda j: j if j["status"] in ("done", "failed") else None)(
            c.get(f"/api/capture/sessions/{job['id']}").json()), timeout=240, every=1)
        assert job["status"] == "done", job
        session = job["session"]
        assert [cl["view"] for cl in session["clips"]] == ["cam0", "cam1", "cam2"]
        assert all(cl["height"] == 720 and cl["fps"] == 30.0 and 9 <= cl["duration_s"] <= 12 for cl in session["clips"])
        assert session["clips"][0]["offset_s"] == 0.0 and all(0 <= cl["offset_s"] <= 0.04 for cl in session["clips"])

    # The main app serves the new session folder as is.
    from app.limits import DailyLimit
    from app.main import create_app as create_main
    from app.store import LocalStore, Samples

    store = LocalStore(tmp_path / "data")
    with TestClient(create_main(store=store, samples=Samples(tmp_path / "samples"), limit=DailyLimit(store, cap=1))) as m:
        listed = m.get("/api/sessions").json()
        assert [s["id"] for s in listed] == ["e2e-emu"] and listed[0]["synthetic"] is False
        body = m.get("/api/sessions/e2e-emu").json()
        assert body["clip_urls"] == {v: f"/media/e2e-emu/{v}.mp4" for v in ("cam0", "cam1", "cam2")}
        r = m.get("/media/e2e-emu/cam2.mp4", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
