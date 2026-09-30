"""Subscribe replay/dedupe (Deep Dive §2) and fan-out over gw:{gateway_id} (§3, §4)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from gw_support import (
    SESSION,
    Client,
    FakeChat,
    Running,
    created_frame,
    gw_settings,
    publish,
    publish_raw,
    running_gateway,
    uid,
    wait_until,
)
from redis.asyncio import Redis

from huddle_contracts import redis_keys
from huddle_gateway.connection import Connection, Phase

pytestmark = pytest.mark.integration


def conn_of(gw: Running, user_id: str, device_id: str | None = None) -> Connection:
    for conn in gw.gateway.connections.values():
        if conn.info and conn.info.user_id == user_id:
            if device_id is None or conn.info.device_id == device_id:
                return conn
    raise AssertionError(f"no connection for {user_id}/{device_id}")


async def no_more_frames(c: Client) -> list[dict[str, Any]]:
    """Everything the server sends before answering a ping (the queue is FIFO)."""
    await c.send({"type": "ping"})
    frames: list[dict[str, Any]] = []
    while (f := await c.recv())["type"] != "pong":
        frames.append(f)
    return frames


async def test_replay_pages_buffer_merge_and_dedupe(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    chat.seed(450)
    chat.page_gate = asyncio.Event()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.send({"type": "subscribe", "session_id": SESSION, "after_seq": 0})
        conn = conn_of(gw, user)
        await wait_until(lambda: conn.phase is Phase.REPLAYING)

        # Live events racing the replay: duplicates of stored messages, newer ones out of
        # order, a repeat, a non-message frame, and a frame for a session we did not join.
        receipt = {"v": 1, "type": "receipt.updated", "session_id": SESSION, "cursors": {"u": 3}}
        for frame in [
            created_frame(SESSION, 449),
            created_frame(SESSION, 452),
            receipt,
            created_frame(SESSION, 451),
            created_frame(SESSION, 451),
            created_frame(SESSION, 450),
            created_frame("ses_elsewhere", 453),
        ]:
            await publish(redis, gw.settings.gateway_id, [conn.id], frame)
        await wait_until(lambda: len(conn.buffer) == 6)
        chat.page_gate.set()

        batches: list[dict[str, Any]] = []
        while not batches or batches[-1]["has_more"]:
            batches.append(await c.recv_type("sync.batch"))
        assert [len(b["messages"]) for b in batches] == [200, 200, 50]
        assert [b["has_more"] for b in batches] == [True, True, False]
        seqs = [m["seq"] for b in batches for m in b["messages"]]
        assert seqs == list(range(1, 451))
        assert [p["after_seq"] for p in chat.page_requests] == ["0", "200", "400"]
        assert {p["limit"] for p in chat.page_requests} == {"200"}

        rest = await no_more_frames(c)
        assert [(f["type"], f.get("sequence")) for f in rest] == [
            ("message.created", 451),
            ("message.created", 452),
            ("receipt.updated", None),
        ]
        assert conn.phase is Phase.LIVE

        # Live: repeats and already-replayed seqs are dropped, late arrivals still delivered.
        for seq in (455, 455, 454, 300, 452):
            await publish(redis, gw.settings.gateway_id, [conn.id], created_frame(SESSION, seq))
        await asyncio.sleep(0.2)
        assert [f.get("sequence") for f in await no_more_frames(c)] == [455, 454]
        await c.close()


async def test_resubscribe_from_cursor(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    chat.seed(30)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        [batch] = await c.subscribe(after_seq=25)
        assert [m["seq"] for m in batch["messages"]] == [26, 27, 28, 29, 30]
        [empty] = await c.subscribe(after_seq=30)
        assert empty["messages"] == [] and empty["has_more"] is False
        await c.close()


async def test_replay_failure_reports_retryable_error(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    chat.page_error = httpx.ConnectError("chat down")
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.send({"type": "subscribe", "session_id": SESSION, "after_seq": 0})
        err = await c.recv_type("error")
        assert err["error"]["code"] == "INTERNAL"
        assert err["error"]["retryable"] is True
        assert conn_of(gw, user).session_id is None
        await c.close()


async def test_fanout_to_every_device_and_ignores_dead(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    alice, bob, carol = uid(), uid(), uid()
    for u in (alice, bob, carol):
        chat.add_member(u)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        a1, a2, b1, c1 = [await Client.open(gw.ws_url) for _ in range(4)]
        await a1.auth(alice, device_id="phone")
        await a2.auth(alice, device_id="laptop")
        await b1.auth(bob)
        await c1.auth(carol)
        for cl in (a1, a2, b1):
            await cl.subscribe()
        # carol is authenticated but not subscribed: session frames must not reach her.

        members = await redis.zrange(redis_keys.user_conns(alice), 0, -1)  # pyright: ignore[reportUnknownMemberType]
        ids = {conn_of(gw, alice, "phone").id, conn_of(gw, alice, "laptop").id}
        assert set(members) == ids

        # b1 goes away; its id stays in the envelope like a stale registry entry would.
        dead_id = conn_of(gw, bob).id
        await b1.close()
        await wait_until(lambda: dead_id not in gw.gateway.connections)

        stale_before = gw.gateway.stale_hits
        targets = [*ids, dead_id, "conn_unknown", conn_of(gw, carol).id]
        await publish(redis, gw.settings.gateway_id, targets, created_frame(SESSION, 1))
        for cl in (a1, a2):
            frame = await cl.recv()
            assert frame["type"] == "message.created" and frame["sequence"] == 1
        assert await no_more_frames(c1) == []
        assert gw.gateway.stale_hits - stale_before == 2

        # Another gateway's channel never reaches these sockets.
        await publish(redis, "gw-someone-else", list(ids), created_frame(SESSION, 2))
        # A malformed envelope is dropped without harming the listener.
        await publish_raw(redis, gw.settings.gateway_id, "{not json")
        await publish(redis, gw.settings.gateway_id, list(ids), created_frame(SESSION, 3))
        for cl in (a1, a2):
            assert (await cl.recv())["sequence"] == 3
        for cl in (a1, a2, c1):
            await cl.close()
