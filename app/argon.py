"""Gemini 4 Argon on Gemini Enterprise Agent Platform (formerly Vertex AI): one long-lived client and the three calls.

Call A (steps):    the three clips, then the shared context, then the task -> TaskGraph
Call D (verifier): the same clips and context, identically, then A's JSON -> Verify
Call C (layout):   the same clips and context, then the verified steps -> ArgonLayout
                   (a top-view estimate in metres; optional, the study ships without it)

The clips and the context text come first and identically in every call, so
Agent Platform's implicit caching can reuse that prefix. Credentials come only from the
environment (GOOGLE_APPLICATION_CREDENTIALS or the Cloud Run service account).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from .catalogue import public_catalogue
from .payback import payback
from .schemas import ArgonLayout, Notes, Result, Step, TaskGraph, Verify
from .store import Store

log = logging.getLogger("cellpilot.argon")

INLINE_LIMIT_BYTES = 15 * 1024 * 1024
CALL_TIMEOUT_MS = 20 * 60 * 1000  # Agent Platform caps a request at 20 minutes
RETRY_CODES = {429, 503}  # rejected before any generation: not billed

_client: Optional[genai.Client] = None
_client_lock = threading.Lock()


class ArgonError(RuntimeError):
    pass


def model_name() -> str:
    return os.environ.get("CELLPILOT_MODEL", "gemini-4-argon")


def notes_model_name() -> str:
    """The general Gemini model that writes the engineer's notes from the result (text only, no video)."""
    return os.environ.get("CELLPILOT_NOTES_MODEL", "gemini-3.8-flash")


def video_fps() -> float:
    return float(os.environ.get("CELLPILOT_VIDEO_FPS", "1"))


def client() -> genai.Client:
    # One long-lived client: the SDK closes its HTTP connection when a Client is collected.
    global _client
    with _client_lock:
        if _client is None:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise ArgonError("GOOGLE_CLOUD_PROJECT is not set on the server.")
            _client = genai.Client(enterprise=True, project=project,
                                   location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
                                   http_options=types.HttpOptions(timeout=CALL_TIMEOUT_MS))
        return _client


@dataclass
class CallResult:
    parsed: Any
    text: str
    usage: dict = field(default_factory=dict)


# ------------------------------------------------------------------ contents

def video_parts(session: dict, sample_dir: Path, store: Store) -> list[types.Part]:
    """The clips in cam0, cam1, cam2 order: inline under 15 MB, else a gs:// URI when the store is a bucket."""
    fps = video_fps()
    clips = sorted(session["clips"], key=lambda c: c["view"])
    total = sum((sample_dir / c["file"]).stat().st_size for c in clips)
    by_uri = total >= INLINE_LIMIT_BYTES  # the three clips share one request
    if by_uri and not getattr(store, "is_bucket", False):
        raise ArgonError(f"The clips total {total / 1e6:.0f} MB, too much to send inline; "
                         "set CELLPILOT_STORE=gs://<bucket> so they go by gs:// URI.")
    parts = []
    for clip in clips:
        path = sample_dir / clip["file"]
        meta = types.VideoMetadata(fps=fps)
        if by_uri:
            uri = store.media_uri(f"media/{session['id']}/{clip['file']}", path)
            parts.append(types.Part(file_data=types.FileData(file_uri=uri, mime_type="video/mp4"),
                                    video_metadata=meta))
        else:
            parts.append(types.Part(inline_data=types.Blob(data=path.read_bytes(), mime_type="video/mp4"),
                                    video_metadata=meta))
    return parts


def context_text(session: dict) -> str:
    """Shared by calls A and D, word for word, right after the clips."""
    cams = [{"view": c["view"], "offset_s": c["offset_s"], "duration_s": c["duration_s"]}
            for c in sorted(session["clips"], key=lambda c: c["view"])]
    brief = " ".join(str(session.get("brief") or "").split())[:600]
    return (
        "Three synchronized cameras filmed the same manual workstation. The videos above are, "
        "in order: " + ", ".join(c["view"] for c in cams) + ".\n"
        "Time base: cam0 is the reference. A camera's offset_s is the cam0 time at which its clip "
        "starts, so a moment at time t in that camera's own clip is at cam0 time t + offset_s.\n"
        "Cameras: " + json.dumps(cams) + "\n"
        + (f"Station, as described by the people who filmed it: {brief}\n" if brief else "")
        + "Robot catalogue (6-axis cobots, each fitted with a two-finger parallel gripper): "
        + json.dumps(public_catalogue()) + "\n"
    )


