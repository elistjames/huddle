"""Unit tests that need no stores: auth, tracing, queue/merge logic, chat client, logs, config."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from gw_support import JWT_SECRET, SERVICE_TOKEN, created_frame, make_token, raw_token

from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.internal import (
    AckDeliveryRequest,
    Sender,
    SubmitMessageError,
    SubmitMessageRequest,
)
from huddle_contracts.models import SenderType
from huddle_contracts.ws import Pong
from huddle_gateway import tracing
from huddle_gateway.auth import AuthError, verify_token
from huddle_gateway.chat_client import ChatClient, ChatError
from huddle_gateway.config import ConfigError, Settings
from huddle_gateway.logs import JsonFormatter, log_event, logger
from huddle_gateway.outbound import OutboundQueue, Outgoing, SeqWindow, merge_buffered

# ---- auth ------------------------------------------------------------------------------------


def test_verify_token_accepts_a_valid_access_token() -> None:
    ident = verify_token(make_token("usr_1"), JWT_SECRET)
    assert ident.user_id == "usr_1"
    assert ident.expires_at > 0
    assert verify_token(make_token("usr_1", typ="access"), JWT_SECRET).user_id == "usr_1"


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (make_token("u", exp_in=-10), ErrorCode.TOKEN_EXPIRED),
        (make_token("u", secret="x" * 64), ErrorCode.UNAUTHENTICATED),
        (make_token("u", exp_in=36_000, typ="refresh"), ErrorCode.UNAUTHENTICATED),
        (make_token("", exp_in=36_000), ErrorCode.UNAUTHENTICATED),
        ("not-a-jwt", ErrorCode.UNAUTHENTICATED),
        (raw_token({"sub": "u"}, JWT_SECRET, "HS256"), ErrorCode.UNAUTHENTICATED),
        (raw_token({"sub": "u", "exp": 2**40}, None, "none"), ErrorCode.UNAUTHENTICATED),
        (
            raw_token({"sub": "u", "exp": 2**40}, JWT_SECRET, "HS512"),
            ErrorCode.UNAUTHENTICATED,
        ),
    ],
    ids=[
        "expired",
        "bad-signature",
        "refresh",
        "empty-sub",
        "garbage",
        "no-exp",
        "alg-none",
        "hs512",
    ],
)
def test_verify_token_rejections(token: str, code: ErrorCode) -> None:
    with pytest.raises(AuthError) as info:
        verify_token(token, JWT_SECRET)
    assert info.value.code is code


# ---- tracing ---------------------------------------------------------------------------------


def test_new_trace_is_valid_traceparent() -> None:
    t = tracing.start_span()
    assert len(t.trace_id) == 32 and len(t.span_id) == 16
    assert t.traceparent == f"00-{t.trace_id}-{t.span_id}-01"
    assert tracing.start_span().trace_id != t.trace_id


def test_trace_continues_valid_parent_and_ignores_garbage() -> None:
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
    t = tracing.start_span(parent)
    assert t.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert t.span_id != "00f067aa0ba902b7"
    assert t.flags == "00"
    for bad in ("garbage", "00-" + "0" * 32 + "-00f067aa0ba902b7-01", "01-abc"):
        assert tracing.start_span(bad).trace_id != "0" * 32


# ---- outbound queue and merge ----------------------------------------------------------------


def item(seq: int | None = None, size: int = 10, ftype: str = "message.created") -> Outgoing:
    return Outgoing(text="x" * size, nbytes=size, type=ftype, session_id="s", seq=seq)


def test_queue_bounds_events_and_bytes() -> None:
    q = OutboundQueue(max_events=3, max_bytes=100)
    assert q.put(item(1)) and q.put(item(2)) and q.put(item(3))
    assert not q.put(item(4))  # 500-event style cap
    q2 = OutboundQueue(max_events=10, max_bytes=100)
    assert q2.put(item(1, size=60))
    assert not q2.put(item(2, size=50))  # 1 MB style cap
    assert q2.put(item(3, size=40))
    q3 = OutboundQueue(max_events=10, max_bytes=100)
    assert q3.put(item(1, size=500))  # one oversized frame fits an empty queue


async def test_queue_get_wait_for_room_and_wait_empty() -> None:
    q = OutboundQueue(max_events=1, max_bytes=100)
    assert q.put(item(1))
    waiter = asyncio.create_task(q.wait_for_room(item(2)))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    got = await q.get()
    assert got.seq == 1
    await asyncio.wait_for(waiter, 1)
    empty = asyncio.create_task(q.wait_empty())
    await asyncio.sleep(0.01)
    assert not empty.done()  # one frame is still in flight
    q.task_done()
    await asyncio.wait_for(empty, 1)


def test_merge_buffered_dedupes_and_orders() -> None:
    receipt = item(None, ftype="receipt.updated")
    buffered = [item(9), item(12), receipt, item(11), item(11), item(10)]
    merged = merge_buffered(buffered, replayed_through=10)
    assert [(m.type, m.seq) for m in merged] == [
        ("message.created", 11),
        ("message.created", 12),
        ("receipt.updated", None),
    ]


def test_seq_window_forgets_oldest() -> None:
    w = SeqWindow(size=3)
    for s in (1, 2, 3, 4):
        w.add(s)
    assert 1 not in w and 4 in w and 2 in w


def test_outgoing_reads_seq_from_frames() -> None:
    frame = created_frame("ses_x", 42)
    out = Outgoing.from_frame(frame)
    assert (out.type, out.session_id, out.seq) == ("message.created", "ses_x", 42)
    assert json.loads(out.text) == frame
    del frame["sequence"]
    assert Outgoing.from_frame(frame).seq == 42
    pong = Outgoing.from_model(Pong())
    assert (pong.type, pong.seq, pong.nbytes) == ("pong", None, len(pong.text))


# ---- chat client -----------------------------------------------------------------------------


def client_for(handler: httpx.MockTransport) -> ChatClient:
    return ChatClient("http://chat.test", SERVICE_TOKEN, transport=handler)


def submit_req() -> SubmitMessageRequest:
    return SubmitMessageRequest(
        session_id="ses_1",
        sender=Sender(type=SenderType.USER, id="usr_1"),
        client_msg_id="c1",
        body="b",
    )


async def test_chat_client_sends_service_token_and_quotes_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"is_active": True})

    chat = client_for(httpx.MockTransport(handler))
    member = await chat.check_member("../admin", "usr/1")
    assert member.is_active
    assert seen[0].headers["X-Service-Token"] == SERVICE_TOKEN
    assert seen[0].url.raw_path == b"/internal/sessions/..%2Fadmin/members/usr%2F1"
    await chat.aclose()


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (
            httpx.Response(
                409,
                content=SubmitMessageError(
                    error=ErrorBody.of(ErrorCode.SESSION_CLOSED, "closed")
                ).model_dump_json(),
            ),
            ErrorCode.SESSION_CLOSED,
            False,
        ),
        (httpx.Response(429), ErrorCode.RATE_LIMITED, True),
        (httpx.Response(500, text="boom"), ErrorCode.INTERNAL, True),
        (httpx.Response(401), ErrorCode.INTERNAL, True),
        (httpx.Response(200, json={"nope": 1}), ErrorCode.INTERNAL, True),
    ],
    ids=["structured", "429", "500", "401", "malformed-200"],
)
async def test_chat_client_error_mapping(
    response: httpx.Response, code: ErrorCode, retryable: bool
) -> None:
    chat = client_for(httpx.MockTransport(lambda _r: response))
    with pytest.raises(ChatError) as info:
        await chat.submit_message(submit_req())
    assert info.value.body.code is code
    assert info.value.body.retryable is retryable
    await chat.aclose()


async def test_chat_client_transport_errors_are_retryable_internal() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    chat = client_for(httpx.MockTransport(handler))
    with pytest.raises(ChatError) as info:
        await chat.ack(AckDeliveryRequest(participant_id="u", session_id="s", up_to_seq=1))
    assert info.value.body.code is ErrorCode.INTERNAL and info.value.body.retryable
    await chat.aclose()


async def test_check_member_404_means_not_member() -> None:
    chat = client_for(httpx.MockTransport(lambda _r: httpx.Response(404)))
    assert not (await chat.check_member("s", "u")).is_active
    await chat.aclose()


# ---- logs and config -------------------------------------------------------------------------


def test_json_logs_carry_required_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=logger.name)
    log_event("message.sent", trace_id="t" * 32, session_id="s", actor_id="u", seq=3)
    line = json.loads(JsonFormatter().format(caplog.records[-1]))
    for key in ("ts", "service", "trace_id", "session_id", "actor_id", "message_id", "seq"):
        assert key in line
    assert line["event"] == "message.sent"
    assert line["latency_ms"] is None and line["message_id"] is None
    assert (line["service"], line["seq"], line["level"]) == ("gateway", 3, "info")


def test_settings_from_env() -> None:
    env = {"HUDDLE_JWT_SECRET": "s", "HUDDLE_SERVICE_TOKEN": "t", "HUDDLE_GATEWAY_ID": "gw-7"}
    s = Settings.from_env(env)
    assert (s.gateway_id, s.port, s.auth_timeout_s) == ("gw-7", 8000, 5.0)
    assert (s.heartbeat_interval_s, s.heartbeat_timeout_s, s.conn_ttl_s) == (20, 60, 60)
    assert (s.queue_max_events, s.queue_max_bytes, s.max_conns_per_user) == (500, 1 << 20, 5)
    generated = Settings.from_env({"HUDDLE_JWT_SECRET": "s", "HUDDLE_SERVICE_TOKEN": "t"})
    assert generated.gateway_id.startswith("gw-")
    with pytest.raises(ConfigError):
        Settings.from_env({"HUDDLE_SERVICE_TOKEN": "t"})
    with pytest.raises(ConfigError):
        Settings.from_env({**env, "HUDDLE_GATEWAY_PORT": "eighty"})
