"""The envelope and the binary preview frame (capture/protocol.py), and the config rules."""
import json
import struct

import pytest

from capture import config, protocol
from capture.protocol import Envelope


def test_envelope_round_trip_and_defaults():
    env = Envelope(protocol.CMD_PING)
    raw = env.to_json()
    d = json.loads(raw)
    assert d["type"] == "cmd.ping" and d["payload"] == {} and d["v"] == "4.0.0"
    assert len(d["id"]) == 32 and int(d["id"], 16) >= 0
    assert isinstance(d["wall"], float) and isinstance(d["mono"], float)
    back = Envelope.from_json(raw)
    assert back.id == env.id and back.type == env.type and back.v == "4.0.0"
    assert "\n" not in raw and ": " not in raw  # compact

    # A reply with only the required key is accepted and gets defaults.
    e = Envelope.from_json(b'{"type": "ack.ok"}')
    assert e.ok and e.payload == {} and e.v == "4.0.0" and e.is_ack and not e.is_event


def test_bad_envelopes_and_ack_reasons():
    with pytest.raises(ValueError):
        Envelope.from_json('{"payload": {}}')
    with pytest.raises(ValueError):
        Envelope.from_json("[1, 2]")
    with pytest.raises(ValueError):
        Envelope.from_json("not json")
    err = Envelope.from_json('{"type": "ack.error", "payload": {"reason": "already armed; call stop() first"}}')
    assert not err.ok and err.reason == "already armed; call stop() first"
    busy = Envelope.from_json('{"type": "ack.busy", "payload": {}}')
    assert busy.reason == "ack.busy"
    assert Envelope.from_json('{"type": "ack.ok", "payload": "junk"}').payload == {}
    assert Envelope.from_json('{"type": "evt.heartbeat"}').is_event


def test_version_compatibility_is_by_major():
    assert protocol.compatible("4.0.0") and protocol.compatible("4.1.0-draft")
    assert not protocol.compatible("5.0.0") and not protocol.compatible("") and not protocol.compatible(None)


def test_preview_frame_layout():
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"
    data = protocol.encode_preview("rx-cam1", 48_330_903_484_990, 4211, jpeg)
    assert data[0] == 7 and data[1:8] == b"rx-cam1"
    assert struct.unpack(">QI", data[8:20]) == (48_330_903_484_990, 4211)
    f = protocol.decode_preview(data)
    assert (f.node_id, f.mono_ns, f.seq, f.jpeg) == ("rx-cam1", 48_330_903_484_990, 4211, jpeg)
    with pytest.raises(ValueError):
        protocol.decode_preview(data[:10])
    with pytest.raises(ValueError):
        protocol.decode_preview(b"\x02ab" + b"\x00" * 12 + b"PNG!")  # not a JPEG
    with pytest.raises(ValueError):
        protocol.encode_preview("x" * 256, 0, 0, jpeg)


def test_config_parses_json_and_refuses_lan_by_default(tmp_path):
    cfg = config.parse({"cameras": [{"id": "rx-cam1", "url": "ws://127.0.0.1:5101"}],
                        "controller": {"url": "ws://localhost:5000"}, "offload_dir": "takes", "samples_dir": "/abs/s"})
    assert cfg.node_ids == ["rx-cam1"] and cfg.controller_url == "ws://localhost:5000"
    assert cfg.offload_dir == config.ROOT / "takes" and str(cfg.samples_dir).replace("\\", "/").endswith("/abs/s")
    assert cfg.public()["allow_lan"] is False
    with pytest.raises(config.ConfigError, match="allow_lan"):
        config.parse({"cameras": [{"id": "rx-cam1", "url": "ws://192.168.1.42:5100"}]})
    with pytest.raises(config.ConfigError, match="allow_lan"):
        config.parse({"controller": {"url": "ws://192.168.1.40:5000"}})
    lan = config.parse({"cameras": [{"id": "rx-cam1", "url": "ws://192.168.1.42:5100"}], "allow_lan": True})
    assert lan.allow_lan and lan.cameras[0].url == "ws://192.168.1.42:5100"
    for bad in ({"cameras": [{"id": "a b", "url": "ws://127.0.0.1:1"}]}, {"cameras": [{"id": "a", "url": "http://x"}]},
                {"cameras": [{"id": "a", "url": "ws://127.0.0.1:1"}, {"id": "a", "url": "ws://127.0.0.1:2"}]}, []):
        with pytest.raises(config.ConfigError):
            config.parse(bad)
    p = tmp_path / "capture.json"
    p.write_text(json.dumps({"cameras": [{"id": "c", "url": "ws://127.0.0.1:9"}]}), "utf-8")
    assert config.load(p).path == p.resolve() and config.load(p).node_ids == ["c"]
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "missing.json")
