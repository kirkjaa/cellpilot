"""The cobot twin: a kinematic simulation of the automatable steps on the chosen arm.

From the layout (bench, objects, pick and place points) and the robot choice it builds a MuJoCo scene with
the arm from MuJoCo Menagerie, places the base where every automatable point is reachable with the most
margin, moves the tool through pick, lift and place with inverse kinematics (mink), checks for collisions
with the bench, the objects and the fixtures, and times the motion at collaborative speeds. The part is
welded to the gripper: there is no physics grasp, and the page says so. A rendered clip is optional (it
needs OpenGL, so it is made on a PC, not on Cloud Run).

Models: Menagerie's UR5e and UR10e (declared dependency, fetched at build time; see Dockerfile). An arm
without a Menagerie model is simulated with the nearest one and the result says which.

    python -m app.twin <sid> [--analysis <aid>] [--render]   # updates the stored sample analysis
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("cellpilot.twin")

ROOT = Path(__file__).resolve().parent.parent
MENAGERIE_MODELS = {"UR5e": "universal_robots_ur5e/ur5e.xml", "UR10e": "universal_robots_ur10e/ur10e.xml"}
NEAREST_MODEL = {"UR3e": "UR5e", "xArm 6": "UR5e", "Franka FR3": "UR5e", "Doosan M0609": "UR10e"}
HOME_Q = [-1.5708, -1.5708, 1.5708, -1.5708, -1.5708, 0.0]   # Menagerie's "home" keyframe, elbow up

BENCH_H = 0.75        # m, bench top above the floor; the scene's z = 0 is the bench top
OBJECT_H = 0.05       # m, objects on the bench are drawn this tall
PART_H = 0.02         # m, the part's thickness at a pick or place point
FIXTURE_H = 0.03      # m, a locating nest raises the part by this much
FIXTURE_SIZE = 0.16   # m, square
TOOL_LENGTH = 0.16    # m, a two-finger gripper (2F-85 class) from the flange to the fingertips
RISER_H = 0.10        # m, the arm stands on a riser on the bench, as a bench-mounted cobot does
MIN_BASE_DIST = 0.30  # m, a point closer than this to the base cannot be reached with the tool vertical
SNAP_TO_NEST = 0.20   # m, an automatable pick this close to a nest is taken from the nest
LIFT = 0.15           # m, clearance above a point before moving sideways
PATH_STEP = 0.05      # m, spacing of the IK samples along a straight tool path
TCP_SPEED = 0.25      # m/s, a collaborative speed (ISO/TS 15066 guidance)
JOINT_SPEED = math.radians(60)   # rad/s, conservative joint speed cap
GRIPPER_S = 0.5       # s, close or open
REACH_MARGIN = 0.85   # points must lie within this share of the rated reach
PENETRATION_M = 0.002 # contacts deeper than this count as collisions

METHOD = ("Kinematic twin in MuJoCo with the Menagerie arm model: the base is placed where every automatable point "
          "is within 85 % of the rated reach with the most margin; the tool goes pick, lift, place with inverse "
          "kinematics (mink); each configuration and the path between them are checked for contact with the bench, "
          "the objects and the fixtures; time = the longer of joint travel at 60°/s and tool travel at 0.25 m/s, "
          "plus 0.5 s to close or open the gripper. The part is welded to the gripper, not grasped; the gripper is "
          "a 160 mm tool; nothing here is a physics or safety validation.")


class TwinError(RuntimeError):
    pass


def menagerie_dir() -> Path:
    for cand in (os.environ.get("CELLPILOT_MENAGERIE"), "/opt/menagerie", ROOT / "data" / "menagerie"):
        if cand and Path(cand).is_dir():
            return Path(cand)
    raise TwinError("MuJoCo Menagerie not found: set CELLPILOT_MENAGERIE to a checkout with universal_robots_ur5e")


def pick_model(model: str) -> tuple[str, str]:
    """(Menagerie model used, note) for the catalogue arm."""
    if model in MENAGERIE_MODELS:
        return model, ""
    used = NEAREST_MODEL.get(model, "UR5e")
    return used, (f"{model} has no MuJoCo Menagerie model; simulated with the {used} model, the nearest in the catalogue. "
                  f"Reach and timing are the {used}'s.")


# ------------------------------------------------------------------ geometry from the result

def _points(result) -> list[dict]:
    """Pick and place points of the automate steps, in bench metres, with the part height at each."""
    lay = result.layout
    verdict = {s.id: s.verdict for s in result.steps}
    out = []
    for a in lay.anchors:
        if verdict.get(a.step_id) != "automate":
            continue
        out.append({"step_id": a.step_id, "name": a.name, "kind": a.kind, "x_m": float(a.x_m), "y_m": float(a.y_m)})
    return out


def _fixtures(result) -> list[dict]:
    """A locating nest at the pick point of every needs_fixture step (its place point if it has no pick)."""
    out = []
    by_step: dict[str, list] = {}
    for a in result.layout.anchors:
        by_step.setdefault(a.step_id, []).append(a)
    for s in result.steps:
        if s.verdict != "needs_fixture":
            continue
        anchors = by_step.get(s.id) or []
        anchor = next((a for a in anchors if a.kind == "pick"), None) or (anchors[0] if anchors else None)
        if anchor is None:
            continue
        part = (s.parts[0] if getattr(s, "parts", None) else "the part")
        out.append({"step_id": s.id, "name": f"locating nest for {part}", "x_m": float(anchor.x_m), "y_m": float(anchor.y_m),
                    "length_m": FIXTURE_SIZE, "width_m": FIXTURE_SIZE, "height_m": FIXTURE_H})
    return out


def base_candidates(layout, points: list[dict], reach_mm: float) -> list[dict]:
    """Bases on the bench's back edge (and the layout's own), best first: every point within 85 % of reach,
    the most clearance from the objects, then the shortest farthest reach."""
    L, W = float(layout.bench["length_m"]), float(layout.bench["width_m"])
    reach = reach_mm / 1000.0 * REACH_MARGIN
    cands = []
    if layout.robot_base:
        cands.append((float(layout.robot_base.x_m), float(layout.robot_base.y_m), "the layout's base"))
    for x in np.linspace(0.12, max(0.12, L - 0.12), 25):
        cands.append((float(x), W - 0.08, "back edge of the bench"))
    out = []
    for x, y, how in cands:
        far = max((math.hypot(p["x_m"] - x, p["y_m"] - y) for p in points), default=0.0)
        far = math.hypot(far, LIFT + PART_H)  # the lift point is the farthest
        near = min((math.hypot(p["x_m"] - x, p["y_m"] - y) for p in points), default=1.0)
        clear = min((max(abs(o.x_m - x) - o.length_m / 2, abs(o.y_m - y) - o.width_m / 2, 0.0) for o in layout.objects), default=1.0)
        fits = far <= reach and near >= MIN_BASE_DIST
        out.append({"x_m": round(x, 3), "y_m": round(y, 3), "how": how, "farthest_point_mm": round(far * 1000), "fits": fits,
                    "clearance_m": round(clear, 3), "_key": (not fits, -min(clear, 0.25), far)})
    out.sort(key=lambda c: c["_key"])
    return out


# ------------------------------------------------------------------ the scene

def build_scene(layout, model_used: str, base: dict, fixtures: list[dict], points: list[dict]):
    import mujoco

    L, W = float(layout.bench["length_m"]), float(layout.bench["width_m"])
    spec = mujoco.MjSpec()
    spec.modelname = "cellpilot twin"
    spec.visual.global_.offwidth, spec.visual.global_.offheight = 1920, 1080  # room for the rendered clip
    world = spec.worldbody
    world.add_light(pos=[L / 2, W / 2, 2.2], dir=[0, 0, -1], type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL, diffuse=[0.7, 0.7, 0.7])
    world.add_light(pos=[L / 2, -1.5, 1.5], dir=[0, 0.6, -0.8], type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL, diffuse=[0.4, 0.4, 0.4])
    world.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[3, 3, 0.1], pos=[L / 2, W / 2, -BENCH_H],
                   rgba=[0.82, 0.82, 0.8, 1])
    world.add_geom(name="bench", type=mujoco.mjtGeom.mjGEOM_BOX, size=[L / 2, W / 2, BENCH_H / 2],
                   pos=[L / 2, W / 2, -BENCH_H / 2], rgba=[0.45, 0.30, 0.18, 1])
    colours = {"bin": [0.35, 0.55, 0.85, 1], "fixture": [0.85, 0.55, 0.2, 1], "tool": [0.5, 0.5, 0.5, 1],
               "part_source": [0.4, 0.7, 0.4, 1], "part_destination": [0.3, 0.6, 0.6, 1], "other": [0.6, 0.6, 0.6, 1]}
    for i, o in enumerate(layout.objects):
        if any(abs(f["x_m"] - o.x_m) < f["length_m"] / 2 and abs(f["y_m"] - o.y_m) < f["width_m"] / 2 for f in fixtures):
            continue  # the nest stands where this zone was
        world.add_geom(name=f"obj{i}", type=mujoco.mjtGeom.mjGEOM_BOX,
                       size=[max(0.01, o.length_m / 2), max(0.01, o.width_m / 2), OBJECT_H / 2],
                       pos=[o.x_m, o.y_m, OBJECT_H / 2], rgba=colours.get(o.kind, colours["other"]))
    for i, f in enumerate(fixtures):
        world.add_geom(name=f"fixture{i}", type=mujoco.mjtGeom.mjGEOM_BOX,
                       size=[f["length_m"] / 2, f["width_m"] / 2, f["height_m"] / 2],
                       pos=[f["x_m"], f["y_m"], f["height_m"] / 2], rgba=[0.9, 0.6, 0.2, 1])
    # The parts: one small box per pick point, shown on the bench until picked, then carried on the tool.
    for i, p in enumerate(points):
        if p["kind"] == "pick":
            world.add_geom(name=f"part{i}", type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.05, 0.03, PART_H / 2],
                           pos=[p["x_m"], p["y_m"], p.get("z_base", 0.0) + PART_H / 2], rgba=[0.2, 0.2, 0.25, 1],
                           contype=0, conaffinity=0)
    # A marker at each point, for the picture only.
    for i, p in enumerate(points):
        world.add_site(name=f"pt{i}", pos=[p["x_m"], p["y_m"], p.get("z_base", 0.0) + 0.002], size=[0.02, 0.02, 0.001],
                       type=mujoco.mjtGeom.mjGEOM_CYLINDER,
                       rgba=[0.2, 0.75, 0.4, 0.9] if p["kind"] == "pick" else [0.2, 0.45, 0.9, 0.9])
    # The arm, from Menagerie, on the bench top at the base position.
    arm = mujoco.MjSpec.from_file(str(menagerie_dir() / MENAGERIE_MODELS[model_used]))
    wrist = arm.body("wrist_3_link")
    # A 160 mm tool on the flange (a 2F-85 class gripper), and the TCP at its tip; both along the attachment site's axis.
    wrist.add_geom(name="tool", type=mujoco.mjtGeom.mjGEOM_CYLINDER, size=[0.03, TOOL_LENGTH / 2, 0],
                   pos=[0, 0.1 + TOOL_LENGTH / 2, 0], quat=[-1, 1, 0, 0], rgba=[0.15, 0.15, 0.15, 1])
    wrist.add_geom(name="carried", type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.05, 0.03, PART_H / 2],
                   pos=[0, 0.1 + TOOL_LENGTH + PART_H / 2, 0], quat=[-1, 1, 0, 0], rgba=[0.2, 0.2, 0.25, 0],
                   contype=0, conaffinity=0)
    wrist.add_site(name="tcp", pos=[0, 0.1 + TOOL_LENGTH, 0], quat=[-1, 1, 0, 0], size=[0.005, 0.005, 0.005])
    world.add_geom(name="riser", type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.09, 0.09, RISER_H / 2],
                   pos=[base["x_m"], base["y_m"], RISER_H / 2], rgba=[0.25, 0.25, 0.27, 1])
    frame = world.add_frame(pos=[base["x_m"], base["y_m"], RISER_H])
    spec.attach(arm, prefix="arm/", frame=frame)
    model = spec.compile()
    return model


# ------------------------------------------------------------------ the motion

def _tool_down():
    import mink
    return mink.SO3.from_rpy_radians(math.pi, 0.0, 0.0)  # tool z pointing down


class Solver:
    def __init__(self, model):
        import mink
        import mujoco
        self.model, self.mink, self.mujoco = model, mink, mujoco
        self.data = mujoco.MjData(model)
        self.cfg = mink.Configuration(model)
        self.cfg.update(self._home())
        self.task = mink.FrameTask(frame_name="arm/tcp", frame_type="site", position_cost=1.0, orientation_cost=0.3, lm_damping=1.0)
        self.posture = mink.PostureTask(model, cost=1e-3)
        self.posture.set_target(self._home())
        self.limits = [mink.ConfigurationLimit(model)]
        self.tcp_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "arm/tcp")
        self.robot_geoms = {g for g in range(model.ngeom)
                            if mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) and
                            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g).startswith("arm/")
                            or model.geom_bodyid[g] in self._arm_bodies()}
        self.base_body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "arm/base")

    def _arm_bodies(self) -> set[int]:
        m, mujoco = self.model, self.mujoco
        return {b for b in range(m.nbody) if (mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, b) or "").startswith("arm/")}

    def _home(self) -> np.ndarray:
        q = np.zeros(self.model.nq)
        q[: len(HOME_Q)] = HOME_Q
        return q

    def tcp(self, q: np.ndarray) -> np.ndarray:
        self.data.qpos[:] = q
        self.mujoco.mj_forward(self.model, self.data)
        return self.data.site_xpos[self.tcp_id].copy()

    def seeds(self, target: np.ndarray) -> list[np.ndarray]:
        """Elbow-up home poses with the shoulder turned in four directions around the target's bearing."""
        self.data.qpos[:] = self._home()
        self.mujoco.mj_forward(self.model, self.data)
        base = self.data.xpos[self.base_body] if self.base_body >= 0 else np.zeros(3)
        bearing = math.atan2(target[1] - base[1], target[0] - base[0])
        out = []
        for k in range(4):
            q = self._home()
            q[0] = bearing + k * math.pi / 2
            out.append(q)
        return out

    def _iterate(self, target: np.ndarray, n: int) -> tuple[np.ndarray, float, float]:
        mink = self.mink
        self.task.set_target(mink.SE3.from_rotation_and_translation(_tool_down(), np.asarray(target, dtype=float)))
        dt = 0.02
        pos_err = ori_err = float("inf")
        for _ in range(n):
            v = mink.solve_ik(self.cfg, [self.task, self.posture], dt, "daqp", damping=1e-3, limits=self.limits)
            self.cfg.integrate_inplace(v, dt)
            err = self.task.compute_error(self.cfg)
            pos_err, ori_err = float(np.linalg.norm(err[:3])), float(np.linalg.norm(err[3:]))
            if pos_err < 1e-3 and ori_err < 2e-2:
                break
        return self.cfg.q.copy(), pos_err, ori_err

    def solve(self, target: np.ndarray, start: Optional[np.ndarray] = None) -> tuple[np.ndarray, float, float]:
        """IK to the target with the tool pointing down; returns (q, position error m, orientation error rad).
        Warm-started from `start` when given (path samples); otherwise four shoulder directions are tried
        briefly and the best collision-free one is refined."""
        if start is not None:
            self.cfg.update(start)
            self.posture.set_target(start)
            return self._iterate(target, 150)
        tries = []
        for seed in self.seeds(target):
            self.cfg.update(seed)
            self.posture.set_target(seed)
            q, perr, oerr = self._iterate(target, 60)
            tries.append((perr > 5e-3 or oerr > 5e-2, len(self.collisions(q)), perr, q, seed))
        tries.sort(key=lambda t: t[:3])
        _, _, _, q, seed = tries[0]
        self.cfg.update(q)
        self.posture.set_target(seed)
        return self._iterate(target, 150)

    def collisions(self, q: np.ndarray) -> list[str]:
        """Names of bench/object/fixture geoms the arm penetrates in this configuration (the base on the bench excepted)."""
        m, mujoco = self.model, self.mujoco
        self.data.qpos[:] = q
        mujoco.mj_forward(m, self.data)
        hits = []
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            if c.dist > -PENETRATION_M:
                continue
            b1, b2 = m.geom_bodyid[c.geom1], m.geom_bodyid[c.geom2]
            n1 = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, c.geom1) or f"geom{c.geom1}"
            n2 = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, c.geom2) or f"geom{c.geom2}"
            arm1, arm2 = (n1.startswith("arm/") or b1 in self._arm_bodies()), (n2.startswith("arm/") or b2 in self._arm_bodies())
            if arm1 == arm2:
                continue  # arm with itself, or scenery with scenery
            other = n2 if arm1 else n1
            if other in ("bench", "riser") and self.base_body in (b1, b2):
                continue  # the base stands on the riser, on the bench
            if other not in hits:
                hits.append(other)
        return hits


