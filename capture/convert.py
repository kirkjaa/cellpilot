"""Turn one take (three clips with sidecars) into a CellPilot session folder.

    python -m capture.convert <offload folder> <samples dir>/<sid> --id kitting-01 --name "Kitting station, 8 cycles"
                              [--clip take_001] [--ref rx-cam1] [--trim-start S] [--trim-end S] [--height 720]

The offsets come from the sidecars (see sidecar.py). The earliest-starting camera becomes cam0
unless --ref names one. Every clip is re-encoded to 720p H.264, constant frame rate, a keyframe
every second, no audio, with the moov atom first, so the three views scrub together in the
browser. Trimming the same number of seconds from every clip leaves the offsets unchanged.

Needs ffmpeg and ffprobe on PATH.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .sidecar import OffsetCheck, SidecarError, check_session, find_clips

SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


class ConvertError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: Path) -> dict:
    """fps, width, height and duration of a clip's first video stream."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                             check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        raise ConvertError(f"ffprobe failed on {path.name}: {(e.stderr or '').strip()}") from None
    j = json.loads(out)
    if not j.get("streams"):
        raise ConvertError(f"{path.name} has no video stream")
    s = j["streams"][0]
    num, _, den = str(s.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1)
    return {"fps": fps, "width": int(s["width"]), "height": int(s["height"]),
            "duration_s": float(j.get("format", {}).get("duration") or 0.0)}


def encode(src: Path, dst: Path, fps: float, trim_start: float = 0.0, trim_end: float = 0.0,
           height: int = 720) -> None:
    """720p H.264, constant frame rate, a keyframe every second, no audio, moov first."""
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    if trim_start > 0:
        args += ["-ss", f"{trim_start:.3f}"]
    args += ["-i", str(src)]
    if trim_end > 0:
        total = probe(src)["duration_s"]
        args += ["-t", f"{max(total - trim_start - trim_end, 0.1):.3f}"]
    g = max(1, round(fps))
    args += ["-vf", f"scale=-2:{height}", "-r", f"{fps:g}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-an",
             "-movflags", "+faststart", str(dst)]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise ConvertError(f"ffmpeg failed on {src.name}: {(e.stderr or '').strip()[-400:]}") from None


def make_session(session_dir: Path, out_dir: Path, sid: str, name: str, *, clip: Optional[str] = None,
                 ref: Optional[str] = None, recorded: Optional[str] = None, trim_start: float = 0.0,
                 trim_end: float = 0.0, height: int = 720, synthetic: bool = False,
                 scenario: str = "", brief: str = "",
                 progress: Optional[Callable[[str], None]] = None) -> dict:
    """Write <out_dir>/{cam0,cam1,cam2}.mp4 + session.json + sources.json; return the session."""
    say = progress or (lambda _msg: None)
    if not SID_RE.match(sid):
        raise ConvertError("session id: letters, digits, dashes and underscores, 80 at most")
    if not name.strip():
        raise ConvertError("session name is empty")
    if not ffmpeg_available():
        raise ConvertError("ffmpeg and ffprobe must be on PATH")
    session_dir, out_dir = Path(session_dir), Path(out_dir)
    try:
        clip, sidecars = find_clips(session_dir, clip)
    except SidecarError as e:
        raise ConvertError(str(e)) from None
    if len(sidecars) != 3:
        raise ConvertError(f"CellPilot takes exactly three cameras; clip {clip!r} has {len(sidecars)}: "
                           f"{', '.join(sorted(sidecars))}")
    for node, side in sidecars.items():
        if not side.with_suffix(".mp4").exists():
            raise ConvertError(f"missing {side.with_suffix('.mp4').name} next to the sidecar of {node}")
    try:
        check: OffsetCheck = check_session(session_dir, clip, ref)
    except SidecarError as e:
        raise ConvertError(str(e)) from None

    out_dir.mkdir(parents=True, exist_ok=True)
    clips, sources = [], {}
    for i, node in enumerate(check.order):
        view = f"cam{i}"
        src = sidecars[node].with_suffix(".mp4")
        meta = probe(src)
        dst = out_dir / f"{view}.mp4"
        say(f"{view} <- {node}: delay after edge {check.delays_ns[node] / 1e6:.1f} ms, "
            f"{meta['width']}x{meta['height']} @ {meta['fps']:g} fps, {meta['duration_s']:.1f} s; encoding")
        encode(src, dst, meta["fps"], trim_start, trim_end, height)
        out = probe(dst)
        clips.append({"view": view, "file": dst.name, "fps": round(out["fps"], 3), "width": out["width"],
                      "height": out["height"], "duration_s": round(out["duration_s"], 3),
                      "offset_s": round(check.offsets_s[node], 4)})
        sources[view] = {"node_id": node, "sidecar": str(sidecars[node]), "delay_ns": check.delays_ns[node]}

    session = {"id": sid, "name": name.strip(), "kind": "sample",
               "recorded": recorded or dt.date.today().isoformat(), "synthetic": synthetic,
               "scenario": (scenario or "").strip(), "brief": " ".join((brief or "").split())[:600], "clips": clips}
    (out_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    (out_dir / "sources.json").write_text(json.dumps({"clip": clip, "offload_dir": str(session_dir),
                                                      "check": check.to_dict(), "cameras": sources},
                                                     indent=2) + "\n", encoding="utf-8")
    say(f"wrote {out_dir / 'session.json'}")
    return session


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path, help="the offload folder with <clip>_<node>.mp4 and .jsonl")
    ap.add_argument("out_dir", type=Path, help="CellPilot session folder to create, e.g. samples/kitting-01")
    ap.add_argument("--id", required=True, help="session id: letters, digits, dashes")
    ap.add_argument("--name", required=True)
    ap.add_argument("--clip", help="clip name when the folder holds several takes")
    ap.add_argument("--ref", help="node id to use as cam0 (default: the earliest-starting camera)")
    ap.add_argument("--recorded", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--scenario", default="", help="job id from the capture guide, e.g. kitting")
    ap.add_argument("--brief", default="", help="one or two sentences on the station, given to the model as context")
    ap.add_argument("--trim-start", type=float, default=0.0, help="seconds to drop from the start of every clip")
    ap.add_argument("--trim-end", type=float, default=0.0, help="seconds to drop from the end of every clip")
    ap.add_argument("--height", type=int, default=720)
    a = ap.parse_args(argv)
    try:
        session = make_session(a.session_dir, a.out_dir, a.id, a.name, clip=a.clip, ref=a.ref,
                               recorded=a.recorded, trim_start=a.trim_start, trim_end=a.trim_end, height=a.height,
                               scenario=a.scenario, brief=a.brief, progress=print)
    except ConvertError as e:
        sys.exit(f"error: {e}")
    for c in session["clips"]:
        print(f"  {c['view']}: offset {c['offset_s']:+.4f} s, {c['duration_s']:.1f} s")


if __name__ == "__main__":
    main()
