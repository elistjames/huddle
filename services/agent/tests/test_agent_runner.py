"""Run lifecycle (Deep Dive §5, §7, §11: retry/timeout, lease expiry, superseded runs, idempotent
commit, restart rebuild). In-memory store + chat; scripted model."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from agent_testkit import make_executor

from huddle_agent.clock import FakeClock
from huddle_agent.config import Settings
from huddle_agent.domain import RunRecord, RunStatus, TriggerKind
from huddle_agent.memory import MemoryStore, MemoryWorld
from huddle_agent.models.base import (
    Decision,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
    Stage,
    Usage,
)
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.runner import RunExecutor, new_run_id
from huddle_contracts.budget import FULL_TURN_TOKENS
from huddle_contracts.models import AgentState, SenderType


def ask(world: MemoryWorld, body: str = "what is the deadline for the launch?") -> None:
    world.post_human("usr_alice", body)


def run_row(world: MemoryWorld, run_id: str | None) -> RunRecord:
    assert run_id is not None
    return world.runs[run_id]


# ---- happy path -----------------------------------------------------------------------------


async def test_speaks_through_the_single_write_path(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    states_at_decide: list[list[AgentState]] = []

    def decide(req: ModelRequest) -> bool:
        states_at_decide.append(world.states())
        return True

    model.script(Stage.DECIDE, decide).script(Stage.WRITE, "Friday at noon, per the plan.")
    out = await executor.execute(world.session_id, trace_id="00-trace")

    assert out.status == RunStatus.OBSERVED
    [agent_msg] = world.agent_messages()
    assert agent_msg.body == "Friday at noon, per the plan."
    assert agent_msg.client_msg_id == out.run_id  # idempotency key is the run id
    assert agent_msg.sender_type == SenderType.AGENT
    # "writing" only during the writer stage, never during the decider.
    assert AgentState.WRITING not in states_at_decide[0]
    assert world.states() == [AgentState.WRITING, AgentState.FOLLOWING]
    assert model.stages() == [Stage.DECIDE, Stage.WRITE, Stage.SAFETY]
    # Read ack (amber tick) covers the agent's own message; tokens are debited.
    assert world.agent_cursor() == agent_msg.seq
    row = run_row(world, out.run_id)
    assert row.output_message_id == agent_msg.id
    assert row.input_tokens > 0 and row.output_tokens > 0
    assert row.latency_ms is not None
    assert world.usage == [(world.session_id, out.run_id, row.input_tokens + row.output_tokens)]


async def test_decider_declines(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world, "thanks, see you")
    model.script(Stage.DECIDE, False)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.SKIPPED and out.skip_reason == "decider_declined"
    assert world.agent_messages() == []
    assert AgentState.WRITING not in world.states()
    assert world.agent_cursor() == 1  # it read the message
    assert world.usage and world.usage[0][2] > 0  # decider tokens debited


async def test_redelivered_trigger_is_not_reevaluated(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, False)
    await executor.execute(world.session_id)
    out = await executor.execute(world.session_id)
    assert out.skip_reason == "already_evaluated"
    assert model.stages() == [Stage.DECIDE]


# ---- validation -----------------------------------------------------------------------------


async def test_too_long_output_is_regenerated_once(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "x" * 1001, "Short answer.")
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.OBSERVED
    assert [m.body for m in world.agent_messages()] == ["Short answer."]
    writes = [c for c in model.calls if c.stage == Stage.WRITE]
    assert writes[1].hints["too_long"] is True


async def test_too_long_twice_is_discarded(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "x" * 1001, "y" * 5000)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.FAILED and out.error_code == "output_too_long"
    assert world.agent_messages() == []  # errors never reach the thread
    assert world.states()[-1] == AgentState.FOLLOWING


async def test_unsafe_output_is_not_committed(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "bad").script(Stage.SAFETY, False)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.FAILED and out.error_code == "unsafe_output"
    assert world.agent_messages() == []
    assert world.states()[-1] == AgentState.FOLLOWING


# ---- staleness and cancellation ------------------------------------------------------------


async def test_stale_run_replans_once_with_new_messages(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    def write_then_interrupt(req: ModelRequest) -> str:
        world.post_human("usr_bob", "also, what about the budget?")
        return "draft one"

    model.script(Stage.DECIDE, True, True).script(Stage.WRITE, write_then_interrupt, "draft two")
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.OBSERVED
    assert [m.body for m in world.agent_messages()] == ["draft two"]
    decides = [c for c in model.calls if c.stage == Stage.DECIDE]
    assert len(decides) == 2 and decides[1].messages[-1].seq == 2  # re-planned with msg 2


async def test_still_stale_after_replan_is_superseded(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    def interrupt(req: ModelRequest) -> str:
        world.post_human("usr_bob", "wait, one more thing")
        return "draft"

    model.script(Stage.DECIDE, True, True).script(Stage.WRITE, interrupt, interrupt)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.SUPERSEDED
    assert world.agent_messages() == []
    assert world.states()[-1] == AgentState.FOLLOWING


async def test_nudge_cancelled_when_a_human_speaks(
    world: MemoryWorld, clock: FakeClock, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world, "should we pick a venue?")
    clock.advance(21 * 60)

    def interrupt(req: ModelRequest) -> str:
        world.post_human("usr_bob", "I'm back")
        return "Anyone still up for picking a venue?"

    model.script(Stage.DECIDE, True).script(Stage.WRITE, interrupt)
    out = await executor.execute(world.session_id, TriggerKind.NUDGE)
    assert out.status == RunStatus.CANCELLED and out.error_code == "human_message"
    assert world.agent_messages() == []


async def test_nudge_run_records_the_lull(
    world: MemoryWorld, clock: FakeClock, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world, "should we pick a venue?")
    clock.advance(21 * 60)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "Still keen to pick a venue?")
    out = await executor.execute(world.session_id, TriggerKind.NUDGE)
    assert out.status == RunStatus.OBSERVED
    row = run_row(world, out.run_id)
    assert row.trigger_kind == TriggerKind.NUDGE and row.trigger_seq == 1
    again = await executor.execute(world.session_id, TriggerKind.NUDGE)
    assert again.skip_reason in ("nudge_done", "agent_spoke_last")


async def test_closed_conversation_gets_no_nudge(
    world: MemoryWorld, clock: FakeClock, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world, "sounds good, thanks all, bye")
    clock.advance(21 * 60)
    model.script(Stage.DECIDE, False)
    out = await executor.execute(world.session_id, TriggerKind.NUDGE)
    assert out.skip_reason == "conversation_closed"
    assert world.agent_messages() == []


async def test_paused_mid_run_cancels_before_commit(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    def pause(req: ModelRequest) -> str:
        world.set_agent_status("paused")
        return "draft"

    model.script(Stage.DECIDE, True).script(Stage.WRITE, pause)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.CANCELLED and out.error_code == "paused"
    assert world.agent_messages() == []
    assert world.states()[-1] == AgentState.PAUSED


async def test_session_closed_at_commit_cancels_and_still_debits(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    def close(req: ModelRequest) -> bool:
        world.sessions[world.session_id].status = "closed"
        return True

    # Closure lands after the pre-commit check: the chat service rejects the commit.
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "draft").script(Stage.SAFETY, True)
    store = executor._store  # pyright: ignore[reportPrivateUsage]
    original = store.latest_human_seq

    async def closing_latest(session_id: str) -> int:
        close(model.calls[-1])
        return await original(session_id)

    store.latest_human_seq = closing_latest  # type: ignore[method-assign]
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.CANCELLED and out.error_code == "SESSION_CLOSED"
    assert world.usage  # tokens are still debited


# ---- inactive states (D-020) ---------------------------------------------------------------


async def test_paused_agent_does_not_run_or_advance_cursor(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    world.set_agent_status("paused")
    ask(world)
    out = await executor.execute(world.session_id)
    assert out.skip_reason == "paused"
    assert model.calls == []
    assert world.states() == [AgentState.PAUSED]
    assert world.agent_cursor() == 0 and world.acks == []


async def test_resting_when_budget_short(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    world.set_budget_used(world.capacity_tokens - FULL_TURN_TOKENS + 1)
    ask(world, "Huddle AI, are you there?")
    out = await executor.execute(world.session_id)
    assert out.skip_reason == "resting"
    assert world.states() == [AgentState.RESTING]
    assert model.calls == [] and world.acks == []


async def test_budget_refill_returns_to_following(
    world: MemoryWorld, clock: FakeClock, model: ScriptedModel, executor: RunExecutor
) -> None:
    world.set_budget_used(world.capacity_tokens)
    ask(world)
    await executor.execute(world.session_id)
    assert executor.presence.get(world.session_id) == AgentState.RESTING
    clock.advance(86_400)  # next fixed window: usage resets (D-019)
    ask(world, "and now?")
    model.script(Stage.DECIDE, False)
    await executor.execute(world.session_id)
    assert world.states() == [AgentState.RESTING, AgentState.FOLLOWING]
    assert world.agent_cursor() == 2


async def test_kill_switch(world: MemoryWorld, model: ScriptedModel) -> None:
    ex = make_executor(world, model, Settings(worker_id="w", agents_disabled=True))
    ask(world)
    out = await ex.execute(world.session_id)
    assert out.skip_reason == "disabled"
    assert world.states() == [AgentState.PAUSED] and model.calls == []


async def test_unreachable_model_shows_away_then_recovers(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, ModelUnavailable(), ModelUnavailable())
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.FAILED and out.error_code == "model_unavailable"
    assert world.states() == [AgentState.AWAY]
    assert world.acks == [] and world.agent_messages() == []

    ask(world, "hello?")
    model.script(Stage.DECIDE, False)
    await executor.execute(world.session_id)
    assert world.states() == [AgentState.AWAY, AgentState.FOLLOWING]
    assert world.agent_cursor() == 2


# ---- retries, timeouts, fallback -----------------------------------------------------------


async def test_transient_error_retries_once_with_fallback_model(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    world.agent = replace(world.agent, fallback_model="llama3.2:1b")
    ask(world)
    model.script(Stage.DECIDE, ModelTimeout(), True).script(Stage.WRITE, "ok then")
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.OBSERVED
    decides = [c for c in model.calls if c.stage == Stage.DECIDE]
    assert [c.model for c in decides] == [executor._s.decider_model, "llama3.2:1b"]  # pyright: ignore[reportPrivateUsage]


async def test_timeout_after_retry_ends_timed_out(world: MemoryWorld) -> None:
    class Slow(ScriptedModel):
        async def decide(self, req: ModelRequest) -> Decision:
            await asyncio.sleep(5)
            return Decision(True, "late", Usage(req.model, 1, 1))

    world.set_agent_config(decider_timeout_s=0.01)
    slow = Slow()
    ex = make_executor(world, slow)
    ask(world)
    out = await ex.execute(world.session_id)
    assert out.status == RunStatus.TIMED_OUT and out.error_code == "model_timeout"
    assert world.agent_messages() == [] and world.states() == []


async def test_writer_failure_leaves_writing_state(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, ModelTimeout(), ModelTimeout())
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.TIMED_OUT
    assert world.states() == [AgentState.WRITING, AgentState.FOLLOWING]


async def test_budget_endpoint_down_fails_quietly(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    world.fail_budget = True
    ask(world)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.FAILED and out.error_code == "budget_CHAT_UNREACHABLE"
    assert world.agent_messages() == [] and world.states() == []


# ---- leases and takeover -------------------------------------------------------------------


def _live_run(world: MemoryWorld, owner: str, expires_in_s: float, **kw: object) -> RunRecord:
    now = world.clock.now()
    run = RunRecord(
        id=new_run_id(),
        session_id=world.session_id,
        agent_id=world.agent.id,
        trigger_kind=TriggerKind.MESSAGE,
        trigger_seq=1,
        context_seq=1,
        status=RunStatus.GENERATING,
        lease_owner=owner,
        lease_expires_at=now + timedelta(seconds=expires_in_s),
    )
    run = replace(run, **kw)  # type: ignore[arg-type]
    world.runs[run.id] = run
    return run


async def test_live_run_with_valid_lease_blocks_a_second_run(
    world: MemoryWorld, executor: RunExecutor
) -> None:
    ask(world)
    live = _live_run(world, "other-worker", 20)
    out = await executor.execute(world.session_id)
    assert out.busy and out.run_id == live.id
    assert len(world.runs) == 1


async def test_expired_lease_is_taken_over_under_the_same_run_id(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    crashed = _live_run(world, "dead-worker", -1)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, "resumed answer")
    out = await executor.execute(world.session_id)
    assert out.took_over and out.run_id == crashed.id
    assert out.status == RunStatus.OBSERVED
    row = world.runs[crashed.id]
    assert row.attempts == 2 and row.lease_owner == "worker-1"
    [m] = world.agent_messages()
    assert m.client_msg_id == crashed.id


async def test_takeover_never_posts_twice_when_crash_was_after_commit(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)
    crashed = _live_run(world, "dead-worker", -1, status=RunStatus.VALIDATED)
    # The dead worker's commit landed (idempotency key = run id) before it crashed.
    world.post_agent("already posted")
    msgs = world.messages[world.session_id]
    msgs[-1] = replace(msgs[-1], client_msg_id=crashed.id)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.OBSERVED and out.took_over
    assert len(world.agent_messages()) == 1 and model.calls == []
    assert world.runs[crashed.id].output_message_id == msgs[-1].id


async def test_repeated_crashes_end_timed_out(world: MemoryWorld, executor: RunExecutor) -> None:
    ask(world)
    crashed = _live_run(world, "dead-worker", -1, attempts=3)
    out = await executor.execute(world.session_id)
    assert out.status == RunStatus.TIMED_OUT and out.error_code == "lease_expired"
    assert world.runs[crashed.id].status == RunStatus.TIMED_OUT


async def test_lost_lease_aborts_before_commit(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    def steal(req: ModelRequest) -> str:
        for r in world.runs.values():
            r.lease_owner = "thief"
        return "draft"

    model.script(Stage.DECIDE, True).script(Stage.WRITE, steal)
    out = await executor.execute(world.session_id)
    assert out.error_code == "lease_lost" and out.status is None
    assert world.agent_messages() == []


async def test_losing_the_unique_index_race_coalesces(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    ask(world)

    async def lose(run_id: str, owner: str) -> bool:
        return False

    executor._store.promote_to_eligible = lose  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    out = await executor.execute(world.session_id)
    assert out.busy and out.skip_reason == "coalesced"
    assert model.calls == []


async def test_new_run_ids_are_uuids() -> None:
    import uuid

    rid = new_run_id()
    assert str(uuid.UUID(rid)) == rid


# ---- context folding and restart rebuild --------------------------------------------------


async def test_fold_writes_a_summary_and_debits_summarizer_tokens(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    for i in range(14):
        world.post_human("usr_alice" if i % 2 else "usr_bob", f"{i} " + "z" * 4000)
    model.script(Stage.SUMMARIZE, "They debated the launch plan.").script(Stage.DECIDE, False)
    await executor.execute(world.session_id)
    assert model.stages() == [Stage.SUMMARIZE, Stage.DECIDE]
    [(_, _, summary)] = world.summaries
    assert summary.summary == "They debated the launch plan."
    decide = model.calls[1]
    assert decide.messages[0].seq == summary.covers_through_seq + 1
    assert 11 <= summary.covers_through_seq <= 12  # oldest ~80% folded
    assert world.usage[0][2] == sum(
        c
        for c in (
            world.runs[world.usage[0][1]].input_tokens,
            world.runs[world.usage[0][1]].output_tokens,
        )
    )


async def test_oversized_summary_is_recompressed(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor
) -> None:
    for i in range(14):
        world.post_human("usr_alice", "z" * 4000 + str(i))
    model.script(Stage.SUMMARIZE, "s" * 20_000, "short summary").script(Stage.DECIDE, False)
    await executor.execute(world.session_id)
    assert model.stages() == [Stage.SUMMARIZE, Stage.SUMMARIZE, Stage.DECIDE]
    assert world.summaries[-1][2].summary == "short summary"


async def test_restart_rebuilds_from_summary_plus_window_only(
    world: MemoryWorld, model: ScriptedModel
) -> None:
    from huddle_agent.domain import Summary

    for i in range(300):
        world.post_human("usr_alice", f"message {i} " + "w" * 200)
    world.summaries.append((world.session_id, world.agent.id, Summary(250, "old stuff", 3, "m")))

    loads: list[tuple[int, int]] = []

    class SpyStore(MemoryStore):
        async def load_window(self, session_id: str, agent_id: str, max_tokens: int):  # type: ignore[override]
            summary, window = await super().load_window(session_id, agent_id, max_tokens)
            loads.append((len(window), window[0].seq if window else 0))
            return summary, window

    fresh = make_executor(world, model)  # a restarted worker: no local state at all
    fresh._store = SpyStore(world)  # pyright: ignore[reportPrivateUsage]
    state = await fresh.catch_up(world.session_id)
    assert state == AgentState.FOLLOWING
    assert loads == [(50, 251)]  # only messages after covers_through_seq, never the history
    assert world.agent_cursor() == 300  # "after rebuilding, it acks its cursor"


async def test_restart_while_paused_does_not_ack(world: MemoryWorld, model: ScriptedModel) -> None:
    ask(world)
    world.set_agent_status("paused")
    fresh = make_executor(world, model)
    assert await fresh.catch_up(world.session_id) == AgentState.PAUSED
    assert world.acks == []


async def test_restart_with_model_down_is_away(world: MemoryWorld, model: ScriptedModel) -> None:
    ask(world)
    model.available = False
    fresh = make_executor(world, model)
    assert await fresh.catch_up(world.session_id) == AgentState.AWAY
    assert world.acks == []


@pytest.mark.parametrize("draft", ['"Huddle AI: Quoted reply."', "huddle ai:  Quoted reply."])
async def test_writer_output_is_cleaned(
    world: MemoryWorld, model: ScriptedModel, executor: RunExecutor, draft: str
) -> None:
    ask(world)
    model.script(Stage.DECIDE, True).script(Stage.WRITE, draft)
    await executor.execute(world.session_id)
    assert world.agent_messages()[0].body == "Quoted reply."
