"""Bridge configuration: which cameras, which controller, where the files go.

A JSON file (YAML too, when PyYAML is installed), by default `capture.json` in the repository
root, or the path in `CELLPILOT_CAPTURE_CONFIG` / `--config`:

    {
      "cameras": [
        {"id": "rx-cam1", "url": "ws://127.0.0.1:5101"},
        {"id": "rx-cam2", "url": "ws://127.0.0.1:5102"},
        {"id": "rx-cam3", "url": "ws://127.0.0.1:5103"}
      ],
      "controller": {"url": "ws://127.0.0.1:5000"},
      "offload_dir": "D:/cellpilot-verify/capture/rec",
      "samples_dir": "samples",
      "preset": "1080p30",
      "allow_lan": false
    }

`offload_dir` is where a take's clips and sidecars appear on this PC: the emulator writes them
there itself; with the real cameras it is the folder NODAL Capture offloads into. `samples_dir`
is where "Make session" writes a session folder the main app serves (relative paths are anchored
at the repository root). `allow_lan` must be true before the bridge will connect to anything but
localhost: the first real run happens with the founder present, so the default refuses it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "capture.json"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    pass


@dataclass
class Camera:
    id: str
    url: str


@dataclass
class ConsoleConfig:
    """A rig behind NODAL's M1 emulator console: the cameras are reached through its proxied controller."""
    url: str                                   # https://m1.ques8.com, or http://127.0.0.1:5800 for a local console
    username: str = ""
    password: str = field(default="", repr=False)
    rig: dict = field(default_factory=dict)    # what to start when the user's rig is down: cameras, preset, scene
    preview_width: int = 640
    max_take_seconds: float = 60.0             # a public page should not tie the rig up for long

    def ws_controller_url(self) -> str:
        u = urlparse(self.url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return f"{scheme}://{u.netloc}/ws/controller?preview={int(self.preview_width)}"

    def public(self) -> dict:
        return {"url": self.url, "username": self.username, "rig": dict(self.rig), "preview_width": self.preview_width,
                "max_take_seconds": self.max_take_seconds}


@dataclass
class Config:
    cameras: list[Camera] = field(default_factory=list)
    controller_url: str = "ws://127.0.0.1:5000"
    offload_dir: Path = ROOT / "capture_out"
    samples_dir: Path = ROOT / "samples"
    preset: str = "1080p30"
    allow_lan: bool = False
    console: Optional[ConsoleConfig] = None
    path: Optional[Path] = None

    @property
    def node_ids(self) -> list[str]:
        return [c.id for c in self.cameras]

    def public(self) -> dict:
        """What the page may see (no secrets exist here, but keep it explicit)."""
        return {"cameras": [{"id": c.id, "url": c.url} for c in self.cameras],
                "controller_url": self.controller_url, "offload_dir": str(self.offload_dir),
                "samples_dir": str(self.samples_dir), "preset": self.preset, "allow_lan": self.allow_lan,
                "console": self.console.public() if self.console else None,
                "path": str(self.path) if self.path else None}


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOOPBACK


def parse(d: dict, base: Path = ROOT, path: Optional[Path] = None) -> Config:
    if not isinstance(d, dict):
        raise ConfigError("the config must be an object")
    cams = []
    seen = set()
    for i, c in enumerate(d.get("cameras") or []):
        if not isinstance(c, dict) or not c.get("id") or not c.get("url"):
            raise ConfigError(f"cameras[{i}] needs an id and a ws:// url")
        cid, url = str(c["id"]), str(c["url"])
        if not ID_RE.match(cid):
            raise ConfigError(f"camera id {cid!r}: letters, digits, dashes, 40 at most")
        if urlparse(url).scheme not in ("ws", "wss"):
            raise ConfigError(f"camera {cid}: url must start with ws://")
        if cid in seen:
            raise ConfigError(f"camera id {cid!r} appears twice")
        seen.add(cid)
        cams.append(Camera(cid, url))
    console = None
    cblock = d.get("console")
    if cblock:
        if not isinstance(cblock, dict) or urlparse(str(cblock.get("url") or "")).scheme not in ("http", "https"):
            raise ConfigError("console.url must start with http:// or https://")
        username = str(cblock.get("username") or os.environ.get(str(cblock.get("username_env") or "M1SIM_USER"), ""))
        password = str(cblock.get("password") or os.environ.get(str(cblock.get("password_env") or "M1SIM_PASSWORD"), ""))
        rig = cblock.get("rig") if isinstance(cblock.get("rig"), dict) else {}
        try:
            width = int(cblock.get("preview_width", 640))
            max_take = float(cblock.get("max_take_seconds", 60))
        except (TypeError, ValueError):
            raise ConfigError("console.preview_width and console.max_take_seconds must be numbers") from None
        console = ConsoleConfig(url=str(cblock["url"]).rstrip("/"), username=username, password=password,
                                rig=dict(rig), preview_width=width, max_take_seconds=max_take)
    ctrl = d.get("controller") or {}
    ctrl_url = str(ctrl.get("url") if isinstance(ctrl, dict) else ctrl) if ctrl else "ws://127.0.0.1:5000"
    if console is not None:
        ctrl_url = console.ws_controller_url()
    if urlparse(ctrl_url).scheme not in ("ws", "wss"):
        raise ConfigError("controller.url must start with ws://")
    allow_lan = bool(d.get("allow_lan", False))
    if not allow_lan and console is None:  # a console is reached over HTTPS on purpose; it is not a LAN camera
        remote = [u for u in [c.url for c in cams] + [ctrl_url] if not _is_loopback(u)]
        if remote:
            raise ConfigError("refusing non-localhost addresses while allow_lan is false: " + ", ".join(remote)
                              + ". Set \"allow_lan\": true for the real rig, with the founder present.")

    def folder(key: str, default: Path) -> Path:
        v = d.get(key)
        if not v:
            return default
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else (base / p)

    preset = str(d.get("preset") or "1080p30")
    return Config(cameras=cams, controller_url=ctrl_url, offload_dir=folder("offload_dir", ROOT / "capture_out"),
                  samples_dir=folder("samples_dir", ROOT / "samples"), preset=preset, allow_lan=allow_lan,
                  console=console, path=path)


def load(path: Optional[str | Path] = None) -> Config:
    """Read the config file; a missing default file gives an empty config (no cameras)."""
    p = Path(path or os.environ.get("CELLPILOT_CAPTURE_CONFIG") or DEFAULT_PATH)
    if not p.is_file():
        if path is None and not os.environ.get("CELLPILOT_CAPTURE_CONFIG"):
            return Config(path=None)
        raise ConfigError(f"config file not found: {p}")
    text = p.read_text("utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional
        except ImportError:
            raise ConfigError("a YAML config needs PyYAML; use JSON or pip install pyyaml") from None
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    return parse(data, base=ROOT, path=p.resolve())
