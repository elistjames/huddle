"""The gateway process: live connections, registry upkeep, fan-out and draining.

Deep Dive §3 (gateway owns sockets, heartbeats, send queues, registry entries), §4 (registry),
§2 and §7 (server-side draining), §12 (any gateway can take any connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any, cast

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.websockets import WebSocket

from huddle_contracts import redis_keys
from huddle_contracts.ws import FanoutEnvelope, ServerDraining
from huddle_gateway.chat_client import ChatClient
from huddle_gateway.config import Settings
from huddle_gateway.connection import CLOSE_GOING_AWAY, Connection
from huddle_gateway.logs import log_event
from huddle_gateway.outbound import Outgoing
from huddle_gateway.registry import ConnInfo, Registry


class Gateway:
    def __init__(
        self,
        settings: Settings,
        *,
        redis: Redis | None = None,
        chat_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.redis: Redis = redis or Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
        self.registry = Registry(
            self.redis, conn_ttl_s=settings.conn_ttl_s, gw_alive_ttl_s=settings.gw_alive_ttl_s
        )
        self.chat = ChatClient(
            settings.chat_url,
            settings.service_token,
            timeout_s=settings.chat_timeout_s,
            transport=chat_transport,
        )
        # Registered (authenticated) connections by id, the targets of fan-out.
        self.connections: dict[str, Connection] = {}
        # Every open socket, including ones still in the auth handshake.
        self._open: set[Connection] = set()
        self._handlers: set[asyncio.Task[Any]] = set()
        self.draining = False
        self.started_at = time.time()
        self.stale_hits = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._subscribed = asyncio.Event()

    @property
    def channel(self) -> str:
        return redis_keys.gw_channel(self.settings.gateway_id)

    def _log(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        log_event(event, level=level, gateway_id=self.settings.gateway_id, **fields)

    # ---- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        await self.registry.mark_alive(self.settings.gateway_id, self.started_at)
        self._tasks = [
            asyncio.create_task(self._alive_loop(), name="gw-alive"),
            asyncio.create_task(self._fanout_loop(), name="gw-fanout"),
        ]
        # Accept sockets only once fan-out is subscribed, so no publish to us is missed.
        await asyncio.wait_for(self._subscribed.wait(), timeout=10)
        self._log("gateway.started", channel=self.channel)

    async def stop(self) -> None:
        for conn in list(self._open):
            conn.request_close(CLOSE_GOING_AWAY, "server shutting down", flush=False)
        await self._wait_handlers(self.settings.close_grace_s * 2)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(RedisError, OSError):
            await self.registry.mark_dead(self.settings.gateway_id)
        await self.chat.aclose()
        await self.redis.aclose()
        self._log("gateway.stopped")

    async def _wait_handlers(self, limit_s: float) -> None:
        pending = [t for t in self._handlers if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=limit_s)

    async def _alive_loop(self) -> None:
        interval = self.settings.gw_alive_ttl_s / 3
        while True:
            await asyncio.sleep(interval)
            try:
                await self.registry.mark_alive(self.settings.gateway_id, self.started_at)
            except (RedisError, OSError):
                self._log("registry.error", level=logging.WARNING, op="gw_alive")

    # ---- fan-out (chat -> gateway over gw:{gateway_id}) ----------------------------------

    async def _fanout_loop(self) -> None:
        backoff = 0.1
        while True:
            pubsub = self.redis.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
            try:
                await pubsub.subscribe(self.channel)  # pyright: ignore[reportUnknownMemberType]
                self._subscribed.set()
                backoff = 0.1
                while True:
                    msg = cast(
                        "dict[str, Any] | None",
                        await pubsub.get_message(timeout=1.0),  # pyright: ignore[reportUnknownMemberType]
                    )
                    if msg is not None and msg.get("type") == "message":
                        self.dispatch_fanout(msg["data"])
            except (RedisError, OSError):
                # Pub/sub may drop messages; clients recover through their cursor (§3, §7).
                self._log("fanout.redis_error", level=logging.WARNING)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()

    def dispatch_fanout(self, data: bytes | str) -> None:
        """Write one FanoutEnvelope's frame to each listed connection that lives here."""
        try:
            env = FanoutEnvelope.model_validate_json(data)
        except ValidationError:
            self._log("fanout.bad_envelope", level=logging.WARNING)
            return
        item = Outgoing.from_frame(env.frame)
        delivered = 0
        for cid in env.connection_ids:
            conn = self.connections.get(cid)
            if conn is None or conn.closed:
                # §4: a push to a dead or unknown socket does nothing; the cursor recovers it.
                self.stale_hits += 1
                continue
            conn.deliver(item)
            delivered += 1
        self._log(
            "fanout.dispatched",
            level=logging.DEBUG,
            session_id=item.session_id,
            seq=item.seq,
            frame_type=item.type,
            targets=len(env.connection_ids),
            delivered=delivered,
        )

    # ---- connections ---------------------------------------------------------------------

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        conn = Connection(self, ws)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._open.add(conn)
        try:
            if self.draining:
                conn.send(ServerDraining(reconnect_after_ms=self._jitter_ms()))
                conn.request_close(CLOSE_GOING_AWAY, "server draining")
            await conn.serve()
        finally:
            self._open.discard(conn)
            if task is not None:
                self._handlers.discard(task)

    async def register(self, conn: Connection, info: ConnInfo) -> bool:
        ok = await self.registry.register(info, max_per_user=self.settings.max_conns_per_user)
        if ok:
            self.connections[conn.id] = conn
        return ok

    async def unregister(self, conn: Connection) -> None:
        self.connections.pop(conn.id, None)
        if conn.info is None:
            return
        try:
            await self.registry.unregister(conn.info)
        except (RedisError, OSError):
            # The entry expires on its own within the TTL (§4).
            self._log("registry.error", level=logging.WARNING, op="unregister")

    # ---- draining (§2, §7, §12 runbook "Gateway outage") ----------------------------------

    def _jitter_ms(self) -> int:
        spread = max(1, self.settings.drain_spread_ms)
        return secrets.randbelow(spread)

    async def drain(self) -> None:
        """Tell every client to reconnect after a jittered delay, then close their sockets."""
        if self.draining:
            return
        self.draining = True
        conns = list(self._open)
        self._log("gateway.draining", connections=len(conns))
        for conn in conns:
            conn.send(ServerDraining(reconnect_after_ms=self._jitter_ms()))
            conn.request_close(CLOSE_GOING_AWAY, "server draining")
        await self._wait_handlers(self.settings.close_grace_s * 2)
        self._log("gateway.drained", remaining=len(self.connections))
