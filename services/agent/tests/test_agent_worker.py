"""The worker against real Redis + Postgres and the fake chat service (Deep Dive §5 triggers and
debounce, §7 queue/retry and "Nudge timer lost", §11 integration: triggers and acks work, nudge
timers re-armed after a Redis flush, worker restart rebuilds from the store)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from agent_fake_chat import SERVICE_TOKEN, create_fake_chat_app
from agent_pg import AGENT_ID, SID, PgChatBackend
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from huddle_agent.chat_client import HttpChatClient
from huddle_agent.config import Settings
from huddle_agent.domain import ChatMessage, Summary
from huddle_agent.models.base import Decision, ModelRequest, Stage, Usage
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.runner import new_run_id
from huddle_agent.store import PostgresStore
from huddle_agent.worker import Worker
from huddle_contracts.models import AgentState

pytestmark = pytest.mark.integration

FAST = {"debounce_quiet_s": 0.25, "debounce_cap_s": 1.0}


async def wait_for(pred: Callable[[], Awaitable[bool]], within_s: float = 8.0) -> None:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if await pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def set_agent_config(engine: AsyncEngine, config: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE agents SET config = CAST(:c AS jsonb)"), {"c": json.dumps(config)}
        )


@dataclass
class Rig:
    worker: Worker
    backend: PgChatBackend
    store: PostgresStore
    redis: Redis
    settings: Settings

    async def pending(self) -> int:
        s = self.settings
        info = await self.redis.xpending(s.triggers_stream, s.triggers_group)
        return int(info["pending"])

    async def say(self, who: str, body: str, **kw: Any) -> int:
        return (await self.backend.insert(SID, "user", who, body, **kw)).seq

    async def run_rows(self) -> list[dict[str, Any]]:
        return await self.backend.runs()


def make_rig(
    engine: AsyncEngine,
    redis: Redis,
    settings: Settings,
    model: Any,
    store: PostgresStore | None = None,
) -> Rig:
    backend = PgChatBackend(engine, redis=redis, stream=settings.triggers_stream)
    app = create_fake_chat_app(backend)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat")
    chat = HttpChatClient("http://chat", SERVICE_TOKEN, client=http)
    st = store or PostgresStore(engine)
    worker = Worker(settings=settings, redis=redis, store=st, chat=chat, models=model, tick_s=0.02)
    return Rig(worker, backend, st, redis, settings)


@contextlib.asynccontextmanager
async def running(worker: Worker) -> AsyncGenerator[None]:
    task = asyncio.create_task(worker.run())

    async def started() -> bool:
        if task.done():
            task.result()  # surface startup errors
        return worker.started

    await wait_for(started)
    try:
        yield
    finally:
        worker.stop()
        await asyncio.wait_for(task, 5)


async def test_trigger_to_committed_message_end_to_end(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, FAST)
    model = ScriptedModel().script(Stage.DECIDE, True).script(Stage.WRITE, "Thursday, 3 pm.")
    rig = make_rig(engine, redis_client, redis_settings, model)
    async with running(rig.worker):
        await rig.say("usr_alice", "Huddle AI, when is the review?")

        async def posted() -> bool:
            return bool(await rig.backend.agent_bodies())

        await wait_for(posted)
        await wait_for(lambda: _zero(rig.pending))
    assert await rig.backend.agent_bodies() == ["Thursday, 3 pm."]
    assert await rig.backend.agent_cursor() == 2  # amber tick covers the question and the reply
    states = [s for _, s in rig.backend.statuses]
    assert states.index(AgentState.WRITING) < len(states) - 1
    assert states[-1] == AgentState.FOLLOWING
    [row] = [r for r in await rig.run_rows() if r["status"] == "observed"]
    assert row["trigger_kind"] == "message" and row["trigger_seq"] == 1


async def _zero(fn: Callable[[], Awaitable[int]]) -> bool:
    return await fn() == 0


async def test_burst_is_coalesced_into_one_evaluation(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, {"debounce_quiet_s": 0.4, "debounce_cap_s": 3.0})
    model = ScriptedModel()  # decider says no by default
    rig = make_rig(engine, redis_client, redis_settings, model)
    async with running(rig.worker):
        for i in range(4):
            await rig.say("usr_alice" if i % 2 else "usr_bob", f"quick message {i}")
            await asyncio.sleep(0.05)
        await wait_for(lambda: _zero(rig.pending))
        await asyncio.sleep(0.3)
    decides = [c for c in model.calls if c.stage == Stage.DECIDE]
    assert len(decides) == 1  # one evaluation for the whole burst
    assert decides[0].messages[-1].seq == 4


async def test_debounce_cap_evaluates_during_a_long_burst(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, {"debounce_quiet_s": 0.5, "debounce_cap_s": 0.8})
    model = ScriptedModel()
    rig = make_rig(engine, redis_client, redis_settings, model)
    async with running(rig.worker):
        start = time.monotonic()
        while time.monotonic() - start < 2.0:  # never 0.5 s of quiet
            await rig.say("usr_alice", "still typing more thoughts")
            await asyncio.sleep(0.15)
        first_decide = len([c for c in model.calls if c.stage == Stage.DECIDE])
    assert first_decide >= 1  # the 8 s (here 0.8 s) cap forced an evaluation mid-burst


async def test_triggers_during_a_run_set_dirty_and_reevaluate_once(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, FAST)
    release = asyncio.Event()
    seen: list[int] = []

    class Blocking(ScriptedModel):
        async def decide(self, req: ModelRequest) -> Decision:
            seen.append(req.messages[-1].seq)
            if len(seen) == 1:
                await release.wait()
            return Decision(False, "no", Usage(req.model, 10, 2))

    rig = make_rig(engine, redis_client, redis_settings, Blocking())
    async with running(rig.worker):
        await rig.say("usr_alice", "first")

        async def blocked() -> bool:
            return len(seen) == 1

        await wait_for(blocked)
        await rig.say("usr_bob", "second")
        await rig.say("usr_alice", "third")
        await asyncio.sleep(0.3)
        release.set()

        async def reevaluated() -> bool:
            return len(seen) >= 2

        await wait_for(reevaluated)
        await wait_for(lambda: _zero(rig.pending))
        await asyncio.sleep(0.5)
    assert seen == [1, 3]  # exactly one re-evaluation, against the latest seq


async def test_cooldown_skip_is_rechecked_when_the_cooldown_ends(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, {**FAST, "cooldown_s": 1.5})
    model = ScriptedModel()
    rig = make_rig(engine, redis_client, redis_settings, model)
    await rig.say("usr_alice", "first", trigger=False)
    await rig.backend.insert(SID, "agent", AGENT_ID, "an earlier point", trigger=False)
    async with running(rig.worker):
        await rig.say("usr_bob", "a follow-up question?")

        async def decided() -> bool:
            return any(c.stage == Stage.DECIDE for c in model.calls)

        await wait_for(decided)
    reasons = [r["skip_reason"] for r in await rig.run_rows()]
    assert "cooldown" in reasons and "decider_declined" in reasons


async def test_stale_trigger_is_skipped_but_still_resets_the_nudge_timer(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    model = ScriptedModel()
    rig = make_rig(engine, redis_client, redis_settings, model)
    await rig.worker.ensure_group()
    seq = await rig.say("usr_alice", "an old question?", trigger=False)
    old_ms = int(time.time() * 1000) - 60_000  # 60 s of queue lag
    await redis_client.xadd(
        redis_settings.triggers_stream,
        {"session_id": SID, "seq": str(seq), "message_id": "msg_x", "sender_type": "user"},
        id=f"{old_ms}-0",
    )
    async with running(rig.worker):
        await wait_for(lambda: _zero(rig.pending))
        await asyncio.sleep(0.3)
    assert [c for c in model.calls if c.stage == Stage.DECIDE] == []
    score = await redis_client.zscore(redis_settings.timers_zset, SID)
    # Armed from the latest human message; the late, older trigger (GT) cannot pull it earlier.
    assert score is not None and score >= old_ms / 1000 + 20 * 60


async def test_crashed_workers_trigger_is_reclaimed_and_its_run_taken_over(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    await set_agent_config(engine, FAST)
    model = ScriptedModel().script(Stage.DECIDE, True).script(Stage.WRITE, "Resumed after crash.")
    rig = make_rig(engine, redis_client, redis_settings, model)
    await rig.worker.ensure_group()
    await rig.say("usr_alice", "Huddle AI, summarize please?")
    # A worker that crashed: it read the trigger (pending, never acked) and left a live run.
    await redis_client.xreadgroup(
        redis_settings.triggers_group, "dead-worker", {redis_settings.triggers_stream: ">"}
    )
    crashed = new_run_id()
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO agent_runs (id, session_id, agent_id, trigger_kind, trigger_seq,"
                " context_seq, status, lease_owner, lease_expires_at, attempts) VALUES (:id,"
                " :s, :a, 'message', 1, 1, 'generating', 'dead-worker', :exp, 1)"
            ),
            {
                "id": crashed,
                "s": SID,
                "a": AGENT_ID,
                "exp": datetime.now(UTC) - timedelta(seconds=1),
            },
        )
    rig.settings = replace(redis_settings, reclaim_idle_s=0.5)
    rig.worker.s = rig.settings
    async with running(rig.worker):

        async def posted() -> bool:
            return bool(await rig.backend.agent_bodies())

        await wait_for(posted, within_s=10)
        await wait_for(lambda: _zero(rig.pending))
    assert await rig.backend.agent_bodies() == ["Resumed after crash."]
    async with engine.connect() as conn:
        cmid = (
            await conn.execute(
                sa.text("SELECT client_msg_id::text FROM messages WHERE sender_type = 'agent'")
            )
        ).scalar_one()
        run = (
            (await conn.execute(sa.text("SELECT * FROM agent_runs WHERE id = :i"), {"i": crashed}))
            .mappings()
            .one()
        )
    assert cmid == crashed  # same run id: the idempotency key survives the takeover
    assert run["attempts"] == 2 and run["status"] == "observed"


async def test_restart_rebuilds_from_summary_and_window_then_acks(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    loads: list[list[ChatMessage]] = []

    class Spy(PostgresStore):
        async def load_window(  # type: ignore[override]
            self, session_id: str, agent_id: str, max_tokens: int
        ) -> tuple[Summary | None, list[ChatMessage]]:
            summary, window = await super().load_window(session_id, agent_id, max_tokens)
            loads.append(window)
            return summary, window

    seeder = PgChatBackend(engine)
    for i in range(300):
        await seeder.insert(SID, "user", "usr_alice", f"line {i}", trigger=False)
    store = Spy(engine)
    await store.insert_summary(SID, AGENT_ID, Summary(250, "the first 250 lines", 5, "m"))
    model = ScriptedModel()
    rig = make_rig(engine, redis_client, redis_settings, model, store=store)
    async with running(rig.worker):
        await wait_for(lambda: _eq(rig.backend.agent_cursor, 300))
    assert [m.seq for m in loads[0]] == list(range(251, 301))  # never the full history
    assert model.calls == []
    assert (SID, AgentState.FOLLOWING) in rig.backend.statuses


async def _eq(fn: Callable[[], Awaitable[int]], value: int) -> bool:
    return await fn() == value


async def test_nudge_timer_rearmed_after_redis_flush_and_fires_once(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    model = (
        ScriptedModel()
        .script(Stage.DECIDE, True)
        .script(Stage.WRITE, "Did we ever pick the venue? Happy to list options.")
    )
    rig = make_rig(engine, redis_client, redis_settings, model)
    lull_start = datetime.now(UTC) - timedelta(minutes=25)
    await rig.say("usr_alice", "should we book the venue?", created_at=lull_start, trigger=False)
    # Redis was flushed: no timer exists. Startup re-arms it from last_human_message_at.
    assert await redis_client.zscore(redis_settings.timers_zset, SID) is None
    await rig.worker.startup()
    score = await redis_client.zscore(redis_settings.timers_zset, SID)
    assert score is not None
    assert abs(score - (lull_start + timedelta(minutes=20)).timestamp()) < 1

    assert await rig.worker.timers_once() == 1
    out = await rig.worker.evaluate(SID)
    assert out is not None and out.message_id is not None
    assert await rig.backend.agent_bodies() == [
        "Did we ever pick the venue? Happy to list options."
    ]
    [nudge] = [r for r in await rig.run_rows() if r["trigger_kind"] == "nudge"]
    assert nudge["status"] == "observed" and nudge["trigger_seq"] == 1

    # Flushed again: the lull already had its nudge, so nothing is re-armed (once per lull).
    await redis_client.delete(redis_settings.timers_zset)
    assert await rig.worker.rearm_timers() == 0
    assert await redis_client.zscore(redis_settings.timers_zset, SID) is None

    # Only a new human message re-arms it.
    now = datetime.now(UTC)
    await rig.worker.reset_timer(SID, now)
    score = await redis_client.zscore(redis_settings.timers_zset, SID)
    assert score is not None and abs(score - (now + timedelta(minutes=20)).timestamp()) < 1


async def test_no_nudge_after_a_closed_conversation(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    model = ScriptedModel().script(Stage.DECIDE, False)
    rig = make_rig(engine, redis_client, redis_settings, model)
    await rig.say(
        "usr_alice",
        "sounds good, thanks all, bye",
        created_at=datetime.now(UTC) - timedelta(minutes=21),
        trigger=False,
    )
    await rig.worker.startup()
    assert await rig.worker.timers_once() == 1
    out = await rig.worker.evaluate(SID)
    assert out is not None and out.skip_reason == "conversation_closed"
    assert await rig.backend.agent_bodies() == []
    assert await rig.worker.rearm_timers() == 0  # the lull's single attempt is used


async def test_a_due_timer_is_claimed_by_exactly_one_worker(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    a = make_rig(engine, redis_client, redis_settings, ScriptedModel())
    b = make_rig(engine, redis_client, replace(redis_settings, worker_id="other"), ScriptedModel())
    await redis_client.zadd(redis_settings.timers_zset, {SID: time.time() - 1})
    got = await asyncio.gather(a.worker.timers_once(), b.worker.timers_once())
    assert sorted(got) == [0, 1]


async def test_startup_waits_for_the_chat_schema(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    rig = make_rig(engine, redis_client, redis_settings, ScriptedModel())
    async with engine.begin() as conn:
        await conn.execute(sa.text("ALTER TABLE agent_runs RENAME TO agent_runs_hidden"))
    task = asyncio.create_task(rig.worker.run())
    try:
        await asyncio.sleep(0.5)
        assert not rig.worker.started and not task.done()  # retrying, /healthz is 503
        async with engine.begin() as conn:
            await conn.execute(sa.text("ALTER TABLE agent_runs_hidden RENAME TO agent_runs"))

        async def started() -> bool:
            return rig.worker.started

        await wait_for(started)
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("ALTER TABLE IF EXISTS agent_runs_hidden RENAME TO agent_runs")
            )
        rig.worker.stop()
        await asyncio.wait_for(task, 5)


async def test_stream_flush_recreates_the_consumer_group(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    rig = make_rig(engine, redis_client, redis_settings, ScriptedModel())
    await rig.worker.ensure_group()
    await redis_client.delete(redis_settings.triggers_stream)
    assert await rig.worker.consume_once() == 0
    groups = await redis_client.xinfo_groups(redis_settings.triggers_stream)
    assert [g["name"] for g in groups] == ["agent-workers"]


async def test_kill_switch_acks_triggers_and_never_runs(
    engine: AsyncEngine, redis_client: Redis, redis_settings: Settings
) -> None:
    model = ScriptedModel()
    rig = make_rig(engine, redis_client, replace(redis_settings, agents_disabled=True), model)
    async with running(rig.worker):
        await rig.say("usr_alice", "Huddle AI?")
        await wait_for(lambda: _zero(rig.pending))
        await asyncio.sleep(0.3)
    assert model.calls == [] and await rig.run_rows() == []
    assert (SID, AgentState.AWAY) in rig.backend.statuses
    assert await rig.backend.agent_cursor() == 0  # inactive: no read ack (D-020)
