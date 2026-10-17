"""Shared fixtures. Argon is always stubbed here: no test touches the network."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import argon
from app.limits import DailyLimit
from app.schemas import ArgonLayout, Notes, TaskGraph, Verify
from app.store import LocalStore, Samples

SID = "dev-test"
CLIP_BYTES = 50_000


def make_graph() -> TaskGraph:
    """Three cycles, 10 s apart; s1 automate 4 s, s2 keep_human 5 s, s3 needs_fixture 1 s."""
    def occ(start, length):
        return [{"cycle": c + 1, "start_s": start + 10 * c, "end_s": start + 10 * c + length} for c in range(3)]

    def reasons(t):
        return [{"text": "seen", "view": "cam0", "t_s": t, "box": [100, 200, 300, 400]},
                {"text": "also seen", "view": "cam1", "t_s": t + 1, "box": []}]

    return TaskGraph.model_validate({
        "cycles": 3,
        "summary": "Three cycles of a kitting task.",
        "steps": [
            {"id": "s1", "name": "Pick bracket", "verdict": "automate", "reasons": reasons(1),
             "occurrences": occ(0, 4), "parts": ["bracket"], "confidence": 0.8},
            {"id": "s2", "name": "Insert screw", "verdict": "keep_human", "reasons": reasons(5),
             "occurrences": occ(4, 5), "parts": ["screw"], "confidence": 1.4},
            {"id": "s3", "name": "Place in tray", "verdict": "needs_fixture", "reasons": reasons(9),
             "occurrences": occ(9, 1), "parts": ["bracket"], "confidence": 0.6},
        ],
        "parts": [
            {"name": "bracket", "mass_g": 120, "size_mm": [80, 40, 3], "confidence": 0.6},
            {"name": "screw", "mass_g": 5, "size_mm": [12, 4, 4], "confidence": 0.5},
        ],
    })


def make_check() -> Verify:
    return Verify.model_validate({"steps": [
        {"id": "s1", "agree": True, "timing_delta_s": 0.3, "corrected": [], "verdict_agree": True, "note": ""},
        {"id": "s2", "agree": False, "timing_delta_s": 2.5,
         "corrected": [{"cycle": 2, "start_s": 15.5, "end_s": 19.5}], "verdict_agree": True,
         "note": "cam1 shows the screw going in a second later."},
        {"id": "s3", "agree": True, "timing_delta_s": 0.2, "corrected": [], "verdict_agree": False,
         "note": "A gripper could place it without a fixture."},
        {"id": "s9", "agree": False, "timing_delta_s": 9.0, "corrected": [], "verdict_agree": False,
         "note": "an invented step"},
    ]})


def make_layout() -> ArgonLayout:
    """A 1.2 x 0.6 m bench: bin A on the left, a tray on the right; s1 picks from the bin and places
    mid-bench, s3 moves the bracket from there to the tray; s2 picks nothing; s9 does not exist."""
    return ArgonLayout.model_validate({
        "bench_length_m": 1.2, "bench_width_m": 0.6, "operator_x_m": 0.6,
        "objects": [
            {"name": "bin A", "kind": "bin", "x_m": 0.2, "y_m": 0.4, "length_m": 0.2, "width_m": 0.2,
             "step_ids": ["s1"], "confidence": 0.7},
            {"name": "tray", "kind": "part_destination", "x_m": 1.0, "y_m": 0.4, "length_m": 0.3, "width_m": 0.2,
             "step_ids": ["s3"], "confidence": 0.6},
            {"name": "screwdriver", "kind": "tool", "x_m": 0.6, "y_m": 0.5, "length_m": 0.2, "width_m": 0.05,
             "step_ids": ["s2"], "confidence": 0.5},
        ],
        "steps": [
            {"id": "s1", "pick_xy_m": [0.2, 0.4], "place_xy_m": [0.6, 0.2], "confidence": 0.7},
            {"id": "s2", "pick_xy_m": [], "place_xy_m": [], "confidence": 0.5},
            {"id": "s3", "pick_xy_m": [0.6, 0.2], "place_xy_m": [1.0, 0.4], "confidence": 0.6},
            {"id": "s9", "pick_xy_m": [5.0, 5.0], "place_xy_m": [], "confidence": 0.9},
        ],
        "confidence": 0.6, "notes": "Scale from the bracket's 80 mm length and the operator's hands.",
    })


@pytest.fixture
def samples_root(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    d = root / SID
    d.mkdir(parents=True)
    clips = []
    for view, offset in (("cam0", 0.0), ("cam1", 0.2), ("cam2", 0.45)):
        (d / f"{view}.mp4").write_bytes(bytes(range(256)) * (CLIP_BYTES // 256) + b"x" * (CLIP_BYTES % 256))
        clips.append({"view": view, "file": f"{view}.mp4", "fps": 30, "width": 640, "height": 360,
                      "duration_s": 30.0, "offset_s": offset})
    (d / "session.json").write_text(json.dumps({
        "id": SID, "name": "Test session", "kind": "sample", "recorded": "2026-10-14",
        "synthetic": True, "clips": clips}), "utf-8")
    return root


@pytest.fixture
def samples(samples_root: Path) -> Samples:
    return Samples(samples_root)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def make_notes() -> Notes:
    return Notes.model_validate({
        "for_the_owner": "A cobot could take the bracket pick and the tray place; the screw stays with the operator. "
                         "Payback is about two years at the default inputs. The open question is a nest for the tray.",
        "step_notes": [
            {"id": "s1", "recommendation": "A parallel gripper on the 80 mm faces; present brackets in a tray.", "fixture": ""},
            {"id": "s2", "recommendation": "Stays manual: the screw needs feel to start the thread.", "fixture": ""},
            {"id": "s3", "recommendation": "A nest that locates the tray turns this into a plain place.", "fixture": "tray nest"},
        ],
        "risks": [{"title": "Part presentation", "detail": "Loose brackets vary in pose; check with a tray."},
                  {"title": "Cycle time", "detail": "The robot is slower than the operator on the pick."},
                  {"title": "Grant terms", "detail": "Confirm the cap with the agency."}],
        "next_steps": ["Measure the pose spread of the brackets", "Try a tray nest with a part in hand",
                       "Ask two integrators for a quote"],
        "questions": ["Who maintains the cell?"],
    })


@pytest.fixture
def stub_argon(monkeypatch):
    """Replace calls A, D, C and E; records what they were given."""
    calls = {"a": [], "d": [], "c": [], "e": []}

    def fake_a(session, media):
        calls["a"].append(media)
        return argon.CallResult(parsed=make_graph(), text=make_graph().model_dump_json(),
                                usage={"prompt_tokens": 1000, "cached_tokens": 0, "thoughts_tokens": 300,
                                       "candidates_tokens": 200, "seconds": 0.01})

    def fake_d(session, media, graph):
        calls["d"].append(media)
        return argon.CallResult(parsed=make_check(), text=make_check().model_dump_json(),
                                usage={"prompt_tokens": 1500, "cached_tokens": 900, "thoughts_tokens": 100,
                                       "candidates_tokens": 50, "seconds": 0.01})

    def fake_c(session, media, steps, parts):
        calls["c"].append(media)
        return argon.CallResult(parsed=make_layout(), text=make_layout().model_dump_json(),
                                usage={"prompt_tokens": 1200, "cached_tokens": 900, "thoughts_tokens": 50,
                                       "candidates_tokens": 150, "seconds": 0.01})

    def fake_e(session, result):
        calls["e"].append(result)
        return argon.CallResult(parsed=make_notes(), text=make_notes().model_dump_json(),
                                usage={"prompt_tokens": 800, "cached_tokens": 0, "thoughts_tokens": 0,
                                       "candidates_tokens": 300, "seconds": 0.01, "model_version": "gemini-3.8-flash-test"})

    monkeypatch.setattr(argon, "task_graph", fake_a)
    monkeypatch.setattr(argon, "verify", fake_d)
    monkeypatch.setattr(argon, "layout", fake_c)
    from app import twin as twin_mod

    def fake_twin(result):
        return {"simulator": "stub", "arm_requested": result.robot.model, "arm_model": "UR5e", "model_note": "",
                "base": {"x_m": 0.5, "y_m": 0.7, "rule": "stub", "farthest_point_mm": 400, "fits": True}, "points": [],
                "steps_simulated": [], "all_reachable": True, "collision_free": True, "path_collisions": [],
                "robot_cycle_s": 2.0, "operator_cycle_s": 1.0, "unassigned_s": 0.5, "parallel_cycle_s": 2.0,
                "estimate_parallel_cycle_s": 2.0, "segments": [], "assumptions": {}, "fixtures": [], "parts": [],
                "parts_total": {"low": 0, "high": 0, "currency": "USD", "note": ""}, "clip": None, "method": "stub"}
    monkeypatch.setattr(twin_mod, "simulate", fake_twin)
    monkeypatch.setattr(argon, "notes", fake_e)
    return calls


@pytest.fixture
def make_client(store, samples):
    def make(cap: int = 10) -> TestClient:
        from app.main import create_app

        return TestClient(create_app(store=store, samples=samples, limit=DailyLimit(store, cap=cap)))
    return make


def wait_done(client: TestClient, aid: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = client.get(f"/api/sessions/{SID}/analyses/{aid}").json()
        if rec["status"] in ("done", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError(f"analysis {aid} did not finish: {rec}")
