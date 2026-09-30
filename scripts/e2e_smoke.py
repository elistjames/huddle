"""End-to-end smoke: real chat service + gateway + Postgres + Redis (no agent, no Ollama).

Usage: eval "$(scripts/dev-stores.sh)"; uv run python scripts/e2e_smoke.py
Two guests join, exchange messages over WebSockets, and we check seq order, the stored ack,
receipt cursors and reconnect catch-up.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx
import websockets

PG = os.environ.get("HUDDLE_TEST_POSTGRES_DSN", "postgresql://huddlepg@127.0.0.1:5432/postgres")
RD = os.environ.get("HUDDLE_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
ENV = {
    **os.environ,
    "HUDDLE_POSTGRES_DSN": PG,
    "HUDDLE_REDIS_URL": RD,
    "HUDDLE_JWT_SECRET": "e2e-secret-e2e-secret-e2e-secret-0123",
    "HUDDLE_SERVICE_TOKEN": "e2e-token",
    "HUDDLE_CHAT_URL": "http://127.0.0.1:8001",
    "HUDDLE_GATEWAY_ID": "gw-e2e",
    "HUDDLE_SESSION_ID": "ses_e2e",
}


def spawn(mod: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-m", mod], env=ENV)  # noqa: S603  # nosec B603


async def wait_http(url: str) -> None:
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                if (await c.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise SystemExit(f"timeout waiting for {url}")


async def recv_until(ws: Any, pred: Any, wait_s: float = 5.0) -> dict[str, Any]:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        raw = await asyncio.wait_for(ws.recv(), end - time.monotonic())
        f = json.loads(raw)
        if pred(f):
            return f
    raise SystemExit("timeout waiting for frame")


async def connect(token: str, sid: str, after: int = 0) -> Any:
    ws = await websockets.connect("ws://127.0.0.1:8000/ws")
    await ws.send(
        json.dumps({"v": 1, "type": "auth", "token": token, "device_id": str(uuid.uuid4())})
    )
    await recv_until(ws, lambda f: f["type"] == "auth.ok")
    await ws.send(json.dumps({"v": 1, "type": "subscribe", "session_id": sid, "after_seq": after}))
    await recv_until(ws, lambda f: f["type"] == "sync.batch")
    return ws


async def main() -> None:
    procs = [spawn("huddle_chat"), spawn("huddle_gateway")]
    try:
        await wait_http("http://127.0.0.1:8001/healthz")
        await wait_http("http://127.0.0.1:8000/healthz")
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as c:
            a = (await c.post("/auth/guest", json={"display_name": "Maya"})).json()
            b = (await c.post("/auth/guest", json={"display_name": "Eli"})).json()
            sid = "ses_e2e"
            det = (
                await c.get(
                    f"/sessions/{sid}", headers={"Authorization": f"Bearer {a['access_token']}"}
                )
            ).json()
            assert det["agent"]["name"] == "Huddle AI", det
            assert det["agent"]["budget"]["used_pct"] == 0, det
        wa = await connect(a["access_token"], sid)
        wb = await connect(b["access_token"], sid)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, cid in enumerate(ids):
            await wa.send(
                json.dumps({"v": 1, "type": "message.send", "client_msg_id": cid, "body": f"m{i}"})
            )
        acks = [await recv_until(wa, lambda f: f["type"] == "message.ack") for _ in ids]
        seqs = [x["seq"] for x in acks]
        assert seqs == sorted(seqs) and len(set(seqs)) == 3, seqs
        # resend same client_msg_id -> same seq (idempotent)
        await wa.send(
            json.dumps({"v": 1, "type": "message.send", "client_msg_id": ids[0], "body": "m0"})
        )
        again = await recv_until(wa, lambda f: f["type"] == "message.ack")
        assert again["seq"] == seqs[0], again
        got = [await recv_until(wb, lambda f: f["type"] == "message.created") for _ in ids]
        assert [g["message"]["body"] for g in got] == ["m0", "m1", "m2"]
        await wb.send(json.dumps({"v": 1, "type": "ack", "up_to_seq": seqs[-1]}))
        rc = await recv_until(wa, lambda f: f["type"] == "receipt.updated")
        assert max(rc["cursors"].values()) >= seqs[-1], rc
        # reconnect catch-up
        await wb.close()
        await wa.send(
            json.dumps(
                {
                    "v": 1,
                    "type": "message.send",
                    "client_msg_id": str(uuid.uuid4()),
                    "body": "later",
                }
            )
        )
        await recv_until(wa, lambda f: f["type"] == "message.ack")
        wb2 = await websockets.connect("ws://127.0.0.1:8000/ws")
        await wb2.send(
            json.dumps({"v": 1, "type": "auth", "token": b["access_token"], "device_id": "d2"})
        )
        await recv_until(wb2, lambda f: f["type"] == "auth.ok")
        await wb2.send(
            json.dumps({"v": 1, "type": "subscribe", "session_id": sid, "after_seq": seqs[-1]})
        )
        batch = await recv_until(wb2, lambda f: f["type"] == "sync.batch")
        assert [m["body"] for m in batch["messages"]] == ["later"], batch
        print("E2E SMOKE PASSED", seqs)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(10)


asyncio.run(main())
