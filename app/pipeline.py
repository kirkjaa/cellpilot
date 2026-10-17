"""Run one analysis: queued -> steps (Argon A) -> verifier (Argon D) -> layout (Argon C)
-> robot -> payback -> notes (Gemini E, a general model, text only) -> done. The layout and
notes stages are optional: if one fails, the study ships without it and says why.

The API starts each run in a background thread (`start`). The same code runs
synchronously from the command line to make a sample's stored analysis:

    python -m app.pipeline <session-id> --save-sample     # three paid Argon calls
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from . import argon
from .catalogue import choose_robot, cycle_estimate
from .layout import build_layout
from .payback import roi_defaults
from .schemas import (Analysis, ArgonLayout, Counts, Layout, Occurrence, Part, Reason, Result, Step, TaskGraph, Twin,
                      VerifierCheck, Verify)
from .store import Samples, Store, analysis_key, argon_key, latest_key, make_store

log = logging.getLogger("cellpilot.pipeline")

TIMING_TOLERANCE_S = 1.0

_active: dict[str, str] = {}  # aid -> sid
_active_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return "a-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def is_active(aid: str) -> bool:
    with _active_lock:
        return aid in _active


def active_runs() -> dict[str, str]:
    with _active_lock:
        return dict(_active)


def new_record(sid: str, aid: str, live: bool = True) -> dict:
    return Analysis(id=aid, session_id=sid, status="queued", stage="queued", live=live,
                    model=argon.model_name(), started_at=now_iso()).model_dump()


# ------------------------------------------------------- pure result building

def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


def apply_verifier(graph: TaskGraph, check: Optional[Verify]) -> list[Step]:
    """Apply the verifier's timing corrections over 1 s and set `preflagged`; never change a verdict."""
    checks = {}
    for c in (check.steps if check else []):
        checks.setdefault(c.id, c)
    steps = []
    for s in graph.steps:
        occ = [Occurrence(cycle=o.cycle, start_s=round(o.start_s, 2), end_s=round(o.end_s, 2))
               for o in s.occurrences]
        c = checks.get(s.id)
        verifier = None
        preflagged = True  # no verifier entry: the engineer should look
        if c is not None:
            corrected = False
            for fix in c.corrected:
                same_cycle = [o for o in occ if o.cycle == fix.cycle]
                if not same_cycle or fix.end_s < fix.start_s:
                    continue  # never add occurrences; ignore malformed fixes
                o = min(same_cycle, key=lambda o: abs(o.start_s - fix.start_s) + abs(o.end_s - fix.end_s))
                if max(abs(o.start_s - fix.start_s), abs(o.end_s - fix.end_s)) > TIMING_TOLERANCE_S:
                    o.start_s, o.end_s = round(fix.start_s, 2), round(fix.end_s, 2)
                    corrected = True
            delta = abs(c.timing_delta_s)
            verifier = VerifierCheck(agree=c.agree, verdict_agree=c.verdict_agree,
                                     timing_delta_s=round(delta, 2), note=c.note.strip())
            preflagged = (not c.verdict_agree) or delta > TIMING_TOLERANCE_S or corrected
        occ.sort(key=lambda o: (o.cycle, o.start_s))
        durations = [max(0.0, o.end_s - o.start_s) for o in occ]
        steps.append(Step(
            id=s.id, name=s.name.strip(), verdict=s.verdict,
            reasons=[Reason(text=r.text.strip(), view=r.view, t_s=round(r.t_s, 2), box=clean_box(r.box))
                     for r in s.reasons],
            occurrences=occ, parts=[p.strip() for p in s.parts], confidence=round(_clamp01(s.confidence), 2),
            mean_duration_s=round(sum(durations) / len(durations), 2) if durations else 0.0,
            verifier=verifier, preflagged=preflagged,
        ))
    return steps