def _cycle_points(result, points: list[dict]) -> tuple[list[str], dict[str, dict], list[str]]:
    """Per automate step: its pick and place. A step with a place but no pick takes the part from the nearest
    pick point of another step (the nest of a needs_fixture step), which is the cell the verdicts describe."""
    by_step: dict[str, dict] = {}
    for p in points:
        by_step.setdefault(p["step_id"], {})[p["kind"]] = p
    notes = []
    picks = [{"step_id": a.step_id, "name": a.name, "kind": "pick", "x_m": float(a.x_m), "y_m": float(a.y_m)}
             for a in result.layout.anchors if a.kind == "pick"]
    order = sorted(by_step)
    for sid in order:
        pts = by_step[sid]
        if "place" in pts and "pick" not in pts and picks:
            pl = pts["place"]
            src = min(picks, key=lambda a: math.hypot(a["x_m"] - pl["x_m"], a["y_m"] - pl["y_m"]))
            pts["pick"] = {**src, "step_id": sid, "borrowed_from": src["step_id"]}
            notes.append(f"{pl['name']}: no pick point of its own; the part is taken from the point of step "
                         f"{src['step_id']} ({src['name']})")
    return order, by_step, notes


def _path(sol: Solver, q_from: np.ndarray, tcp_from: np.ndarray, tcp_to: np.ndarray) -> list[np.ndarray]:
    """Configurations along a straight tool line from tcp_from to tcp_to, each solved from the one before."""
    n = max(1, int(math.ceil(float(np.linalg.norm(tcp_to - tcp_from)) / PATH_STEP)))
    qs, q = [], q_from
    for i in range(1, n + 1):
        target = tcp_from + (tcp_to - tcp_from) * (i / n)
        q, _, _ = sol.solve(target, start=q)
        qs.append(q)
    return qs


