"""The layout: Argon's estimate cleaned, the robot base placed, the needed reach computed."""
import pytest

from app.layout import MARGIN_M, build_layout, place_base
from app.pipeline import apply_verifier, build_result
from app.schemas import ArgonLayout, Layout

from .conftest import make_check, make_graph, make_layout


def steps():
    return apply_verifier(make_graph(), make_check())


def test_layout_keeps_known_steps_only_and_places_the_base_on_the_back_edge():
    lay = build_layout(make_layout(), steps())
    Layout.model_validate(lay.model_dump())
    assert lay.bench == {"length_m": 1.2, "width_m": 0.6}
    assert lay.operator == {"x_m": 0.6, "y_m": -0.35}
    assert [o.name for o in lay.objects] == ["bin A", "tray", "screwdriver"]
    # s2 picks nothing, s9 is not a step: four anchors from s1 and s3
    assert [(a.step_id, a.kind) for a in lay.anchors] == [("s1", "pick"), ("s1", "place"), ("s3", "pick"), ("s3", "place")]
    assert lay.anchors[0].verdict == "automate" and lay.anchors[2].verdict == "needs_fixture"
    assert (lay.robot_base.x_m, lay.robot_base.y_m) == (0.6, 0.6)
    # farthest robot point from (0.6, 0.6) is 0.447 m; within 85 % of reach -> 0.526 m
    assert lay.needed_reach_m == pytest.approx(0.447 / 0.85, abs=0.002)
    assert "estimated from video" in lay.source and lay.confidence == 0.6 and "80 mm" in lay.notes


def test_place_base_prefers_an_end_when_the_work_is_at_one_side():
    base, need = place_base([(0.05, 0.3), (0.1, 0.2)], 1.2, 0.6)
    assert (base.x_m, base.y_m) == (0.0, 0.3)
    assert need == pytest.approx(0.1414 / 0.85, abs=0.002)  # (0.1, 0.2) is the farthest point
    base, need = place_base([], 1.2, 0.6)
    assert (base.x_m, base.y_m) == (0.6, 0.6) and need is None


def test_values_are_clamped_and_bad_pairs_dropped():
    est = make_layout()
    est.bench_length_m, est.bench_width_m, est.operator_x_m = 99.0, -5.0, 50.0
    est.objects[0].x_m, est.objects[0].length_m = -40.0, 0.0
    est.steps[0].pick_xy_m = [1.0]  # not a pair
    est.steps[2].place_xy_m = [40.0, 40.0]  # far outside: clamped to the margin
    lay = build_layout(est, steps())
    assert lay.bench == {"length_m": 4.0, "width_m": 0.3} and lay.operator["x_m"] == 4.0
    assert lay.objects[0].x_m == -MARGIN_M and lay.objects[0].length_m == 0.02
    assert [(a.step_id, a.kind) for a in lay.anchors] == [("s1", "place"), ("s3", "pick"), ("s3", "place")]
    assert (lay.anchors[-1].x_m, lay.anchors[-1].y_m) == (4.0 + MARGIN_M, 0.3 + MARGIN_M)


def test_only_robot_steps_set_the_needed_reach():
    est = ArgonLayout.model_validate({
        "bench_length_m": 1.0, "bench_width_m": 0.5, "operator_x_m": 0.5, "objects": [],
        "steps": [{"id": "s2", "pick_xy_m": [0.0, 0.0], "place_xy_m": [1.0, 0.0], "confidence": 0.5}],
        "confidence": 0.4, "notes": ""})
    lay = build_layout(est, steps())  # s2 is keep_human: anchors are drawn but set no reach
    assert len(lay.anchors) == 2 and lay.needed_reach_m is None and lay.reach_rule == ""


def test_build_result_feeds_the_reach_into_the_robot_choice():
    r = build_result(make_graph(), make_check(), make_layout())
    assert r.layout is not None and r.layout_error is None
    assert r.robot.needed_reach_mm == 526 and r.robot.model == "xArm 6" and r.robot.reach_fits is True
    assert r.cycle_estimate.robot_cycle_s == pytest.approx(5.2)
    no_layout = build_result(make_graph(), make_check())
    assert no_layout.layout is None and no_layout.layout_error and no_layout.robot.needed_reach_mm is None
