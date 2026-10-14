"""The two-arm robot catalogue and the payload-based choice."""
from __future__ import annotations

from typing import Iterable

from .schemas import Part, Step

GRIPPER_ALLOWANCE_KG = 1.0

# Listed in order of preference when more than one arm fits.
# Both payloads are rated at the tool flange, so the gripper and fingers count against them.
CATALOGUE: list[dict] = [
    {"model": "UR5e + Robotiq 2F-85", "payload_kg": 5.0, "reach_mm": 850, "payload_includes_gripper": False},
    {"model": "Franka FR3", "payload_kg": 3.0, "reach_mm": 855, "payload_includes_gripper": False},
]


def public_catalogue() -> list[dict]:
    """What Argon is told about the arms."""
    return [{"model": a["model"], "payload_kg": a["payload_kg"], "reach_mm": a["reach_mm"],
             "gripper": "two-finger parallel gripper"} for a in CATALOGUE]


def _kg(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + " kg"


def choose_robot(parts: Iterable[Part], steps: Iterable[Step], catalogue: list[dict] | None = None) -> dict:
    """Pick the first arm (in catalogue order) whose payload covers the heaviest part plus the gripper.

    The heaviest part is taken among the parts of `automate` steps; if those name no
    known part, among all parts.
    """
    catalogue = catalogue or CATALOGUE
    parts = list(parts)
    by_name = {p.name.strip().lower(): p for p in parts}
    robot_parts = {n.strip().lower() for s in steps if s.verdict == "automate" for n in s.parts}
    handled = [by_name[n] for n in robot_parts if n in by_name]
    basis = "parts in the automatable steps"
    if not handled:
        handled, basis = parts, "all parts seen"
    heaviest = max(handled, key=lambda p: p.mass_g, default=None)
    part_kg = max(0.0, heaviest.mass_g / 1000.0) if heaviest else 0.0
    part_text = (f"the heaviest of the {basis} is {heaviest.name} at about {heaviest.mass_g:.0f} g"
                 if heaviest else "Argon listed no parts, so the part mass is taken as 0")

    def needed(arm: dict) -> float:
        return part_kg + (0.0 if arm["payload_includes_gripper"] else GRIPPER_ALLOWANCE_KG)

    fitting = [a for a in catalogue if a["payload_kg"] >= needed(a)]
    if fitting:
        arm, fits = fitting[0], True
    else:
        arm, fits = max(catalogue, key=lambda a: a["payload_kg"] - needed(a)), False
    need = needed(arm)
    allowance = ("" if arm["payload_includes_gripper"] else
                 f" plus a {_kg(GRIPPER_ALLOWANCE_KG)} allowance for the gripper and fingers, "
                 "assuming the rated payload is at the tool flange and excludes the gripper")
    why = f"Needed payload {_kg(need)}: {part_text}{allowance}. "
    if fits and len(fitting) > 1:
        others = ", ".join(f"{a['model']} ({_kg(a['payload_kg'])})" for a in fitting[1:])
        why += (f"Every arm in the catalogue carries it; {arm['model']} is preferred for its "
                f"{_kg(arm['payload_kg'])} payload, leaving {_kg(arm['payload_kg'] - need)} of margin "
                f"(also enough: {others}).")
    elif fits:
        short = ", ".join(f"{a['model']} ({_kg(a['payload_kg'])})" for a in catalogue if a not in fitting)
        why += (f"{arm['model']} carries it with {_kg(arm['payload_kg'] - need)} of margin"
                + (f"; {short} would not." if short else "."))
    else:
        why += (f"No arm in the catalogue carries it; {arm['model']} has the most payload "
                f"({_kg(arm['payload_kg'])}) and falls short by {_kg(need - arm['payload_kg'])}.")
    return {"model": arm["model"], "payload_kg": arm["payload_kg"], "reach_mm": arm["reach_mm"],
            "needed_payload_kg": round(need, 3), "fits": fits, "why": why}