def _run_cycle(sol: Solver, result, order: list[str], by_step: dict[str, dict], fixtures: list[dict]) -> dict:
    """IK through every step's pick, lift and place; returns the waypoints of one repeating cycle and the checks."""
    def z_base(p):
        return FIXTURE_H if any(abs(f["x_m"] - p["x_m"]) < 0.01 and abs(f["y_m"] - p["y_m"]) < 0.01 for f in fixtures) else 0.0

    waypoints: list[dict] = []
    point_rows: list[dict] = []
    q_prev = sol._home()
    for sid in order:
        step_name = next((s.name for s in result.steps if s.id == sid), sid)
        pts = by_step[sid]
        seq = []
        for kind, hold in (("pick", "grip"), ("place", "release")):
            if kind in pts:
                p = pts[kind]
                z = z_base(p) + PART_H
                seq += [(f"above {kind}", p, z + LIFT, 0.0, None), (kind, p, z, GRIPPER_S, hold), (f"above {kind}", p, z + LIFT, 0.0, None)]
        for label, p, z, dwell, hold in seq:
            q, perr, oerr = sol.solve(np.array([p["x_m"], p["y_m"], z]))
            hits = sol.collisions(q)
            reachable = perr < 0.005 and oerr < 0.05
            if label in ("pick", "place"):
                note = "; ".join(x for x in [f"part taken from step {p['borrowed_from']}'s point" if p.get("borrowed_from") else "",
                                             p.get("note", "")] if x)
                point_rows.append({"step_id": sid, "name": step_name, "kind": label, "x_m": p["x_m"], "y_m": p["y_m"], "z_m": round(z, 3),
                                   "reachable": bool(reachable), "position_error_mm": round(perr * 1000, 1), "collisions": hits, "note": note})
            waypoints.append({"q": q, "tcp": sol.tcp(q), "label": f"{step_name}: {label}", "dwell": dwell, "hold": hold})
            q_prev = q
    # The repeating cycle closes from the last waypoint back to the first. Between waypoints the tool moves in a
    # straight line, solved sample by sample; those configurations are what gets checked and what the clip shows.
    segments, path_hits, total = [], [], 0.0
    loop = waypoints + [dict(waypoints[0], label="back to the first point", dwell=0.0, hold=None)] if waypoints else []
    for a, b in zip(loop, loop[1:]):
        qs = _path(sol, a["q"], a["tcp"], b["tcp"])
        b["path"] = qs
        t, prev_q, prev_tcp = 0.0, a["q"], a["tcp"]
        for q in qs:
            tcp = sol.tcp(q)
            t += max(float(np.max(np.abs(q - prev_q))) / JOINT_SPEED, float(np.linalg.norm(tcp - prev_tcp)) / TCP_SPEED)
            for hit in sol.collisions(q):
                if hit not in path_hits:
                    path_hits.append(hit)
            prev_q, prev_tcp = q, tcp
        dq = float(np.max(np.abs(b["q"] - a["q"])))
        dx = float(np.linalg.norm(b["tcp"] - a["tcp"]))
        segments.append({"to": b["label"], "joint_rad": round(dq, 3), "tool_m": round(dx, 3), "seconds": round(t, 2)})
        total += t + b["dwell"]
    return {"waypoints": waypoints, "points": point_rows, "segments": segments, "path_collisions": path_hits,
            "robot_cycle_s": round(total, 2),
            "all_reachable": all(r["reachable"] for r in point_rows),
            "collision_free": not path_hits and all(not r["collisions"] for r in point_rows)}


