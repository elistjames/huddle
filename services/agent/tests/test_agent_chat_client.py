"""The agent's client for the chat service's internal API, against a fake chat service
(Deep Dive §3; §11 "the tool call goes through the same path"; idempotent commit on retry)."""

from __future__ import annotations

import httpx
import pytest
from agent_fake_chat import SERVICE_TOKEN, Faults, MemoryBackend, create_fake_chat_app
from agent_testkit import make_executor

from huddle_agent.chat_client import UNREACHABLE, ChatError, HttpChatClient
from huddle_agent.memory import MemoryStore, MemoryWorld
from huddle_agent.models.base import Stage
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.runner import new_run_id
from huddle_contracts.models import AgentState

SID = "ses_main"


async def no_sleep(_: float) -> None:
    return None


def client(
    world: MemoryWorld, faults: Faults | None = None, token: str = SERVICE_TOKEN
) -> HttpChatClient:
    app = create_fake_chat_app(MemoryBackend(world), faults)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat")
    return HttpChatClient("http://chat", token, client=http, sleep=no_sleep)


async def test_submit_uses_agent_sender_and_run_id(world: MemoryWorld) -> None:
    faults = Faults()
    c = client(world, faults)
    rid = new_run_id()
    resp = await c.submit_message(SID, world.agent.id, rid, "hello all", "00-abc")
    assert resp.seq == 1 and not resp.duplicate
    _, body = faults.calls[0]
    assert body["sender"] == {"type": "agent", "id": world.agent.id}
    assert body["client_msg_id"] == rid and body["trace_id"] == "00-abc"


async def test_idempotent_commit_when_the_response_is_lost(world: MemoryWorld) -> None:
    faults = Faults(after_commit=1)  # committed, but the first response is a 503
    c = client(world, faults)
    rid = new_run_id()
    resp = await c.submit_message(SID, world.agent.id, rid, "only once", None)
    assert resp.duplicate  # the retry replayed the original commit
    assert len(world.agent_messages()) == 1
    assert [name for name, _ in faults.calls] == ["submit", "submit"]
    assert {b["client_msg_id"] for _, b in faults.calls} == {rid}


async def test_retryable_errors_are_retried_with_the_same_key(world: MemoryWorld) -> None:
    faults = Faults().fail("submit", 503, 429)
    c = client(world, faults)
    resp = await c.submit_message(SID, world.agent.id, new_run_id(), "hi", None)
    assert resp.seq == 1


async def test_rate_limit_honours_retry_after(world: MemoryWorld) -> None:
    """The chat service rate-limits agent submits too (10 per 10 s): 429 with Retry-After."""
    slept: list[float] = []

    async def record(s: float) -> None:
        slept.append(s)

    app = create_fake_chat_app(MemoryBackend(world), Faults(retry_after="4").fail("submit", 429))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat")
    c = HttpChatClient("http://chat", SERVICE_TOKEN, client=http, sleep=record)
    resp = await c.submit_message(SID, world.agent.id, new_run_id(), "hi", None)
    assert resp.seq == 1 and slept == [4.0]


async def test_retries_are_bounded(world: MemoryWorld) -> None:
    c = client(world, Faults().fail("submit", 503, 503, 503))
    with pytest.raises(ChatError) as ei:
        await c.submit_message(SID, world.agent.id, new_run_id(), "hi", None)
    assert ei.value.code == "PERSISTENCE_UNAVAILABLE" and ei.value.retryable
    assert world.agent_messages() == []


async def test_non_retryable_errors_are_not_retried(world: MemoryWorld) -> None:
    world.sessions[SID].status = "closed"
    faults = Faults()
    c = client(world, faults)
    with pytest.raises(ChatError) as ei:
        await c.submit_message(SID, world.agent.id, new_run_id(), "hi", None)
    assert ei.value.code == "SESSION_CLOSED" and not ei.value.retryable
    assert len(faults.calls) == 1


async def test_bad_service_token_is_rejected(world: MemoryWorld) -> None:
    c = client(world, token="wrong")
    with pytest.raises(ChatError) as ei:
        await c.get_budget(SID)
    assert ei.value.code == "UNAUTHENTICATED" and ei.value.status == 401


async def test_non_json_5xx_is_retryable_internal(world: MemoryWorld) -> None:
    c = client(world, Faults().fail("budget", 502))
    budget = await c.get_budget(SID)
    assert budget.capacity_tokens == world.capacity_tokens


async def test_unreachable_chat_service() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://chat")
    c = HttpChatClient("http://chat", SERVICE_TOKEN, client=http, sleep=no_sleep)
    with pytest.raises(ChatError) as ei:
        await c.set_status(SID, AgentState.AWAY)
    assert ei.value.code == UNREACHABLE and ei.value.retryable


async def test_usage_is_not_retried_after_an_ambiguous_failure(world: MemoryWorld) -> None:
    faults = Faults().fail("usage", 503)
    c = client(world, faults)
    with pytest.raises(ChatError):
        await c.record_usage(SID, "run-1", 100)
    assert world.usage == []  # no blind retry that could double-debit


async def test_read_ack_status_usage_budget_and_names(world: MemoryWorld) -> None:
    world.post_human("usr_alice", "hi")
    faults = Faults()
    c = client(world, faults)
    await c.ack_read(SID, world.agent.id, 1)
    await c.set_status(SID, AgentState.WRITING)
    await c.record_usage(SID, "run-1", 1234)
    budget = await c.get_budget(SID)
    assert await c.display_name(SID, "usr_alice") == "Alice"
    assert await c.display_name(SID, "usr_alice") == "Alice"  # cached
    assert await c.display_name(SID, "usr_nobody") is None

    ack = next(b for n, b in faults.calls if n == "ack")
    assert ack["kind"] == "read" and ack["participant_id"] == world.agent.id
    assert world.agent_cursor() == 1
    assert world.status_log == [(SID, AgentState.WRITING)]
    assert budget.used_tokens == 1234 and budget.view.used_pct == 1
    assert sum(1 for n, _ in faults.calls if n == "member") == 2


async def test_run_commits_through_the_http_tool(world: MemoryWorld) -> None:
    """End to end over HTTP: the agent's message takes the same submit_message path."""
    model = ScriptedModel()
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "Via the tool.")
    faults = Faults(after_commit=1)
    ex = make_executor(world, model)
    ex._chat = client(world, faults)  # pyright: ignore[reportPrivateUsage]
    ex.presence._chat = ex._chat  # pyright: ignore[reportPrivateUsage]
    ex._store = MemoryStore(world)  # pyright: ignore[reportPrivateUsage]
    world.post_human("usr_alice", "Huddle AI, can you check?")
    out = await ex.execute(SID)
    assert out.message_id is not None
    [m] = world.agent_messages()
    assert m.body == "Via the tool." and m.client_msg_id == out.run_id
    names = [n for n, _ in faults.calls]
    assert names.count("submit") == 2  # retried after the lost response, still one message
    assert "usage" in names and "ack" in names and "status" in names
