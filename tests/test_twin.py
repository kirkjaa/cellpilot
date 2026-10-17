"""The cobot twin: base placement, reachability and timing on a small layout. The simulation itself needs
MuJoCo, mink and the Menagerie arm models (CELLPILOT_MENAGERIE); without them only the pure parts run."""
import os

import pytest

from app import twin
from app.schemas import Layout, LayoutAnchor, LayoutObjectOut, Result, RobotBase, Twin


def _layout(far=False):
    px = 2.6 if far else 0.45   # 2.6 m: more than a metre past the bench end, beyond any base on it
    return Layout(source="test", frame="bench", bench={"length_m": 1.2, "width_m": 0.8}, operator={"x_m": 0.6, "y_m": -0.35},
                  objects=[LayoutObjectOut(name="bin A", kind="bin", x_m=0.2, y_m=0.6, length_m=0.2, width_m=0.15),
                           LayoutObjectOut(name="tray", kind="part_destination", x_m=0.8, y_m=0.3, length_m=0.3, width_m=0.2)],
                  anchors=[LayoutAnchor(step_id="s1", name="Pick part", verdict="needs_fixture", kind="pick", x_m=0.25, y_m=0.35),
                           LayoutAnchor(step_id="s2", name="Place part", verdict="automate", kind="place", x_m=px, y_m=0.3)],
                  robot_base=RobotBase(x_m=0.6, y_m=0.75, rule="test"), needed_reach_m=0.5, reach_rule="test")


def _result(layout):
    return Result.model_validate({
        "summary": "t", "cycles": 2, "cycle_time_s": 6.0,
        "steps": [{"id": "s1", "name": "Pick part", "verdict": "needs_fixture", "confidence": 0.9, "parts": ["part"], "occurrences": [],
                   "mean_duration_s": 1.0, "reasons": [], "verifier": None, "preflagged": False},
                  {"id": "s2", "name": "Place part", "verdict": "automate", "confidence": 0.9, "parts": ["part"], "occurrences": [],
                   "mean_duration_s": 1.5, "reasons": [], "verifier": None, "preflagged": False}],
        "parts": [], "counts": {"automate": 1, "needs_fixture": 1, "keep_human": 0, "total": 2},
        "automated_share": 0.25,
        "robot": {"model": "UR3e", "vendor": "Universal Robots", "payload_kg": 3.0, "reach_mm": 500.0, "needed_payload_kg": 1.1,
                  "fits": True, "why": "t", "gripper": "t", "source": "t", "needed_reach_mm": 400.0, "payload_fits": True,
                  "reach_fits": True, "reach_basis": "t", "price_band": {"low": 23000, "high": 28000, "currency": "USD", "label": "indicative"},
                  "alternatives": []},
        "roi_defaults": {}, "layout": layout.model_dump(),
        "cycle_estimate": {"human_cycle_s": 6.0, "automated_steps_s": 1.5, "robot_cycle_s": 1.95, "operator_cycle_s": 1.0,
                           "unassigned_s": 3.5, "parallel_cycle_s": 4.5, "speed_factor": 1.3, "method": "t"},
    })


def test_pick_model_names_the_stand_in():
    assert twin.pick_model("UR5e") == ("UR5e", "")
    used, note = twin.pick_model("UR3e")
    assert used == "UR5e" and "UR3e has no MuJoCo Menagerie model" in note


def test_base_candidates_prefer_fitting_clear_positions():
    lay = _layout()
    pts = [{"x_m": 0.25, "y_m": 0.35}, {"x_m": 0.45, "y_m": 0.3}]
    cands = twin.base_candidates(lay, pts, 850)
    assert cands[0]["fits"] and cands[0]["clearance_m"] >= min(c["clearance_m"] for c in cands)
    assert all(c["fits"] for c in cands[:3])


def test_parts_list_totals_only_priced_items():
    class R:  # the bits parts_list reads
        model, vendor = "UR3e", "Universal Robots"
    items = twin.parts_list(R(), [{"name": "nest"}])
    total = twin.parts_total(items)
    assert any("Locating nest" in i["item"] for i in items)
    assert total["low"] == 23000 + 4000 + 800 + 800 and total["high"] == 28000 + 6500 + 2500 + 2000
    assert all(i["low"] is None for i in items if "Risk assessment" in i["item"])


needs_sim = pytest.mark.skipif(not os.environ.get("CELLPILOT_MENAGERIE"), reason="needs MuJoCo Menagerie (CELLPILOT_MENAGERIE)")


@needs_sim
def test_simulate_reaches_nearby_points_and_borrows_the_pick():
    tw = Twin.model_validate(twin.simulate(_result(_layout())))
    assert tw.arm_model == "UR5e" and tw.arm_requested == "UR3e"
    kinds = {(p.step_id, p.kind) for p in tw.points}
    assert kinds == {("s2", "pick"), ("s2", "place")}   # the place-only step picks from the nest's point
    assert tw.all_reachable and tw.collision_free
    assert tw.robot_cycle_s > 1.0 and tw.parallel_cycle_s >= tw.robot_cycle_s
    assert len(tw.fixtures) == 1 and tw.fixtures[0].step_id == "s1"
    assert tw.parts_total["low"] > 0


@needs_sim
def test_simulate_reports_an_unreachable_point():
    tw = Twin.model_validate(twin.simulate(_result(_layout(far=True))))
    assert not tw.all_reachable and not tw.base["fits"]
    assert any(not p.reachable for p in tw.points if p.kind == "place")