def simulate(result, *, render_to: Optional[Path] = None) -> dict:
    """The twin of this result; raises TwinError when there is nothing to simulate."""
    lay = result.layout
    if lay is None or not lay.anchors:
        raise TwinError("no layout with pick and place points to simulate")
    points = _points(result)
    if not points:
        raise TwinError("no automatable step has a pick or place point in the layout")
    fixtures = _fixtures(result)
    order, by_step, point_notes = _cycle_points(result, points)
    for pts in by_step.values():
        pk = pts.get("pick")
        if pk:
            near = min(fixtures, key=lambda f: math.hypot(f["x_m"] - pk["x_m"], f["y_m"] - pk["y_m"]), default=None)
            if near and math.hypot(near["x_m"] - pk["x_m"], near["y_m"] - pk["y_m"]) <= SNAP_TO_NEST:
                pk["x_m"], pk["y_m"] = near["x_m"], near["y_m"]
                pk["note"] = f"picked from the {near['name']}"
    all_pts = [p for pts in by_step.values() for p in pts.values()]
    arm_requested = result.robot.model
    model_used, note = pick_model(arm_requested)
    reach_mm = next((a["reach_mm"] for a in CATALOGUE_REACH() if a["model"] == model_used), float(result.robot.reach_mm or 850))

    # Try the best few bases in full; the first collision-free one wins, else the one with the fewest collisions.
    tried = []
    for cand in base_candidates(lay, all_pts, reach_mm)[:6]:
        model = build_scene(lay, model_used, cand, fixtures, all_pts)
        sol = Solver(model)
        run = _run_cycle(sol, result, order, by_step, fixtures)
        tried.append((cand, model, sol, run))
        if run["all_reachable"] and run["collision_free"]:
            break
    cand, model, sol, run = min(tried, key=lambda t: (not t[3]["all_reachable"], not t[3]["collision_free"],
                                                       len(t[3]["path_collisions"]), t[0]["_key"]))
    base = {"x_m": cand["x_m"], "y_m": cand["y_m"], "farthest_point_mm": cand["farthest_point_mm"], "fits": cand["fits"],
            "clearance_m": cand["clearance_m"],
            "rule": (f"placed at the {cand['how']}: the farthest automatable point is {cand['farthest_point_mm']} mm away, "
                     f"{'within' if cand['fits'] else 'beyond'} {int(REACH_MARGIN * 100)} % of the {reach_mm:.0f} mm reach; "
                     f"{len(tried)} base position{'s' if len(tried) != 1 else ''} tried for a collision-free cycle")}

    ce = result.cycle_estimate
    operator = float(ce.operator_cycle_s) if ce else 0.0
    unassigned = float(ce.unassigned_s) if ce else 0.0
    parallel = round(max(run["robot_cycle_s"], operator + unassigned), 2)
    twin = {
        "simulator": "MuJoCo 3.16 + mink (kinematic)", "arm_requested": arm_requested, "arm_model": model_used,
        "model_note": " ".join(x.rstrip(".") + "." for x in [note, *point_notes] if x),
        "base": base, "points": run["points"], "steps_simulated": order, "all_reachable": run["all_reachable"],
        "collision_free": run["collision_free"], "path_collisions": run["path_collisions"],
        "robot_cycle_s": run["robot_cycle_s"], "operator_cycle_s": round(operator, 2), "unassigned_s": round(unassigned, 2),
        "parallel_cycle_s": parallel, "estimate_parallel_cycle_s": float(ce.parallel_cycle_s) if ce else None,
        "segments": run["segments"],
        "assumptions": {"tcp_speed_mps": TCP_SPEED, "joint_speed_dps": 60, "gripper_s": GRIPPER_S, "lift_m": LIFT,
                        "tool_length_m": TOOL_LENGTH, "reach_margin": REACH_MARGIN},
        "fixtures": fixtures, "parts": parts_list(result.robot, fixtures), "clip": None, "method": METHOD,
    }
    twin["parts_total"] = parts_total(twin["parts"])
    if render_to is not None:
        home = {"q": sol._home(), "tcp": sol.tcp(sol._home()), "label": "home", "dwell": 0.0, "hold": None}
        twin["clip"] = render(model, sol, [home, *run["waypoints"], home], render_to,
                              title=f"{model_used} · {len(order)} step{'s' if len(order) != 1 else ''} · simulated cycle {run['robot_cycle_s']} s")
    return twin


