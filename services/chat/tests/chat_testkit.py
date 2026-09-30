"""Helpers shared by the chat service tests (fixtures live in conftest.py)."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis

from huddle_contracts import redis_keys as keys

JWT_SECRET = "test-secret-0123456789abcdef0123456789abcdef"
SERVICE_TOKEN = "test-service-token"


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 9, 30, 12, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---- a fake gateway on the registry ------------------------------------------------------------


class FakeGateways:
    """Registers connections the way a gateway does and records what arrives on gw:{id}."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.pubsub = redis.pubsub()  # pyright: ignore[reportUnknownMemberType]
        self.received: dict[str, list[dict[str, Any]]] = {}
        self.prefix = f"gwt{secrets.token_hex(4)}"

    def gw(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    async def start(self, *names: str, alive: bool = True) -> None:
        for name in names:
            gid = self.gw(name)
            self.received.setdefault(gid, [])
            await self.pubsub.subscribe(keys.gw_channel(gid))
            if alive:
                await self.redis.set(keys.gw_alive(gid), "1", ex=keys.GW_ALIVE_TTL_SECONDS)

    async def connect(
        self,
        user_id: str,
        gateway: str,
        *,
        ttl: float = keys.CONN_TTL_SECONDS,
        live: bool = True,
    ) -> str:
        """Register a device connection. `ttl` < 0 leaves an already-expired sorted-set member;
        `live=False` leaves a member whose `conn:` hash has expired."""
        cid = f"conn_{secrets.token_hex(6)}"
        gid = self.gw(gateway)
        now = datetime.now(UTC).timestamp()
        if live:
            await self.redis.hset(
                keys.conn(cid),
                mapping={
                    "user_id": user_id,
                    "device_id": secrets.token_hex(4),
                    "gateway_id": gid,
                    "connected_at": str(now),
                    "last_heartbeat": str(now),
                },
            )
            await self.redis.expire(keys.conn(cid), keys.CONN_TTL_SECONDS)
        await self.redis.zadd(keys.user_conns(user_id), {cid: now + ttl})
        return cid

    async def drain(self, quiet: float = 0.2) -> dict[str, list[dict[str, Any]]]:
        """Collect envelopes until the channels have been quiet for `quiet` seconds."""
        while True:
            msg: dict[str, Any] | None = await self.pubsub.get_message(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                ignore_subscribe_messages=True, timeout=quiet
            )
            if msg is None:
                return self.received
            if msg.get("type") == "message":
                channel = str(msg["channel"]).removeprefix("gw:")
                self.received.setdefault(channel, []).append(json.loads(msg["data"]))

    def frames(self, gateway: str, frame_type: str | None = None) -> list[dict[str, Any]]:
        envs = self.received.get(self.gw(gateway), [])
        return [e for e in envs if frame_type is None or e["frame"]["type"] == frame_type]

    async def close(self) -> None:
        await self.pubsub.aclose()


async def triggers_for(redis: Redis, session_id: str) -> list[dict[str, str]]:
    entries: Any = await redis.xrange(keys.AGENT_TRIGGERS_STREAM, "-", "+")
    return [dict(f) for _, f in entries if f.get("session_id") == session_id]
