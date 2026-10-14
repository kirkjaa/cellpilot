"""Gemini 4 Argon on Vertex AI: one long-lived client and the two calls.

Call A (steps):    the three clips, then the shared context, then the task -> TaskGraph
Call D (verifier): the same clips and context, identically, then A's JSON -> Verify

The clips and the context text come first and identically in both calls, so
Vertex's implicit caching can reuse that prefix. Credentials come only from the
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
from google.genai import types
from pydantic import BaseModel, ValidationError

from .catalogue import public_catalogue
from .schemas import TaskGraph, Verify
from .store import Store

log = logging.getLogger("cellpilot.argon")

INLINE_LIMIT_BYTES = 15 * 1024 * 1024

_client: Optional[genai.Client] = None
_client_lock = threading.Lock()


class ArgonError(RuntimeError):
    pass


def model_name() -> str:
    return os.environ.get("CELLPILOT_MODEL", "gemini-4-argon")


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
                                   location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
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
    parts = []
    for clip in sorted(session["clips"], key=lambda c: c["view"]):
        path = sample_dir / clip["file"]
        size = path.stat().st_size
        meta = types.VideoMetadata(fps=fps)
        if size >= INLINE_LIMIT_BYTES and getattr(store, "is_bucket", False):
            uri = store.media_uri(f"media/{session['id']}/{clip['file']}", path)
            parts.append(types.Part(file_data=types.FileData(file_uri=uri, mime_type="video/mp4"),
                                    video_metadata=meta))
        else:
            if size >= INLINE_LIMIT_BYTES:
                log.warning("%s is %.1f MB; sending inline because the store is not a bucket",
                            path.name, size / 1e6)
            parts.append(types.Part(inline_data=types.Blob(data=path.read_bytes(), mime_type="video/mp4"),
                                    video_metadata=meta))
    return parts


def context_text(session: dict) -> str:
    """Shared by calls A and D, word for word, right after the clips."""
    cams = [{"view": c["view"], "offset_s": c["offset_s"], "duration_s": c["duration_s"]}
            for c in sorted(session["clips"], key=lambda c: c["view"])]
    return (
        "Three synchronized cameras filmed the same manual workstation. The videos above are, "
        "in order: " + ", ".join(c["view"] for c in cams) + ".\n"
        "Time base: cam0 is the reference. A camera's offset_s is the cam0 time at which its clip "
        "starts, so a moment at time t in that camera's own clip is at cam0 time t + offset_s.\n"
        "Cameras: " + json.dumps(cams) + "\n"
        "Robot catalogue (6-axis cobots, each fitted with a two-finger parallel gripper): "
        + json.dumps(public_catalogue()) + "\n"
    )


TASK_A = """You are a manufacturing engineer judging which steps of this manual task a cobot could take over.

1. Every timestamp you return is in cam0 seconds: the time in a camera's own clip plus that camera's offset_s.
2. The operator repeats the task; each repetition is a cycle. List the steps of one cycle in order, with ids s1, s2, s3 and so on. For each step give the start and end of every occurrence you see, with its cycle number counting from 1.
3. Verdict per step: choose "automate" only if a two-finger parallel gripper on a 6-axis cobot with the listed payload could do it with no tactile judgement. Choose "needs_fixture" if a cobot could do it once a fixture holds or presents the part in a known position. Otherwise choose "keep_human".
4. Give two reasons per step from what you saw, each citing a camera (cam0, cam1 or cam2) and a timestamp in cam0 seconds.
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


# --------------------------------------------------------------------- calls

def _generate(contents: list, schema: type[BaseModel], thinking: str) -> CallResult:
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
    )
    t0 = time.perf_counter()
    resp = client().models.generate_content(model=model_name(), contents=contents, config=config)
    seconds = time.perf_counter() - t0
    u = resp.usage_metadata
    usage = {
        "prompt_tokens": (u and u.prompt_token_count) or 0,
        "cached_tokens": (u and u.cached_content_token_count) or 0,
        "thoughts_tokens": (u and u.thoughts_token_count) or 0,
        "candidates_tokens": (u and u.candidates_token_count) or 0,
        "seconds": round(seconds, 2),
        "model_version": resp.model_version or model_name(),
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