def CATALOGUE_REACH():
    from .catalogue import CATALOGUE
    return CATALOGUE


# ------------------------------------------------------------------ fixtures and parts

def parts_list(robot, fixtures: list[dict]) -> list[dict]:
    """An indicative parts list for the proposed cell; prices are bands to be quoted, not offers."""
    from .catalogue import CATALOGUE
    arm = next((a for a in CATALOGUE if a["model"] == robot.model), None)
    low, high = (arm["price_band_usd"] if arm else (None, None))
    n = len(fixtures)
    items = [
        {"item": f"{robot.model} cobot arm ({robot.vendor})", "qty": 1, "low": low, "high": high, "currency": "USD",
         "basis": "catalogue price band, indicative"},
        {"item": "Two-finger parallel gripper (Robotiq 2F-85 class) with fingers", "qty": 1, "low": 4000, "high": 6500,
         "currency": "USD", "basis": "typical list price, indicative"},
    ]
    if n:
        items.append({"item": "Locating nest or presenter, custom (one per needs-fixture step)", "qty": n, "low": 800 * n,
                      "high": 2500 * n, "currency": "USD", "basis": "machined or 3D-printed, indicative"})
    items.append({"item": "Pedestal or bench mount, cabling, control box placement", "qty": 1, "low": 800, "high": 2000,
                  "currency": "USD", "basis": "indicative"})
    items.append({"item": "Risk assessment, programming, commissioning", "qty": 1, "low": None, "high": None, "currency": "USD",
                  "basis": "the system multiple in the payback covers this (a cell costs 3 to 5 × the robot)"})
    return items


