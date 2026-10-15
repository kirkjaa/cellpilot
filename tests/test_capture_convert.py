"""The converter (capture/convert.py) on tiny real clips made with ffmpeg."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from capture import convert

from .test_capture_sidecar import EDGE, write_sidecar

needs_ffmpeg = pytest.mark.skipif(not convert.ffmpeg_available(), reason="ffmpeg and ffprobe are not on PATH")


def make_clip(path: Path, seconds: float = 1.0, fps: int = 30, size: str = "96x54") -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
                    "-t", f"{seconds}", "-c:v", "mjpeg", "-q:v", "5", str(path)], check=True)


@pytest.fixture
def take_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rec" / "kitting"
    d.mkdir(parents=True)
    for node, delay in (("rx-cam1", 10_366_000), ("rx-cam2", 806_000), ("rx-cam3", 30_700_000)):
        write_sidecar(d / f"take_001_{node}.jsonl", node, "take_001", EDGE, EDGE + delay, session="kitting")
        make_clip(d / f"take_001_{node}.mp4")
    return d


@needs_ffmpeg
def test_make_session_writes_720p_cfr_clips_with_the_sidecar_offsets(take_dir: Path, tmp_path: Path):
    out = tmp_path / "samples" / "kitting-01"
    log = []
    session = convert.make_session(take_dir, out, "kitting-01", "Kitting take", progress=log.append)
    assert session["id"] == "kitting-01" and session["synthetic"] is False and session["kind"] == "sample"
    views = {c["view"]: c for c in session["clips"]}
    assert list(views) == ["cam0", "cam1", "cam2"]
    assert views["cam0"]["offset_s"] == 0.0 and views["cam1"]["offset_s"] == 0.0096 and views["cam2"]["offset_s"] == 0.0299
    for c in session["clips"]:
        assert c["height"] == 720 and c["fps"] == 30.0 and (out / c["file"]).stat().st_size > 1000
        assert c["duration_s"] == pytest.approx(1.0, abs=0.1)
    on_disk = json.loads((out / "session.json").read_text("utf-8"))
    assert on_disk == session
    sources = json.loads((out / "sources.json").read_text("utf-8"))
    assert sources["clip"] == "take_001" and sources["cameras"]["cam0"]["node_id"] == "rx-cam2"
    assert sources["check"]["within_a_frame"] is True
    assert any("cam0 <- rx-cam2" in line for line in log)
    # the output is H.264 in yuv420p with the moov atom first (faststart) and no audio
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type,pix_fmt", "-of", "json",
                            str(out / "cam0.mp4")], check=True, capture_output=True, text=True).stdout
    streams = json.loads(probe)["streams"]
    assert [s["codec_type"] for s in streams] == ["video"] and streams[0]["codec_name"] == "h264"
    assert streams[0]["pix_fmt"] in ("yuv420p", "yuvj420p")  # ffprobe tags a full-range MJPEG source with the j
    head = (out / "cam0.mp4").read_bytes()[:64]
    assert b"moov" in head or b"ftyp" in head


@needs_ffmpeg
def test_make_session_can_pick_the_reference_and_trim(take_dir: Path, tmp_path: Path):
    out = tmp_path / "s2"
    session = convert.make_session(take_dir, out, "s2", "Ref cam1", ref="rx-cam1", trim_start=0.2)
    views = {c["view"]: c for c in session["clips"]}
    srcs = json.loads((out / "sources.json").read_text("utf-8"))["cameras"]
    assert srcs["cam0"]["node_id"] == "rx-cam1" and srcs["cam1"]["node_id"] == "rx-cam2"
    assert views["cam1"]["offset_s"] == -0.0096  # cam2 (rx-cam2) started before the chosen reference
    assert views["cam0"]["duration_s"] == pytest.approx(0.8, abs=0.1)


def test_make_session_refuses_bad_input(take_dir: Path, tmp_path: Path, monkeypatch):
    with pytest.raises(convert.ConvertError, match="session id"):
        convert.make_session(take_dir, tmp_path / "o", "bad id!", "x")
    with pytest.raises(convert.ConvertError, match="name"):
        convert.make_session(take_dir, tmp_path / "o", "ok", "  ")
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: True)
    (take_dir / "take_001_rx-cam3.jsonl").unlink()
    with pytest.raises(convert.ConvertError, match="exactly three cameras"):
        convert.make_session(take_dir, tmp_path / "o", "ok", "x")
    with pytest.raises(convert.ConvertError, match="not found"):
        convert.make_session(take_dir, tmp_path / "o", "ok", "x", clip="take_777")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(convert.ConvertError, match="no sidecars"):
        convert.make_session(empty, tmp_path / "o", "ok", "x")
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: False)
    with pytest.raises(convert.ConvertError, match="ffmpeg"):
        convert.make_session(take_dir, tmp_path / "o", "ok", "x")


@needs_ffmpeg
def test_missing_mp4_next_to_a_sidecar_is_reported(take_dir: Path, tmp_path: Path):
    (take_dir / "take_001_rx-cam2.mp4").unlink()
    with pytest.raises(convert.ConvertError, match="missing take_001_rx-cam2.mp4"):
        convert.make_session(take_dir, tmp_path / "o", "ok", "x")


@needs_ffmpeg
def test_cli_converts_a_folder(take_dir: Path, tmp_path: Path, capsys):
    out = tmp_path / "cli"
    convert.main([str(take_dir), str(out), "--id", "cli-01", "--name", "CLI take", "--clip", "take_001"])
    assert (out / "session.json").is_file() and shutil.which("ffmpeg")
    printed = capsys.readouterr().out
    assert "cam2: offset +0.0299 s" in printed


def test_make_session_records_the_job_and_the_brief(take_dir: Path, tmp_path: Path):
    out = tmp_path / "s4"
    session = convert.make_session(take_dir, out, "s4", "Kitting", scenario="kitting",
                                   brief="A kitting station.  Three bins,\n one tray.")
    assert session["scenario"] == "kitting"
    assert session["brief"] == "A kitting station. Three bins, one tray."
    on_disk = json.loads((out / "session.json").read_text(encoding="utf-8"))
    assert on_disk["scenario"] == "kitting" and on_disk["brief"] == session["brief"]
    plain = convert.make_session(take_dir, tmp_path / "s5", "s5", "Plain")
    assert plain["scenario"] == "" and plain["brief"] == ""
