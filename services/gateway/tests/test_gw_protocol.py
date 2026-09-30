"""Connection protocol against a real server, real Redis and a fake chat service (Deep Dive §6)."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import pytest
from gw_support import (
    SESSION,
    Client,
    FakeChat,
    gw_settings,
    make_token,
    running_gateway,
    uid,
)

from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.models import MAX_BODY_CHARS
from huddle_gateway.logs import JsonFormatter

pytestmark = pytest.mark.integration


async def test_healthz_and_ping(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{gw.http_url}/healthz")
        assert resp.status_code == 200
        assert resp.json()["gateway_id"] == gw.settings.gateway_id
        user = uid()
        c = await Client.open(gw.ws_url)
        ok = await c.auth(user)
        assert ok == {"v": 1, "type": "auth.ok", "user_id": user}
        await c.send({"type": "ping"})
        assert await c.recv() == {"v": 1, "type": "pong"}
        await c.close()


async def test_auth_timeout_closes_4001(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url, auth_timeout_s=0.3), chat) as gw:
        c = await Client.open(gw.ws_url)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        code, _ = await c.closed()
        assert code == 4001
        assert 0.25 <= loop.time() - t0 < 3


async def test_frames_before_auth_are_rejected(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url, auth_timeout_s=0.5), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.send({"type": "subscribe", "session_id": SESSION, "after_seq": 0})
        err = await c.recv()
        assert err["type"] == "error"
        assert err["error"]["code"] == "UNAUTHENTICATED"
        code, _ = await c.closed()
        assert code == 4001  # still bound by the auth deadline


async def test_invalid_token_closes(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        bad = make_token(uid(), secret="another-secret-" + "z" * 64)
        await c.send({"type": "auth", "token": bad, "device_id": "d"})
        code, frames = await c.closed()
        assert frames[0]["error"]["code"] == "UNAUTHENTICATED"
        assert frames[0]["error"]["retryable"] is False
        assert code == 1008


async def test_refresh_token_type_is_rejected(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.send({"type": "auth", "token": make_token(uid(), typ="refresh"), "device_id": "d"})
        code, frames = await c.closed()
        assert frames[0]["error"]["code"] == "UNAUTHENTICATED"
        assert code == 1008


async def test_expired_token_then_reauth_on_same_socket(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        user = uid()
        c = await Client.open(gw.ws_url)
        await c.send({"type": "auth", "token": make_token(user, exp_in=-5), "device_id": "d"})
        err = await c.recv()
        assert err["error"]["code"] == "TOKEN_EXPIRED"
        assert await c.auth(user) == {"v": 1, "type": "auth.ok", "user_id": user}
        await c.close()


async def test_token_expiring_mid_connection(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    async with running_gateway(gw_settings(redis_url, auth_timeout_s=1.5), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.send({"type": "auth", "token": make_token(user, exp_in=1), "device_id": "d"})
        assert (await c.recv())["type"] == "auth.ok"
        await c.subscribe()
        err = await c.recv_type("error", timeout=4)
        assert err["error"]["code"] == "TOKEN_EXPIRED"
        # Operations are refused until the client re-authenticates on the same socket.
        await c.send({"type": "message.send", "client_msg_id": "m1", "body": "hi"})
        refused = await c.recv()
        assert refused["error"]["code"] == "TOKEN_EXPIRED"
        assert refused["error"]["client_msg_id"] == "m1"
        assert chat.submitted == []
        assert await c.auth(user) == {"v": 1, "type": "auth.ok", "user_id": user}
        await c.send({"type": "message.send", "client_msg_id": "m1", "body": "hi"})
        ack = await c.recv_type("message.ack")
        assert ack["client_msg_id"] == "m1"
        await c.close()


async def test_expired_token_without_reauth_closes_4001(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url, auth_timeout_s=0.3), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.send({"type": "auth", "token": make_token(uid(), exp_in=1), "device_id": "d"})
        assert (await c.recv())["type"] == "auth.ok"
        code, frames = await c.closed(timeout=5)
        assert [f["error"]["code"] for f in frames if f["type"] == "error"] == ["TOKEN_EXPIRED"]
        assert code == 4001


async def test_reauth_as_another_user_is_refused(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(uid())
        await c.send({"type": "auth", "token": make_token(uid()), "device_id": "d"})
        code, frames = await c.closed()
        assert frames[0]["error"]["code"] == "UNAUTHENTICATED"
        assert code == 1008


async def test_unsupported_protocol_version(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(uid())
        await c.send({"v": 2, "type": "ping"})
        code, frames = await c.closed()
        assert frames[0]["error"]["code"] == "PROTOCOL_UNSUPPORTED"
        assert frames[0]["error"]["retryable"] is False
        assert code == 1002


async def test_malformed_frames_get_validation_errors(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(uid())
        await c.send({"type": "nope"})
        assert (await c.recv())["error"]["code"] == "VALIDATION"
        await c.ws.send("not json")
        assert (await c.recv())["error"]["code"] == "VALIDATION"
        await c.send({"type": "message.send", "client_msg_id": "x"})  # no body
        err = await c.recv()
        assert err["error"]["code"] == "VALIDATION"
        assert err["error"]["client_msg_id"] == "x"
        await c.send({"type": "ping"})  # the socket is still usable
        assert (await c.recv())["type"] == "pong"
        await c.close()


async def test_subscribe_requires_membership(redis_url: str) -> None:
    chat = FakeChat()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(uid())
        await c.send({"type": "subscribe", "session_id": SESSION, "after_seq": 0})
        err = await c.recv()
        assert err["error"]["code"] == "NOT_MEMBER"
        await c.close()


async def test_send_stamps_identity_and_ignores_client_fields(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user, device_id="dev-A")
        await c.subscribe()
        await c.send(
            {
                "type": "message.send",
                "client_msg_id": "0192-abc",
                "body": "hello",
                "sender_id": "usr_mallory",
                "sender": {"type": "agent", "id": "agt_x"},
                "seq": 999,
                "created_at": "2000-01-01T00:00:00Z",
                "session_id": "ses_other",
            }
        )
        ack = await c.recv_type("message.ack")
        assert ack == {
            "v": 1,
            "type": "message.ack",
            "client_msg_id": "0192-abc",
            "message_id": "msg_00000001",
            "seq": 1,
        }
        [req] = chat.submitted
        assert req.sender.id == user
        assert req.sender.type == "user"
        assert req.session_id == SESSION
        assert req.client_msg_id == "0192-abc"
        assert req.body == "hello"
        # One trace per send (§9): a W3C traceparent in the body and the header.
        assert req.trace_id is not None
        assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", req.trace_id)
        assert chat.submit_headers[0]["traceparent"] == req.trace_id
        await c.close()


async def test_send_continues_client_trace(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.subscribe()
        await c.send(
            {
                "type": "message.send",
                "client_msg_id": "m",
                "body": "x",
                "trace_id": f"00-{trace_id}-00f067aa0ba902b7-01",
            }
        )
        await c.recv_type("message.ack")
        tp = chat.submitted[0].trace_id
        assert tp is not None and tp.startswith(f"00-{trace_id}-")
        assert "00f067aa0ba902b7" not in tp  # the gateway opens its own span
        await c.close()


async def test_send_errors_use_the_stable_error_model(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    chat.submit_error = (503, ErrorBody.of(ErrorCode.PERSISTENCE_UNAVAILABLE, "db down"))
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.subscribe()
        await c.send({"type": "message.send", "client_msg_id": "m-1", "body": "x"})
        err = await c.recv_type("error")
        assert err["error"]["code"] == "PERSISTENCE_UNAVAILABLE"
        assert err["error"]["retryable"] is True
        assert err["error"]["client_msg_id"] == "m-1"
        await c.close()


async def test_send_rejections_before_chat(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.send({"type": "message.send", "client_msg_id": "early", "body": "x"})
        early = await c.recv()
        assert early["error"]["code"] == "VALIDATION"
        assert early["error"]["client_msg_id"] == "early"
        await c.subscribe()
        await c.send(
            {"type": "message.send", "client_msg_id": "long", "body": "x" * (MAX_BODY_CHARS + 1)}
        )
        err = await c.recv_type("error")
        assert err["error"]["code"] == "MESSAGE_TOO_LONG"
        assert err["error"]["client_msg_id"] == "long"
        assert chat.submitted == []
        await c.close()


async def test_not_member_on_send_unsubscribes(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    chat.submit_error = (403, ErrorBody.of(ErrorCode.NOT_MEMBER, "removed"))
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.subscribe()
        await c.send({"type": "message.send", "client_msg_id": "m", "body": "x"})
        err = await c.recv_type("error")
        assert err["error"]["code"] == "NOT_MEMBER"
        [conn] = gw.gateway.connections.values()
        assert conn.session_id is None
        await c.close()


async def test_ack_and_read_are_forwarded(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user, device_id="dev-9")
        await c.subscribe()
        await c.send({"type": "ack", "up_to_seq": 7, "participant_id": "usr_mallory"})
        await c.send({"type": "read", "up_to_seq": 5})
        await c.send({"type": "ping"})
        await c.recv_type("pong")
        assert [
            (a.participant_id, a.device_id, a.session_id, a.up_to_seq, a.kind) for a in chat.acks
        ] == [
            (user, "dev-9", SESSION, 7, "delivered"),
            (user, "dev-9", SESSION, 5, "read"),
        ]
        await c.close()


async def test_max_five_connections_per_user(redis_url: str) -> None:
    chat = FakeChat()
    user = uid()
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        clients = [await Client.open(gw.ws_url) for _ in range(5)]
        for i, c in enumerate(clients):
            assert (await c.auth(user, device_id=f"d{i}"))["type"] == "auth.ok"
        sixth = await Client.open(gw.ws_url)
        await sixth.send({"type": "auth", "token": make_token(user), "device_id": "d6"})
        code, frames = await sixth.closed()
        assert frames[0]["error"]["code"] == "RATE_LIMITED"
        assert code == 1008
        # A clean close frees a slot.
        await clients[0].close()
        await asyncio.sleep(0.2)
        again = await Client.open(gw.ws_url)
        assert (await again.auth(user, device_id="d7"))["type"] == "auth.ok"
        for c in [*clients[1:], again]:
            await c.close()


async def test_logs_never_contain_message_bodies(
    redis_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    chat = FakeChat()
    user = uid()
    chat.add_member(user)
    secret_body = "top-secret-body-7f3a"
    caplog.set_level(logging.DEBUG)
    async with running_gateway(gw_settings(redis_url), chat) as gw:
        c = await Client.open(gw.ws_url)
        await c.auth(user)
        await c.subscribe()
        await c.send({"type": "message.send", "client_msg_id": "m", "body": secret_body})
        await c.recv_type("message.ack")
        chat.submit_error = (503, ErrorBody.of(ErrorCode.PERSISTENCE_UNAVAILABLE))
        await c.send({"type": "message.send", "client_msg_id": "n", "body": secret_body})
        await c.recv_type("error")
        await c.close()
    formatter = JsonFormatter()
    # Everything the server process logged; the test's own websockets client is not the gateway.
    server_records = [r for r in caplog.records if not r.name.startswith("websockets.client")]
    lines = [formatter.format(r) for r in server_records]
    assert not any(secret_body in line for line in lines)
    sent = [json.loads(line) for line in lines if '"message.sent"' in line]
    assert len(sent) == 1
    assert sent[0]["actor_id"] == user and sent[0]["seq"] == 1
    assert sent[0]["message_id"] == "msg_00000001" and sent[0]["session_id"] == SESSION
    assert len(sent[0]["trace_id"]) == 32 and sent[0]["latency_ms"] is not None
