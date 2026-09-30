"""Redis side of the chat service: connection registry fan-out, triggers, rate limits, agent state.

redis-py's annotations leave a few commands (ZRANGE, PUBLISH) partially unknown under pyright
strict; those calls are confined to this module with narrow ignores.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from huddle_contracts import redis_keys as keys
from huddle_contracts.internal import AgentTrigger
from huddle_contracts.models import AgentState
from huddle_contracts.ws import FanoutEnvelope

TRIGGER_STREAM_MAXLEN = 10_000
WRITING_STATE_TTL_SECONDS = 120  # a crashed writer must not show "is writing" forever
REPORTED_STATE_TTL_SECONDS = 24 * 3600

# Sliding-log limiter: one sorted-set member per message (the client_msg_id, so a retry of the
# same message is not counted twice). Uses the Redis clock so replicas agree.
_RATE_LIMIT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZSCORE', KEYS[1], ARGV[3]) then
  return 0
end
if redis.call('ZCARD', KEYS[1]) >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.max(1, tonumber(oldest[2]) + window - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return 0
"""


def agent_state_key(session_id: str) -> str:
    """Chat-private: the agent's last reported live state (following/writing/away)."""
    return f"agent_state:{session_id}"


class RedisOps:
    def __init__(self, redis: Redis, clock: Callable[[], float] = time.time) -> None:
        self.redis = redis
        self._clock = clock

    @classmethod
    def connect(cls, url: str) -> RedisOps:
        client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )
        return cls(client)

    async def ping(self) -> bool:
        return bool(await self.redis.ping())  # pyright: ignore[reportUnknownMemberType]

    async def close(self) -> None:
        await self.redis.aclose()

    # ---- connection registry (Deep Dive §4) --------------------------------------------------

    async def connections_by_gateway(self, user_ids: Iterable[str]) -> dict[str, list[str]]:
        """Live connections of these users grouped by gateway.

        Drops expired sorted-set members (ZREMRANGEBYSCORE), prunes members whose `conn:` hash
        has expired, and skips gateways whose `gw_alive` key is gone.
        """
        uids = list(dict.fromkeys(user_ids))
        if not uids:
            return {}
        now = self._clock()
        pipe = self.redis.pipeline(transaction=False)
        for uid in uids:
            pipe.zremrangebyscore(keys.user_conns(uid), "-inf", f"({now}")
            pipe.zrange(keys.user_conns(uid), 0, -1)  # pyright: ignore[reportUnknownMemberType]
        res: list[Any] = await pipe.execute()
        owned: list[tuple[str, str]] = [
            (uid, str(cid)) for uid, cids in zip(uids, res[1::2], strict=True) for cid in cids
        ]
        if not owned:
            return {}
        pipe = self.redis.pipeline(transaction=False)
        for _, cid in owned:
            pipe.hget(keys.conn(cid), "gateway_id")
        gateways: list[Any] = await pipe.execute()
        by_gateway: dict[str, list[str]] = {}
        stale = self.redis.pipeline(transaction=False)
        n_stale = 0
        for (uid, cid), gw in zip(owned, gateways, strict=True):
            if not gw:
                stale.zrem(keys.user_conns(uid), cid)
                n_stale += 1
                continue
            by_gateway.setdefault(str(gw), []).append(cid)
        if n_stale:
            await stale.execute()
        if not by_gateway:
            return {}
        pipe = self.redis.pipeline(transaction=False)
        names = list(by_gateway)
        for gw in names:
            pipe.exists(keys.gw_alive(gw))
        alive: list[Any] = await pipe.execute()
        return {gw: by_gateway[gw] for gw, ok in zip(names, alive, strict=True) if ok}

    async def publish(self, targets: dict[str, list[str]], frame: dict[str, Any]) -> int:
        """PUBLISH one FanoutEnvelope per live gateway. Returns the number of gateways reached."""
        if not targets:
            return 0
        pipe = self.redis.pipeline(transaction=False)
        for gw, conn_ids in targets.items():
            env = FanoutEnvelope(connection_ids=conn_ids, frame=frame)
            pipe.publish(keys.gw_channel(gw), env.model_dump_json())  # pyright: ignore[reportUnknownMemberType]
        await pipe.execute()
        return len(targets)

    # ---- agent triggers (Deep Dive §3) -------------------------------------------------------

    async def enqueue_trigger(self, trigger: AgentTrigger) -> str:
        fields: dict[FieldT, EncodableT] = {
            k: str(v) for k, v in trigger.model_dump(mode="json", exclude_none=True).items()
        }
        entry_id = await self.redis.xadd(
            keys.AGENT_TRIGGERS_STREAM, fields, maxlen=TRIGGER_STREAM_MAXLEN, approximate=True
        )
        return str(entry_id)

    # ---- rate limit (Deep Dive §6: 10 messages / 10 s / user / session) ----------------------

    async def rate_limit(
        self, session_id: str, sender_id: str, member: str, limit: int, window_ms: int
    ) -> int:
        """0 when allowed, else milliseconds until a slot frees up."""
        key = keys.rate_limit("msg", f"{session_id}:{sender_id}")
        result: Any = await self.redis.eval(_RATE_LIMIT_LUA, 1, key, window_ms, limit, member)
        return int(result)

    # ---- agent live state ---------------------------------------------------------------------

    async def set_reported_state(self, session_id: str, state: AgentState) -> None:
        ttl = (
            WRITING_STATE_TTL_SECONDS if state == AgentState.WRITING else REPORTED_STATE_TTL_SECONDS
        )
        await self.redis.set(agent_state_key(session_id), state.value, ex=ttl)

    async def reported_state(self, session_id: str) -> AgentState | None:
        raw = await self.redis.get(agent_state_key(session_id))
        if raw is None:
            return None
        try:
            return AgentState(raw if isinstance(raw, str) else raw.decode())
        except ValueError:
            return None
