"""The top-view layout: Argon's bench-frame estimate (call C) cleaned up, plus the robot base
and the needed reach, which are our code.

Bench frame: origin at the bench's front-left corner as the operator sees it; x to the
operator's right along the front edge; y away from the operator; metres. The operator
stands at y < 0. Everything here is an estimate from video and is labelled so.
"""
from __future__ import annotations

import math
from typing import Optional

from .catalogue import REACH_USE
from .schemas import ArgonLayout, Layout, LayoutAnchor, LayoutObjectOut, RobotBase, Step

BENCH_MIN_M, BENCH_MAX_M = 0.3, 4.0
MARGIN_M = 0.5            # how far outside the bench an object or anchor may sit
OPERATOR_Y_M = -0.35      # where the operator is drawn, in front of the bench
BASE_STEP_M = 0.05        # candidate base positions along the back edge, every 5 cm
ROBOT_VERDICTS = ("automate", "needs_fixture")  # steps a robot would do, with or without a fixture

SOURCE = "estimated from video by Argon (call C): positions and sizes are approximate, not measured"
FRAME = ("bench frame: origin at the bench's front-left corner as the operator sees it; "
         "x to the operator's right along the front edge; y away from the operator; metres")
BASE_RULE = ("on the bench's back edge (across from the operator) or at either end, at the position that "
             "minimises the farthest pick or place of the automate and needs_fixture steps")
REACH_RULE = (f"farthest pick or place of the automate and needs_fixture steps from the proposed base, "
              f"divided by {REACH_USE} so it sits within {REACH_USE:.0%} of the rated reach")


def _num(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def _xy(pair, length: float, width: float) -> Optional[tuple[float, float]]:
    """A finite [x, y] clamped to the bench plus a margin; None for anything else."""
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    x, y = _num(pair[0], math.nan), _num(pair[1], math.nan)
    if math.isnan(x) or math.isnan(y):
        return None
    return (round(_clamp(x, -MARGIN_M, length + MARGIN_M), 3), round(_clamp(y, -MARGIN_M, width + MARGIN_M), 3))


def place_base(points: list[tuple[float, float]], length: float, width: float) -> tuple[RobotBase, Optional[float]]:
    """The base position that minimises the farthest robot point, and the reach that needs.

    Candidates: along the back edge (y = width) every 5 cm, and the middle of each end.
    With no points the base goes to the middle of the back edge and the needed reach is None.
    """
    candidates = [(round(i * BASE_STEP_M, 3), width) for i in range(int(length / BASE_STEP_M) + 1)]
    candidates += [(0.0, round(width / 2, 3)), (length, round(width / 2, 3))]
    if not points:
        return RobotBase(x_m=round(length / 2, 3), y_m=width, rule=BASE_RULE), None
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)

    def score(c):
        farthest = max(math.hypot(p[0] - c[0], p[1] - c[1]) for p in points)
        return (round(farthest, 6), math.hypot(cx - c[0], cy - c[1]))

    best = min(candidates, key=score)
    farthest = score(best)[0]
    return RobotBase(x_m=best[0], y_m=best[1], rule=BASE_RULE), round(farthest / REACH_USE, 3)


def build_layout(estimate: ArgonLayout, steps: list[Step]) -> Layout:
    """Clean Argon's estimate, keep only anchors for known steps, place the base, compute the needed reach."""
    length = round(_clamp(_num(estimate.bench_length_m, 1.2), BENCH_MIN_M, BENCH_MAX_M), 3)
    width = round(_clamp(_num(estimate.bench_width_m, 0.6), BENCH_MIN_M, BENCH_MAX_M), 3)
    operator_x = round(_clamp(_num(estimate.operator_x_m, length / 2), 0.0, length), 3)

    objects = []
    for o in estimate.objects:
        centre = _xy([o.x_m, o.y_m], length, width)
        if centre is None or not o.name.strip():
            continue
        objects.append(LayoutObjectOut(
            name=o.name.strip()[:60], kind=o.kind, x_m=centre[0], y_m=centre[1],
            length_m=round(_clamp(_num(o.length_m, 0.1), 0.02, BENCH_MAX_M), 3),
            width_m=round(_clamp(_num(o.width_m, 0.1), 0.02, BENCH_MAX_M), 3),
            step_ids=[s.strip() for s in o.step_ids if s.strip()], confidence=round(_clamp(_num(o.confidence), 0, 1), 2)))

    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    anchors: list[LayoutAnchor] = []
    for ls in estimate.steps:
        step = by_id.get(ls.id)
        if step is None or ls.id in seen:
            continue  # never add steps; take the first entry per id
        seen.add(ls.id)
        for kind, pair in (("pick", ls.pick_xy_m), ("place", ls.place_xy_m)):
            xy = _xy(pair, length, width)
            if xy is not None:
                anchors.append(LayoutAnchor(step_id=step.id, name=step.name, verdict=step.verdict, kind=kind,
                                            x_m=xy[0], y_m=xy[1]))

    robot_points = [(a.x_m, a.y_m) for a in anchors if a.verdict in ROBOT_VERDICTS]
    base, needed = place_base(robot_points, length, width)
    return Layout(source=SOURCE, frame=FRAME, bench={"length_m": length, "width_m": width},
                  operator={"x_m": operator_x, "y_m": OPERATOR_Y_M}, objects=objects, anchors=anchors,
                  robot_base=base, needed_reach_m=needed, reach_rule=REACH_RULE if needed is not None else "",
                  confidence=round(_clamp(_num(estimate.confidence), 0, 1), 2), notes=estimate.notes.strip()[:600])
