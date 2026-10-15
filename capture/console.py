"""Console mode: the rig behind NODAL's M1 emulator console (`m1sim console --auth`), reached over HTTPS.

The console exposes one rig per user: its controller as a WebSocket at /ws/controller, which relays the
cameras' heartbeats, events and preview frames; the sessions that rig recorded; and each session as a
zip. The bridge logs in with a username and password from the environment, starts the user's rig if
none is running, talks to the controller exactly as it does on a LAN (cmd.fire, cmd.start_preview, ...)
and, once a take has stopped, fetches its clips and sidecars as a zip into the offload folder, where
the usual offset check and "Make session" take over. Nothing here reaches a camera directly, so each
camera is a VirtualNode fed by the controller's relay.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from .client import Link, LinkError
from .config import Config
from .protocol import (CMD_LIST_NODES, CMD_START_PREVIEW, EVT_HEARTBEAT, EVT_NODE_JOINED, EVT_NODE_LEFT,
                       EVT_RECORDING_STARTED, EVT_RECORDING_STOPPED, EVT_RF_EDGE, Envelope, PreviewFrame)
from .rig import OFFLOAD_WAIT_S, Rig, Take

log = logging.getLogger("capture.console")

HEARTBEAT_ONLINE_S = 4.0     # a camera is online while the controller relayed its heartbeat this recently
RETRY_S = 10.0               # between attempts to log in and find the rig
RIG_START_WAIT_S = 45.0      # for the console to bring a fresh rig's cameras up
WARMUP_SECONDS = 3.0         # a first take on a freshly started rig, so a visitor's first take is never the rig's first
WARMUP_WAIT_S = 20.0         # for the cameras to come online before the warm-up
HTTP_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 300.0


class VirtualNode:
    """A camera known only through the controller's relay: the attributes the bridge reads off a Link."""

    kind = "node"

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.url = "via the console's controller"
        self.last_state: Optional[dict] = None
        self.last_heartbeat_at: Optional[float] = None
        self.last_preview: Optional[PreviewFrame] = None
        self.preview_at: Optional[float] = None
        self.preview_count = 0
        self.preview_cond: Optional[asyncio.Condition] = None
        self.connects = 0
        self.connected_at: Optional[float] = None
        self.left = False

    @property
    def online(self) -> bool:
        age = self.heartbeat_age_s()
        return (not self.left) and age is not None and age <= HEARTBEAT_ONLINE_S

    @property
    def error(self) -> Optional[str]:
        if self.online:
            return None
        if self.left:
            return "left the rig"
        if self.last_heartbeat_at is None:
            return "no heartbeat from the console yet"
        return f"no heartbeat for {self.heartbeat_age_s()} s"

    def heartbeat_age_s(self) -> Optional[float]:
        return None if self.last_heartbeat_at is None else round(time.time() - self.last_heartbeat_at, 1)

    def preview_age_s(self) -> Optional[float]:
        return None if self.preview_at is None else round(time.time() - self.preview_at, 1)

    async def cmd(self, type_: str, timeout: float = 6.0, **payload) -> Envelope:
        raise LinkError(f"{self.node_id}: cameras are reached through the console's controller, not directly")

    async def ping(self, timeout: float = 4.0) -> dict:
        raise LinkError(f"{self.node_id}: cameras are reached through the console's controller, not directly")


