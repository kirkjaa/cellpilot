"""Spike 1: reach gemini-4-argon on Vertex AI and measure it.

    python spikes/argon_smoke.py text    # access + structured output
    python spikes/argon_smoke.py video   # three synced clips + offsets in one request

Credentials come from the environment only (GOOGLE_APPLICATION_CREDENTIALS or
gcloud application-default credentials) plus GOOGLE_CLOUD_PROJECT.

The video test uses synthetic clips with a known answer: a red square moves
left to right at 120 px/s on a 640 px frame, so its centre crosses the frame's
centre line at t = 2.5 s and 7.5 s of true time. Each camera starts at a
different true time (its offset), so a correct answer reports 2.5 and 7.5 for
every camera once converted to cam0 time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

try:  # use the Windows certificate store (antivirus HTTPS scanning re-signs traffic)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from google import genai
from google.genai import types
from pydantic import BaseModel

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
MODEL = os.environ.get("CELLPILOT_MODEL", "gemini-4-argon")
OUT = Path(__file__).parent / "out"

# cam0 is the reference; a camera's offset is the cam0 time at which its clip starts.
OFFSETS = {"cam0": 0.0, "cam1": 0.2, "cam2": 0.45}
CLIP_S = 10
TRUE_CROSSINGS = [2.5, 7.5]


class Ping(BaseModel):
    ok: bool
    model_note: str


class Crossing(BaseModel):
    camera: Literal["cam0", "cam1", "cam2"]
    t_cam0_s: float


class VideoCheck(BaseModel):
    description: str
    crossings: list[Crossing]


_client: genai.Client | None = None


def client() -> genai.Client:
    # One long-lived client: the SDK closes its HTTP connection when a Client is collected.
    global _client
    if _client is None:
        _client = genai.Client(enterprise=True, project=PROJECT, location=LOCATION)
    return _client


def run(contents, schema, thinking: str = "LOW"):
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
    )
    t0 = time.perf_counter()
    resp = client().models.generate_content(model=MODEL, contents=contents, config=config)
    elapsed = time.perf_counter() - t0
    u = resp.usage_metadata
    print(f"model: {resp.model_version or MODEL}   location: {LOCATION}   {elapsed:.1f} s")
    print(f"tokens: prompt {u.prompt_token_count}, thoughts {u.thoughts_token_count}, "
          f"output {u.candidates_token_count}, total {u.total_token_count}")
    return resp.parsed, resp.text


def make_clip(cam: str) -> Path:
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{cam}.mp4"
    if path.exists():
        return path
    offset = OFFSETS[cam]
    y = {"cam0": 160, "cam1": 120, "cam2": 200}[cam]
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=gray:s=640x360:r=30:d={CLIP_S}",
            "-f", "lavfi", "-i", f"color=c=red:s=40x40:r=30:d={CLIP_S}",
            "-filter_complex",
            f"[0][1]overlay=x='mod((t+{offset})*120,600)':y={y},"
            "drawbox=x=319:y=0:w=2:h=360:color=white:t=fill",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path),
        ],
        check=True,
    )
    return path


def text_test() -> None:
    parsed, _ = run(
        "This is a connectivity test for a manufacturing analysis app. "
        "Reply with ok=true and one short sentence naming yourself.",
        Ping,
    )
    print(parsed)


def video_test() -> None:
    parts = []
    for cam in OFFSETS:
        data = make_clip(cam).read_bytes()
        parts.append(types.Part(
            inline_data=types.Blob(data=data, mime_type="video/mp4"),
            video_metadata=types.VideoMetadata(fps=5),
        ))
    calibration = {"reference": "cam0", "offsets_s": OFFSETS,
                   "note": "offset = cam0 time at which that camera's clip starts"}
    parts.append(types.Part(text=(
        "Three synchronized cameras, in the order cam0, cam1, cam2, filmed the same "
        "event. Calibration:\n" + json.dumps(calibration) + "\n"
        "A red square moves left to right; a white vertical line marks the frame's "
        "centre. For each camera, list every time the centre of the red square "
        "crosses the white line. Report each time in cam0 seconds: add that "
        "camera's offset to the time you see in its own clip."
    )))
    parsed, _ = run(parts, VideoCheck)
    print("model says:", parsed.description)
    worst = 0.0
    for c in sorted(parsed.crossings, key=lambda c: (c.camera, c.t_cam0_s)):
        err = min(abs(c.t_cam0_s - t) for t in TRUE_CROSSINGS)
        worst = max(worst, err)
        print(f"  {c.camera}: {c.t_cam0_s:6.2f} s   error {err:.2f} s")
    print(f"crossings found: {len(parsed.crossings)} (expected {len(OFFSETS) * len(TRUE_CROSSINGS)}), "
          f"worst error {worst:.2f} s")


if __name__ == "__main__":
    {"text": text_test, "video": video_test}[sys.argv[1] if len(sys.argv) > 1 else "text"]()
