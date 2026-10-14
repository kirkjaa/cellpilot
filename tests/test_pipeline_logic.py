import pytest

from app.pipeline import apply_verifier, build_result, cycle_stats, readable_error
from app.schemas import Verify

from .conftest import make_check, make_graph


def by_id(steps):
    return {s.id: s for s in steps}


def test_agreeing_step_is_not_preflagged():
    s1 = by_id(apply_verifier(make_graph(), make_check()))["s1"]
    assert s1.preflagged is False
    assert s1.verifier.agree and s1.verifier.verdict_agree
    assert s1.verifier.timing_delta_s == 0.3
    assert [(o.start_s, o.end_s) for o in s1.occurrences] == [(0, 4), (10, 14), (20, 24)]
    assert s1.mean_duration_s == 4.0


def test_timing_correction_over_one_second_is_applied():
    s2 = by_id(apply_verifier(make_graph(), make_check()))["s2"]
    assert [(o.cycle, o.start_s, o.end_s) for o in s2.occurrences] == [(1, 4, 9), (2, 15.5, 19.5), (3, 24, 29)]
    assert s2.preflagged is True
    assert s2.mean_duration_s == round((5 + 4 + 5) / 3, 2)
    assert s2.verdict == "keep_human"


def test_timing_correction_within_one_second_is_ignored():
    check = Verify.model_validate({"steps": [
        {"id": "s1", "agree": True, "timing_delta_s": 0.5,
         "corrected": [{"cycle": 1, "start_s": 0.5, "end_s": 4.4}], "verdict_agree": True, "note": ""}]})
    s1 = by_id(apply_verifier(make_graph(), check))["s1"]
    assert (s1.occurrences[0].start_s, s1.occurrences[0].end_s) == (0, 4)
    assert s1.preflagged is False


def test_correction_for_unknown_cycle_adds_nothing():
    check = Verify.model_validate({"steps": [
        {"id": "s1", "agree": False, "timing_delta_s": 0.4,
         "corrected": [{"cycle": 7, "start_s": 70, "end_s": 74}], "verdict_agree": True, "note": ""}]})
    s1 = by_id(apply_verifier(make_graph(), check))["s1"]
    assert len(s1.occurrences) == 3
    assert s1.preflagged is False


def test_verdict_disagreement_preflags_but_keeps_verdict():
    s3 = by_id(apply_verifier(make_graph(), make_check()))["s3"]
    assert s3.verdict == "needs_fixture"
    assert s3.verifier.verdict_agree is False
    assert s3.preflagged is True


def test_large_timing_delta_alone_preflags():
    check = Verify.model_validate({"steps": [
        {"id": "s1", "agree": False, "timing_delta_s": -1.6, "corrected": [], "verdict_agree": True, "note": ""}]})
    s1 = by_id(apply_verifier(make_graph(), check))["s1"]
    assert s1.preflagged is True
    assert s1.verifier.timing_delta_s == 1.6


def test_step_missing_from_verifier_is_preflagged_with_null_verifier():
    check = Verify.model_validate({"steps": []})
    for s in apply_verifier(make_graph(), check):
        assert s.verifier is None and s.preflagged is True


def test_verifier_never_adds_steps():
    steps = apply_verifier(make_graph(), make_check())
    assert [s.id for s in steps] == ["s1", "s2", "s3"]


def test_result_numbers():
    r = build_result(make_graph(), make_check())
    assert r.cycles == 3
    assert r.cycle_time_s == 10.0  # cycle starts at 0 and 20 over two cycle gaps
    assert r.counts.model_dump() == {"automate": 1, "keep_human": 1, "needs_fixture": 1, "total": 3}
    assert r.automated_share == 0.4  # s1's 4.0 s of a 10 s cycle
    assert r.steps[1].confidence == 1.0  # clamped from 1.4
    assert r.robot.model == "UR5e + Robotiq 2F-85"
    assert r.roi_defaults.currency == "SGD"


def test_cycle_time_for_a_single_cycle_is_its_span():
    g = make_graph()
    for s in g.steps:
        s.occurrences = s.occurrences[:1]
    assert cycle_stats(apply_verifier(g, None), g.cycles) == (1, 10.0)


def test_no_occurrences_gives_zero_share():
    g = make_graph()
    for s in g.steps:
        s.occurrences = []
    r = build_result(g, None)
    assert r.cycle_time_s == 0.0 and r.automated_share == 0.0 and r.cycles == 3


def test_readable_error_hides_credentials_path(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secret/place/key.json")
    msg = readable_error("steps", RuntimeError("File /secret/place/key.json was not found"))
    assert "/secret/place" not in msg
    assert msg.startswith("Failed during the step analysis (Argon call A): RuntimeError:")