TASK_A = """You are a manufacturing engineer judging which steps of this manual task a cobot could take over.

1. Every timestamp you return is in cam0 seconds: the time in a camera's own clip plus that camera's offset_s.
2. The operator repeats the task; each repetition is a cycle. List the steps of one cycle in order, with ids s1, s2, s3 and so on. For each step give the start and end of every occurrence you see, with its cycle number counting from 1.
3. Verdict per step: choose "automate" only if a two-finger parallel gripper on a 6-axis cobot with the listed payload could do it with no tactile judgement. Choose "needs_fixture" if a cobot could do it once a fixture holds or presents the part in a known position. Otherwise choose "keep_human".
4. Give two reasons per step from what you saw, each citing a camera (cam0, cam1 or cam2), a timestamp in cam0 seconds, and a box [ymin, xmin, ymax, xmax] on a 0 to 1000 grid over that camera's frame around what the reason describes (the hand, the part, the tool); an empty list only if nothing can be boxed.
5. Do not list a step you cannot see in at least two views.
6. List every part handled, with a short name, estimated mass in grams, size in millimetres as [length, width, height], and your confidence from 0 to 1. In each step, refer to parts by these names.
7. Give each step a confidence from 0 to 1, the number of cycles you see, and a one-paragraph summary for a production manager.
"""

TASK_D = """You are checking another engineer's analysis. For each step and each camera say whether the boundaries and the verdict match what you see; give corrected start and end if they differ by more than one second; never add steps.

The analysis, with every time in cam0 seconds:
{analysis}

Return one entry per step id in the analysis, and only those ids.
- agree: true if the step's start and end match what you see in every camera, within one second.
- timing_delta_s: the largest difference in seconds between the analysis's boundaries and what you see; 0 if they match.
- corrected: for each occurrence whose start or end is off by more than one second, its cycle and the corrected start_s and end_s in cam0 seconds; an empty list otherwise.
- verdict_agree: true if you agree with the verdict under the same rule: "automate" only if a two-finger parallel gripper on a 6-axis cobot with the listed payload could do it with no tactile judgement.
- note: one or two sentences; name each camera where what you see differs from the analysis.
"""

TASK_C = """You are estimating the top-view layout of this workstation so a cobot can be placed. Use a bench frame in metres: the origin is the bench's front-left corner as the operator sees it (the operator stands in front of the bench, at y below 0); x runs to the operator's right along the front edge; y runs away from the operator across the bench.

Fix the scale from things of known size: the parts below with their estimated sizes, a hand (about 0.09 m across the palm), an adult's shoulder width (about 0.45 m), a typical bench (0.6 to 0.8 m deep). Say in notes what fixed the scale and what is uncertain. Use all three cameras; their offsets are as given above.

The steps, with every time in cam0 seconds:
{steps}

The parts, with estimated sizes in millimetres:
{parts}

Return:
- bench_length_m and bench_width_m: the working surface the operator uses.
- operator_x_m: where along the front edge the operator mostly stands.
- objects: every bin, fixture, tool, part source or part destination on the bench, each with its centre, size, kind, the ids of the steps that use it, and your confidence.
- steps: one entry per step id above, and only those ids, with pick_xy_m (where the part is taken from in that step) and place_xy_m (where it is put down); an empty list when the step picks or places nothing.
- confidence: your overall confidence in the scale, 0 to 1.
"""


TASK_E = """You are a senior automation engineer writing the engineer's notes for a feasibility study. You did not see the video. You have the analysis below, made from three synchronized camera views by a vision model and checked by a second pass, plus the robot arm the app chose from its catalogue and the payback at the default inputs. Take every step, verdict and timing as given; never change a verdict or add a step.

Station: {station}

Steps, with the verdict (automate, needs_fixture or keep_human), mean duration in seconds, confidence, whether the check pass flagged the step, the reasons the vision model gave, and its verifier's note:
{steps}

Parts seen, with estimated mass in grams and size in millimetres:
{parts}

Robot arm chosen, with its checks:
{robot}

Cycle: {cycle_time_s} s by hand over {cycles} cycles. With the cobot, estimated: {cycle_estimate}

Payback at the default inputs, in {currency}: {payback}

Write for a production engineer at a small manufacturer: plain words, no marketing, numbers where you have them. Return:
- for_the_owner: three or four plain sentences for the factory owner: what the cobot would take over, what stays with the operator, the payback in months, and the main caveat.
- step_notes: one entry per step id above, and only those ids. recommendation: for an automate step, the gripper or approach and anything to watch; for a needs_fixture step, the fixture or presenter that would make it automatable; for a keep_human step, why it stays with the operator and what would have to change. fixture: the kind of fixture or presenter, or empty.
- risks: the top three risks of this cell, each with a short title and one or two sentences on how to check it before buying.
- next_steps: three to five concrete next steps for the engineer, in order: what to measure, what to try with a part in hand, whom to ask for a quote.
- questions: up to three questions the engineer must answer before committing.
"""


