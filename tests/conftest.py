"""Shared fixtures. Argon is always stubbed here: no test touches the network."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import argon
from app.limits import DailyLimit
from app.schemas import TaskGraph, Verify
from app.store import LocalStore, Samples

SID = "dev-test"
CLIP_BYTES = 50_000


def make_graph() -> TaskGraph:
    """Three cycles, 10 s apart; s1 automate 4 s, s2 keep_human 5 s, s3 needs_fixture 1 s."""
    def occ(start, length):
        return [{"cycle": c + 1, "start_s": start + 10 * c, "end_s": start + 10 * c + length} for c in range(3)]

    def reasons(t):
        return [{"text": "seen", "view": "cam0", "t_s": t}, {"text": "also seen", "view": "cam1", "t_s": t + 1}]

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


@pytest.fixture
def stub_argon(monkeypatch):
    """Replace calls A and D; records what they were given."""
    calls = {"a": [], "d": []}

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

    monkeypatch.setattr(argon, "task_graph", fake_a)
    monkeypatch.setattr(argon, "verify", fake_d)
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