def clean_box(box) -> list[int]:
    """[ymin, xmin, ymax, xmax] on the 0-1000 grid, ordered and clamped; [] for anything degenerate."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return []
    try:
        y0, x0, y1, x1 = (int(min(1000, max(0, round(float(v))))) for v in box)
    except (TypeError, ValueError):
        return []
    y0, y1 = sorted((y0, y1))
    x0, x1 = sorted((x0, x1))
    return [y0, x0, y1, x1] if y1 > y0 and x1 > x0 else []


def clean_parts(parts: list[Part]) -> list[Part]:
    return [Part(name=p.name.strip(), mass_g=round(max(0.0, p.mass_g), 1),
                 size_mm=[round(max(0.0, x), 1) for x in p.size_mm], confidence=round(_clamp01(p.confidence), 2))
            for p in parts]


def cycle_stats(steps: list[Step], reported_cycles: int) -> tuple[int, float]:
    """Cycles seen and the mean cycle time: the spacing of cycle starts, or one cycle's span."""
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for s in steps:
        for o in s.occurrences:
            starts[o.cycle] = min(starts.get(o.cycle, o.start_s), o.start_s)
            ends[o.cycle] = max(ends.get(o.cycle, o.end_s), o.end_s)
    if not starts:
        return max(0, reported_cycles), 0.0
    nums = sorted(starts)
    cycle_time = 0.0
    if nums[-1] > nums[0]:
        cycle_time = (starts[nums[-1]] - starts[nums[0]]) / (nums[-1] - nums[0])
    if cycle_time <= 0:
        spans = [ends[n] - starts[n] for n in nums if ends[n] > starts[n]]
        cycle_time = sum(spans) / len(spans) if spans else 0.0
    return len(nums), round(cycle_time, 2)


def count_verdicts(steps: list[Step]) -> Counts:
    n = {v: sum(1 for s in steps if s.verdict == v) for v in ("automate", "keep_human", "needs_fixture")}
    return Counts(**n, total=len(steps))


def automated_share(steps: list[Step], cycle_time_s: float) -> float:
    if cycle_time_s <= 0:
        return 0.0
    auto = sum(s.mean_duration_s for s in steps if s.verdict == "automate")
    return round(_clamp01(auto / cycle_time_s), 3)


def assemble(graph: TaskGraph, steps: list[Step], parts: list[Part], robot: dict,
             layout: Optional[Layout] = None, layout_error: Optional[str] = None) -> Result:
    cycles, cycle_time = cycle_stats(steps, graph.cycles)
    return Result(summary=graph.summary.strip(), cycles=cycles, cycle_time_s=cycle_time, steps=steps,
                  parts=parts, counts=count_verdicts(steps),
                  automated_share=automated_share(steps, cycle_time), robot=robot, roi_defaults=roi_defaults(),
                  layout=layout, layout_error=layout_error, cycle_estimate=cycle_estimate(steps, cycle_time))


def build_result(graph: TaskGraph, check: Optional[Verify], estimate: Optional[ArgonLayout] = None) -> Result:
    steps = apply_verifier(graph, check)
    parts = clean_parts(graph.parts)
    layout = build_layout(estimate, steps) if estimate is not None else None
    robot = choose_robot(parts, steps, needed_reach_m=layout.needed_reach_m if layout else None)
    return assemble(graph, steps, parts, robot, layout,
                    None if layout else "No layout pass was run for this result.")


# ------------------------------------------------------------------- running

STAGE_TEXT = {"queued": "start-up", "steps": "the step analysis (Argon call A)",
              "verifier": "the verifier (Argon call D)", "layout": "the layout estimate (Argon call C)",
              "robot": "the robot choice", "payback": "the payback figures", "twin": "the cobot twin (kinematic simulation)",
              "notes": "the engineer's notes (Gemini call E)", "done": "saving"}


def readable_error(stage: str, e: BaseException) -> str:
    from google.genai import errors as genai_errors

    if isinstance(e, genai_errors.APIError):  # the full message, with project paths, stays in the log
        who = "the notes model" if stage == "notes" else "Argon"
        return f"Failed during {STAGE_TEXT.get(stage, stage)}: {who} answered {e.code} {e.status or ''}".rstrip()
    msg = " ".join((str(e) or "").split()) or "no details"
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds:
        msg = msg.replace(creds, "<credentials file>")
    if len(msg) > 500:
        msg = msg[:497] + "..."
    return f"Failed during {STAGE_TEXT.get(stage, stage)}: {type(e).__name__}: {msg}"


