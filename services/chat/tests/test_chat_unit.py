"""Unit tests that need no Postgres or Redis."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from chat_testkit import JWT_SECRET, SERVICE_TOKEN
from fastapi import FastAPI
from sqlalchemy import exc as sa_exc

from huddle_chat import auth
from huddle_chat.app import create_app
from huddle_chat.config import ConfigError, Settings, to_async_dsn
from huddle_chat.db import create_engine, is_persistence_failure
from huddle_chat.errors import ChatError
from huddle_chat.obs import JsonFormatter, TraceContext, current_trace, log_event
from huddle_chat.redis_ops import RedisOps
from huddle_chat.service import ChatService, effective_state, normalize_name
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import Sender, SubmitMessageRequest
from huddle_contracts.models import AgentState, SenderType

# Nothing listens on port 1: connections are refused immediately.
DEAD_PG = "postgresql://nobody@127.0.0.1:1/nothing"
DEAD_REDIS = "redis://127.0.0.1:1/0"
CMID = "0192f0b4-7c1e-7000-8000-000000000001"


def _settings(**overrides: str) -> Settings:
    env = {
        "HUDDLE_POSTGRES_DSN": DEAD_PG,
        "HUDDLE_REDIS_URL": DEAD_REDIS,
        "HUDDLE_JWT_SECRET": JWT_SECRET,
        "HUDDLE_SERVICE_TOKEN": SERVICE_TOKEN,
        **overrides,
    }
    return Settings.from_env(env)


# ---- config ------------------------------------------------------------------------------------


def test_settings_defaults_and_overrides() -> None:
    s = _settings()
    assert s.session_id == "ses_main"
    assert s.port == 8001
    assert s.access_ttl_seconds == 15 * 60
    assert s.refresh_ttl_seconds == 30 * 24 * 3600
    s = _settings(HUDDLE_SESSION_ID="ses_x", HUDDLE_CORS_ORIGINS="https://a.example, https://b")
    assert s.session_id == "ses_x"
    assert s.cors_origins == ("https://a.example", "https://b")
    assert _settings(HUDDLE_CORS_ORIGINS="").cors_origins == ()


@pytest.mark.parametrize(
    "missing",
    ["HUDDLE_POSTGRES_DSN", "HUDDLE_REDIS_URL", "HUDDLE_JWT_SECRET", "HUDDLE_SERVICE_TOKEN"],
)
def test_settings_require_secrets_and_stores(missing: str) -> None:
    with pytest.raises(ConfigError, match=missing):
        _settings(**{missing: ""})


def test_async_dsn() -> None:
    assert to_async_dsn("postgresql://u@h/d") == "postgresql+asyncpg://u@h/d"
    assert to_async_dsn("postgres://u@h/d") == "postgresql+asyncpg://u@h/d"
    with pytest.raises(ConfigError):
        to_async_dsn("mysql://u@h/d")


# ---- tracing and logs --------------------------------------------------------------------------


def test_traceparent_parse_and_child() -> None:
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = TraceContext.parse(tp)
    assert ctx is not None and ctx.traceparent == tp
    child = TraceContext.continue_from(tp)
    assert child.trace_id == ctx.trace_id and child.span_id != ctx.span_id
    for bad in (None, "", "garbage", "00-" + "0" * 32 + "-00f067aa0ba902b7-01"):
        assert TraceContext.parse(bad) is None
    assert len(TraceContext.continue_from("garbage").trace_id) == 32


def test_json_logs_have_section9_fields_and_never_bodies() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("huddle_chat")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    token = current_trace.set(TraceContext.new())
    try:
        log_event(
            "message.committed",
            session_id="ses_1",
            actor_id="usr_1",
            message_id="msg_1",
            seq=7,
            latency_ms=1.234,
            body="secret words",
            text="secret words",
        )
    finally:
        current_trace.reset(token)
        logger.removeHandler(handler)
    line = stream.getvalue().strip().splitlines()[-1]
    data = json.loads(line)
    for key in ("ts", "service", "trace_id", "session_id", "actor_id", "message_id", "seq"):
        assert data[key] is not None
    assert data["event"] == "message.committed" and data["latency_ms"] == 1.23
    assert "secret words" not in line


# ---- auth --------------------------------------------------------------------------------------


def test_access_token_roundtrip_and_errors() -> None:
    now = datetime.now(UTC)
    token = auth.issue_access_token("usr_1", JWT_SECRET, 900, now)
    assert auth.verify_access_token(token, JWT_SECRET) == "usr_1"
    expired = auth.issue_access_token("usr_1", JWT_SECRET, 900, now - timedelta(hours=1))
    with pytest.raises(ChatError) as e:
        auth.verify_access_token(expired, JWT_SECRET)
    assert e.value.code == ErrorCode.TOKEN_EXPIRED
    with pytest.raises(ChatError) as e:
        auth.verify_access_token(token, "another-secret-0123456789abcdef0123456789")
    assert e.value.code == ErrorCode.UNAUTHENTICATED


def test_bearer_and_refresh_hash() -> None:
    assert auth.bearer("Bearer abc") == "abc"
    assert auth.bearer("bearer  abc ") == "abc"
    assert auth.bearer("Basic abc") is None
    assert auth.bearer(None) is None
    t = auth.new_refresh_token()
    assert auth.hash_refresh_token(t) == auth.hash_refresh_token(t) != t


# ---- agent state and names ---------------------------------------------------------------------


def test_effective_state() -> None:
    f = AgentState.FOLLOWING
    assert effective_state("active", None, resting=False) == f
    assert effective_state("active", AgentState.WRITING, resting=False) == AgentState.WRITING
    assert effective_state("active", AgentState.AWAY, resting=False) == AgentState.AWAY
    assert effective_state("active", AgentState.WRITING, resting=True) == AgentState.RESTING
    assert effective_state("paused", AgentState.WRITING, resting=True) == AgentState.PAUSED
    # the orchestrator cannot pause itself or claim resting: those are chat-owned
    assert effective_state("active", AgentState.PAUSED, resting=False) == f
    assert effective_state("active", AgentState.RESTING, resting=False) == f


def test_normalize_name() -> None:
    assert normalize_name("  Maya   Chen ") == "Maya Chen"
    for bad in ("", "   ", "x" * 41):
        with pytest.raises(ChatError) as e:
            normalize_name(bad)
        assert e.value.code == ErrorCode.VALIDATION


# ---- submit_message validation and D-008 (no stores) --------------------------------------------


@pytest.fixture
async def dead_service() -> ChatService:
    s = _settings()
    return ChatService(create_engine(s.postgres_dsn), RedisOps.connect(s.redis_url), s)


def _req(
    body: str = "hi", sender: SenderType = SenderType.USER, cmid: str = CMID
) -> SubmitMessageRequest:
    return SubmitMessageRequest(
        session_id="ses_main", sender=Sender(type=sender, id="usr_1"), client_msg_id=cmid, body=body
    )


@pytest.mark.parametrize(
    ("req", "code"),
    [
        (_req(cmid="not-a-uuid"), ErrorCode.VALIDATION),
        (_req(body="   "), ErrorCode.VALIDATION),
        (_req(sender=SenderType.SYSTEM), ErrorCode.VALIDATION),
        (_req(body="x" * 4001), ErrorCode.MESSAGE_TOO_LONG),
        (_req(body="x" * 1001, sender=SenderType.AGENT), ErrorCode.MESSAGE_TOO_LONG),
    ],
)
async def test_submit_validation_runs_before_the_store(
    dead_service: ChatService, req: SubmitMessageRequest, code: ErrorCode
) -> None:
    with pytest.raises(ChatError) as e:
        await dead_service.submit_message(req)
    assert e.value.code == code
    assert e.value.client_msg_id == req.client_msg_id


async def test_postgres_down_rejects_send_with_persistence_unavailable(
    dead_service: ChatService,
) -> None:
    """D-008: consistency over availability. The send is rejected (retryable), never buffered."""
    with pytest.raises(ChatError) as e:
        await dead_service.submit_message(_req(body="x" * 4000))
    assert e.value.code == ErrorCode.PERSISTENCE_UNAVAILABLE
    assert e.value.body().retryable
    await dead_service.engine.dispose()


def test_persistence_failure_classification() -> None:
    assert is_persistence_failure(ConnectionRefusedError())
    assert is_persistence_failure(sa_exc.TimeoutError())
    assert is_persistence_failure(sa_exc.OperationalError("SELECT 1", {}, OSError()))
    assert not is_persistence_failure(sa_exc.IntegrityError("INSERT", {}, ValueError()))
    assert not is_persistence_failure(ValueError())


# ---- HTTP surface without stores ---------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    # No lifespan: only routes that fail before touching the service are exercised.
    return create_app(_settings())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://chat") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert TraceContext.parse(r.headers["traceparent"]) is not None


async def test_traceparent_is_continued(client: httpx.AsyncClient) -> None:
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    r = await client.get("/healthz", headers={"traceparent": tp})
    ctx = TraceContext.parse(r.headers["traceparent"])
    assert ctx is not None and ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize("token", [None, "wrong"])
async def test_internal_api_requires_service_token(
    client: httpx.AsyncClient, token: str | None
) -> None:
    headers = {} if token is None else {"X-Service-Token": token}
    r = await client.get("/internal/sessions/ses_main/budget", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_public_api_requires_bearer(client: httpx.AsyncClient) -> None:
    r = await client.get("/sessions/ses_main")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    expired = auth.issue_access_token(
        "usr_1", JWT_SECRET, 1, datetime.now(UTC) - timedelta(minutes=5)
    )
    r = await client.get("/sessions/ses_main", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_request_validation_uses_error_model_and_never_echoes_input(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.service = object()  # request validation fails before it is used
    r = await client.post(
        "/internal/messages",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"session_id": "ses_main", "body": "private words"},
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION" and err["retryable"] is False
    assert "private words" not in r.text


async def test_cors_preflight_for_configured_origin() -> None:
    app = create_app(_settings(HUDDLE_CORS_ORIGINS="https://huddle.example"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://chat"
    ) as c:
        r = await c.options(
            "/auth/guest",
            headers={
                "Origin": "https://huddle.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.headers["access-control-allow-origin"] == "https://huddle.example"
        r = await c.options(
            "/auth/guest",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in r.headers
