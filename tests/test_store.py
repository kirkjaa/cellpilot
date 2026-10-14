import pytest

from app.limits import DailyLimit, today
from app.store import LocalStore, Samples, make_store

from .conftest import SID


def test_local_round_trip(store):
    assert store.read_json("sessions/x/analyses/a1.json") is None
    store.write_json("sessions/x/analyses/a1.json", {"id": "a1", "text": "Grüße"})
    store.write_json("sessions/x/analyses/a2.json", {"id": "a2"})
    store.write_json("sessions/x/argon/a1.json", {"raw": True})
    assert store.read_json("sessions/x/analyses/a1.json") == {"id": "a1", "text": "Grüße"}
    assert store.list_keys("sessions/x/analyses/") == ["sessions/x/analyses/a1.json", "sessions/x/analyses/a2.json"]
    assert store.list_keys("sessions/none/") == []
    store.write_json("sessions/x/analyses/a1.json", {"id": "a1", "v": 2})
    assert store.read_json("sessions/x/analyses/a1.json")["v"] == 2


def test_local_store_refuses_keys_outside(store):
    with pytest.raises(ValueError):
        store.write_json("../escape.json", {})


def test_make_store_picks_local_for_a_path(tmp_path):
    assert isinstance(make_store(str(tmp_path)), LocalStore)


def test_samples_are_read_only_lookups(samples):
    assert samples.ids() == [SID]
    assert samples.session(SID)["synthetic"] is True
    assert samples.clip_path(SID, "cam0.mp4").is_file()
    assert samples.clip_path(SID, "session.json") is None
    assert samples.clip_path("../x", "cam0.mp4") is None
    assert samples.analyses(SID) == []


def test_daily_limit(store):
    limit = DailyLimit(store, cap=2)
    assert limit.left() == 2
    assert limit.take() and limit.take()
    assert not limit.take()
    assert limit.left() == 0
    assert store.read_json(f"limits/{today()}.json") == {"count": 2}


def test_daily_limit_reads_env(store, monkeypatch):
    monkeypatch.setenv("CELLPILOT_DAILY_LIVE_RUNS", "3")
    assert DailyLimit(store).cap == 3
    monkeypatch.delenv("CELLPILOT_DAILY_LIVE_RUNS")
    assert DailyLimit(store).cap == 10
