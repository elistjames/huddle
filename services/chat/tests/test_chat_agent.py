"""Agent controls, live status and the fixed-window budget (Deep Dive §5, §6, §8; D-019, D-020)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from chat_testkit import Clock, FakeGateways

from huddle_chat import store
from huddle_chat.errors import ChatError
from huddle_chat.schema import agent_budgets, agents, audit_events, messages
from huddle_chat.service import ChatService
from huddle_contracts import budget as budget_math
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import RecordUsageRequest
from huddle_contracts.models import AgentState

pytestmark = pytest.mark.integration


async def agent_id(service: ChatService) -> str:
    async with service.tx() as conn:
        agent = await store.session_agent(conn, service.settings.session_id)
    assert agent is not None
    return agent.id


async def usage(service: ChatService, tokens: int) -> int:
    state = await service.record_usage(
        service.settings.session_id, RecordUsageRequest(run_id="run-1", tokens=tokens)
    )
    return state.used_tokens


# ---- budget ------------------------------------------------------------------------------------


async def test_budget_is_created_with_an_aligned_fixed_window(
    service: ChatService, clock: Clock
) -> None:
    state = await service.budget_state(service.settings.session_id)
    assert state.capacity_tokens == budget_math.DEFAULT_CAPACITY_TOKENS
    assert state.window_seconds == 86_400 and state.used_tokens == 0
    assert state.window_started_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert state.refills_at == datetime(2026, 10, 1, tzinfo=UTC) == state.view.refills_at
    assert state.view.used_pct == 0 and state.remaining_tokens == state.capacity_tokens


async def test_budget_debits_and_resets_only_at_the_window_boundary(
    service: ChatService, clock: Clock
) -> None:
    sid = service.settings.session_id
    assert await usage(service, 100_000) == 100_000
    clock.now = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    assert await usage(service, 1) == 100_001  # no gradual refill inside the window
    state = await service.budget_state(sid)
    assert state.view.used_pct == 21  # rounds up: 20.0002% -> 21
    clock.now = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
    state = await service.budget_state(sid)  # lazy reset on read
    assert state.used_tokens == 0 and state.window_started_at == clock.now
    assert state.refills_at == datetime(2026, 10, 2, tzinfo=UTC)
    async with service.tx() as conn:  # the reset was persisted
        row = (await conn.execute(sa.select(agent_budgets))).one()
    assert row.used_tokens == 0 and row.window_started_at == clock.now


async def test_budget_skips_idle_windows(service: ChatService, clock: Clock) -> None:
    await usage(service, 5_000)
    clock.now = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
    state = await service.budget_state(service.settings.session_id)
    assert state.used_tokens == 0
    assert state.window_started_at == datetime(2026, 10, 5, tzinfo=UTC)


async def test_first_debit_of_a_new_window_announces_the_end_of_resting(
    service: ChatService, gateways: FakeGateways, clock: Clock
) -> None:
    ana = (await service.guest_join("Ana")).user_id
    await gateways.start("g1")
    await gateways.connect(ana, "g1")
    await usage(service, budget_math.DEFAULT_CAPACITY_TOKENS)  # resting
    clock.now = datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC)
    assert await usage(service, 0) == 0  # the roll alone changes what users see
    await gateways.drain()
    statuses = [e["frame"] for e in gateways.frames("g1", "agent.status")]
    assert [(s["state"], s["budget"]["used_pct"]) for s in statuses] == [
        ("resting", 100),
        ("following", 0),
    ]
    assert statuses[-1]["budget"]["refills_at"] == "2026-10-02T00:00:00Z"


async def test_resting_when_less_than_one_turn_left_and_status_broadcast(
    service: ChatService, gateways: FakeGateways
) -> None:
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    await gateways.start("g1")
    await gateways.connect(ana, "g1")
    cap = budget_math.DEFAULT_CAPACITY_TOKENS
    await usage(service, cap - budget_math.FULL_TURN_TOKENS)  # exactly one turn left
    assert (await service.agent_view(sid, ana)).state == AgentState.FOLLOWING
    await usage(service, 1)
    view = await service.agent_view(sid, ana)
    assert view.state == AgentState.RESTING
    assert view.budget.refills_at == datetime(2026, 10, 1, tzinfo=UTC)
    await usage(service, 0)  # no visible change: no broadcast
    await gateways.drain()
    statuses = [e["frame"] for e in gateways.frames("g1", "agent.status")]
    assert [s["state"] for s in statuses] == ["following", "resting"]
    assert statuses[-1]["budget"] == {"used_pct": 97, "refills_at": "2026-10-01T00:00:00Z"}
    assert "used_tokens" not in statuses[-1]["budget"]  # users never see raw tokens (D-019)


# ---- live status -------------------------------------------------------------------------------


async def test_orchestrator_status_is_broadcast_as_effective_state(
    service: ChatService, gateways: FakeGateways
) -> None:
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    await gateways.start("g1")
    await gateways.connect(ana, "g1")
    assert (await service.set_agent_status(sid, AgentState.WRITING)).state == AgentState.WRITING
    assert (await service.agent_view(sid, ana)).state == AgentState.WRITING
    assert (await service.set_agent_status(sid, AgentState.AWAY)).state == AgentState.AWAY
    await service.patch_agent(sid, ana, status="paused", name=None)
    # a paused agent stays paused whatever the orchestrator reports
    assert (await service.set_agent_status(sid, AgentState.WRITING)).state == AgentState.PAUSED
    await gateways.drain()
    states = [e["frame"]["state"] for e in gateways.frames("g1", "agent.status")]
    assert states == ["writing", "away", "paused", "paused"]
    assert all(e["frame"]["name"] == "Huddle AI" for e in gateways.frames("g1", "agent.status"))


# ---- member controls: pause, resume, rename ------------------------------------------------------


async def test_pause_resume_rename_write_audit_note_and_status(
    service: ChatService, gateways: FakeGateways
) -> None:
    sid = service.settings.session_id
    maya = (await service.guest_join("Maya Chen")).user_id
    eli = (await service.guest_join("Eli")).user_id
    aid = await agent_id(service)
    await gateways.start("g1")
    await gateways.connect(eli, "g1")

    view = await service.patch_agent(sid, maya, status="paused", name=None)
    assert view.state == AgentState.PAUSED
    view = await service.patch_agent(sid, eli, status="active", name="Sage")
    assert view.state == AgentState.FOLLOWING and view.name == "Sage"
    same = await service.patch_agent(sid, eli, status="active", name="Sage")  # no-op
    assert same.name == "Sage"

    async with service.tx() as conn:
        notes = (
            await conn.execute(
                sa.select(messages.c.body, messages.c.sender_type, messages.c.kind)
                .where(messages.c.session_id == sid, messages.c.kind == "system")
                .order_by(messages.c.seq)
            )
        ).all()
        audit = (
            await conn.execute(
                sa.select(audit_events.c.actor_id, audit_events.c.action, audit_events.c.target)
                .where(audit_events.c.action.like("agent.%"))
                .order_by(audit_events.c.id)
            )
        ).all()
        status = (await conn.execute(sa.select(agents.c.status, agents.c.name))).one()
    assert [n.body for n in notes] == [
        "Huddle AI joined",
        "Maya Chen joined",
        "Eli joined",
        "Maya Chen paused Huddle AI",
        "Eli resumed Huddle AI",
        "Eli renamed the AI to Sage",
    ]
    assert {(n.sender_type, n.kind) for n in notes} == {("system", "system")}
    assert [(a.actor_id, a.action, a.target) for a in audit] == [
        (maya, "agent.paused", aid),
        (eli, "agent.resumed", aid),
        (eli, "agent.renamed", aid),
    ]
    assert (status.status, status.name) == ("active", "Sage")

    await gateways.drain()
    created = [e["frame"]["message"]["body"] for e in gateways.frames("g1", "message.created")]
    assert created[-3:] == [
        "Maya Chen paused Huddle AI",
        "Eli resumed Huddle AI",
        "Eli renamed the AI to Sage",
    ]
    statuses = [e["frame"] for e in gateways.frames("g1", "agent.status")]
    assert [(s["state"], s["name"]) for s in statuses] == [
        ("paused", "Huddle AI"),
        ("following", "Sage"),
    ]


async def test_patch_agent_validation_and_membership(service: ChatService) -> None:
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    for kwargs in (
        {"status": None, "name": None},
        {"status": "removed", "name": None},
        {"status": None, "name": "   "},
        {"status": None, "name": "x" * 41},
    ):
        with pytest.raises(ChatError) as e:
            await service.patch_agent(sid, ana, **kwargs)
        assert e.value.code == ErrorCode.VALIDATION
    with pytest.raises(ChatError) as e:
        await service.patch_agent(sid, "usr_stranger", status="paused", name=None)
    assert e.value.code == ErrorCode.NOT_MEMBER
    # "following" is accepted as resume
    await service.patch_agent(sid, ana, status="paused", name=None)
    assert (await service.patch_agent(sid, ana, status="following", name=None)).state == (
        AgentState.FOLLOWING
    )
