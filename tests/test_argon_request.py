"""How the Argon requests are built, with a fake client: no network."""
from types import SimpleNamespace

import pytest

from app import argon
from app.pipeline import apply_verifier, clean_parts
from app.schemas import ArgonLayout, TaskGraph

from .conftest import SID, make_check, make_graph, make_layout


def test_clips_inline_in_camera_order(samples, store, monkeypatch):
    monkeypatch.setenv("CELLPILOT_VIDEO_FPS", "2")
    parts = argon.video_parts(samples.session(SID), samples.dir(SID), store)
    assert len(parts) == 3
    assert all(p.inline_data.mime_type == "video/mp4" and p.video_metadata.fps == 2 for p in parts)
    assert parts[0].inline_data.data == (samples.dir(SID) / "cam0.mp4").read_bytes()


def test_large_clips_go_by_gs_uri_when_store_is_a_bucket(samples, monkeypatch):
    monkeypatch.setattr(argon, "INLINE_LIMIT_BYTES", 1000)
    uploaded = []

    class FakeBucket:
        is_bucket = True

        def media_uri(self, key, local):
            uploaded.append(key)
            return f"gs://bucket/{key}"

    parts = argon.video_parts(samples.session(SID), samples.dir(SID), FakeBucket())
    assert [p.file_data.file_uri for p in parts] == [f"gs://bucket/media/{SID}/cam{i}.mp4" for i in range(3)]
    assert parts[0].video_metadata.fps == 1.0
    assert uploaded == [f"media/{SID}/cam{i}.mp4" for i in range(3)]


def fake_client(answer, sent):
    usage = SimpleNamespace(prompt_token_count=12000, cached_content_token_count=8000,
                            thoughts_token_count=900, candidates_token_count=400)

    def generate_content(model, contents, config):
        sent.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=answer.model_dump_json(), parsed=None, usage_metadata=usage,
                               model_version="gemini-4-argon-test", candidates=[])

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


def test_calls_share_the_same_prefix_and_think_high(samples, store, monkeypatch):
    monkeypatch.delenv("CELLPILOT_MODEL", raising=False)
    sent = []
    session = samples.session(SID)
    media = argon.video_parts(session, samples.dir(SID), store)

    monkeypatch.setattr(argon, "client", lambda: fake_client(make_graph(), sent))
    a = argon.task_graph(session, media)
    monkeypatch.setattr(argon, "client", lambda: fake_client(make_check(), sent))
    d = argon.verify(session, media, a.parsed)

    steps = apply_verifier(a.parsed, d.parsed)
    monkeypatch.setattr(argon, "client", lambda: fake_client(make_layout(), sent))
    c = argon.layout(session, media, steps, clean_parts(a.parsed.parts))

    assert isinstance(a.parsed, TaskGraph) and len(d.parsed.steps) == 4 and isinstance(c.parsed, ArgonLayout)
    a_parts, d_parts, c_parts = sent[0]["contents"], sent[1]["contents"], sent[2]["contents"]
    assert a_parts[:4] == d_parts[:4] == c_parts[:4]  # three clips + shared context, identical for implicit caching
    assert "two-finger parallel gripper" in a_parts[4].text and "at least two views" in a_parts[4].text
    assert "[ymin, xmin, ymax, xmax]" in a_parts[4].text and "0 to 1000" in a_parts[4].text
    assert "You are checking another engineer's analysis" in d_parts[4].text and '"s1"' in d_parts[4].text
    assert "top-view layout" in c_parts[4].text and '"Pick bracket"' in c_parts[4].text
    assert '"bracket"' in c_parts[4].text and "pick_xy_m" in c_parts[4].text
    assert "offset_s" in a_parts[3].text and '"cam2"' in a_parts[3].text and "Franka FR3" in a_parts[3].text
    assert "Doosan M0609" in a_parts[3].text and "UR10e" in a_parts[3].text
    assert [s["config"].thinking_config.thinking_level.value for s in sent] == ["HIGH", "HIGH", "MEDIUM"]
    assert sent[0]["model"] == "gemini-4-argon"
    assert a.usage == {"prompt_tokens": 12000, "cached_tokens": 8000, "thoughts_tokens": 900,
                       "candidates_tokens": 400, "seconds": a.usage["seconds"],
                       "model_version": "gemini-4-argon-test"}


def test_bad_answer_raises_readable_error(monkeypatch):
    def generate_content(model, contents, config):
        return SimpleNamespace(text='{"steps": "nope"}', parsed=None, usage_metadata=None,
                               model_version=None, candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])

    monkeypatch.setattr(argon, "client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    with pytest.raises(argon.ArgonError, match="did not match the TaskGraph schema.*MAX_TOKENS"):
        argon._generate(["x"], TaskGraph, "HIGH")


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("CELLPILOT_MODEL", "gemini-3.8-flash")
    assert argon.model_name() == "gemini-3.8-flash"


def test_context_carries_the_station_brief_only_when_given():
    clips = [{"view": "cam0", "offset_s": 0.0, "duration_s": 10.0}, {"view": "cam1", "offset_s": 0.2, "duration_s": 10.0},
             {"view": "cam2", "offset_s": 0.45, "duration_s": 10.0}]
    plain = argon.context_text({"clips": clips})
    assert "Station" not in plain
    with_brief = argon.context_text({"clips": clips, "brief": "  A kitting   station.\nThree bins. "})
    assert "Station, as described by the people who filmed it: A kitting station. Three bins." in with_brief
    assert with_brief.replace("Station, as described by the people who filmed it: A kitting station. Three bins.\n", "") == plain
    long = argon.context_text({"clips": clips, "brief": "x" * 2000})
    assert "x" * 600 in long and "x" * 601 not in long


def test_notes_call_is_text_only_on_the_notes_model(samples, store, monkeypatch):
    from app.pipeline import build_result
    from .conftest import make_notes

    monkeypatch.delenv("CELLPILOT_NOTES_MODEL", raising=False)
    session = {**samples.session(SID), "brief": "A kitting station with three bins."}
    result = build_result(make_graph(), make_check(), make_layout())
    sent = []
    monkeypatch.setattr(argon, "client", lambda: fake_client(make_notes(), sent))
    e = argon.notes(session, result)
    assert e.parsed.step_notes[2].fixture == "tray nest"
    assert sent[0]["model"] == "gemini-3.8-flash"
    assert sent[0]["config"].thinking_config is None
    parts = sent[0]["contents"]
    assert len(parts) == 1 and parts[0].text  # no clips: the notes are written from the result
    text = parts[0].text
    assert "A kitting station with three bins." in text
    assert "never change a verdict" in text and '"s1"' in text and '"Pick bracket"' in text
    assert '"xArm 6"' in text and "payback_months" in text and "for_the_owner" in text
    assert "alternatives" not in text
    monkeypatch.setenv("CELLPILOT_NOTES_MODEL", "gemini-2.5-flash")
    argon.notes(session, result)
    assert sent[1]["model"] == "gemini-2.5-flash"
