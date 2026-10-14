import pytest

from app.catalogue import CATALOGUE, GRIPPER_ALLOWANCE_KG, REACH_FLOOR_MM, choose_robot, cycle_estimate
from app.schemas import Part, Robot, Step


def part(name, grams):
    return Part(name=name, mass_g=grams, size_mm=[10, 10, 10], confidence=0.5)


def step(verdict, parts, mean=1.0, sid="s1"):
    return Step(id=sid, name="x", verdict=verdict, reasons=[], occurrences=[], parts=parts,
                confidence=0.5, mean_duration_s=mean, verifier=None, preflagged=False)


def test_catalogue_is_six_arms_in_ascending_reach_with_sources():
    assert [a["model"] for a in CATALOGUE] == ["UR3e", "xArm 6", "UR5e", "Franka FR3", "Doosan M0609", "UR10e"]
    assert [a["reach_mm"] for a in CATALOGUE] == sorted(a["reach_mm"] for a in CATALOGUE)
    assert all(a["source"].startswith("https://") and a["price_band_usd"][0] < a["price_band_usd"][1]
               for a in CATALOGUE)


def test_light_part_without_layout_takes_smallest_arm_over_the_reach_floor():
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])])
    assert r["model"] == "xArm 6" and r["fits"] is True
    assert r["needed_payload_kg"] == pytest.approx(0.12 + GRIPPER_ALLOWANCE_KG)
    assert r["needed_reach_mm"] is None and r["reach_fits"] is None and r["payload_fits"] is True
    assert "allowance" in r["why"] and "excludes the gripper" in r["why"]
    assert f"{REACH_FLOOR_MM} mm" in r["why"] and "Reach not checked" in r["why"]
    assert r["price_band"] == {"low": 10000, "high": 14000, "currency": "USD", "label": "indicative"}
    assert r["gripper"].startswith("two-finger") and r["source"].startswith("https://")
    Robot.model_validate(r)  # the contract shape


def test_alternatives_judge_every_arm():
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])], needed_reach_m=0.526)
    alts = {o["model"]: o for o in r["alternatives"]}
    assert len(alts) == 6 and sum(o["chosen"] for o in alts.values()) == 1
    assert r["model"] == "xArm 6" and r["needed_reach_mm"] == 526 and r["reach_fits"] is True
    assert alts["UR3e"]["reach_fits"] is False and alts["UR3e"]["fits"] is False
    assert alts["UR3e"]["note"] == "reach short by 26 mm"
    assert alts["xArm 6"]["note"].startswith("chosen")
    assert alts["UR5e"]["fits"] is True and alts["UR5e"]["note"].startswith("also adequate")
    assert "Needed reach 526 mm" in r["why"] and "estimated from video" in r["why"]
    assert r["reach_basis"].startswith("layout estimated")


def test_reach_check_moves_the_choice_up_the_catalogue():
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])], needed_reach_m=0.88)
    assert r["model"] == "Doosan M0609" and r["fits"] is True
    assert [o["model"] for o in r["alternatives"] if o["reach_fits"]] == ["Doosan M0609", "UR10e"]


def test_heavy_part_skips_three_kilo_arms():
    r = choose_robot([part("housing", 2500)], [step("automate", ["housing"])], needed_reach_m=0.8)
    assert r["model"] == "UR5e" and r["fits"] is True
    assert r["needed_payload_kg"] == pytest.approx(3.5)
    alts = {o["model"]: o for o in r["alternatives"]}
    assert alts["Franka FR3"]["payload_fits"] is False and "payload short by 0.5 kg" in alts["Franka FR3"]["note"]
    assert alts["xArm 6"]["reach_fits"] is False


def test_nothing_fits_picks_the_closest_and_says_so():
    r = choose_robot([part("block", 13000)], [step("automate", ["block"])], needed_reach_m=0.5)
    assert r["fits"] is False and r["model"] == "UR10e" and r["payload_fits"] is False
    assert r["needed_payload_kg"] == pytest.approx(14.0)
    assert "No arm in the catalogue passes" in r["why"] and "closest" in r["why"]


def test_preference_follows_catalogue_order_when_several_fit():
    cat = list(reversed(CATALOGUE))
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])], cat, needed_reach_m=0.5)
    assert r["model"] == "UR10e"


def test_only_parts_of_automate_steps_count():
    parts = [part("bracket", 200), part("engine", 4500)]
    steps = [step("automate", ["Bracket"]), step("keep_human", ["engine"], sid="s2")]
    r = choose_robot(parts, steps)
    assert r["needed_payload_kg"] == pytest.approx(1.2) and r["fits"] is True


def test_falls_back_to_all_parts_and_to_no_parts():
    r = choose_robot([part("bracket", 800)], [step("keep_human", ["bracket"])])
    assert r["needed_payload_kg"] == pytest.approx(1.8)
    assert choose_robot([], [])["needed_payload_kg"] == pytest.approx(1.0)


def test_cycle_estimate_uses_the_speed_factor_and_keeps_gaps_with_the_operator():
    steps = [step("automate", [], 4.0, "s1"), step("keep_human", [], 3.0, "s2"), step("needs_fixture", [], 1.0, "s3")]
    ce = cycle_estimate(steps, 10.0)
    assert ce["human_cycle_s"] == 10.0 and ce["automated_steps_s"] == 4.0
    assert ce["robot_cycle_s"] == pytest.approx(5.2) and ce["speed_factor"] == 1.3
    assert ce["operator_cycle_s"] == 4.0 and ce["unassigned_s"] == 2.0
    assert ce["parallel_cycle_s"] == 6.0  # max(5.2, 4 + 2)
    assert "no robot motion was planned" in ce["method"]
    assert cycle_estimate(steps, 0.0) is None