def run(store: Store, samples: Samples, sid: str, aid: str) -> dict:
    """Run every stage synchronously, writing the record after each stage. Never raises."""
    key = analysis_key(sid, aid)
    with _active_lock:
        _active[aid] = sid
    rec = store.read_json(key) or new_record(sid, aid)
    raw: dict = {}
    tokens = {"prompt_tokens": 0, "output_tokens": 0}
    t0 = time.perf_counter()

    def save(**changes) -> None:
        rec.update(changes)
        rec["usage"] = {**tokens, "seconds": round(time.perf_counter() - t0, 1)}
        store.write_json(key, rec)

    def count(call: argon.CallResult) -> None:
        tokens["prompt_tokens"] += call.usage.get("prompt_tokens", 0)
        tokens["output_tokens"] += call.usage.get("thoughts_tokens", 0) + call.usage.get("candidates_tokens", 0)

    try:
        session, sample_dir = samples.session(sid), samples.dir(sid)
        if session is None or sample_dir is None:
            raise LookupError(f"session {sid!r} not found")
        save(status="running", stage="steps", model=argon.model_name(), error=None)
        media = argon.video_parts(session, sample_dir, store)
        a = argon.task_graph(session, media)
        raw["task_graph"] = {"text": a.text, "usage": a.usage}
        count(a)

        save(stage="verifier")
        d = argon.verify(session, media, a.parsed)
        raw["verify"] = {"text": d.text, "usage": d.usage}
        count(d)

        save(stage="layout")
        steps = apply_verifier(a.parsed, d.parsed)
        parts = clean_parts(a.parsed.parts)
        layout, layout_error = None, None
        try:  # optional: the study ships without a layout rather than losing calls A and D
            c = argon.layout(session, media, steps, parts)
            raw["layout"] = {"text": c.text, "usage": c.usage}
            count(c)
            layout = build_layout(c.parsed, steps)
        except Exception as e:  # noqa: BLE001
            log.exception("analysis %s: the layout pass failed; continuing without a layout", aid)
            layout_error = readable_error("layout", e)

        save(stage="robot")
        robot = choose_robot(parts, steps, needed_reach_m=layout.needed_reach_m if layout else None)

        save(stage="payback")
        result = assemble(a.parsed, steps, parts, robot, layout, layout_error)

        save(stage="twin")
        try:  # optional: a kinematic simulation of the automatable steps on the chosen arm (no clip on the server)
            from . import twin as twin_mod
            result.twin = Twin.model_validate(twin_mod.simulate(result))
        except Exception as e:  # noqa: BLE001
            log.exception("analysis %s: the cobot twin failed; continuing without it", aid)
            result.twin_error = readable_error("twin", e)

        save(stage="notes")
        try:  # optional: a general Gemini model writes the engineer's notes from the result, text only
            e = argon.notes(session, result)
            raw["notes"] = {"text": e.text, "usage": e.usage}
            count(e)
            result.notes = e.parsed
            result.notes_model = e.usage.get("model_version") or argon.notes_model_name()
        except Exception as e:  # noqa: BLE001
            log.exception("analysis %s: the notes pass failed; continuing without notes", aid)
            result.notes_error = readable_error("notes", e)

        save(status="done", stage="done", result=result.model_dump(), finished_at=now_iso())
        store.write_json(latest_key(sid), {"id": aid, "finished_at": rec["finished_at"]})
    except Exception as e:  # noqa: BLE001 - every failure must reach the page as text
        log.exception("analysis %s failed at stage %s", aid, rec.get("stage"))
        try:
            save(status="failed", error=readable_error(rec.get("stage", "queued"), e), finished_at=now_iso())
        except Exception:  # noqa: BLE001
            log.exception("could not record the failure of %s", aid)
    finally:
        if raw:  # Argon's answers, word for word, beside the analysis
            try:
                store.write_json(argon_key(sid, aid), {"analysis_id": aid, "model": rec.get("model"), **raw})
            except Exception:  # noqa: BLE001
                log.exception("could not store Argon's raw answers for %s", aid)
        with _active_lock:
            _active.pop(aid, None)
    return rec


