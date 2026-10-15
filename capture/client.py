"""One WebSocket link to one camera node, or to the rig controller.

Requests are matched to acks by envelope id; everything else (heartbeats, events, binary
preview frames) goes to callbacks and is cached. The link reconnects by itself, with a backoff
of 1 s doubling to 10 s, so a camera that reboots comes back without anyone noticing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from websockets.asyncio.client import connect

from .protocol import (CMD_PING, EVT_HEARTBEAT, Envelope, PreviewFrame, compatible, decode_preview)

log = logging.getLogger("capture.link")

MAX_MESSAGE = 8 * 1024 * 1024
OPEN_TIMEOUT_S = 5.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 10.0

EventHandler = Callable[["Link", Envelope], Optional[Awaitable[None]]]


class LinkError(RuntimeError):
    pass


class Link:
    def __init__(self, node_id: str, url: str, *, kind: str = "node",
                 on_event: Optional[EventHandler] = None,
                 on_connect: Optional[Callable[["Link"], Optional[Awaitable[None]]]] = None,
                 on_preview: Optional[Callable[["Link", PreviewFrame], Optional[Awaitable[None]]]] = None,
                 headers: Optional[dict | Callable[[], dict]] = None) -> None:
        self.node_id, self.url, self.kind = node_id, url, kind
        self.on_event, self.on_connect, self.on_preview = on_event, on_connect, on_preview
        self.headers = headers  # sent on every connect: a dict, or a callable giving one (a console's login cookie)
        self.ws = None
        self.online = False
        self.error: Optional[str] = None
        self.connected_at: Optional[float] = None
        self.connects = 0
        self.last_state: Optional[dict] = None       # last heartbeat payload
        self.last_heartbeat_at: Optional[float] = None
        self.last_preview: Optional[PreviewFrame] = None
        self.preview_at: Optional[float] = None
        self.preview_count = 0
        self.preview_cond: Optional[asyncio.Condition] = None
        self.pending: dict[str, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is None:
            self.preview_cond = asyncio.Condition()
            self._task = asyncio.create_task(self._run(), name=f"link-{self.node_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending("link closed")

    async def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while True:
            try:
                extra = self.headers() if callable(self.headers) else self.headers
                async with connect(self.url, max_size=MAX_MESSAGE, open_timeout=OPEN_TIMEOUT_S,
                                   ping_interval=10, ping_timeout=15, additional_headers=extra or None) as ws:
                    self.ws = ws
                    self.online = True
                    self.error = None
                    self.connected_at = time.time()
                    self.connects += 1
                    backoff = BACKOFF_MIN_S
                    log.info("%s: connected to %s", self.node_id, self.url)
                    if self.on_connect is not None:
                        await _maybe_await(self.on_connect(self))
                    async for raw in ws:
                        await self._on_message(raw)
                    self.error = "connection closed"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # refused, timed out, dropped: all the same to us
                self.error = f"{type(e).__name__}: {e}".strip(": ")
                if backoff == BACKOFF_MIN_S:
                    log.warning("%s: %s (%s)", self.node_id, self.error, self.url)
            finally:
                self.online = False
                self.ws = None
                self._fail_pending(self.error or "disconnected")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(LinkError(f"{self.node_id} {reason}"))
        self.pending.clear()

    # ---------------------------------------------------------------- inbound

    async def _on_message(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            try:
                frame = decode_preview(bytes(raw))
            except ValueError as e:
                log.debug("%s: bad preview frame: %s", self.node_id, e)
                return
            self.last_preview = frame
            self.preview_at = time.time()
            self.preview_count += 1
            if self.preview_cond is not None:
                async with self.preview_cond:
                    self.preview_cond.notify_all()
            if self.on_preview is not None:
                try:
                    await _maybe_await(self.on_preview(self, frame))
                except Exception:
                    log.exception("%s: preview handler failed", self.node_id)
            return
        try:
            env = Envelope.from_json(raw)
        except (ValueError, TypeError) as e:
            log.debug("%s: bad envelope: %s", self.node_id, e)
            return
        if env.is_ack:
            fut = self.pending.pop(env.id, None)
            if fut is not None and not fut.done():
                fut.set_result(env)
            else:
                log.debug("%s: stray ack %s %s", self.node_id, env.type, env.payload)
            return
        if env.type == EVT_HEARTBEAT and self.kind == "node":
            self.last_state = env.payload
            self.last_heartbeat_at = time.time()
        if self.on_event is not None:
            try:
                await _maybe_await(self.on_event(self, env))
            except Exception:
                log.exception("%s: event handler failed on %s", self.node_id, env.type)

    # ---------------------------------------------------------------- outbound

    async def cmd(self, type_: str, timeout: float = 6.0, **payload) -> Envelope:
        """Send one command and wait for its ack. Raises LinkError offline or on timeout; a
        refused command comes back as an ack.error or ack.busy envelope, not an exception."""
        ws = self.ws
        if ws is None or not self.online:
            raise LinkError(f"{self.node_id} offline")
        env = Envelope(type=type_, payload=payload)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[env.id] = fut
        try:
            await ws.send(env.to_json())
            reply = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise LinkError(f"{self.node_id} timeout on {type_} after {timeout:g} s") from None
        except LinkError:
            raise
        except Exception as e:
            raise LinkError(f"{self.node_id} send failed: {e}") from None
        finally:
            self.pending.pop(env.id, None)
        if not compatible(reply.v):
            raise LinkError(f"{self.node_id} speaks protocol {reply.v}, not ours")
        return reply

    async def ping(self, timeout: float = 4.0) -> dict:
        """Round trip and the camera's wall clock against ours (NTP-style midpoint)."""
        t0 = time.time()
        reply = await self.cmd(CMD_PING, timeout=timeout)
        t1 = time.time()
        out = {"rtt_ms": round((t1 - t0) * 1000, 1), "state": reply.payload.get("state")}
        wall = reply.payload.get("wall")
        if isinstance(wall, (int, float)):
            out["wall_offset_ms"] = round((wall - (t0 + t1) / 2) * 1000, 1)
        return out

    # ---------------------------------------------------------------- views

    def heartbeat_age_s(self) -> Optional[float]:
        return None if self.last_heartbeat_at is None else round(time.time() - self.last_heartbeat_at, 1)

    def preview_age_s(self) -> Optional[float]:
        return None if self.preview_at is None else round(time.time() - self.preview_at, 1)


async def _maybe_await(x) -> None:
    if asyncio.iscoroutine(x) or isinstance(x, asyncio.Future):
        await x
