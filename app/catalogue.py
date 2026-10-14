"""The cobot catalogue, the choice by payload and reach, and the cycle-time estimate.

Payload and reach are the vendors' published figures (the `source` link on each entry).
The price bands are NOT vendor figures: they are rough list-price bands seen in public
reseller listings in 2024-2026, kept only to give the payback card a starting point.
They are labelled "indicative" everywhere they appear and must not be read as quotes.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .schemas import Part, Step

GRIPPER_ALLOWANCE_KG = 1.0       # a two-finger parallel gripper of the Robotiq 2F-85 class, with fingers
REACH_USE = 0.85                 # pick and place points should sit within 85 % of the rated reach
REACH_FLOOR_MM = 700             # with no layout, arms under this reach are not proposed for a bench job
ROBOT_SPEED_FACTOR = 1.3         # a cobot at collaborative speed takes about 30 % longer per step than the operator
GRIPPER_TEXT = "two-finger parallel gripper (Robotiq 2F-85 class), 1 kg allowance"

UR_DATASHEET = "https://www.universal-robots.com/media/1829346/11_2023_collective_data-sheet.pdf"

# In ascending reach: the first arm that passes both checks is the smallest adequate one.
# Every payload is rated at the tool flange, so the gripper and fingers count against it.
CATALOGUE: list[dict] = [
    {"model": "UR3e", "vendor": "Universal Robots", "payload_kg": 3.0, "reach_mm": 500,
     "source": UR_DATASHEET, "price_band_usd": (23000, 28000)},
    {"model": "xArm 6", "vendor": "UFACTORY", "payload_kg": 5.0, "reach_mm": 700,
     "source": "https://www.ufactory.cc/xarm-collaborative-robot/", "price_band_usd": (10000, 14000)},
    {"model": "UR5e", "vendor": "Universal Robots", "payload_kg": 5.0, "reach_mm": 850,
     "source": UR_DATASHEET, "price_band_usd": (35000, 42000)},
    {"model": "Franka FR3", "vendor": "Franka Robotics", "payload_kg": 3.0, "reach_mm": 855,
     "source": "https://franka.de/products/franka-research-3", "price_band_usd": (20000, 30000)},
    {"model": "Doosan M0609", "vendor": "Doosan Robotics", "payload_kg": 6.0, "reach_mm": 900,
     "source": "https://manual.doosanrobotics.com/en/user-manual/3.4.0/1-m-h-series/robot-specifications",
     "price_band_usd": (30000, 40000)},
    {"model": "UR10e", "vendor": "Universal Robots", "payload_kg": 12.5, "reach_mm": 1300,
     "source": UR_DATASHEET, "price_band_usd": (50000, 60000)},
]


def public_catalogue() -> list[dict]:
    """What Argon is told about the arms."""
    return [{"model": a["model"], "vendor": a["vendor"], "payload_kg": a["payload_kg"], "reach_mm": a["reach_mm"],
             "gripper": "two-finger parallel gripper"} for a in CATALOGUE]


def _kg(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + " kg"


def _band(arm: dict) -> Optional[dict]:
    band = arm.get("price_band_usd")
    if not band:
        return None
    return {"low": float(band[0]), "high": float(band[1]), "currency": "USD", "label": "indicative"}


def needed_payload(parts: Iterable[Part], steps: Iterable[Step]) -> tuple[float, str]:
    """Heaviest part of the `automate` steps (else of all parts) plus the gripper allowance, and why."""
    parts = list(parts)
    by_name = {p.name.strip().lower(): p for p in parts}
    robot_parts = {n.strip().lower() for s in steps if s.verdict == "automate" for n in s.parts}
    handled = [by_name[n] for n in robot_parts if n in by_name]
    basis = "parts in the automatable steps"
    if not handled:
        handled, basis = parts, "all parts seen"
    heaviest = max(handled, key=lambda p: p.mass_g, default=None)
    part_kg = max(0.0, heaviest.mass_g / 1000.0) if heaviest else 0.0
    text = (f"the heaviest of the {basis} is {heaviest.name} at about {heaviest.mass_g:.0f} g"
            if heaviest else "Argon listed no parts, so the part mass is taken as 0")
    text += (f" plus a {_kg(GRIPPER_ALLOWANCE_KG)} allowance for the gripper and fingers, "
             "assuming the rated payload is at the tool flange and excludes the gripper")
    return part_kg + GRIPPER_ALLOWANCE_KG, text


def choose_robot(parts: Iterable[Part], steps: Iterable[Step], catalogue: list[dict] | None = None,
                 needed_reach_m: Optional[float] = None) -> dict:
    """The smallest arm (catalogue order, ascending reach) that carries the payload and, when a layout
    gave a needed reach, reaches it. With no layout the reach is not checked and arms under
    REACH_FLOOR_MM are not proposed. Every arm is judged and returned under `alternatives`.
    """
    catalogue = catalogue or CATALOGUE
    steps = list(steps)
    need, part_text = needed_payload(parts, steps)
    reach_mm = round(needed_reach_m * 1000) if needed_reach_m and needed_reach_m > 0 else None

    options = []
    for arm in catalogue:
        payload_fits = arm["payload_kg"] >= need
        reach_fits = None if reach_mm is None else arm["reach_mm"] >= reach_mm
        fits = payload_fits and reach_fits is not False
        candidate = fits and (reach_mm is not None or arm["reach_mm"] >= REACH_FLOOR_MM)
        notes = []
        if not payload_fits:
            notes.append(f"payload short by {_kg(need - arm['payload_kg'])}")
        if reach_fits is False:
            notes.append(f"reach short by {reach_mm - arm['reach_mm']:.0f} mm")
        if fits and not candidate:
            notes.append(f"payload ok; reach not checked (no layout) and under the {REACH_FLOOR_MM} mm assumed for a bench")
        options.append({**{k: arm[k] for k in ("model", "vendor", "payload_kg", "reach_mm", "source")},
                        "payload_fits": payload_fits, "reach_fits": reach_fits, "fits": fits, "chosen": False,
                        "price_band": _band(arm), "note": "; ".join(notes), "_candidate": candidate})

    candidates = [o for o in options if o["_candidate"]]
    if candidates:
        chosen = candidates[0]
        chosen["note"] = "chosen: the smallest arm in the catalogue that passes every check"
        for o in candidates[1:]:
            o["note"] = "also adequate; a larger arm than this job needs"
    else:
        # Nothing passes: the arm that comes closest (payload first, then reach, then payload margin).
        chosen = max(options, key=lambda o: (o["payload_fits"], o["reach_fits"] is not False,
                                             o["payload_kg"] - need, o["reach_mm"]))
        chosen["note"] = (chosen["note"] + "; " if chosen["note"] else "") + "closest in the catalogue, still short"
    chosen["chosen"] = True
    fits = chosen["fits"]

    why = f"Needed payload {_kg(need)}: {part_text}. "
    if reach_mm is not None:
        why += (f"Needed reach {reach_mm} mm: the farthest pick or place of the automatable steps from the "
                f"proposed base, kept within {REACH_USE:.0%} of the rated reach; the layout is estimated from "
                "video, so treat it as approximate. ")
        reach_basis = "layout estimated from video (Argon call C)"
    else:
        why += (f"Reach not checked: no layout estimate; arms under {REACH_FLOOR_MM} mm are not proposed "
                "for a bench job. ")
        reach_basis = f"not checked; {REACH_FLOOR_MM} mm floor assumed"
    if candidates:
        others = [o["model"] for o in candidates[1:]]
        short = [f"{o['model']} ({o['note']})" for o in options if not o["_candidate"]]
        why += (f"{chosen['model']} ({_kg(chosen['payload_kg'])}, {chosen['reach_mm']:.0f} mm) is the smallest "
                f"arm in the catalogue that passes every check, with {_kg(chosen['payload_kg'] - need)} of "
                "payload margin")
        why += f"; also adequate: {', '.join(others)}" if others else ""
        why += f". Not proposed: {'; '.join(short)}." if short else "."
    else:
        why += (f"No arm in the catalogue passes every check; {chosen['model']} comes closest "
                f"({_kg(chosen['payload_kg'])}, {chosen['reach_mm']:.0f} mm): {chosen['note']}.")

    for o in options:
        o.pop("_candidate")
    return {"model": chosen["model"], "vendor": chosen["vendor"], "gripper": GRIPPER_TEXT, "source": chosen["source"],
            "payload_kg": chosen["payload_kg"], "reach_mm": chosen["reach_mm"],
            "needed_payload_kg": round(need, 3), "needed_reach_mm": reach_mm,
            "payload_fits": chosen["payload_fits"], "reach_fits": chosen["reach_fits"], "fits": fits,
            "reach_basis": reach_basis, "price_band": chosen["price_band"], "why": why, "alternatives": options}


CYCLE_METHOD = (f"Robot time = the operator's time on the automate steps x {ROBOT_SPEED_FACTOR}, a conservative "
                "factor for a cobot moving at collaborative speed; no robot motion was planned or simulated. "
                "Side by side = the longer of the robot's time and the operator's remaining time, the operator "
                "keeping the seconds no step covers.")


def cycle_estimate(steps: Iterable[Step], cycle_time_s: float, factor: float = ROBOT_SPEED_FACTOR) -> Optional[dict]:
    """Where the cycle would go with the automate steps on a cobot; None without a measured cycle."""
    if not cycle_time_s or cycle_time_s <= 0:
        return None
    steps = list(steps)
    auto = sum(max(0.0, s.mean_duration_s) for s in steps if s.verdict == "automate")
    other = sum(max(0.0, s.mean_duration_s) for s in steps if s.verdict != "automate")
    unassigned = max(0.0, cycle_time_s - auto - other)
    robot = auto * factor
    operator = other
    return {"human_cycle_s": round(cycle_time_s, 2), "automated_steps_s": round(auto, 2),
            "robot_cycle_s": round(robot, 2), "operator_cycle_s": round(operator, 2),
            "unassigned_s": round(unassigned, 2), "parallel_cycle_s": round(max(robot, operator + unassigned), 2),
            "speed_factor": factor, "method": CYCLE_METHOD}
