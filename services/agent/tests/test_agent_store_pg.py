"""PostgresStore against real Postgres (Deep Dive §4 tables, tests-only schema)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from agent_fake_chat import SERVICE_TOKEN, create_fake_chat_app
from agent_pg import AGENT_ID, SID, PgChatBackend
from sqlalchemy.ext.asyncio import AsyncEngine

from huddle_agent.chat_client import HttpChatClient
from huddle_agent.clock import SystemClock
from huddle_agent.config import Settings
from huddle_agent.domain import RunRecord, RunStatus, Summary, TriggerKind
from huddle_agent.models.base import Decision, ModelRequest, Stage, Usage
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.presence import Presence
from huddle_agent.runner import RunExecutor, new_run_id
from huddle_agent.store import PostgresStore

pytestmark = pytest.mark.integration


async def store_for(engine: AsyncEngine) -> PostgresStore:
    s = PostgresStore(engine)
    await s.probe_columns()
    return s


def run(
    owner: str = "w1", lease_s: float = 30, kind: TriggerKind = TriggerKind.MESSAGE
) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id=new_run_id(),
        session_id=SID,
        agent_id=AGENT_ID,
        trigger_kind=kind,
        trigger_seq=1,
        context_seq=1,
        status=RunStatus.TRIGGERED,
        lease_owner=owner,
        lease_expires_at=now + timedelta(seconds=lease_s),
    )


async def test_gate_snapshot(engine: AsyncEngine) -> None:
    chat = PgChatBackend(engine)
    await chat.insert(SID, "user", "usr_alice", "hi all")
    await chat.insert(SID, "system", "system", "Bob joined", kind="system")
    await chat.insert(SID, "agent", AGENT_ID, "hello", client_msg_id=None)
    await chat.insert(SID, "user", "usr_bob", "what's the plan?")
    store = await store_for(engine)
    snap = await store.load_gate_snapshot(SID, 30)
    assert snap is not None
    assert snap.session.last_seq == 4 and snap.session.last_human_message_at is not None
    assert [m.seq for m in snap.recent] == [1, 3, 4]  # text only
    assert snap.active_participants == 3  # two humans + the agent
    assert snap.last_agent_message is not None and snap.last_agent_message.seq == 3
    assert snap.agent.name == "Huddle AI" and snap.agent.config == {}
    assert snap.agent.fallback_model is None


async def test_agent_config_jsonb_and_missing_agent(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE agents SET config = CAST(:c AS jsonb)"),
            {"c": json.dumps({"cooldown_s": 45})},
        )
    store = await store_for(engine)
    snap = await store.load_gate_snapshot(SID, 30)
    assert snap is not None and snap.agent.config == {"cooldown_s": 45}
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE session_memberships SET status = 'removed' WHERE participant_type = 'agent'"
            )
        )
    assert await store.load_gate_snapshot(SID, 30) is None


async def test_window_loads_only_after_the_summary_and_up_to_the_cap(engine: AsyncEngine) -> None:
    chat = PgChatBackend(engine)
    for i in range(450):
        await chat.insert(SID, "user", "usr_alice", f"m{i} " + "a" * 60, trigger=False)
    store = await store_for(engine)
    await store.insert_summary(SID, AGENT_ID, Summary(100, "first hundred", 3, "m"))
    await store.insert_summary(SID, AGENT_ID, Summary(50, "older", 3, "m"))
    summary, window = await store.load_window(SID, AGENT_ID, max_tokens=100_000)
    assert summary is not None and summary.covers_through_seq == 100  # the latest wins
    assert [m.seq for m in window] == list(range(101, 451))  # paged past 200, stops at summary
    _, capped = await store.load_window(SID, AGENT_ID, max_tokens=1000)
    assert capped[-1].seq == 450 and len(capped) < 50  # newest first, capped


async def test_one_live_run_per_session(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    a, b = run("w1"), run("w2")
    await store.create_run(a, "v1")
    await store.create_run(b, "v1")
    assert await store.promote_to_eligible(a.id, "w1")
    assert not await store.promote_to_eligible(b.id, "w2")  # the partial unique index
    live = await store.find_live_run(SID, AGENT_ID)
    assert live is not None and live.id == a.id
    assert not await store.promote_to_eligible(a.id, "w2")  # wrong owner


async def test_takeover_only_after_lease_expiry(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    r = run("dead", lease_s=30)
    await store.create_run(r, "v1")
    assert await store.promote_to_eligible(r.id, "dead")
    now = datetime.now(UTC)
    assert await store.takeover_run(r.id, "w2", now, now + timedelta(seconds=30)) is None
    later = now + timedelta(seconds=31)
    taken = await store.takeover_run(r.id, "w2", later, later + timedelta(seconds=30))
    assert taken is not None and taken.id == r.id
    assert taken.attempts == 2 and taken.lease_owner == "w2"
    assert not await store.update_run(r.id, "dead", status=RunStatus.GENERATING)  # fenced
    assert await store.update_run(r.id, "w2", status=RunStatus.GENERATING)


async def test_terminal_runs_are_immutable_and_orphans_expire(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    r = run("w1")
    await store.create_run(r, "v1")
    assert await store.update_run(r.id, "w1", status=RunStatus.SKIPPED, skip_reason="cooldown")
    assert not await store.update_run(r.id, "w1", status=RunStatus.OBSERVED)
    assert not await store.renew_lease(r.id, "w1", datetime.now(UTC))
    orphan = run("dead", lease_s=-5)
    await store.create_run(orphan, "v1")
    assert await store.expire_orphans(datetime.now(UTC)) == 1
    got = await store.get_run(orphan.id)
    assert (
        got is not None and got.status == RunStatus.TIMED_OUT and got.error_code == "lease_expired"
    )


async def test_prompt_version_is_recorded_when_the_column_exists(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    r = run()
    await store.create_run(r, "v7")
    async with engine.connect() as conn:
        pv = (await conn.execute(sa.text("SELECT prompt_version FROM agent_runs"))).scalar_one()
    assert pv == "v7"


async def test_evaluated_seq_and_nudge_bookkeeping(engine: AsyncEngine) -> None:
    chat = PgChatBackend(engine)
    await chat.insert(SID, "user", "usr_alice", "a", trigger=False)
    await chat.insert(SID, "user", "usr_bob", "b", trigger=False)
    store = await store_for(engine)

    declined = run()
    await store.create_run(declined, "v1")
    await store.update_run(
        declined.id, "w1", status=RunStatus.SKIPPED, skip_reason="decider_declined", context_seq=2
    )
    cooldown = run()
    await store.create_run(cooldown, "v1")
    await store.update_run(
        cooldown.id, "w1", status=RunStatus.SKIPPED, skip_reason="cooldown", context_seq=9
    )
    snap = await store.load_gate_snapshot(SID, 30)
    assert snap is not None and snap.last_evaluated_seq == 2  # time-based skips don't count
    assert not snap.nudge_done_for_lull

    early = run(kind=TriggerKind.NUDGE)
    early.trigger_seq = 2
    await store.create_run(early, "v1")
    await store.update_run(early.id, "w1", status=RunStatus.SKIPPED, skip_reason="lull_not_over")
    assert [c.nudge_done for c in await store.rearm_candidates()] == [False]

    nudge = run(kind=TriggerKind.NUDGE)
    nudge.trigger_seq = 2
    await store.create_run(nudge, "v1")
    await store.update_run(
        nudge.id, "w1", status=RunStatus.SKIPPED, skip_reason="conversation_closed"
    )
    [cand] = await store.rearm_candidates()
    assert cand.nudge_done and cand.session_id == SID
    await chat.insert(SID, "user", "usr_alice", "new lull", trigger=False)
    assert not (await store.rearm_candidates())[0].nudge_done  # a human message re-arms


async def test_agent_message_for_run(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    r = run()
    await store.create_run(r, "v1")  # the run row exists before the commit (FK)
    chat = PgChatBackend(engine)
    resp = await chat.insert(SID, "agent", AGENT_ID, "posted", client_msg_id=r.id)
    assert await store.agent_message_for_run(SID, AGENT_ID, r.id) == (resp.message_id, resp.seq)
    assert await store.agent_message_for_run(SID, AGENT_ID, new_run_id()) is None


# ---- executor over real Postgres and the HTTP tool -----------------------------------------


def executor_for(
    engine: AsyncEngine, store: PostgresStore, model: ScriptedModel, worker: str
) -> tuple[RunExecutor, PgChatBackend]:
    backend = PgChatBackend(engine)
    app = create_fake_chat_app(backend)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat")
    chat = HttpChatClient("http://chat", SERVICE_TOKEN, client=http)
    ex = RunExecutor(
        store=store,
        chat=chat,
        models=model,
        settings=Settings(worker_id=worker),
        clock=SystemClock(),
        presence=Presence(chat),
    )
    return ex, backend


async def test_run_commits_via_http_and_links_the_run(engine: AsyncEngine) -> None:
    store = await store_for(engine)
    model = ScriptedModel().script(Stage.DECIDE, True).script(Stage.WRITE, "Tuesday works.")
    ex, backend = executor_for(engine, store, model, "w1")
    await backend.insert(SID, "user", "usr_alice", "Huddle AI, which day works?", trigger=False)
    out = await ex.execute(SID)
    assert out.status == RunStatus.OBSERVED
    assert await backend.agent_bodies() == ["Tuesday works."]
    assert await backend.agent_cursor() == 2
    async with engine.connect() as conn:
        row = (await conn.execute(sa.text("SELECT * FROM agent_runs"))).mappings().one()
        link = (
            await conn.execute(
                sa.text("SELECT agent_run_id FROM messages WHERE sender_type = 'agent'")
            )
        ).scalar_one()
    assert row["status"] == "observed" and row["output_message_id"] == out.message_id
    assert (
        row["input_tokens"] > 0 and row["latency_ms"] is not None and row["prompt_version"] == "v1"
    )
    assert link == out.run_id
    assert sum(t for _, t in backend.usage_log) == row["input_tokens"] + row["output_tokens"]


async def test_concurrent_workers_cannot_both_run(engine: AsyncEngine) -> None:
    store_a, store_b = await store_for(engine), await store_for(engine)
    gate = asyncio.Event()

    class Blocking(ScriptedModel):
        async def decide(self, req: ModelRequest) -> Decision:
            await gate.wait()
            return Decision(False, "no", Usage(req.model, 1, 1))

    ex_a, backend = executor_for(engine, store_a, Blocking(), "wa")
    ex_b, _ = executor_for(engine, store_b, Blocking(), "wb")
    await backend.insert(SID, "user", "usr_alice", "question?", trigger=False)
    task = asyncio.create_task(ex_a.execute(SID))
    for _ in range(100):
        if await store_a.find_live_run(SID, AGENT_ID):
            break
        await asyncio.sleep(0.02)
    out_b = await ex_b.execute(SID)
    assert out_b.busy
    gate.set()
    out_a = await task
    assert out_a.skip_reason == "decider_declined"
