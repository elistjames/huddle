"""Connection registry TTLs and heartbeats (Deep Dive §4), backpressure (§6, §7)."""

from __future__ import annotations

import asyncio
import time

import pytest
from gw_support import (
    SESSION,
    Client,
    FakeChat,
    RawClient,
    created_frame,
    gw_settings,
    make_token,
    publish,
    running_gateway,
    uid,
    wait_until,
)
from redis.asyncio import Redis

from huddle_contracts import redis_keys
from huddle_contracts.ws import CLOSE_SLOW_CONSUMER
from huddle_gateway.registry import ConnInfo, Registry

pytestmark = pytest.mark.integration


async def test_entry_written_on_auth_and_deleted_on_clean_close(
    redis_url: str, redis: Redis
) -> None:
    chat = FakeChat()
    user = uid()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        gw_id = gw.settings.gateway_id
        alive_ttl = await redis.pttl(redis_keys.gw_alive(gw_id))
        assert 0 < alive_ttl <= 30_000

        c = await Client.open(gw.ws_url)
        before = time.time()
        await c.auth(user, device_id="dev-x")
        [conn_id] = gw.gateway.connections
        entry = await redis.hgetall(redis_keys.conn(conn_id))  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]
        assert entry["user_id"] == user
        assert entry["device_id"] == "dev-x"
        assert entry["gateway_id"] == gw_id
        assert before - 1 <= float(entry["connected_at"]) <= time.time()
        assert float(entry["last_heartbeat"]) >= float(entry["connected_at"])
        assert 55_000 < await redis.pttl(redis_keys.conn(conn_id)) <= 60_000
        score = await redis.zscore(redis_keys.user_conns(user), conn_id)
        assert score is not None and abs(score - (time.time() + 60)) < 5

        await c.close()
        await wait_until(lambda: conn_id not in gw.gateway.connections)
        await asyncio.sleep(0.05)
        assert await redis.exists(redis_keys.conn(conn_id)) == 0
        assert await redis.zscore(redis_keys.user_conns(user), conn_id) is None
    assert await redis.exists(redis_keys.gw_alive(gw_id)) == 0


async def test_heartbeats_refresh_then_three_misses_close(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    user = uid()
    settings = gw_settings(redis_url, conn_ttl_s=1.0, heartbeat_interval_s=0.3)
    async with running_gateway(settings, chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        [conn_id] = gw.gateway.connections
        key = redis_keys.conn(conn_id)
        first = float(await redis.hget(key, "last_heartbeat") or 0)  # pyright: ignore[reportGeneralTypeIssues]

        # Keep pinging well past the TTL: the entry must stay alive and be refreshed.
        for _ in range(8):
            await asyncio.sleep(0.2)
            await c.send({"type": "ping"})
            assert (await c.recv())["type"] == "pong"
        assert await redis.pttl(key) > 0
        assert float(await redis.hget(key, "last_heartbeat") or 0) > first  # pyright: ignore[reportGeneralTypeIssues]

        # Stop pinging: after three missed intervals (0.9 s) the gateway closes the socket.
        t0 = time.monotonic()
        code, _ = await c.closed(timeout=5)
        assert code == 1001
        assert 0.8 <= time.monotonic() - t0 < 3
        await wait_until(lambda: conn_id not in gw.gateway.connections)
        await asyncio.sleep(0.05)
        assert await redis.exists(key) == 0


async def test_entries_expire_when_a_gateway_dies(redis: Redis) -> None:
    """A crashed gateway never cleans up: TTLs and expiry scores retire its entries."""
    registry = Registry(redis, conn_ttl_s=0.4, gw_alive_ttl_s=0.4)
    user, gw_id = uid(), uid("gw-dead")
    info = ConnInfo(uid("conn"), user, "dev", gw_id, time.time())
    assert await registry.register(info, max_per_user=5)
    await registry.mark_alive(gw_id, time.time())
    assert await redis.exists(redis_keys.conn(info.connection_id), redis_keys.gw_alive(gw_id)) == 2
    await asyncio.sleep(0.6)
    assert await redis.exists(redis_keys.conn(info.connection_id)) == 0
    assert await redis.exists(redis_keys.gw_alive(gw_id)) == 0
    # The chat service's lazy cleanup: members scored before now are expired.
    await redis.zadd(redis_keys.user_conns(user), {info.connection_id: time.time() - 0.2})
    assert await redis.zremrangebyscore(redis_keys.user_conns(user), "-inf", time.time()) == 1


async def test_per_user_cap_is_atomic_across_gateways(redis: Redis) -> None:
    registry = Registry(redis, conn_ttl_s=60, gw_alive_ttl_s=30)
    user = uid()
    infos = [ConnInfo(uid("conn"), user, f"d{i}", f"gw-{i % 3}", time.time()) for i in range(12)]
    results = await asyncio.gather(*(registry.register(i, max_per_user=5) for i in infos))
    assert sum(results) == 5
    assert await redis.zcard(redis_keys.user_conns(user)) == 5
    for info, ok in zip(infos, results, strict=True):
        if ok:
            await registry.unregister(info)
    assert await redis.exists(redis_keys.user_conns(user)) == 0


async def test_heartbeat_rebuilds_a_flushed_entry(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    user = uid()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user, device_id="dev-r")
        [conn_id] = gw.gateway.connections
        await redis.delete(redis_keys.conn(conn_id), redis_keys.user_conns(user))
        await c.send({"type": "ping"})
        await c.recv_type("pong")
        await asyncio.sleep(0.05)
        entry = await redis.hgetall(redis_keys.conn(conn_id))  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]
        assert entry["device_id"] == "dev-r" and entry["gateway_id"] == gw.settings.gateway_id
        assert await redis.zscore(redis_keys.user_conns(user), conn_id) is not None
        await c.close()


async def test_slow_consumer_is_closed_with_4008(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    settings = gw_settings(redis_url, queue_max_events=50, queue_max_bytes=1024 * 1024)
    async with running_gateway(settings, chat) as gw:
        c = await RawClient.open(gw.port)
        await c.send({"type": "auth", "token": make_token(user), "device_id": "slow"})
        assert (await c.recv())["type"] == "auth.ok"
        await c.send({"type": "subscribe", "session_id": SESSION, "after_seq": 0})
        assert (await c.recv())["type"] == "sync.batch"
        [(conn_id, conn)] = gw.gateway.connections.items()

        # The client now stops reading while the chat service keeps publishing.
        pad = "x" * 32_000
        sent = 0
        for seq in range(1, 3001):
            frame = created_frame(SESSION, seq)
            frame["message"]["body"] = pad
            await publish(redis, settings.gateway_id, [conn_id], frame)
            sent = seq
            if seq % 100 == 0:
                await asyncio.sleep(0.05)
                if conn.close_code == CLOSE_SLOW_CONSUMER:
                    break
        await wait_until(lambda: conn.close_code == CLOSE_SLOW_CONSUMER, timeout=10)
        # A slow (not dead) client catches up and finds the 4008 close after what was in flight.
        code, delivered = await asyncio.wait_for(c.close_code(), timeout=20)
        c.close()
        assert code == 4008
        assert delivered < sent  # the queued backlog was dropped, not delivered
        await wait_until(lambda: conn_id not in gw.gateway.connections)
        await asyncio.sleep(0.05)
        assert await redis.exists(redis_keys.conn(conn_id)) == 0
