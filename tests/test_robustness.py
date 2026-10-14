"""Concurrency, retry, timeout and error-text behaviour."""
import json
import threading
from types import SimpleNamespace

import pytest
from google.genai import errors

from app import argon, pipeline
from app.schemas import Analysis, TaskGraph

from .conftest import SID, make_graph, wait_done


def test_second_click_joins_the_running_analysis(make_client, monkeypatch):
    gate = threading.Event()

    def slow_a(session, media):
        gate.wait(5)
        raise RuntimeError("stop")

    monkeypatch.setattr(argon, "task_graph", slow_a)
    c = make_client(cap=5)
    ids = [c.post(f"/api/sessions/{SID}/analyses").json()["id"] for _ in range(4)]
    assert len(set(ids)) == 1  # one run, one unit of the cap
    assert c.get(f"/api/sessions/{SID}").json()["live_runs_left"] == 4
    gate.set()
    wait_done(c, ids[0])


def test_layout_failure_ships_the_study_without_a_layout(make_client, stub_argon, monkeypatch):
    def broken_c(session, media, steps, parts):
        raise argon.ArgonError("Argon's answer did not match the ArgonLayout schema (3 problems).")

    monkeypatch.setattr(argon, "layout", broken_c)
    c = make_client()
    rec = wait_done(c, c.post(f"/api/sessions/{SID}/analyses").json()["id"])
    assert rec["status"] == "done" and rec["error"] is None, rec["error"]
    res = rec["result"]
    assert res["layout"] is None
    assert res["layout_error"].startswith("Failed during the layout estimate (Argon call C): ArgonError:")
    assert res["robot"]["needed_reach_mm"] is None and res["robot"]["reach_fits"] is None
    assert res["robot"]["model"] == "xArm 6"  # payload fit over the 700 mm floor
    assert rec["usage"]["prompt_tokens"] == 2500  # calls A and D only were counted


def test_retry_on_429_then_success(monkeypatch):
    calls = []

    def generate_content(model, contents, config):
        calls.append(1)
        if len(calls) == 1:
            raise errors.APIError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        return SimpleNamespace(text=make_graph().model_dump_json(), parsed=None, usage_metadata=None,
                               model_version=None, candidates=[])

    monkeypatch.setattr(argon, "client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(argon.time, "sleep", lambda s: None)
    r = argon._generate(["x"], TaskGraph, "HIGH")
    assert len(calls) == 2 and isinstance(r.parsed, TaskGraph)


def test_no_retry_on_400(monkeypatch):
    def generate_content(model, contents, config):
        raise errors.APIError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad"}})

    monkeypatch.setattr(argon, "client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    with pytest.raises(errors.APIError):
        argon._generate(["x"], TaskGraph, "HIGH")


def test_client_has_a_timeout(monkeypatch):
    monkeypatch.setattr(argon, "_client", None)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    c = argon.client()
    assert c._api_client._http_options.timeout == 20 * 60 * 1000
    monkeypatch.setattr(argon, "_client", None)


def test_shipped_sample_analyses_match_the_contract():
    from app.store import Samples
    s = Samples()
    for sid in s.ids():
        sess = s.session(sid)
        for clip in sess["clips"]:
            assert (s.dir(sid) / clip["file"]).is_file()
        for a in s.analyses(sid):
            rec = Analysis.model_validate(a)
            assert rec.session_id == sid and rec.live is False and rec.status == "done"


def test_big_clips_need_a_bucket(samples, store, monkeypatch):
    monkeypatch.setattr(argon, "INLINE_LIMIT_BYTES", 100_000)  # three 50 kB clips = 150 kB total
    with pytest.raises(argon.ArgonError, match="CELLPILOT_STORE=gs://"):
        argon.video_parts(samples.session(SID), samples.dir(SID), store)


def test_api_error_text_is_short():
    from app.pipeline import readable_error
    e = errors.APIError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                        "message": "denied on projects/secret-project/locations/global"}})
    msg = readable_error("steps", e)
    assert "secret-project" not in msg and msg.endswith("403 PERMISSION_DENIED")


def test_development_files_are_not_served(make_client):
    c = make_client()
    for path in ("/dev/mock_server.py", "/dev/fixture-analysis.json", "/dev/shot-analysis.png", "/logic.test.mjs"):
        assert c.get(path).status_code == 404, path