def parts_total(items: list[dict]) -> dict:
    priced = [i for i in items if i["low"] is not None and i["high"] is not None]
    return {"low": sum(i["low"] for i in priced), "high": sum(i["high"] for i in priced), "currency": "USD",
            "note": "hardware only, indicative; integration is in the payback's system multiple"}


# ------------------------------------------------------------------ the clip

def render(model, sol: Solver, waypoints: list[dict], out: Path, *, title: str = "", fps: int = 30, size=(1280, 720)) -> str:
    """A clip of the cycle, 'SIMULATED' stamped on every frame. Needs OpenGL and ffmpeg; returns the file name."""
    import mujoco
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TwinError("ffmpeg not on PATH; no clip")
    w, h = size
    renderer = mujoco.Renderer(model, height=h, width=w)
    cam = mujoco.MjvCamera()
    cam.type = mujoco.mjtCamera.mjCAMERA_FREE
    bench = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "bench")
    cx, cy = model.geom_pos[bench][0], model.geom_pos[bench][1]
    L = model.geom_size[bench][0] * 2
    cam.lookat[:] = [cx, cy, 0.15]
    cam.distance = max(1.8, 1.4 * L)
    cam.azimuth, cam.elevation = 215, -32
    opt = mujoco.MjvOption()
    carried = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "arm/carried")
    parts = [g for g in range(model.ngeom) if (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) or "").startswith("part")]
    out.parent.mkdir(parents=True, exist_ok=True)
    font = Path(os.environ.get("CELLPILOT_TWIN_FONT", "")) if os.environ.get("CELLPILOT_TWIN_FONT") else None
    stamp = "SIMULATED  ·  CellPilot cobot twin, kinematic, part welded to the gripper"
    fp = font.as_posix().replace(":", "\\:") if font else ""
    draw = (f",drawtext=fontfile='{fp}':text='{stamp}':x=24:y=h-52:fontsize=26:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=10"
            f",drawtext=fontfile='{fp}':text='{title}':x=24:y=24:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=10"
            if font and font.is_file() else "")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-vf", f"format=yuv420p{draw}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    held = False
    try:
        def frame(q):
            sol.data.qpos[:] = q
            mujoco.mj_forward(model, sol.data)
            renderer.update_scene(sol.data, cam, opt)
            proc.stdin.write(renderer.render().tobytes())

        for _ in range(fps):  # a second at home
            frame(waypoints[0]["q"])
        for a, b in zip(waypoints, waypoints[1:]):
            knots = [a["q"], *(b.get("path") or []), b["q"]] if b.get("path") else [a["q"], b["q"]]
            for ka, kb in zip(knots, knots[1:]):
                dq = float(np.max(np.abs(kb - ka))); dx = float(np.linalg.norm(sol.tcp(kb) - sol.tcp(ka)))
                n = max(1, int(round(max(dq / JOINT_SPEED, dx / TCP_SPEED) * fps)))
                for i in range(1, n + 1):
                    frame(ka + (kb - ka) * (i / n))
            if b.get("hold") == "grip":
                held = True
                model.geom_rgba[carried][3] = 1.0
                for g in parts:
                    model.geom_rgba[g][3] = 0.0
            elif b.get("hold") == "release":
                held = False
                model.geom_rgba[carried][3] = 0.0
            for _ in range(int(b["dwell"] * fps)):
                frame(b["q"])
        for _ in range(fps):
            frame(waypoints[-1]["q"])
    finally:
        proc.stdin.close()
        proc.wait()
        renderer.close()
    if proc.returncode:
        raise TwinError("ffmpeg failed while writing the clip")
    return out.name


