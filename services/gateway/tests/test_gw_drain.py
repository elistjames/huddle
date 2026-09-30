"""Graceful draining (Deep Dive §2 "Server-side draining", §7, §12 runbook)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys

import httpx
import pytest
from gw_support import (
    JWT_SECRET,
    SERVICE_TOKEN,
    Client,
    FakeChat,
    gw_settings,
    running_gateway,
    uid,
)
from redis.asyncio import Redis

from huddle_contracts import redis_keys

pytestmark = pytest.mark.integration


async def test_drain_sends_jittered_reconnect_and_closes(redis_url: str, redis: Redis) -> None:
    chat = FakeChat()
    spread = 5_000
    async with running_gateway(gw_settings(redis_url, drain_spread_ms=spread), chat) as gw:
        clients = [await Client.open(gw.ws_url) for _ in range(8)]
        users = [uid() for _ in clients]
        for c, u in zip(clients, users, strict=True):
            await c.auth(u)
        pending = await Client.open(gw.ws_url)  # still in the auth handshake
        conn_ids = list(gw.gateway.connections)

        drain = asyncio.create_task(gw.gateway.drain())
        results = await asyncio.gather(*(c.closed() for c in [*clients, pending]))
        await drain

        delays: list[int] = []
        for code, frames in results:
            assert code == 1001
            [draining] = [f for f in frames if f["type"] == "server.draining"]
            assert 0 <= draining["reconnect_after_ms"] < spread
            delays.append(draining["reconnect_after_ms"])
        assert len(set(delays)) > 1, "reconnect delays must be jittered, not identical"

        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{gw.http_url}/healthz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "draining"

        late = await Client.open(gw.ws_url)
        code, frames = await late.closed()
        assert [f["type"] for f in frames] == ["server.draining"]
        assert code == 1001

        await asyncio.sleep(0.05)
        for cid in conn_ids:
            assert await redis.exists(redis_keys.conn(cid)) == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_sigterm_drains_the_real_process(redis_url: str, redis: Redis) -> None:
    """`python -m huddle_gateway` drains on SIGTERM, then exits and clears gw_alive."""
    port = _free_port()
    gw_id = uid("gw-proc")
    env = {
        **os.environ,
        "HUDDLE_JWT_SECRET": JWT_SECRET,
        "HUDDLE_SERVICE_TOKEN": SERVICE_TOKEN,
        "HUDDLE_REDIS_URL": redis_url,
        "HUDDLE_CHAT_URL": "http://127.0.0.1:9",
        "HUDDLE_GATEWAY_ID": gw_id,
        "HUDDLE_GATEWAY_PORT": str(port),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "huddle_gateway",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient() as http:
            for _ in range(100):
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                await asyncio.sleep(0.1)
        assert await redis.exists(redis_keys.gw_alive(gw_id)) == 1
        c = await Client.open(f"ws://127.0.0.1:{port}/ws")
        assert (await c.auth(uid()))["type"] == "auth.ok"

        proc.send_signal(signal.SIGTERM)
        code, frames = await c.closed(timeout=10)
        assert [f["type"] for f in frames] == ["server.draining"]
        assert code == 1001
        await asyncio.wait_for(proc.wait(), timeout=15)
        assert proc.returncode in (0, -signal.SIGTERM)
        assert await redis.exists(redis_keys.gw_alive(gw_id)) == 0

        assert proc.stdout is not None
        lines = [json.loads(line) for line in (await proc.stdout.read()).decode().splitlines()]
        events = [line["event"] for line in lines]
        assert "gateway.draining" in events and "auth.ok" in events
        for line in lines:  # every line is structured with the §9 fields
            for key in ("ts", "service", "trace_id", "session_id", "actor_id", "event"):
                assert key in line
            assert line["service"] == "gateway"
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