class ConsoleHttp:
    """Blocking HTTP to the console with its login cookie. Call it from a thread."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.username, self.password = username, password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _open(self, method: str, path: str, body: Optional[dict] = None, timeout: float = HTTP_TIMEOUT_S):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        return self.opener.open(req, timeout=timeout)

    def login(self) -> None:
        if not self.username or not self.password:
            raise LinkError("console credentials missing: set M1SIM_USER and M1SIM_PASSWORD")
        try:
            with self._open("POST", "/api/login", {"username": self.username, "password": self.password}) as r:
                json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise LinkError(f"console login failed: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise LinkError(f"console unreachable: {e.reason}") from None

    def json(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """One JSON call; logs in again once if the cookie has expired."""
        for attempt in range(2):
            try:
                with self._open(method, path, body) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.login()
                    continue
                detail = ""
                try:
                    detail = str(json.loads(e.read() or b"{}").get("error") or "")
                except Exception:  # noqa: BLE001
                    detail = ""
                raise LinkError(f"console {method} {path}: HTTP {e.code} {detail}".strip()) from None
            except urllib.error.URLError as e:
                raise LinkError(f"console unreachable: {e.reason}") from None
        raise LinkError(f"console {method} {path}: not authorised")

    def download(self, path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._open("GET", path, timeout=DOWNLOAD_TIMEOUT_S) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as e:
            raise LinkError(f"console download {path}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise LinkError(f"console unreachable: {e.reason}") from None

    def cookie_header(self) -> dict:
        pairs = [f"{c.name}={c.value}" for c in self.jar]
        return {"Cookie": "; ".join(pairs)} if pairs else {}


class ConsoleRig(Rig):
    """The bridge's rig when the cameras live behind an emulator console."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        if cfg.console is None:
            raise ValueError("ConsoleRig needs a console block in the config")
        self.console = cfg.console
        self.http = ConsoleHttp(self.console.url, self.console.username, self.console.password)
        self.controller = Link("controller", cfg.controller_url, kind="controller", on_event=self._controller_event,
                               on_connect=self._controller_connect, on_preview=self._preview_frame,
                               headers=self.http.cookie_header)
        self.console_state: dict = {"url": self.console.url, "user": self.console.username, "logged_in": False,
                                    "rig_running": False, "rig": None, "error": None}
        self._boot: Optional[asyncio.Task] = None
        self.started_rig = False      # this bridge started the rig (so it should warm it up)
        self.warmup_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._boot = asyncio.create_task(self._bootstrap(), name="console-bootstrap")

    async def stop(self) -> None:
        boot, self._boot = self._boot, None
        if boot is not None:
            boot.cancel()
            try:
                await boot
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await super().stop()

    async def _bootstrap(self) -> None:
        """Log in, make sure the user's rig is up, then keep the controller link; retry until it works."""
        while True:
            try:
                await asyncio.to_thread(self.http.login)
                self.console_state["logged_in"] = True
                await asyncio.to_thread(self._ensure_rig)
                self.console_state["error"] = None
                self.controller.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the page shows the reason; the app keeps running
                self.console_state["error"] = str(e) if isinstance(e, LinkError) else f"{type(e).__name__}: {e}"
                log.warning("console: %s; retrying in %g s", self.console_state["error"], RETRY_S)
                await asyncio.sleep(RETRY_S)

    @staticmethod
    def _rig_nodes(state: dict) -> list[str]:
        rig = state.get("rig") or {}
        out = []
        for n in rig.get("nodes") or []:
            nid = n.get("node_id") or n.get("id") if isinstance(n, dict) else n
            if nid:
                out.append(str(nid))
        return out

    def _ensure_rig(self) -> None:
        state = self.http.json("GET", "/api/state")
        nodes = self._rig_nodes(state)
        if not nodes:
            spec = dict(self.console.rig) or {"cameras": 3}
            self.http.json("POST", "/api/rig/start", spec)
            self.started_rig = True
            deadline = time.monotonic() + RIG_START_WAIT_S
            while time.monotonic() < deadline:
                time.sleep(2.0)
                state = self.http.json("GET", "/api/state")
                nodes = self._rig_nodes(state)
                if nodes:
                    break
            if not nodes:
                raise LinkError("the console did not bring the rig up in time")
        rig = state.get("rig") or {}
        spec = rig.get("spec") or {}
        self.console_state.update(rig_running=True, rig={
            "nodes": nodes, "preset": spec.get("preset"), "scene": spec.get("scene"), "cameras": spec.get("cameras")})
        for nid in nodes:
            self._virtual(nid)

    # ---------------------------------------------------------------- the relay

    def _virtual(self, nid: str) -> VirtualNode:
        v = self.nodes.get(nid)
        if v is None:
            v = VirtualNode(nid)
            v.preview_cond = asyncio.Condition()
            self.nodes[nid] = v
        return v  # type: ignore[return-value]

    async def _controller_connect(self, link: Link) -> None:
        try:
            r = await link.cmd(CMD_LIST_NODES, timeout=6.0)
            for n in r.payload.get("nodes") or []:
                nid = (n.get("node_id") or n.get("id")) if isinstance(n, dict) else n
                if nid:
                    self._virtual(str(nid))
        except LinkError as e:
            log.warning("console controller: list_nodes failed: %s", e)
        if self.preview.get("on"):
            try:
                await link.cmd(CMD_START_PREVIEW, timeout=6.0, fps=self.preview["fps"], gray=False,
                               max_width=self.preview["max_width"])
            except LinkError as e:
                log.warning("console controller: start_preview failed: %s", e)
        if self.started_rig and self.warmup_task is None:
            self.warmup_task = asyncio.create_task(self._warmup(), name="console-warmup")

    async def _warmup(self) -> None:
        """A freshly started emulator rig has missed a camera on its very first take (the recorder's first clip);
        record a short take ourselves so the first take a visitor fires is not the rig's first."""
        deadline = time.monotonic() + WARMUP_WAIT_S
        want = len(self.nodes) or 1
        while time.monotonic() < deadline and sum(1 for v in self.nodes.values() if v.online) < want:
            await asyncio.sleep(0.5)
        try:
            take = await self.start_take("cellpilot-warmup", WARMUP_SECONDS)
            log.info("console: warm-up take %s fired on the fresh rig", take.clip)
        except Exception as e:  # noqa: BLE001 - a failed warm-up is only a missed precaution
            log.warning("console: warm-up take failed: %s", e)

    async def _controller_event(self, link: Link, env: Envelope) -> None:
        p = env.payload or {}
        nid = p.get("node_id")
        nid = str(nid) if nid else None
        if env.type == EVT_HEARTBEAT and nid:
            v = self._virtual(nid)
            v.last_state, v.last_heartbeat_at, v.left = p, time.time(), False
            return
        if env.type == EVT_NODE_JOINED and nid:
            self._virtual(nid).left = False
            return
        if env.type == EVT_NODE_LEFT and nid:
            v = self.nodes.get(nid)
            if v is not None:
                v.left = True  # type: ignore[attr-defined]
            return
        if env.type in (EVT_RECORDING_STARTED, EVT_RECORDING_STOPPED, EVT_RF_EDGE) and nid:
            await self._node_event(self._virtual(nid), env)  # type: ignore[arg-type]
            return
        await super()._controller_event(link, env)

    async def _preview_frame(self, link: Link, frame: PreviewFrame) -> None:
        v = self._virtual(frame.node_id)
        v.last_preview, v.preview_at = frame, time.time()
        v.preview_count += 1
        if v.preview_cond is not None:
            async with v.preview_cond:
                v.preview_cond.notify_all()

    # ---------------------------------------------------------------- commands

    async def fan_out(self, type_: str, timeout: float, **payload) -> dict[str, dict]:
        """The cameras sit behind the controller: one command to it, reported for every camera."""
        try:
            r = await self.controller.cmd(type_, timeout=timeout, **payload)
            res = {"ok": r.ok, "reason": r.reason or None, "type": r.type, "payload": r.payload}
        except LinkError as e:
            res = {"ok": False, "reason": str(e)}
        return {nid: dict(res) for nid in self.nodes} or {"controller": res}

    async def ping_all(self) -> dict[str, dict]:
        return {nid: {"ok": v.online, "via": "console", "heartbeat_age_s": v.heartbeat_age_s(),
                      "preview_age_s": v.preview_age_s()} for nid, v in self.nodes.items()}

    # ---------------------------------------------------------------- the clips

    async def _offload_check(self, take: Take) -> None:
        deadline = time.monotonic() + OFFLOAD_WAIT_S + 20.0
        last = "not listed yet"
        while True:
            try:
                await asyncio.to_thread(self._fetch_take, take)
            except LookupError as e:
                last = str(e)
            except Exception as e:  # noqa: BLE001
                last = str(e) if isinstance(e, LinkError) else f"{type(e).__name__}: {e}"
                log.warning("take %s: fetching the clips from the console failed: %s", take.id, last)
            else:
                await super()._offload_check(take)  # the files are local now: the usual offset check
                return
            if time.monotonic() >= deadline:
                take.check_error = f"the console did not provide the clips of {take.clip} in time ({last})"
                return
            await asyncio.sleep(2.0)

    def _fetch_take(self, take: Take) -> None:
        """Download the take's session zip from the console and unpack this take's files into the offload folder."""
        listing = self.http.json("GET", "/api/sessions")
        hit = next((s for s in listing.get("sessions") or []
                    if s.get("session") == take.session and take.clip in (s.get("clips") or [])), None)
        if hit is None:
            raise LookupError(f"{take.session}/{take.clip} is not in the console's recordings yet")
        dest = Path(self.cfg.offload_dir)
        dest.mkdir(parents=True, exist_ok=True)
        zpath = dest / f"{take.session}-{take.clip}.zip"
        self.http.download(f"/api/sessions/{urllib.parse.quote(take.session)}/download", zpath)
        root = dest.resolve()
        with zipfile.ZipFile(zpath) as zf:
            for m in zf.infolist():
                if m.is_dir():
                    continue
                rel = Path(m.filename)
                if not rel.parts or rel.parts[0] != take.session or ".." in rel.parts:
                    continue
                mine = rel.name.startswith(take.clip + "_") or (len(rel.parts) > 2 and rel.parts[1] == "truth")
                if not mine:
                    continue
                target = (dest / rel).resolve()
                if root not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        zpath.unlink(missing_ok=True)
