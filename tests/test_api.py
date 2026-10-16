"""The API end to end through FastAPI's TestClient, with Argon stubbed."""
import json

from app import argon
from app.main import INTERRUPTED
from app.store import analysis_key, argon_key

from .conftest import CLIP_BYTES, SID, make_check, make_graph, make_layout, wait_done


def test_health_and_sessions(make_client):
    c = make_client()
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/sessions").json() == [
        {"id": SID, "name": "Test session", "kind": "sample", "synthetic": True, "scenario": "", "clips": 3, "duration_s": 30.0}]
    body = c.get(f"/api/sessions/{SID}").json()
    assert body["clip_urls"] == {v: f"/media/{SID}/{v}.mp4" for v in ("cam0", "cam1", "cam2")}
    assert body["session"]["clips"][1]["offset_s"] == 0.2
    assert body["latest"] is None and body["live_runs_left"] == 10 and body["live_runs_per_day"] == 10
    assert c.get("/api/sessions/nope").status_code == 404


def test_start_poll_done_signoff_export(make_client, stub_argon, store):
    c = make_client()
    r = c.post(f"/api/sessions/{SID}/analyses")
    assert r.status_code == 202
    aid = r.json()["id"]
    assert aid.startswith("a-")

    rec = wait_done(c, aid)
    assert rec["status"] == "done" and rec["stage"] == "done", rec["error"]
    assert rec["live"] is True and rec["model"] == argon.model_name() and rec["finished_at"]
    assert rec["usage"]["prompt_tokens"] == 4500 and rec["usage"]["output_tokens"] == 1150  # four calls
    res = rec["result"]
    assert res["counts"] == {"automate": 1, "keep_human": 1, "needs_fixture": 1, "total": 3}
    assert res["automated_share"] == 0.4 and res["cycle_time_s"] == 10.0
    assert [s["preflagged"] for s in res["steps"]] == [False, True, True]
    assert res["robot"]["model"] == "xArm 6" and res["robot"]["needed_reach_mm"] == 526
    assert res["layout"]["bench"] == {"length_m": 1.2, "width_m": 0.6} and len(res["layout"]["anchors"]) == 4
    assert res["layout_error"] is None and res["cycle_estimate"]["speed_factor"] == 1.3
    # the notes pass saw the assembled result and its answer is on the result, with the model that wrote it
    assert [n["id"] for n in res["notes"]["step_notes"]] == ["s1", "s2", "s3"]
    assert res["notes_model"] == "gemini-3.8-flash-test" and res["notes_error"] is None
    assert stub_argon["e"][0].robot.model == "xArm 6"  # the notes pass saw the assembled result
    # all three calls got the same clips; Argon's answers are kept word for word
    assert stub_argon["a"][0] == stub_argon["d"][0] == stub_argon["c"][0]
    raw = store.read_json(argon_key(SID, aid))
    assert json.loads(raw["task_graph"]["text"]) == json.loads(make_graph().model_dump_json())
    assert json.loads(raw["verify"]["text"]) == json.loads(make_check().model_dump_json())
    assert json.loads(raw["layout"]["text"]) == json.loads(make_layout().model_dump_json())

    session = c.get(f"/api/sessions/{SID}").json()
    assert session["latest"]["id"] == aid and session["live_runs_left"] == 9

    url = f"/api/sessions/{SID}/analyses/{aid}"
    r = c.put(f"{url}/signoffs/s1", json={"decision": "approved", "note": " ok ", "engineer": "Kirk"})
    assert r.status_code == 200
    s1 = r.json()["signoffs"]["s1"]
    assert s1["decision"] == "approved" and s1["note"] == "ok" and s1["engineer"] == "Kirk" and s1["at"]
    assert c.put(f"{url}/signoffs/s2", json={"decision": "maybe"}).status_code == 422
    assert c.put(f"{url}/signoffs/s9", json={"decision": "flagged"}).status_code == 404
    assert c.get(url).json()["signoffs"]["s1"]["decision"] == "approved"

    r = c.get(f"{url}/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == f'attachment; filename="cellpilot-{SID}-{aid}.json"'
    exported = r.json()
    assert exported["signoffs"]["s1"]["decision"] == "approved"
    assert exported["result"]["counts"]["total"] == 3 and exported["session"]["id"] == SID


def test_429_after_the_daily_cap(make_client, stub_argon):
    c = make_client(cap=2)
    for _ in range(2):  # one at a time: a click during a run joins it
        r = c.post(f"/api/sessions/{SID}/analyses")
        assert r.status_code == 202
        wait_done(c, r.json()["id"])
    r = c.post(f"/api/sessions/{SID}/analyses")
    assert r.status_code == 429 and r.json() == {"detail": "Daily limit reached"}
    assert c.get(f"/api/sessions/{SID}").json()["live_runs_left"] == 0


def test_failure_is_reported_readably(make_client, monkeypatch):
    def boom(session, media):
        raise RuntimeError("403 PERMISSION_DENIED")

    monkeypatch.setattr(argon, "task_graph", boom)
    c = make_client()
    rec = wait_done(c, c.post(f"/api/sessions/{SID}/analyses").json()["id"])
    assert rec["status"] == "failed" and rec["stage"] == "steps" and rec["result"] is None
    assert rec["error"] == "Failed during the step analysis (Argon call A): RuntimeError: 403 PERMISSION_DENIED"


def test_stored_sample_analysis_is_latest_and_signoffs_go_to_the_store(make_client, stub_argon, samples, store):
    from app.pipeline import build_result

    stored = {"id": "a-20261014-000000-0000", "session_id": SID, "status": "done", "stage": "done",
              "live": False, "model": "gemini-4-argon", "started_at": "2026-10-14T00:00:00Z",
              "finished_at": "2026-10-14T00:01:00Z", "error": None,
              "usage": {"prompt_tokens": 1, "output_tokens": 1, "seconds": 1.0},
              "result": build_result(make_graph(), make_check()).model_dump(), "signoffs": {}}
    (samples.dir(SID) / "analyses").mkdir()
    sample_file = samples.dir(SID) / "analyses" / f"{stored['id']}.json"
    sample_file.write_text(json.dumps(stored), "utf-8")
    c = make_client()

    assert c.get(f"/api/sessions/{SID}").json()["latest"]["id"] == stored["id"]
    url = f"/api/sessions/{SID}/analyses/{stored['id']}"
    assert c.put(f"{url}/signoffs/s2", json={"decision": "flagged", "note": "check cam1"}).status_code == 200
    assert json.loads(sample_file.read_text("utf-8"))["signoffs"] == {}  # samples/ stays read-only
    assert store.read_json(analysis_key(SID, stored["id"]))["signoffs"]["s2"]["decision"] == "flagged"
    assert c.get(f"/api/sessions/{SID}").json()["latest"]["signoffs"]["s2"]["note"] == "check cam1"

    aid = c.post(f"/api/sessions/{SID}/analyses").json()["id"]
    assert wait_done(c, aid)["status"] == "done"
    # the curated sample stays the default; the live run is shown to its visitor by id
    assert c.get(f"/api/sessions/{SID}").json()["latest"]["id"] == stored["id"]
    assert c.get(f"/api/sessions/{SID}/analyses/{aid}").json()["live"] is True


def test_run_orphaned_by_a_restart_is_marked_failed(make_client, store):
    rec = {"id": "a-20261014-010101-abcd", "session_id": SID, "status": "running", "stage": "verifier",
           "live": True, "model": "gemini-4-argon", "started_at": "2026-10-14T01:01:01Z", "finished_at": None,
           "error": None, "usage": {"prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0},
           "result": None, "signoffs": {}}
    store.write_json(analysis_key(SID, rec["id"]), rec)
    got = make_client().get(f"/api/sessions/{SID}/analyses/{rec['id']}").json()
    assert got["status"] == "failed" and got["error"] == INTERRUPTED


def test_signoff_before_done_is_refused(make_client, store):
    rec = {"id": "a-20261014-020202-abcd", "session_id": SID, "status": "failed", "stage": "steps",
           "live": True, "model": "m", "started_at": "x", "finished_at": "y", "error": "e",
           "usage": {}, "result": None, "signoffs": {}}
    store.write_json(analysis_key(SID, rec["id"]), rec)
    c = make_client()
    url = f"/api/sessions/{SID}/analyses/{rec['id']}"
    assert c.put(f"{url}/signoffs/s1", json={"decision": "approved"}).status_code == 409
    assert c.get(f"{url}/export").status_code == 409
    assert c.get(f"/api/sessions/{SID}/analyses/a-nope").status_code == 404


def test_media_supports_range_requests(make_client, samples):
    c = make_client()
    full = c.get(f"/media/{SID}/cam0.mp4")
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert len(full.content) == CLIP_BYTES and full.headers.get("accept-ranges") == "bytes"

    part = c.get(f"/media/{SID}/cam0.mp4", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 100-199/{CLIP_BYTES}"
    assert part.content == (samples.dir(SID) / "cam0.mp4").read_bytes()[100:200]

    tail = c.get(f"/media/{SID}/cam0.mp4", headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and len(tail.content) == 10

    assert c.get(f"/media/{SID}/session.json").status_code == 404
    assert c.get(f"/media/{SID}/cam9.mp4").status_code == 404


def test_page_files_revalidate_but_clips_may_cache(make_client, samples):
    c = make_client()
    assert c.get("/").headers.get("cache-control") == "no-cache"
    assert c.get("/api/health").headers.get("cache-control") is None
    media = c.get(f"/media/{SID}/cam0.mp4", headers={"Range": "bytes=0-10"})
    assert media.status_code in (200, 206) and media.headers.get("cache-control") != "no-cache"
