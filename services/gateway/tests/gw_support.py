"""Test support: an in-process fake chat service, token helpers, and a real-server harness.

The fake chat service implements the documented internal HTTP interface (docs/work/README.md) over
httpx.MockTransport, so gateway tests never need the real chat service.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import struct
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import uvicorn
from redis.asyncio import Redis
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from huddle_contracts import redis_keys
from huddle_contracts.errors import ErrorBody
from huddle_contracts.internal import (
    SERVICE_TOKEN_HEADER,
    AckDeliveryRequest,
    MembershipCheck,
    MessagePage,
    SubmitMessageError,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from huddle_contracts.models import Message, SenderType
from huddle_contracts.ws import FanoutEnvelope, MessageCreated
from huddle_gateway.app import create_app, gateway_of
from huddle_gateway.config import Settings
from huddle_gateway.gateway import Gateway

JWT_SECRET = "test-jwt-secret-" + "0123456789abcdef" * 4  # 80 bytes: fine even for HS512
SERVICE_TOKEN = "test-service-token"
SESSION = "ses_main"


def uid(prefix: str = "usr") -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def make_token(sub: str, *, exp_in: float = 900, secret: str = JWT_SECRET, **claims: Any) -> str:
    payload: dict[str, Any] = {"sub": sub, "exp": int(time.time() + exp_in), **claims}
    return jwt.encode(payload, secret, algorithm="HS256")  # pyright: ignore[reportUnknownMemberType]


def raw_token(payload: dict[str, Any], key: str | None, algorithm: str) -> str:
    return jwt.encode(payload, key, algorithm=algorithm)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]


def make_message(session_id: str, seq: int, sender_id: str = "usr_other") -> Message:
    return Message(
        id=f"msg_{seq:08d}",
        session_id=session_id,
        seq=seq,
        sender_type=SenderType.USER,
        sender_id=sender_id,
        client_msg_id=f"cmid-{seq}",
        body=f"body {seq}",
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
    )


def created_frame(session_id: str, seq: int) -> dict[str, Any]:
    msg = make_message(session_id, seq)
    return MessageCreated(
        event_id=f"evt_{seq}", session_id=session_id, sequence=seq, message=msg
    ).model_dump(mode="json")


_MSGS = re.compile(r"^/internal/sessions/([^/]+)/messages$")
_MEMBER = re.compile(r"^/internal/sessions/([^/]+)/members/([^/]+)$")


@dataclass
class FakeChat:
    """In-memory stand-in for the chat service's internal API."""

    messages: dict[str, list[Message]] = field(default_factory=dict[str, list[Message]])
    members: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    submitted: list[SubmitMessageRequest] = field(default_factory=list[SubmitMessageRequest])
    submit_headers: list[httpx.Headers] = field(default_factory=list[httpx.Headers])
    acks: list[AckDeliveryRequest] = field(default_factory=list[AckDeliveryRequest])
    page_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    submit_error: tuple[int, ErrorBody] | None = None
    # When set, message-page requests fail at the transport (chat unreachable).
    page_error: Exception | None = None
    # When set, submits raise it inside the gateway's HTTP client (simulates a gateway bug).
    submit_crash: Exception | None = None
    # When set, message-page requests wait on it (lets a test publish mid-replay).
    page_gate: asyncio.Event | None = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_member(self, user_id: str, session_id: str = SESSION) -> None:
        self.members.add((session_id, user_id))

    def seed(self, count: int, session_id: str = SESSION) -> None:
        log = self.messages.setdefault(session_id, [])
        start = len(log)
        log.extend(make_message(session_id, start + i + 1) for i in range(count))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get(SERVICE_TOKEN_HEADER) != SERVICE_TOKEN:
            return httpx.Response(401, json={"detail": "bad service token"})
        path = request.url.path
        if request.method == "POST" and path == "/internal/messages":
            return self._submit(request)
        if request.method == "POST" and path == "/internal/acks":
            self.acks.append(AckDeliveryRequest.model_validate_json(request.content))
            return httpx.Response(204)
        if request.method == "GET" and (m := _MSGS.match(path)):
            if self.page_gate is not None:
                await self.page_gate.wait()
            if self.page_error is not None:
                raise self.page_error
            return self._page(m.group(1), request)
        if request.method == "GET" and (m := _MEMBER.match(path)):
            active = (m.group(1), m.group(2)) in self.members
            return httpx.Response(200, content=MembershipCheck(is_active=active).model_dump_json())
        return httpx.Response(404)

    def _submit(self, request: httpx.Request) -> httpx.Response:
        if self.submit_crash is not None:
            raise self.submit_crash
        req = SubmitMessageRequest.model_validate_json(request.content)
        self.submitted.append(req)
        self.submit_headers.append(request.headers)
        if self.submit_error is not None:
            status, body = self.submit_error
            return httpx.Response(status, content=SubmitMessageError(error=body).model_dump_json())
        log = self.messages.setdefault(req.session_id, [])
        for existing in log:  # idempotent on (session, sender, client_msg_id)
            if existing.sender_id == req.sender.id and existing.client_msg_id == req.client_msg_id:
                resp = SubmitMessageResponse(
                    message_id=existing.id, seq=existing.seq, duplicate=True
                )
                return httpx.Response(200, content=resp.model_dump_json())
        msg = make_message(req.session_id, len(log) + 1, sender_id=req.sender.id)
        msg = msg.model_copy(update={"client_msg_id": req.client_msg_id, "body": req.body})
        log.append(msg)
        resp = SubmitMessageResponse(message_id=msg.id, seq=msg.seq)
        return httpx.Response(200, content=resp.model_dump_json())

    def _page(self, session_id: str, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.page_requests.append(params)
        after = int(params.get("after_seq", "0"))
        limit = int(params.get("limit", "200"))
        newer = [m for m in self.messages.get(session_id, []) if m.seq > after]
        page = MessagePage(messages=newer[:limit], has_more=len(newer) > limit)
        return httpx.Response(200, content=page.model_dump_json())


def gw_settings(redis_url: str, **overrides: Any) -> Settings:
    base = Settings(
        gateway_id=uid("gw-test"),
        redis_url=redis_url,
        chat_url="http://chat.test",
        jwt_secret=JWT_SECRET,
        service_token=SERVICE_TOKEN,
        port=0,
        auth_timeout_s=2.0,
        watchdog_tick_s=0.05,
        close_grace_s=1.0,
        drain_spread_ms=1000,
    )
    return replace(base, **overrides)


@dataclass
class Running:
    settings: Settings
    gateway: Gateway
    server: uvicorn.Server
    port: int

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@asynccontextmanager
async def running_gateway(settings: Settings, chat: FakeChat) -> AsyncGenerator[Running]:
    """Serve the real app with uvicorn on an ephemeral port, in this event loop."""
    app = create_app(settings, chat_transport=chat.transport)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
        lifespan="on",
        ws_max_size=settings.max_frame_bytes,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started or task.done():
                break
            await asyncio.sleep(0.02)
        if task.done():
            task.result()
        assert server.started, "gateway did not start"
        port = int(server.servers[0].sockets[0].getsockname()[1])
        yield Running(settings=settings, gateway=gateway_of(app), server=server, port=port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


class Client:
    """Thin JSON client over a websockets connection."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    @classmethod
    async def open(cls, url: str, **kwargs: Any) -> Client:
        return cls(await connect(url, open_timeout=5, **kwargs))

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:  # noqa: ASYNC109
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        data: dict[str, Any] = json.loads(raw)
        return data

    async def recv_type(self, ftype: str, timeout: float = 5.0) -> dict[str, Any]:  # noqa: ASYNC109
        """Receive frames until one of type `ftype` arrives (others are discarded)."""
        deadline = time.monotonic() + timeout
        while True:
            frame = await self.recv(max(0.01, deadline - time.monotonic()))
            if frame.get("type") == ftype:
                return frame

    async def auth(self, user_id: str, device_id: str = "dev-1") -> dict[str, Any]:
        await self.send({"type": "auth", "token": make_token(user_id), "device_id": device_id})
        return await self.recv()

    async def subscribe(
        self, after_seq: int = 0, session_id: str = SESSION
    ) -> list[dict[str, Any]]:
        """Subscribe and collect sync.batch frames up to the last page."""
        await self.send({"type": "subscribe", "session_id": session_id, "after_seq": after_seq})
        batches: list[dict[str, Any]] = []
        while True:
            frame = await self.recv_type("sync.batch")
            batches.append(frame)
            if not frame["has_more"]:
                return batches

    async def closed(self, timeout: float = 5.0) -> tuple[int | None, list[dict[str, Any]]]:  # noqa: ASYNC109
        """Read until the server closes; return (close code, frames received before it)."""
        frames: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    frames.append(json.loads(await self.ws.recv()))
        except ConnectionClosed as exc:
            return (exc.rcvd.code if exc.rcvd else None), frames

    async def close(self) -> None:
        await self.ws.close()


async def publish_raw(redis: Redis, gateway_id: str, payload: str) -> None:
    """Publish on gw:{gateway_id} the way the chat service does."""
    await redis.publish(redis_keys.gw_channel(gateway_id), payload)  # pyright: ignore[reportUnknownMemberType]


async def publish(
    redis: Redis, gateway_id: str, connection_ids: list[str], frame: dict[str, Any]
) -> None:
    env = FanoutEnvelope(connection_ids=connection_ids, frame=frame)
    await publish_raw(redis, gateway_id, env.model_dump_json())


async def wait_until(pred: Any, timeout: float = 5.0, interval: float = 0.02) -> None:  # noqa: ASYNC109
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class RawClient:
    """A minimal RFC 6455 client over a plain socket that can genuinely stop reading.

    Library clients keep draining the socket into their own buffers; this one reads only when
    asked, so the kernel buffers fill and the gateway sees a real slow consumer.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, port: int, *, rcvbuf: int = 16_384) -> RawClient:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
        reader, writer = await asyncio.open_connection(sock=sock, limit=16_384)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        return cls(reader, writer)

    async def send(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.writer.write(header + mask + masked)
        await self.writer.drain()

    async def read_frame(self) -> tuple[int, bytes]:
        b1, b2 = await self.reader.readexactly(2)
        n = b2 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", await self.reader.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await self.reader.readexactly(8))
        return b1 & 0x0F, await self.reader.readexactly(n)

    async def recv(self) -> dict[str, Any]:
        while True:
            opcode, payload = await self.read_frame()
            if opcode == 0x1:
                data: dict[str, Any] = json.loads(payload)
                return data
            if opcode == 0x8:
                raise ConnectionError(f"closed: {payload[:2].hex()}")

    async def close_code(self) -> tuple[int | None, int]:
        """Drain everything; return (close code, number of text frames read before it)."""
        count = 0
        while True:
            opcode, payload = await self.read_frame()
            if opcode == 0x1:
                count += 1
            elif opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                return code, count

    def close(self) -> None:
        self.writer.close()
