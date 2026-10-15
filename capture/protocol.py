"""The camera protocol as seen on the wire, written from its description.

Text frames carry one JSON envelope each:

    {"type": "cmd.ping", "payload": {}, "id": "<32 hex>", "wall": 1760000000.1, "mono": 123.4, "v": "4.0.0"}

`type` is required; the rest have defaults. An ack echoes the request id; events carry a fresh
one. Binary frames are preview pictures: a one-byte node-id length, the id in ASCII, the frame's
monotonic timestamp (u64 ns, big-endian), the driver sequence number (u32) and the JPEG bytes.
"""
from __future__ import annotations

import json
import struct
import time
import uuid
from dataclasses import dataclass, field

PROTOCOL_VERSION = "4.0.0"

# Commands a camera node answers.
CMD_PING = "cmd.ping"
CMD_GET_STATE = "cmd.get_state"
CMD_CONFIGURE = "cmd.configure"
CMD_START_PREVIEW = "cmd.start_preview"
CMD_STOP_PREVIEW = "cmd.stop_preview"
CMD_ARM = "cmd.arm"
CMD_STOP = "cmd.stop"
# Commands the rig controller answers (it owns the RF dongle).
CMD_FIRE = "cmd.fire"
CMD_LIST_NODES = "cmd.list_nodes"

ACK_OK = "ack.ok"
ACK_BUSY = "ack.busy"
ACK_ERROR = "ack.error"

EVT_HEARTBEAT = "evt.heartbeat"
EVT_RF_EDGE = "evt.rf_edge"
EVT_RECORDING_STARTED = "evt.recording_started"
EVT_RECORDING_STOPPED = "evt.recording_stopped"
EVT_CAPTURE_RESTARTED = "evt.capture_restarted"
EVT_ERROR = "evt.error"
EVT_NODE_JOINED = "evt.node_joined"
EVT_NODE_LEFT = "evt.node_left"

# Presets the cameras know; the bridge only offers the ones that make sense for a bench.
PRESETS = ("1080p30", "720p30", "1080p60", "720p60")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Envelope:
    type: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    wall: float = field(default_factory=time.time)
    mono: float = field(default_factory=time.monotonic)
    v: str = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "payload": self.payload, "id": self.id,
                           "wall": self.wall, "mono": self.mono, "v": self.v}, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Envelope":
        d = json.loads(raw)
        if not isinstance(d, dict) or not isinstance(d.get("type"), str):
            raise ValueError("bad envelope: no type")
        payload = d.get("payload")
        return cls(type=d["type"], payload=payload if isinstance(payload, dict) else {},
                   id=str(d.get("id") or new_id()),
                   wall=float(d.get("wall") or time.time()), mono=float(d.get("mono") or time.monotonic()),
                   v=str(d.get("v") or PROTOCOL_VERSION))

    @property
    def is_ack(self) -> bool:
        return self.type.startswith("ack.")

    @property
    def is_event(self) -> bool:
        return self.type.startswith("evt.")

    @property
    def ok(self) -> bool:
        return self.type == ACK_OK

    @property
    def reason(self) -> str:
        """The error text of a failed ack ('' for a good one)."""
        if self.ok:
            return ""
        r = self.payload.get("reason")
        return str(r) if r else self.type


def compatible(version: str) -> bool:
    """Same major version as ours."""
    return isinstance(version, str) and version.split(".")[0] == PROTOCOL_VERSION.split(".")[0]


@dataclass(frozen=True)
class PreviewFrame:
    node_id: str
    mono_ns: int
    seq: int
    jpeg: bytes


def decode_preview(data: bytes) -> PreviewFrame:
    """Split a binary preview frame; raises ValueError when it is not one."""
    if len(data) < 14:
        raise ValueError("preview frame too short")
    n = data[0]
    if len(data) < 1 + n + 12:
        raise ValueError("preview frame truncated")
    try:
        node_id = data[1:1 + n].decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"preview node id is not ASCII: {e}") from None
    mono_ns, seq = struct.unpack(">QI", data[1 + n:1 + n + 12])
    jpeg = data[1 + n + 12:]
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("preview payload is not a JPEG")
    return PreviewFrame(node_id, mono_ns, seq, jpeg)


def encode_preview(node_id: str, mono_ns: int, seq: int, jpeg: bytes) -> bytes:
    """The inverse, for the fake node in the tests."""
    nid = node_id.encode("ascii")
    if len(nid) > 255:
        raise ValueError("node id too long")
    return bytes([len(nid)]) + nid + struct.pack(">QI", mono_ns, seq) + jpeg