# ------------------------------------------------------------------ command line: the stored sample

def main(argv=None) -> int:
    from .schemas import Analysis, Twin
    from .store import Samples

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("session")
    p.add_argument("--analysis", default=None, help="analysis id (default: the newest stored one)")
    p.add_argument("--render", action="store_true", help="also render the clip (needs OpenGL)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    samples = Samples()
    d = samples.dir(args.session)
    if d is None:
        sys.exit(f"no sample {args.session}")
    files = sorted((d / "analyses").glob("*.json"))
    path = next((f for f in files if f.stem == args.analysis), None) if args.analysis else (files[-1] if files else None)
    if path is None:
        sys.exit("no stored analysis")
    rec = Analysis.model_validate_json(path.read_text("utf-8"))
    clip_dir = d / "twin"
    twin = simulate(rec.result, render_to=(clip_dir / f"{rec.id}.mp4") if args.render else None)
    Twin.model_validate(twin)  # the contract the page relies on
    data = json.loads(path.read_text("utf-8"))
    data["result"]["twin"] = twin
    data["result"]["twin_error"] = None
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    print(f"twin: {twin['arm_model']} base ({twin['base']['x_m']}, {twin['base']['y_m']}) m; "
          f"{sum(r['reachable'] for r in twin['points'])}/{len(twin['points'])} points reachable; "
          f"{'collision-free' if twin['collision_free'] else 'collisions: ' + ', '.join(twin['path_collisions'])}; "
          f"robot {twin['robot_cycle_s']} s, side by side {twin['parallel_cycle_s']} s "
          f"(estimate {twin['estimate_parallel_cycle_s']} s); clip {twin['clip']}")
    print(f"saved into {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
