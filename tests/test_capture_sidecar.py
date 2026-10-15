"""The sidecar reader and the offset arithmetic (capture/sidecar.py)."""
import json
from pathlib import Path

import pytest

from capture import sidecar

EDGE = 48_330_900_683_723  # an RF edge on a camera's monotonic clock, ns


def write_sidecar(path: Path, node: str, clip: str, edge: int, first: int, fps: int = 30, n: int = 4,
                  session: str = "test", header_extra: dict | None = None, frames: bool = True) -> Path:
    header = {"_": "header", "node_id": node, "session": session, "clip": clip, "edge_id": 1,
              "edge_mono_ns": edge, "first_frame": {"seq": 18, "mono_ns": first},
              "capture": {"width": 1920, "height": 1080, "fps": fps}, "armed_at_ns": edge - 3_000_000}
    header.update(header_extra or {})
    lines = [json.dumps(header)]
    if frames:
        period = 1_000_000_000 // fps
        lines += [json.dumps({"seq": 18 + i, "mono_ns": first + i * period, "phase": "preroll" if i < 2 else "live",
                              "bytes": 93_000}) for i in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def test_delay_is_first_frame_minus_edge_on_the_cameras_own_clock(tmp_path):
    p = write_sidecar(tmp_path / "take_001_rx-cam1.jsonl", "rx-cam1", "take_001", EDGE, EDGE + 2_801_267)
    header, frames = sidecar.read_sidecar(p)
    assert header["node_id"] == "rx-cam1" and len(frames) == 4
    assert sidecar.delay_ns(header, frames) == 2_801_267


def test_delay_falls_back_to_the_header_first_frame_without_frame_lines(tmp_path):
    p = write_sidecar(tmp_path / "take_001_rx-cam1.jsonl", "rx-cam1", "take_001", EDGE, EDGE + 1_000, frames=False)
    header, frames = sidecar.read_sidecar(p)
    assert frames == [] and sidecar.delay_ns(header, frames) == 1_000
    with pytest.raises(sidecar.SidecarError, match="edge_mono_ns"):
        sidecar.delay_ns({"first_frame": {"mono_ns": 1}}, [])
    with pytest.raises(sidecar.SidecarError, match="first_frame"):
        sidecar.delay_ns({"edge_mono_ns": 1}, [])


def test_a_file_without_a_header_is_refused(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"seq": 1, "mono_ns": 2}\n', "utf-8")
    with pytest.raises(sidecar.SidecarError, match="header"):
        sidecar.read_sidecar(p)


def test_offsets_make_the_earliest_camera_cam0_and_are_delay_differences():
    delays = {"rx-cam1": 10_366_000, "rx-cam2": 806_000, "rx-cam3": 30_700_000}  # the emulator take of 15 Oct
    chk = sidecar.offsets_from_delays(delays)
    assert chk.ref == "rx-cam2"
    assert chk.order == ["rx-cam2", "rx-cam1", "rx-cam3"]
    assert chk.offsets_s == {"rx-cam2": 0.0, "rx-cam1": 0.00956, "rx-cam3": 0.029894}
    assert chk.spread_ms == pytest.approx(29.894)
    assert chk.frame_ms == pytest.approx(33.3333, abs=1e-3)
    assert chk.within_a_frame
    d = chk.to_dict()
    assert d["views"] == {"rx-cam2": "cam0", "rx-cam1": "cam1", "rx-cam3": "cam2"}
    assert d["delays_ms"] == {"rx-cam1": 10.366, "rx-cam2": 0.806, "rx-cam3": 30.7}


def test_an_explicit_reference_gives_negative_offsets_for_earlier_cameras():
    chk = sidecar.offsets_from_delays({"a": 5_000_000, "b": 1_000_000, "c": 40_000_000}, ref="a")
    assert chk.order == ["a", "b", "c"]
    assert chk.offsets_s == {"a": 0.0, "b": -0.004, "c": 0.035}
    assert not chk.within_a_frame  # 39 ms spread at 30 fps
    with pytest.raises(sidecar.SidecarError):
        sidecar.offsets_from_delays({"a": 1}, ref="zz")
    with pytest.raises(sidecar.SidecarError):
        sidecar.offsets_from_delays({})


def test_find_clips_handles_the_flat_and_the_per_camera_layouts_and_skips_truth(tmp_path):
    flat = tmp_path / "flat"
    for node, d in (("rx-cam1", 2_801_267), ("rx-cam2", 2_842_967), ("rx-cam3", 1_721_067)):
        write_sidecar(flat / f"take_001_{node}.jsonl", node, "take_001", EDGE, EDGE + d)
    write_sidecar(flat / "take_002_rx-cam1.jsonl", "rx-cam1", "take_002", EDGE, EDGE + 1)
    write_sidecar(flat / "truth" / "take_001_rx-cam1.jsonl", "rx-cam1", "take_001", EDGE, EDGE)
    (flat / "rx-cam1_status.jsonl").write_text('{"_": "header"}\n', "utf-8")
    clip, sides = sidecar.find_clips(flat)
    assert clip == "take_001" and sorted(sides) == ["rx-cam1", "rx-cam2", "rx-cam3"]
    assert sides["rx-cam1"] == flat / "take_001_rx-cam1.jsonl"
    assert sidecar.find_clips(flat, "take_002")[1].keys() == {"rx-cam1"}
    with pytest.raises(sidecar.SidecarError, match="not found"):
        sidecar.find_clips(flat, "take_009")

    nested = tmp_path / "nested"
    for cam in ("rx-cam1", "rx-cam2"):
        p = nested / cam / f"trial3_{cam}.jsonl"
        write_sidecar(p, cam, "trial3", EDGE, EDGE + 1_000)
    assert sidecar.find_clips(nested)[0] == "trial3"
    with pytest.raises(sidecar.SidecarError, match="no sidecars"):
        sidecar.find_clips(tmp_path / "empty")


def test_find_clips_refuses_a_tie_without_a_clip_name(tmp_path):
    for clip in ("take_001", "take_002"):
        for node in ("rx-cam1", "rx-cam2"):
            write_sidecar(tmp_path / f"{clip}_{node}.jsonl", node, clip, EDGE, EDGE + 1)
    with pytest.raises(sidecar.SidecarError, match="same number of cameras"):
        sidecar.find_clips(tmp_path)


def test_check_session_reads_every_sidecar_and_reports_missing_cameras(tmp_path):
    for node, d in (("rx-cam1", 10_366_000), ("rx-cam2", 806_000), ("rx-cam3", 30_700_000)):
        write_sidecar(tmp_path / f"take_001_{node}.jsonl", node, "take_001", EDGE + 7, EDGE + 7 + d, fps=60)
    chk = sidecar.check_session(tmp_path, "take_001", expected=["rx-cam1", "rx-cam2", "rx-cam3", "rx-cam4"])
    assert chk.fps == 60 and chk.frame_ms == pytest.approx(16.667, abs=1e-3)
    assert chk.ref == "rx-cam2" and chk.missing == ["rx-cam4"]
    assert not chk.within_a_frame  # 29.9 ms spread is more than one 60 fps frame
    assert chk.offsets_s["rx-cam3"] == pytest.approx(0.029894)