# --------------------------------------------------------------------- calls

def _generate(contents: list, schema: type[BaseModel], thinking: Optional[str], model: Optional[str] = None) -> CallResult:
    """One schema-checked call. `thinking` None leaves the model's default; `model` None means Argon."""
    name = model or model_name()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=thinking) if thinking else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            resp = client().models.generate_content(model=name, contents=contents, config=config)
            break
        except errors.APIError as e:
            if e.code not in RETRY_CODES or attempt == 2:
                raise
            log.warning("Argon answered %s; retrying in %d s", e.code, 20 * (attempt + 1))
            time.sleep(20 * (attempt + 1))
    seconds = time.perf_counter() - t0
    u = resp.usage_metadata
    usage = {
        "prompt_tokens": (u and u.prompt_token_count) or 0,
        "cached_tokens": (u and u.cached_content_token_count) or 0,
        "thoughts_tokens": (u and u.thoughts_token_count) or 0,
        "candidates_tokens": (u and u.candidates_token_count) or 0,
        "seconds": round(seconds, 2),
        "model_version": resp.model_version or name,
    }
    text = resp.text or ""
    parsed = resp.parsed if isinstance(resp.parsed, schema) else None
    if parsed is None:
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as e:
            finish = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            raise ArgonError(f"Argon's answer did not match the {schema.__name__} schema "
                             f"({e.error_count()} problems; finish reason {finish}).") from e
    log.info("%s: %.1f s, prompt %s (cached %s), thoughts %s, output %s", schema.__name__, seconds,
             usage["prompt_tokens"], usage["cached_tokens"], usage["thoughts_tokens"], usage["candidates_tokens"])
    return CallResult(parsed=parsed, text=text, usage=usage)


def task_graph(session: dict, media: list[types.Part]) -> CallResult:
    """Call A: steps, timings, verdicts and parts (thinking HIGH)."""
    contents = [*media, types.Part(text=context_text(session)), types.Part(text=TASK_A)]
    return _generate(contents, TaskGraph, "HIGH")


def verify(session: dict, media: list[types.Part], graph: TaskGraph) -> CallResult:
    """Call D: a second pass checks A's boundaries and verdicts (thinking HIGH)."""
    task = TASK_D.format(analysis=graph.model_dump_json(indent=1))
    contents = [*media, types.Part(text=context_text(session)), types.Part(text=task)]
    return _generate(contents, Verify, "HIGH")


def layout(session: dict, media: list[types.Part], steps: list[Step], parts: list) -> CallResult:
    """Call C: a top-view layout in metres for the verified steps (thinking MEDIUM)."""
    brief = [{"id": s.id, "name": s.name, "verdict": s.verdict, "parts": s.parts,
              "occurrences": [o.model_dump() for o in s.occurrences[:2]]} for s in steps]
    part_list = [{"name": p.name, "size_mm": p.size_mm} for p in parts]
    task = TASK_C.format(steps=json.dumps(brief, indent=1), parts=json.dumps(part_list))
    contents = [*media, types.Part(text=context_text(session)), types.Part(text=task)]
    return _generate(contents, ArgonLayout, "MEDIUM")


def notes(session: dict, result: Result) -> CallResult:
    """Call E: the engineer's notes, written from the result by a general Gemini model. Text only, no video."""
    r = result
    steps = [{"id": s.id, "name": s.name, "verdict": s.verdict, "mean_duration_s": s.mean_duration_s,
              "confidence": s.confidence, "flagged_by_check": s.preflagged, "parts": s.parts,
              "reasons": [x.text for x in s.reasons], "verifier_note": s.verifier.note if s.verifier else None}
             for s in r.steps]
    robot = {k: v for k, v in r.robot.model_dump().items() if k != "alternatives"}
    pb = payback(r.roi_defaults.model_dump(), r.automated_share)
    pb = {k: (round(v, 1) if isinstance(v, (int, float)) else v) for k, v in pb.items()}
    station = " ".join(str(session.get("brief") or "").split())[:600] or "not described; go by the steps"
    task = TASK_E.format(station=station, steps=json.dumps(steps, indent=1),
                         parts=json.dumps([p.model_dump() for p in r.parts]), robot=json.dumps(robot),
                         cycle_time_s=r.cycle_time_s, cycles=r.cycles,
                         cycle_estimate=json.dumps(r.cycle_estimate.model_dump() if r.cycle_estimate else None),
                         currency=r.roi_defaults.currency, payback=json.dumps(pb))
    return _generate([types.Part(text=task)], Notes, None, model=notes_model_name())