def start(store: Store, samples: Samples, sid: str) -> str:
    """Queue a live analysis and run it in a background thread; returns its id at once."""
    aid = new_id()
    with _active_lock:
        _active[aid] = sid
    try:
        store.write_json(analysis_key(sid, aid), new_record(sid, aid, live=True))
    except Exception:
        with _active_lock:
            _active.pop(aid, None)
        raise
    threading.Thread(target=run, args=(store, samples, sid, aid), name=f"analysis-{aid}", daemon=True).start()
    return aid


# ----------------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run one analysis now (three paid Argon calls).")
    p.add_argument("session", help="sample session id, e.g. kitting-20261017")
    p.add_argument("--save-sample", action="store_true",
                   help="also write the result to samples/<sid>/analyses/<aid>.json with live=false")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    store, samples = make_store(), Samples()
    sid, aid = args.session, new_id()
    if samples.session(sid) is None:
        print(f"no sample session {sid!r} under {samples.root}")
        return 2
    store.write_json(analysis_key(sid, aid), new_record(sid, aid, live=True))
    print(f"analysis {aid} on {sid}, model {argon.model_name()}, store {store.describe()}")
    rec = run(store, samples, sid, aid)

    print(f"status {rec['status']}  stage {rec['stage']}  usage {rec['usage']}")
    if rec["status"] != "done":
        print("error:", rec["error"])
        return 1
    r = rec["result"]
    print(f"cycles {r['cycles']}  cycle time {r['cycle_time_s']} s  automated share {r['automated_share']}")
    print("summary:", r["summary"])
    for s in r["steps"]:
        occ = ", ".join(f"c{o['cycle']} {o['start_s']}-{o['end_s']}" for o in s["occurrences"])
        print(f"  {s['id']} {s['name']!r} {s['verdict']} conf {s['confidence']} mean {s['mean_duration_s']} s"
              f" preflagged {s['preflagged']} [{occ}]")
        if s["verifier"]:
            print(f"      verifier: agree {s['verifier']['agree']} verdict_agree {s['verifier']['verdict_agree']}"
                  f" delta {s['verifier']['timing_delta_s']} s - {s['verifier']['note']}")
    print("parts:", json.dumps(r["parts"]))
    lay = r.get("layout")
    if lay:
        print(f"layout: bench {lay['bench']['length_m']} x {lay['bench']['width_m']} m, {len(lay['objects'])} objects, "
              f"{len(lay['anchors'])} anchors, base ({lay['robot_base']['x_m']}, {lay['robot_base']['y_m']}), "
              f"needed reach {lay['needed_reach_m']} m, confidence {lay['confidence']} - {lay['notes']}")
        for o in lay["objects"]:
            print(f"  {o['kind']:16} {o['name']!r} at ({o['x_m']}, {o['y_m']}) {o['length_m']}x{o['width_m']} m")
    else:
        print("layout: none -", r.get("layout_error"))
    print("robot:", r["robot"]["model"], "-", r["robot"]["why"])
    for o in r["robot"].get("alternatives", []):
        print(f"  {'*' if o['chosen'] else ' '} {o['model']:14} {o['payload_kg']} kg {o['reach_mm']} mm "
              f"payload {o['payload_fits']} reach {o['reach_fits']} - {o['note']}")
    if r.get("cycle_estimate"):
        ce = r["cycle_estimate"]
        print(f"cycle estimate: human {ce['human_cycle_s']} s, robot {ce['robot_cycle_s']} s "
              f"(x{ce['speed_factor']}), operator {ce['operator_cycle_s']} s, side by side {ce['parallel_cycle_s']} s")

    if args.save_sample:
        out = samples.dir(sid)
        (out / "analyses").mkdir(exist_ok=True)
        (out / "analyses" / f"{aid}.json").write_text(json.dumps({**rec, "live": False}, indent=2, ensure_ascii=False), "utf-8")
        raw = store.read_json(argon_key(sid, aid))
        if raw:
            (out / "argon").mkdir(exist_ok=True)
            (out / "argon" / f"{aid}.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), "utf-8")
        print(f"saved as the stored analysis: samples/{sid}/analyses/{aid}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
