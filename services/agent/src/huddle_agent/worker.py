"""The orchestrator worker loop (Deep Dive §5 wake-up triggers, §7 queue and retry behaviour).

* Consumes the `agent:triggers` stream with the `agent-workers` consumer group (at-least-once).
  Entries are acked only after the evaluation that covers them finishes; pending entries idle for
  60 s are reclaimed by any worker (crash recovery) and the in-flight ones are kept fresh.
* Coalesces triggers per session: evaluate after 3 s of quiet, capped at 8 s. Triggers arriving
  during a run mark the session dirty; it is re-evaluated once when the run ends.
* Skips stale triggers: when queue lag exceeds 30 s, only the nudge timer is updated.
* A batch skipped only by the cooldown is re-evaluated once when the cooldown ends
  (`defer_after_cooldown`), rather than waiting for the next human message.
* Re-engagement nudges: each human message resets `agent:timers[session] = message time +
  nudge_delay`; a due timer is claimed with ZREM (exactly one worker wins) and runs one nudge
  evaluation. Timers are re-armed from `last_human_message_at` on startup and periodically, so a
  Redis flush loses nothing.
* Global kill switch (`HUDDLE_AGENTS_DISABLED`): no evaluations, agent shown as paused.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from huddle_agent.chat_client import ChatClient
from huddle_agent.clock import Clock, SystemClock
from huddle_agent.config import PolicyConfig, Settings
from huddle_agent.domain import TriggerKind
from huddle_agent.logs import log_event
from huddle_agent.models.base import ModelClient
from huddle_agent.policy import SkipReason, debounce_due_at
from huddle_agent.presence import Presence
from huddle_agent.runner import RunExecutor, RunOutcome
from huddle_agent.store import Store
from huddle_contracts.internal import AgentTrigger
from huddle_contracts.models import AgentState, SenderType

MAX_BUSY_BACKOFF_S = 5.0
# The Redis client is created with decode_responses=True: stream IDs and fields are str.
StreamEntry = tuple[str, dict[str, str] | None]


@dataclass
class SessionSlot:
    """Per-session coalescing state held by one worker."""

    entry_ids: list[str] = field(default_factory=list[str])  # covered by the next evaluation
    first_at: float | None = None  # monotonic time of the batch's first trigger
    last_at: float | None = None  # ... and of its latest
    trace_id: str | None = None
    max_seq: int = 0
    in_flight: list[str] = field(default_factory=list[str])  # covered by the running evaluation
    running: bool = False
    retry_at: float | None = None
    busy_retries: int = 0
    nudge_pending: bool = False
    deferred_at: float | None = None  # monotonic time to re-check a cooldown-skipped trigger
    force: bool = False  # evaluate without waiting for the debounce (recovery)

    def add(self, entry_id: str, now: float, trace_id: str | None, seq: int) -> None:
        self.entry_ids.append(entry_id)
        if self.first_at is None:
            self.first_at = now
        self.last_at = now
        self.trace_id = trace_id or self.trace_id
        self.max_seq = max(self.max_seq, seq)


def entry_time(entry_id: str) -> datetime:
    """Redis stream IDs start with the enqueue time in ms."""
    return datetime.fromtimestamp(int(entry_id.split("-", 1)[0]) / 1000, tz=UTC)


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        redis: Redis,
        store: Store,
        chat: ChatClient,
        models: ModelClient,
        clock: Clock | None = None,
        tick_s: float = 0.05,
    ) -> None:
        self.s = settings
        self.redis = redis
        self.store = store
        self.chat = chat
        self.models = models
        self.clock = clock or SystemClock()
        self.presence = Presence(chat)
        self.executor = RunExecutor(
            store=store,
            chat=chat,
            models=models,
            settings=settings,
            clock=self.clock,
            presence=self.presence,
        )
        self.slots: dict[str, SessionSlot] = {}
        self._tick_s = tick_s
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loops: list[asyncio.Task[None]] = []
        self.started = False

    # ---- lifecycle ------------------------------------------------------------------------

    async def ensure_group(self) -> None:
        try:
            # id 0: a fresh group also sees entries enqueued before the first worker started
            # (stale ones are skipped by the lag rule).
            await self.redis.xgroup_create(
                self.s.triggers_stream, self.s.triggers_group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def startup(self) -> None:
        """Recover from a restart, retrying with backoff until Redis and the chat service's
        schema are reachable (the agent may start before chat has migrated)."""
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._startup_once()
                return
            except Exception as exc:  # stores not ready yet: /healthz stays 503 meanwhile
                log_event("startup_retry", exc_type=type(exc).__name__, retry_in_s=delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                delay = min(delay * 2, 15.0)

    async def _startup_once(self) -> None:
        """Stream group, optional-column probe, timers, context rebuild and cursor ack."""
        await self.ensure_group()
        await self.store.probe_columns()
        await self.rearm_timers()
        sessions = {c.session_id for c in await self.store.rearm_candidates()}
        sessions.add(self.s.session_id)
        for sid in sorted(sessions):
            try:
                await self.executor.catch_up(sid)
            except Exception as exc:  # one bad session must not block the worker
                log_event("catch_up_failed", session_id=sid, exc_type=type(exc).__name__)
        self.started = True
        log_event("worker_started", actor_id=self.s.worker_id, disabled=self.s.agents_disabled)

    async def run(self) -> None:
        await self.startup()
        if self._stop.is_set():
            return
        self._loops = [
            asyncio.create_task(self._loop(self.consume_once, 0.0, "consume")),
            asyncio.create_task(self._loop(self.schedule_once, self._tick_s, "schedule")),
            asyncio.create_task(
                self._loop(self.reclaim_once, self.s.reclaim_idle_s / 4, "reclaim")
            ),
            asyncio.create_task(
                self._loop(self.keepalive_once, self.s.reclaim_idle_s / 3, "keepalive")
            ),
            asyncio.create_task(self._loop(self.timers_once, self.s.timer_poll_s, "timers")),
            asyncio.create_task(self._loop(self.reconcile_once, self.s.reconcile_s, "reconcile")),
        ]
        await self._stop.wait()
        for t in self._loops:
            t.cancel()
        for t in [*self._loops, *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def stop(self) -> None:
        self._stop.set()

    def healthy(self) -> bool:
        return self.started and all(not t.done() for t in self._loops)

    async def _loop(self, fn: Callable[[], Awaitable[object]], interval: float, name: str) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the worker alive; the store is the source of truth
                log_event("worker_loop_error", loop=name, exc_type=type(exc).__name__)
                await asyncio.sleep(1.0)
            if interval:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)

    # ---- triggers -------------------------------------------------------------------------

    async def consume_once(self) -> int:
        try:
            resp = await self.redis.xreadgroup(
                self.s.triggers_group,
                self.s.worker_id,
                {self.s.triggers_stream: ">"},
                count=100,
                block=self.s.stream_block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):  # the stream or group vanished (Redis flush)
                await self.ensure_group()
                return 0
            raise
        n = 0
        for _stream, entries in cast(list[tuple[str, list[StreamEntry]]], resp or []):
            for entry_id, fields in entries:
                if fields is not None:
                    await self.accept(entry_id, fields, reclaimed=False)
                    n += 1
        return n

    async def reclaim_once(self) -> int:
        """Take over entries another worker left pending for 60 s (it probably crashed)."""
        try:
            resp = await self.redis.xautoclaim(
                self.s.triggers_stream,
                self.s.triggers_group,
                self.s.worker_id,
                min_idle_time=int(self.s.reclaim_idle_s * 1000),
                start_id="0-0",
                count=100,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.ensure_group()
                return 0
            raise
        claimed = cast(tuple[str, list[StreamEntry], list[str]] | None, resp)
        n = 0
        for entry_id, fields in claimed[1] if claimed else []:
            if fields is None:  # deleted from the stream meanwhile
                await self._ack([entry_id])
                continue
            if self._owned(entry_id):
                continue
            await self.accept(entry_id, fields, reclaimed=True)
            n += 1
        if n:
            log_event("triggers_reclaimed", count=n, actor_id=self.s.worker_id)
        return n

    def _owned(self, entry_id: str) -> bool:
        return any(entry_id in s.entry_ids or entry_id in s.in_flight for s in self.slots.values())

    async def keepalive_once(self) -> None:
        """Reset the idle time of entries this worker still holds, so long runs are not
        reclaimed by other workers mid-flight."""
        ids = [i for s in self.slots.values() for i in (*s.in_flight, *s.entry_ids)]
        if ids:
            await self.redis.xclaim(
                self.s.triggers_stream,
                self.s.triggers_group,
                self.s.worker_id,
                min_idle_time=0,
                message_ids=cast(list[Any], ids),
                justid=True,
            )

    async def accept(self, entry_id: str, fields: Mapping[str, str], *, reclaimed: bool) -> None:
        try:
            trig = AgentTrigger.model_validate(fields)
        except ValidationError:
            log_event("trigger_invalid", entry_id=entry_id)
            await self._ack([entry_id])
            return
        if trig.sender_type != SenderType.USER:  # only human messages wake the agent
            await self._ack([entry_id])
            return
        enqueued = entry_time(entry_id)
        await self.reset_timer(trig.session_id, enqueued)
        base: dict[str, Any] = {
            "trace_id": trig.trace_id,
            "session_id": trig.session_id,
            "message_id": trig.message_id,
            "seq": trig.seq,
            "entry_id": entry_id,
        }
        if self.s.agents_disabled:
            await self.presence.set(trig.session_id, AgentState.AWAY)
            await self._ack([entry_id])
            log_event("trigger_skipped", reason="disabled", **base)
            return
        lag_s = (self.clock.now() - enqueued).total_seconds()
        if lag_s > self.s.stale_trigger_s and not reclaimed:
            await self._ack([entry_id])
            log_event("trigger_stale_skipped", lag_ms=int(lag_s * 1000), **base)
            return
        slot = self.slots.setdefault(trig.session_id, SessionSlot())
        slot.add(entry_id, self.clock.monotonic(), trig.trace_id, trig.seq)
        if reclaimed:
            # Crash recovery: evaluate now (a crashed run is taken over and rerun from a fresh
            # context) instead of honouring the stale-trigger rule meant for queue lag.
            slot.force = True
        if slot.running:
            log_event("trigger_coalesced", dirty=True, **base)
        else:
            log_event("trigger_received", lag_ms=int(lag_s * 1000), **base)

    async def _ack(self, ids: list[str]) -> None:
        if ids:
            await self.redis.xack(self.s.triggers_stream, self.s.triggers_group, *ids)

    # ---- scheduling -----------------------------------------------------------------------

    def _policy(self, session_id: str) -> PolicyConfig:
        return self.executor.policies.get(session_id) or PolicyConfig()

    def due(self, session_id: str, slot: SessionSlot, now: float) -> bool:
        if slot.running:
            return False
        if slot.retry_at is not None and now < slot.retry_at:
            return False
        if slot.nudge_pending:
            return True
        if not slot.entry_ids and slot.deferred_at is not None:
            return now >= slot.deferred_at  # cooldown ended: re-evaluate the skipped trigger
        if not slot.entry_ids or slot.first_at is None or slot.last_at is None:
            return False
        if slot.force:
            return True
        return now >= debounce_due_at(slot.first_at, slot.last_at, self._policy(session_id))

    async def schedule_once(self) -> None:
        now = self.clock.monotonic()
        for sid, slot in list(self.slots.items()):
            if self.due(sid, slot, now):
                self._spawn(self.evaluate(sid))

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def evaluate(self, session_id: str) -> RunOutcome | None:
        """Run one evaluation for the session: a pending nudge first, else the coalesced
        message batch. Entries are acked only after the evaluation finishes."""
        slot = self.slots.setdefault(session_id, SessionSlot())
        if slot.running:
            return None
        slot.running = True
        kind = (
            TriggerKind.NUDGE if slot.nudge_pending and not slot.entry_ids else TriggerKind.MESSAGE
        )
        ids, trace_id, max_seq = slot.entry_ids, slot.trace_id, slot.max_seq
        first_at = slot.first_at
        if kind == TriggerKind.MESSAGE:
            slot.in_flight = ids
            slot.entry_ids, slot.first_at, slot.last_at = [], None, None
            slot.trace_id, slot.max_seq, slot.force = None, 0, False
            slot.deferred_at = None  # this evaluation covers any deferred one
        slot.nudge_pending = False if kind == TriggerKind.NUDGE else slot.nudge_pending
        outcome: RunOutcome | None = None
        try:
            outcome = await self.executor.execute(
                session_id, kind, trace_id=trace_id, trigger_seq=max_seq or None
            )
        except Exception as exc:
            log_event("evaluation_error", session_id=session_id, exc_type=type(exc).__name__)
        finally:
            slot.running = False
        retry = outcome is None or outcome.busy or outcome.took_over
        if retry:
            # Busy (another live run), crashed-run takeover, or an error: keep the batch
            # (it stays pending in the stream) and retry with backoff.
            if kind == TriggerKind.MESSAGE and not ids:
                slot.deferred_at = self.clock.monotonic()  # a deferred re-check; retry it
            elif kind == TriggerKind.MESSAGE:
                slot.entry_ids = ids + slot.entry_ids
                slot.first_at = first_at if first_at is not None else self.clock.monotonic()
                slot.last_at = slot.last_at or slot.first_at
                slot.trace_id = slot.trace_id or trace_id
                slot.max_seq = max(slot.max_seq, max_seq)
                slot.in_flight = []
            else:
                slot.nudge_pending = True
            slot.busy_retries += 1
            backoff = min(self.s.busy_retry_s * slot.busy_retries, MAX_BUSY_BACKOFF_S)
            slot.retry_at = self.clock.monotonic() + (
                0 if outcome and outcome.took_over else backoff
            )
            return outcome
        slot.busy_retries, slot.retry_at = 0, None
        if kind == TriggerKind.MESSAGE:
            await self._ack(ids)
            slot.in_flight = []
            if outcome is not None and outcome.retry_after_s is not None:
                slot.deferred_at = self.clock.monotonic() + outcome.retry_after_s + 0.05
        elif outcome is not None and outcome.skip_reason == SkipReason.LULL_NOT_OVER.value:
            await self.rearm_timers()
        return outcome

    # ---- nudge timers ---------------------------------------------------------------------

    async def reset_timer(self, session_id: str, human_message_at: datetime) -> None:
        """Each human message resets the nudge timer (Deep Dive §5). GT keeps a late-processed
        older trigger from pulling the timer earlier."""
        delay = self._policy(session_id).nudge_delay_s
        due = (human_message_at + timedelta(seconds=delay)).timestamp()
        await self.redis.zadd(self.s.timers_zset, {session_id: due}, gt=True)

    async def timers_once(self) -> int:
        now = self.clock.now().timestamp()
        # redis-py leaves the score-cast callback type unknown; the result is list[str] here
        # because the client is created with decode_responses=True.
        due = await self.redis.zrangebyscore(  # pyright: ignore[reportUnknownMemberType]
            self.s.timers_zset, "-inf", now, start=0, num=50
        )
        n = 0
        for sid in cast(list[str], due or []):
            if await self.redis.zrem(self.s.timers_zset, sid) != 1:
                continue  # another worker claimed this lull
            if self.s.agents_disabled:
                continue
            log_event("nudge_timer_fired", session_id=sid)
            self.slots.setdefault(sid, SessionSlot()).nudge_pending = True
            n += 1
        return n

    async def rearm_timers(self) -> int:
        """Re-arm nudge timers from `last_human_message_at` (Deep Dive §4, §7 "Nudge timer
        lost"). A lull that already had its nudge attempt is not re-armed (once per lull)."""
        n = 0
        for cand in await self.store.rearm_candidates():
            policy = PolicyConfig.from_json(cand.agent.config)
            if cand.nudge_done or not policy.nudge_enabled:
                continue
            due = (cand.last_human_message_at + timedelta(seconds=policy.nudge_delay_s)).timestamp()
            added = await self.redis.zadd(self.s.timers_zset, {cand.session_id: due}, nx=True)
            if added:
                n += 1
                log_event("nudge_rearmed", session_id=cand.session_id, due_at=due)
        return n

    # ---- reconcile ------------------------------------------------------------------------

    async def reconcile_once(self) -> None:
        """Periodic repair: orphaned runs, lost timers, crashed runs nobody resumed, and leaving
        paused/resting/away once the cause is gone (then catching up the read cursor)."""
        await self.store.expire_orphans(self.clock.now())
        try:
            await self.rearm_timers()
        except RedisError as exc:
            log_event("rearm_failed", exc_type=type(exc).__name__)
        now = self.clock.now()
        for cand in await self.store.rearm_candidates():
            live = await self.store.find_live_run(cand.session_id, cand.agent.id)
            if (
                live is not None
                and live.lease_expires_at is not None
                and live.lease_expires_at < now
            ):
                slot = self.slots.setdefault(cand.session_id, SessionSlot())
                if not slot.running:
                    self._spawn(self._resume_crashed(cand.session_id))
        for sid in self.presence.sessions_in(
            AgentState.PAUSED, AgentState.RESTING, AgentState.AWAY
        ):
            if self.slots.get(sid, SessionSlot()).running:
                continue
            await self.executor.catch_up(sid)

    async def _resume_crashed(self, session_id: str) -> None:
        slot = self.slots.setdefault(session_id, SessionSlot())
        if slot.running:
            return
        slot.running = True
        try:
            await self.executor.execute(session_id)
        finally:
            slot.running = False
