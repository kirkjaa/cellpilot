import pytest

from app.catalogue import CATALOGUE, choose_robot
from app.schemas import Part, Step


def part(name, grams):
    return Part(name=name, mass_g=grams, size_mm=[10, 10, 10], confidence=0.5)


def step(verdict, parts):
    return Step(id="s1", name="x", verdict=verdict, reasons=[], occurrences=[], parts=parts,
                confidence=0.5, mean_duration_s=1.0, verifier=None, preflagged=False)


def test_light_part_takes_first_arm_with_gripper_allowance():
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])])
    assert r["model"] == "UR5e + Robotiq 2F-85"
    assert r["needed_payload_kg"] == pytest.approx(1.12)
    assert r["fits"] is True
    assert "allowance" in r["why"] and "excludes the gripper" in r["why"]


def test_part_too_heavy_for_fr3_picks_ur5e():
    r = choose_robot([part("housing", 2500)], [step("automate", ["housing"])])
    assert r["model"] == "UR5e + Robotiq 2F-85" and r["fits"] is True
    assert r["needed_payload_kg"] == pytest.approx(3.5)
    assert "Franka FR3" in r["why"]


def test_preference_follows_catalogue_order_when_both_fit():
    r = choose_robot([part("bracket", 120)], [step("automate", ["bracket"])], list(reversed(CATALOGUE)))
    assert r["model"] == "Franka FR3"


def test_nothing_fits():
    r = choose_robot([part("block", 4500)], [step("automate", ["block"])])
    assert r["fits"] is False
    assert r["model"] == "UR5e + Robotiq 2F-85"
    assert r["needed_payload_kg"] == pytest.approx(5.5)
    assert "falls short" in r["why"]


def test_only_parts_of_automate_steps_count():
    parts = [part("bracket", 200), part("engine", 4500)]
    steps = [step("automate", ["Bracket"]), step("keep_human", ["engine"])]
    r = choose_robot(parts, steps)
    assert r["needed_payload_kg"] == pytest.approx(1.2) and r["fits"] is True


def test_falls_back_to_all_parts_and_to_no_parts():
    r = choose_robot([part("bracket", 800)], [step("keep_human", ["bracket"])])
    assert r["needed_payload_kg"] == pytest.approx(1.8)
    assert choose_robot([], [])["needed_payload_kg"] == pytest.approx(1.0)
