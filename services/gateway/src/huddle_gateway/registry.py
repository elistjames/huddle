"""Connection registry in Redis (Deep Dive §4 "Connection registry").

- `conn:{connection_id}` hash {user_id, device_id, gateway_id, connected_at, last_heartbeat}
  with a 60 s TTL refreshed on every heartbeat.
- `user_conns:{user_id}` sorted set of connection ids scored by expiry time (epoch seconds, the
  same unit as `agent:timers`), so the chat service can drop expired members with
  ZREMRANGEBYSCORE -inf <now>.
- `gw_alive:{gateway_id}` with a 30 s TTL, refreshed by the gateway while it runs.

Timestamps in the hash are epoch seconds with millisecond precision.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from huddle_contracts import redis_keys

# Registering is atomic so the per-user cap holds across gateways (Deep Dive §6/§8: 5 per user).
# KEYS: user_conns, conn hash. ARGV: now, expiry, max, connection_id, ttl_ms, then hash pairs.
_REGISTER = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
redis.call('HSET', KEYS[2], unpack(ARGV, 6))
redis.call('PEXPIRE', KEYS[2], ARGV[5])
return 1
"""


@dataclass(slots=True)
class ConnInfo:
    connection_id: str
    user_id: str
    device_id: str
    gateway_id: str
    connected_at: float

    def fields(self, last_heartbeat: float) -> dict[FieldT, EncodableT]:
        return {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "gateway_id": self.gateway_id,
            "connected_at": f"{self.connected_at:.3f}",
            "last_heartbeat": f"{last_heartbeat:.3f}",
        }


class Registry:
    def __init__(self, redis: Redis, *, conn_ttl_s: float, gw_alive_ttl_s: float) -> None:
        self._redis = redis
        self._ttl_ms = max(1, int(conn_ttl_s * 1000))
        self._conn_ttl_s = conn_ttl_s
        self._gw_ttl_ms = max(1, int(gw_alive_ttl_s * 1000))
        self._register: Any = redis.register_script(_REGISTER)  # pyright: ignore[reportUnknownMemberType]

    async def register(self, info: ConnInfo, *, max_per_user: int) -> bool:
        """Add the connection unless the user already has `max_per_user` live ones."""
        now = time.time()
        pairs = [x for kv in info.fields(now).items() for x in kv]
        ok = await self._register(
            keys=[redis_keys.user_conns(info.user_id), redis_keys.conn(info.connection_id)],
            args=[
                f"{now:.3f}",
                f"{now + self._conn_ttl_s:.3f}",
                max_per_user,
                info.connection_id,
                self._ttl_ms,
                *pairs,
            ],
        )
        return bool(ok)

    async def refresh(self, info: ConnInfo) -> None:
        """Heartbeat: rewrite the whole entry (so a flushed Redis is rebuilt) and extend TTLs."""
        now = time.time()
        uc = redis_keys.user_conns(info.user_id)
        ck = redis_keys.conn(info.connection_id)
        async with self._redis.pipeline(transaction=True) as p:
            p.hset(ck, mapping=info.fields(now))
            p.pexpire(ck, self._ttl_ms)
            p.zadd(uc, {info.connection_id: now + self._conn_ttl_s})
            p.pexpire(uc, self._ttl_ms)
            await p.execute()

    async def unregister(self, info: ConnInfo) -> None:
        async with self._redis.pipeline(transaction=True) as p:
            p.zrem(redis_keys.user_conns(info.user_id), info.connection_id)
            p.delete(redis_keys.conn(info.connection_id))
            await p.execute()

    async def mark_alive(self, gateway_id: str, started_at: float) -> None:
        value = json.dumps({"started_at": round(started_at, 3), "ts": round(time.time(), 3)})
        await self._redis.set(redis_keys.gw_alive(gateway_id), value, px=self._gw_ttl_ms)

    async def mark_dead(self, gateway_id: str) -> None:
        await self._redis.delete(redis_keys.gw_alive(gateway_id))
