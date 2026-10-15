"""Clip sidecars and the offset arithmetic.

Every camera records from the first frame after the shared RF edge, so its clip starts

    delay = first_frame.mono_ns - edge_mono_ns

after the edge, both read on that camera's own monotonic clock (wall clocks are not used; the
cameras have no RTC). The earliest-starting camera becomes cam0, and each other camera's
`offset_s = (delay_cam - delay_ref) / 1e9` is the cam0 time at which its clip starts.

A sidecar is a JSON Lines file `<clip>_<node>.jsonl` next to `<clip>_<node>.mp4`: a header line
(`{"_": "header", "node_id", "clip", "edge_mono_ns", "first_frame": {"seq", "mono_ns"}, "capture": {"fps", ...}}`)
then one line per written frame (`{"seq", "mono_ns", "phase", "bytes"}`).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class SidecarError(ValueError):
    pass


def read_sidecar(path: Path) -> tuple[dict, list[dict]]:
    """(header, frame lines). Raises SidecarError when the first line is not a header."""
    with Path(path).open(encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    if not lines or not isinstance(lines[0], dict) or lines[0].get("_") != "header":
        raise SidecarError(f"{path}: first line is not a sidecar header")
    return lines[0], [ln for ln in lines[1:] if isinstance(ln, dict)]


def delay_ns(header: dict, frames: list[dict]) -> int:
    """How long after the RF edge this clip's first written frame was captured, in ns."""
    edge = header.get("edge_mono_ns")
    if edge is None:
        raise SidecarError("header has no edge_mono_ns (was this take fired by RF?)")
    first = frames[0].get("mono_ns") if frames else (header.get("first_frame") or {}).get("mono_ns")
    if first is None:
        raise SidecarError("no frame lines and no first_frame in the header")
    return int(first) - int(edge)


def find_clips(session_dir: Path, clip: Optional[str] = None) -> tuple[str, dict[str, Path]]:
    """(clip name, {node id: sidecar path}) for one clip under an offload folder.

    Layouts seen: `<clip>_<node>.jsonl` flat in the folder (the cameras' own layout), and
    `<cam>/<trial>_<cam>.jsonl` (an offload tool's layout). The header names the node and the
    clip; the file name is the fallback. Without `clip`, the clip with the most cameras is chosen
    and a tie is an error."""
    session_dir = Path(session_dir)
    by_clip: dict[str, dict[str, Path]] = defaultdict(dict)
    for side in sorted(session_dir.rglob("*.jsonl")):
        if side.parent.name == "truth" or side.name.endswith("_status.jsonl"):
            continue
        try:
            header, _ = read_sidecar(side)
        except (SidecarError, ValueError, OSError):
            continue
        node = header.get("node_id") or side.stem.rsplit("_", 1)[-1]
        name = header.get("clip") or side.stem[: -(len(node) + 1)]
        by_clip[str(name)][str(node)] = side
    if not by_clip:
        raise SidecarError(f"no sidecars under {session_dir}")
    if clip is None:
        best = max(by_clip, key=lambda c: (len(by_clip[c]), c))
        if sum(1 for c in by_clip if len(by_clip[c]) == len(by_clip[best])) > 1:
            raise SidecarError("several clips have the same number of cameras; name one. Found: "
                               + ", ".join(f"{c} ({len(n)} cams)" for c, n in sorted(by_clip.items())))
        clip = best
    if clip not in by_clip:
        raise SidecarError(f"clip {clip!r} not found; have: {', '.join(sorted(by_clip))}")
    return clip, by_clip[clip]


@dataclass
class OffsetCheck:
    """The sync check for one take: delays after the edge per camera and the resulting offsets."""
    ref: str
    order: list[str]                              # cam0, cam1, cam2 ... by node id
    delays_ns: dict[str, int]
    fps: float = 30.0
    missing: list[str] = field(default_factory=list)

    @property
    def offsets_s(self) -> dict[str, float]:
        d0 = self.delays_ns[self.ref]
        return {n: round((d - d0) / 1e9, 6) for n, d in self.delays_ns.items()}

    @property
    def spread_ms(self) -> float:
        vals = list(self.delays_ns.values())
        return (max(vals) - min(vals)) / 1e6

    @property
    def frame_ms(self) -> float:
        return 1000.0 / self.fps if self.fps > 0 else 0.0

    @property
    def within_a_frame(self) -> bool:
        return self.frame_ms > 0 and self.spread_ms <= self.frame_ms + 1e-6

    def to_dict(self) -> dict:
        return {
            "ref": self.ref, "order": list(self.order), "fps": self.fps,
            "delays_ms": {n: round(d / 1e6, 3) for n, d in self.delays_ns.items()},
            "offsets_s": self.offsets_s,
            "views": {n: f"cam{i}" for i, n in enumerate(self.order)},
            "spread_ms": round(self.spread_ms, 3), "frame_ms": round(self.frame_ms, 3),
            "within_a_frame": self.within_a_frame, "missing": list(self.missing),
        }


def offsets_from_delays(delays_ns: dict[str, int], ref: Optional[str] = None, fps: float = 30.0) -> OffsetCheck:
    """Order the cameras (ref first, then by delay) and compute the offsets. `ref` defaults to
    the earliest camera, so no offset is negative."""
    if not delays_ns:
        raise SidecarError("no delays")
    if ref is None:
        ref = min(delays_ns, key=lambda n: (delays_ns[n], n))
    if ref not in delays_ns:
        raise SidecarError(f"reference {ref!r} is not one of {sorted(delays_ns)}")
    order = [ref] + sorted((n for n in delays_ns if n != ref), key=lambda n: (delays_ns[n], n))
    return OffsetCheck(ref=ref, order=order, delays_ns=dict(delays_ns), fps=fps)


def check_session(session_dir: Path, clip: Optional[str] = None, ref: Optional[str] = None,
                  expected: Optional[list[str]] = None) -> OffsetCheck:
    """Read every sidecar of one clip and run the offset check. `expected` lists the node ids
    that should be there; the ones without a sidecar are reported in `missing`."""
    clip, sidecars = find_clips(session_dir, clip)
    delays: dict[str, int] = {}
    fps = 30.0
    for node, side in sidecars.items():
        header, frames = read_sidecar(side)
        delays[node] = delay_ns(header, frames)
        cap = header.get("capture") or {}
        if cap.get("fps"):
            fps = float(cap["fps"])
    out = offsets_from_delays(delays, ref, fps)
    if expected:
        out.missing = [n for n in expected if n not in delays]
    return out
