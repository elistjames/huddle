"""One agent run, end to end (Deep Dive §5 "Agent run lifecycle", §7).

    triggered -> eligible | skipped -> context_built -> generating -> validated -> committed
              -> observed          (terminal alternatives: cancelled, superseded, timed_out, failed)

* One live run per session: the run row carries a 30 s lease (renewed while working) and the
  partial unique index on live statuses is the backstop. A crashed worker's run is taken over
  once its lease expires and rerun from a fresh context under the same `run_id`, so the commit
  (`client_msg_id = run_id`) stays idempotent.
* Gate -> decider -> writer (D-005). `writing` is shown only during the writer stage.
* Before committing: length check (regenerate once), safety check, cooperative cancellation
  check, stale check (re-plan once, else `superseded`).
* Agent errors never reach the thread; they surface only as presence (`away` etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from langchain_core.messages import BaseMessage
from ulid import ULID

from huddle_agent.chat_client import ChatClient, ChatError
from huddle_agent.clock import Clock
from huddle_agent.config import PolicyConfig, Settings
from huddle_agent.context import (
    ContextWindow,
    decider_prompt,
    estimate_tokens,
    recompress_prompt,
    safety_prompt,
    select_fold,
    summarizer_prompt,
    writer_prompt,
)
from huddle_agent.domain import (
    AgentInfo,
    ChatMessage,
    GateSnapshot,
    RunRecord,
    RunStatus,
    Summary,
    TriggerKind,
)
from huddle_agent.logs import log_event
from huddle_agent.models.base import (
    Decision,
    ModelClient,
    ModelError,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
    Stage,
    Usage,
)
from huddle_agent.policy import GateResult, SkipReason, evaluate_gate, trailing_agent_messages
from huddle_agent.presence import Presence
from huddle_agent.store import Store
from huddle_contracts.budget import is_resting
from huddle_contracts.errors import ErrorCode
from huddle_contracts.models import AgentState, SenderType

MAX_ATTEMPTS = 3  # a run taken over more often than this ends as timed_out


def new_run_id() -> str:
    """A time-ordered UUID: it doubles as the message's `client_msg_id`, a uuid column
    (Deep Dive §4)."""
    return str(ULID().to_uuid())


@dataclass
class RunOutcome:
    session_id: str
    run_id: str | None = None
    status: RunStatus | None = None
    busy: bool = False  # another live run holds the session; retry after it ends
    took_over: bool = False  # this call resumed a crashed run, not the caller's trigger
    skip_reason: str | None = None
    error_code: str | None = None
    message_id: str | None = None
    seq: int | None = None
    retry_after_s: float | None = None  # cooldown skip: when the agent may speak again


class _LeaseLost(Exception):
    pass


class _HasUsage(Protocol):
    @property
    def usage(self) -> Usage: ...


R = TypeVar("R", bound=_HasUsage)


@dataclass
class _Run:
    run: RunRecord
    agent: AgentInfo
    policy: PolicyConfig
    kind: TriggerKind
    trace_id: str | None
    t0: float
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    lease_lost: bool = False

    @property
    def session_id(self) -> str:
        return self.run.session_id

    def log(self, event: str, **fields: Any) -> None:
        log_event(
            event,
            trace_id=self.trace_id,
            session_id=self.run.session_id,
            actor_id=self.agent.id,
            run_id=self.run.id,
            trigger_kind=self.kind,
            **fields,
        )


# Chat-template artefacts small Llama models sometimes emit at the start or end of a reply: a
# bare role line ("assistant", "assistant:"), header tokens, or an end-of-turn token.
_TEMPLATE_TOKENS = re.compile(
    r"<\|(?:start_header_id|end_header_id|eot_id|begin_of_text|end_of_text)\|>", re.IGNORECASE
)
_ROLE_LINE = re.compile(r"^\s*(?:assistant|system|user)\s*:?\s*(?:\n+|$)", re.IGNORECASE)


def clean_draft(text: str, agent_name: str) -> str:
    """Strip template artefacts, wrapping quotes and a leading "<agent name>:" the model
    sometimes adds. Only a role word on its own line is removed, never a reply that merely
    starts with the word."""
    out = _TEMPLATE_TOKENS.sub("\n", text).strip()
    while True:
        stripped = _ROLE_LINE.sub("", out, count=1).strip()
        if stripped == out:
            break
        out = stripped
    out = out.strip('"').strip()
    prefix = re.compile(rf"^{re.escape(agent_name)}\s*:\s*", re.IGNORECASE)
    return prefix.sub("", out).strip()


class RunExecutor:
    def __init__(
        self,
        *,
        store: Store,
        chat: ChatClient,
        models: ModelClient,
        settings: Settings,
        clock: Clock,
        presence: Presence,
        model_lock: asyncio.Lock | None = None,
    ) -> None:
        self._store = store
        self._chat = chat
        self._models = models
        self._s = settings
        self._clock = clock
        self.presence = presence
        # Model calls are serialized per worker: one local GPU (Deep Dive §12).
        self._model_lock = model_lock or asyncio.Lock()
        self.policies: dict[str, PolicyConfig] = {}
        self._acked: dict[str, int] = {}

    @property
    def owner(self) -> str:
        return self._s.worker_id

    # ---- entry points ---------------------------------------------------------------------

    async def execute(
        self,
        session_id: str,
        kind: TriggerKind = TriggerKind.MESSAGE,
        *,
        trace_id: str | None = None,
        trigger_seq: int | None = None,
    ) -> RunOutcome:
        snap = await self._snapshot(session_id)
        if snap is None:
            log_event("agent_run_no_agent", session_id=session_id, trace_id=trace_id)
            return RunOutcome(session_id, skip_reason="no_agent")
        agent = snap.agent
        now = self._clock.now()
        lease_until = now + timedelta(seconds=self._s.lease_s)
        took_over = False

        live = await self._store.find_live_run(session_id, agent.id)
        if live is not None:
            if live.lease_expires_at is not None and live.lease_expires_at >= now:
                log_event(
                    "trigger_coalesced", session_id=session_id, run_id=live.id, trace_id=trace_id
                )
                return RunOutcome(session_id, run_id=live.id, busy=True)
            run = await self._store.takeover_run(live.id, self.owner, now, lease_until)
            if run is None:
                return RunOutcome(session_id, run_id=live.id, busy=True)
            took_over = True
            kind = run.trigger_kind
            log_event(
                "agent_run_timed_out",
                session_id=session_id,
                actor_id=agent.id,
                run_id=run.id,
                attempts=run.attempts,
                reason="lease_expired",
            )
            if run.attempts > MAX_ATTEMPTS:
                await self._store.update_run(
                    run.id, self.owner, status=RunStatus.TIMED_OUT, error_code="lease_expired"
                )
                return RunOutcome(
                    session_id,
                    run_id=run.id,
                    status=RunStatus.TIMED_OUT,
                    error_code="lease_expired",
                    took_over=True,
                )
        else:
            if kind == TriggerKind.NUDGE:
                # A lull is identified by the last human message before it.
                tseq = await self._store.latest_human_seq(session_id)
            else:
                tseq = trigger_seq if trigger_seq is not None else snap.session.last_seq
            run = RunRecord(
                id=new_run_id(),
                session_id=session_id,
                agent_id=agent.id,
                trigger_kind=kind,
                trigger_seq=tseq,
                context_seq=snap.session.last_seq,
                status=RunStatus.TRIGGERED,
                lease_owner=self.owner,
                lease_expires_at=lease_until,
            )
            await self._store.create_run(run, agent.prompt_version)

        rc = _Run(
            run=run,
            agent=agent,
            policy=self.policies[session_id],
            kind=kind,
            trace_id=trace_id,
            t0=self._clock.monotonic(),
        )
        rc.log("agent_run_started", seq=run.trigger_seq, attempts=run.attempts)
        renewer = asyncio.create_task(self._renew_loop(rc))
        try:
            outcome = await self._guarded(rc, snap, took_over)
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
        outcome.took_over = took_over
        return outcome

    async def catch_up(self, session_id: str) -> AgentState | None:
        """Startup / recovery: rebuild context from the store (latest summary plus the recent
        window only, never the full history) and ack the read cursor, unless the agent is
        inactive (D-020). Returns the presence it settled on."""
        snap = await self._snapshot(session_id)
        if snap is None or snap.session.status != "active":
            return None
        agent = snap.agent
        if self._s.agents_disabled:
            await self.presence.set(session_id, AgentState.AWAY)
            return AgentState.AWAY
        if agent.status != "active":
            await self.presence.set(session_id, AgentState.PAUSED)
            return AgentState.PAUSED
        try:
            budget = await self._chat.get_budget(session_id)
        except ChatError as exc:
            log_event("catch_up_failed", session_id=session_id, code=exc.code)
            return None
        if is_resting(budget.used_tokens, budget.capacity_tokens):
            await self.presence.set(session_id, AgentState.RESTING)
            return AgentState.RESTING
        if not await self._models.ping():
            await self.presence.set(session_id, AgentState.AWAY)
            return AgentState.AWAY
        policy = self.policies[session_id]
        summary, window = await self._store.load_window(
            session_id, agent.id, policy.fold_threshold_tokens
        )
        log_event(
            "context_rebuilt",
            session_id=session_id,
            actor_id=agent.id,
            seq=window[-1].seq if window else None,
            messages=len(window),
            covers_through_seq=summary.covers_through_seq if summary else 0,
        )
        await self.presence.set(session_id, AgentState.FOLLOWING)
        seq = window[-1].seq if window else snap.session.last_seq
        await self._ack(session_id, agent.id, seq, trace_id=None)
        return AgentState.FOLLOWING

    # ---- pipeline -------------------------------------------------------------------------

    async def _guarded(self, rc: _Run, snap: GateSnapshot, took_over: bool) -> RunOutcome:
        sid = rc.session_id
        try:
            return await self._pipeline(rc, snap, took_over)
        except _LeaseLost:
            rc.log("agent_run_lease_lost")
            if self.presence.get(sid) == AgentState.WRITING:
                await self.presence.set(sid, AgentState.FOLLOWING)
            return RunOutcome(sid, run_id=rc.run.id, error_code="lease_lost")
        except ModelUnavailable as exc:
            await self.presence.set(sid, AgentState.AWAY)
            return await self._finish(rc, RunStatus.FAILED, error_code=exc.code)
        except ModelTimeout as exc:
            await self._leave_writing(sid)
            return await self._finish(rc, RunStatus.TIMED_OUT, error_code=exc.code)
        except ModelError as exc:
            await self._leave_writing(sid)
            return await self._finish(rc, RunStatus.FAILED, error_code=exc.code)

    async def _leave_writing(self, session_id: str) -> None:
        if self.presence.get(session_id) == AgentState.WRITING:
            await self.presence.set(session_id, AgentState.FOLLOWING)

    async def _pipeline(self, rc: _Run, snap: GateSnapshot, took_over: bool) -> RunOutcome:
        sid = rc.session_id
        agent = rc.agent
        if took_over:
            existing = await self._store.agent_message_for_run(sid, agent.id, rc.run.id)
            if existing is not None:
                # The crashed worker already committed: finish the run, never post again.
                message_id, seq = existing
                await self._update(rc, status=RunStatus.COMMITTED, output_message_id=message_id)
                await self._ack(sid, agent.id, seq, trace_id=rc.trace_id)
                out = await self._finish(rc, RunStatus.OBSERVED, output_message_id=message_id)
                out.message_id, out.seq = message_id, seq
                return out

        replanned = False
        while True:
            self._check_lease(rc)
            try:
                budget = await self._chat.get_budget(sid)
            except ChatError as exc:
                return await self._finish(rc, RunStatus.FAILED, error_code=f"budget_{exc.code}")
            gate = evaluate_gate(
                snap,
                now=self._clock.now(),
                kind=rc.kind,
                budget_used=budget.used_tokens,
                budget_capacity=budget.capacity_tokens,
                disabled=self._s.agents_disabled,
                policy=rc.policy,
            )
            if not gate.eligible:
                return await self._skip(rc, snap, gate)
            if self.presence.get(sid) in (AgentState.PAUSED, AgentState.RESTING):
                await self.presence.set(sid, AgentState.FOLLOWING)

            if rc.run.status == RunStatus.TRIGGERED:
                if not await self._store.promote_to_eligible(rc.run.id, self.owner):
                    # Another worker's live run won the partial unique index: coalesce.
                    out = await self._finish(
                        rc, RunStatus.SKIPPED, skip_reason=SkipReason.COALESCED.value
                    )
                    out.busy = True
                    return out
                rc.run.status = RunStatus.ELIGIBLE

            ctx = await self._build_context(rc, snap)
            rc.run.context_seq = ctx.context_seq
            await self._update(rc, status=RunStatus.CONTEXT_BUILT, context_seq=ctx.context_seq)
            self._check_lease(rc)
            await self._update(rc, status=RunStatus.GENERATING)

            decision = await self._decide(rc, snap, gate, ctx)
            if self.presence.get(sid) == AgentState.AWAY:
                await self.presence.set(sid, AgentState.FOLLOWING)  # the runtime is back
            if not decision.speak:
                await self._ack(sid, agent.id, ctx.context_seq, trace_id=rc.trace_id)
                reason = (
                    SkipReason.CONVERSATION_CLOSED
                    if rc.kind == TriggerKind.NUDGE
                    else SkipReason.DECIDER_DECLINED
                )
                return await self._finish(rc, RunStatus.SKIPPED, skip_reason=reason.value)

            # Writer stage: the only time the agent shows as writing (Deep Dive §10).
            await self.presence.set(sid, AgentState.WRITING)
            text = await self._write(rc, ctx, decision.reason)
            if text is None:
                await self.presence.set(sid, AgentState.FOLLOWING)
                await self._ack(sid, agent.id, ctx.context_seq, trace_id=rc.trace_id)
                return await self._finish(rc, RunStatus.FAILED, error_code="output_too_long")
            if not await self._safe(rc, text):
                await self.presence.set(sid, AgentState.FOLLOWING)
                await self._ack(sid, agent.id, ctx.context_seq, trace_id=rc.trace_id)
                return await self._finish(rc, RunStatus.FAILED, error_code="unsafe_output")
            await self._update(rc, status=RunStatus.VALIDATED)

            # Cooperative cancellation before committing (Deep Dive §7).
            self._check_lease(rc)
            fresh = await self._snapshot(sid)
            cancel = self._cancel_reason(fresh)
            if fresh is None or cancel is not None:
                cancel = cancel or "removed"
                state = {
                    "paused": AgentState.PAUSED,
                    "removed": AgentState.PAUSED,
                    "disabled": AgentState.AWAY,
                }.get(cancel, AgentState.FOLLOWING)
                await self.presence.set(sid, state)
                return await self._finish(rc, RunStatus.CANCELLED, error_code=cancel)

            # Another worker may have committed a reply since this run's first snapshot (workers
            # have no per-session affinity). This run is the only live run for the session, so
            # this fresh read is final: never post twice in a row (Deep Dive §5).
            if trailing_agent_messages(fresh.recent) >= rc.policy.max_consecutive_agent_messages:
                await self.presence.set(sid, AgentState.FOLLOWING)
                return await self._finish(rc, RunStatus.CANCELLED, error_code="consecutive")

            latest_human = await self._store.latest_human_seq(sid)
            if latest_human > ctx.context_seq:
                await self.presence.set(sid, AgentState.FOLLOWING)
                if rc.kind == TriggerKind.NUDGE:
                    return await self._finish(rc, RunStatus.CANCELLED, error_code="human_message")
                if not replanned:
                    replanned = True
                    rc.log("agent_run_replanned", seq=latest_human)
                    snap = fresh
                    continue
                await self._ack(sid, agent.id, ctx.context_seq, trace_id=rc.trace_id)
                return await self._finish(rc, RunStatus.SUPERSEDED)

            return await self._commit(rc, ctx, text)

    async def _commit(self, rc: _Run, ctx: ContextWindow, text: str) -> RunOutcome:
        sid = rc.session_id
        try:
            resp = await self._chat.submit_message(sid, rc.agent.id, rc.run.id, text, rc.trace_id)
        except ChatError as exc:
            await self.presence.set(sid, AgentState.FOLLOWING)
            closed = exc.code in (ErrorCode.SESSION_CLOSED.value, ErrorCode.NOT_MEMBER.value)
            status = RunStatus.CANCELLED if closed else RunStatus.FAILED
            return await self._finish(rc, status, error_code=exc.code)
        await self._update(rc, status=RunStatus.COMMITTED, output_message_id=resp.message_id)
        await self.presence.set(sid, AgentState.FOLLOWING)
        await self._ack(sid, rc.agent.id, max(ctx.context_seq, resp.seq), trace_id=rc.trace_id)
        out = await self._finish(
            rc,
            RunStatus.OBSERVED,
            output_message_id=resp.message_id,
            message_id=resp.message_id,
            seq=resp.seq,
            duplicate=resp.duplicate,
        )
        out.message_id, out.seq = resp.message_id, resp.seq
        return out

    async def _skip(self, rc: _Run, snap: GateSnapshot, gate: GateResult) -> RunOutcome:
        sid = rc.session_id
        reason = gate.skip_reason.value if gate.skip_reason else "unknown"
        inactive = gate.inactive_state
        if inactive is not None:
            # Inactive agent: show why, and do not advance the read cursor (D-020).
            await self.presence.set(sid, inactive)
        else:
            if self.presence.get(sid) in (AgentState.PAUSED, AgentState.RESTING):
                await self.presence.set(sid, AgentState.FOLLOWING)
            await self._ack(sid, rc.agent.id, snap.session.last_seq, trace_id=rc.trace_id)
        rc.run.context_seq = snap.session.last_seq
        out = await self._finish(
            rc,
            RunStatus.SKIPPED,
            skip_reason=reason,
            context_seq=snap.session.last_seq,
            share=round(gate.share, 3),
            cooldown_s=gate.cooldown_s,
        )
        last = snap.last_agent_message
        deferrable = gate.skip_reason == SkipReason.COOLDOWN and rc.policy.defer_after_cooldown
        if deferrable and last is not None:
            elapsed = (self._clock.now() - last.created_at).total_seconds()
            out.retry_after_s = max(gate.cooldown_s - elapsed, 0.0)
        return out

    def _cancel_reason(self, fresh: GateSnapshot | None) -> str | None:
        """A run is cancelled if the agent is paused or removed, or the session closed."""
        if fresh is None:
            return "removed"
        if self._s.agents_disabled:
            return "disabled"
        if fresh.agent.status in ("paused", "removed"):
            return fresh.agent.status
        if fresh.session.status != "active":
            return "session_closed"
        return None

    def _builder(
        self,
        rc: _Run,
        stage: Stage,
        prompt: list[BaseMessage],
        *,
        timeout_s: float,
        max_output_tokens: int,
        messages: list[ChatMessage] | None = None,
        hints: dict[str, object] | None = None,
        draft: str = "",
    ) -> Callable[[str], ModelRequest]:
        def build(model: str) -> ModelRequest:
            return ModelRequest(
                stage=stage,
                prompt=prompt,
                model=model,
                timeout_s=timeout_s,
                max_output_tokens=max_output_tokens,
                kind=rc.kind,
                agent_name=rc.agent.name,
                agent_id=rc.agent.id,
                messages=messages or [],
                hints=hints or {},
                draft=draft,
            )

        return build

    # ---- context --------------------------------------------------------------------------

    async def _names(self, session_id: str, msgs: list[ChatMessage]) -> dict[str, str]:
        names: dict[str, str] = {}
        for m in msgs:
            if m.sender_type == SenderType.SYSTEM or m.sender_id in names:
                continue
            name = await self._chat.display_name(session_id, m.sender_id)
            if name:
                names[m.sender_id] = name
        return names

    async def _build_context(self, rc: _Run, snap: GateSnapshot) -> ContextWindow:
        sid, agent, policy = rc.session_id, rc.agent, rc.policy
        summary, tail = await self._store.load_window(sid, agent.id, policy.fold_threshold_tokens)
        fold = select_fold(tail, policy)
        if fold:
            summary = await self._fold(rc, summary, fold)
            tail = tail[len(fold) :]
        names = await self._names(sid, tail)
        if tail:
            context_seq = tail[-1].seq
        elif summary is not None:
            context_seq = summary.covers_through_seq
        else:
            context_seq = snap.session.last_seq
        return ContextWindow(summary=summary, messages=tail, context_seq=context_seq, names=names)

    async def _fold(self, rc: _Run, previous: Summary | None, fold: list[ChatMessage]) -> Summary:
        """Fold the oldest 80% of the verbatim window into the rolling summary, regenerated
        incrementally from the previous summary plus the aged-out messages (Deep Dive §5)."""
        policy, agent = rc.policy, rc.agent
        names = await self._names(rc.session_id, fold)
        prompt = summarizer_prompt(
            previous, fold, names=names, agent_name=agent.name, policy=policy
        )
        build = self._builder(
            rc,
            Stage.SUMMARIZE,
            prompt,
            timeout_s=policy.writer_timeout_s,
            max_output_tokens=policy.summary_max_tokens,
            messages=fold,
        )
        res = await self._call(
            rc, Stage.SUMMARIZE, self._s.decider_model, build, self._models.summarize
        )
        text, model = res.text, res.usage.model
        if estimate_tokens(text) > policy.summary_max_tokens:
            # Still too large: re-compress the summary itself once, then hard-cap it.
            build = self._builder(
                rc,
                Stage.SUMMARIZE,
                recompress_prompt(text, policy),
                timeout_s=policy.writer_timeout_s,
                max_output_tokens=policy.summary_max_tokens,
            )
            res = await self._call(
                rc, Stage.SUMMARIZE, self._s.decider_model, build, self._models.summarize
            )
            text = res.text[: policy.summary_max_tokens * 4]
        summary = Summary(
            covers_through_seq=fold[-1].seq,
            summary=text,
            token_count=estimate_tokens(text),
            model=model,
        )
        await self._store.insert_summary(rc.session_id, agent.id, summary)
        rc.log(
            "summary_folded",
            seq=summary.covers_through_seq,
            folded_messages=len(fold),
            token_count=summary.token_count,
        )
        return summary

    # ---- stages ---------------------------------------------------------------------------

    async def _decide(
        self, rc: _Run, snap: GateSnapshot, gate: GateResult, ctx: ContextWindow
    ) -> Decision:
        policy, agent = rc.policy, rc.agent
        lull_minutes = 0
        last_human = snap.session.last_human_message_at
        if last_human is not None:
            lull_minutes = int((self._clock.now() - last_human).total_seconds() // 60)
        prompt = decider_prompt(
            ctx,
            agent_id=agent.id,
            agent_name=agent.name,
            kind=rc.kind,
            encourage=gate.encourage,
            mentioned=gate.mentioned,
            lull_minutes=lull_minutes,
            policy=policy,
        )
        build = self._builder(
            rc,
            Stage.DECIDE,
            prompt,
            timeout_s=policy.decider_timeout_s,
            max_output_tokens=policy.decider_max_output_tokens,
            messages=ctx.messages,
            hints={"encourage": gate.encourage, "mentioned": gate.mentioned},
        )
        decision = await self._call(
            rc, Stage.DECIDE, self._s.decider_model, build, self._models.decide
        )
        rc.log("agent_decision", speak=decision.speak, seq=ctx.context_seq)
        return decision

    async def _write(self, rc: _Run, ctx: ContextWindow, reason: str) -> str | None:
        """Writer with length validation: over `max_chars` regenerates once, then discards."""
        policy, agent = rc.policy, rc.agent
        for too_long in (False, True):
            prompt = writer_prompt(
                ctx,
                agent_id=agent.id,
                agent_name=agent.name,
                kind=rc.kind,
                reason=reason,
                policy=policy,
                too_long=too_long,
            )
            build = self._builder(
                rc,
                Stage.WRITE,
                prompt,
                timeout_s=policy.writer_timeout_s,
                max_output_tokens=policy.writer_max_output_tokens,
                messages=ctx.messages,
                hints={"reason": reason, "too_long": too_long},
            )
            draft = await self._call(
                rc, Stage.WRITE, self._s.writer_model, build, self._models.write
            )
            text = clean_draft(draft.text, agent.name)
            if text and len(text) <= policy.max_chars:
                return text
            rc.log("writer_output_rejected", chars=len(text), retry=not too_long)
        return None

    async def _safe(self, rc: _Run, text: str) -> bool:
        policy = rc.policy
        build = self._builder(
            rc,
            Stage.SAFETY,
            safety_prompt(text, rc.agent.name),
            timeout_s=policy.decider_timeout_s,
            max_output_tokens=5,
            draft=text,
        )
        verdict = await self._call(
            rc, Stage.SAFETY, self._s.decider_model, build, self._models.safety_check
        )
        if not verdict.safe:
            rc.log("safety_rejected", chars=len(text))
        return verdict.safe

    async def _call(
        self,
        rc: _Run,
        stage: Stage,
        primary: str,
        build: Callable[[str], ModelRequest],
        fn: Callable[[ModelRequest], Awaitable[R]],
    ) -> R:
        """One model call with one retry on a transient error, using the fallback model
        (Deep Dive §5 "Retries")."""
        fallback = rc.agent.fallback_model or primary
        for attempt, model in enumerate((primary, fallback)):
            req = build(model)
            started = self._clock.monotonic()
            try:
                async with self._model_lock:
                    result = await asyncio.wait_for(fn(req), timeout=req.timeout_s)
            except TimeoutError:
                err: ModelError = ModelTimeout(f"{stage} timed out")
            except ModelError as exc:
                err = exc
            else:
                u = result.usage
                rc.input_tokens += u.input_tokens
                rc.output_tokens += u.output_tokens
                rc.model = u.model
                await self._debit(rc, u)
                rc.log(
                    "model_call",
                    stage=stage,
                    model=u.model,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    latency_ms=int((self._clock.monotonic() - started) * 1000),
                )
                return result
            rc.log("model_call_failed", stage=stage, model=model, code=err.code, attempt=attempt)
            if attempt == 1 or not err.transient:
                raise err
        raise ModelError("unreachable")  # pragma: no cover

    # ---- bookkeeping ----------------------------------------------------------------------

    async def _snapshot(self, session_id: str) -> GateSnapshot | None:
        cached = self.policies.get(session_id) or PolicyConfig()
        snap = await self._store.load_gate_snapshot(session_id, cached.share_window)
        if snap is None:
            return None
        policy = PolicyConfig.from_json(snap.agent.config)
        self.policies[session_id] = policy
        if policy.share_window != cached.share_window:
            snap = await self._store.load_gate_snapshot(session_id, policy.share_window)
        return snap

    def _check_lease(self, rc: _Run) -> None:
        if rc.lease_lost:
            raise _LeaseLost

    async def _update(self, rc: _Run, **fields: object) -> None:
        if not await self._store.update_run(rc.run.id, self.owner, **fields):
            raise _LeaseLost
        status = fields.get("status")
        if isinstance(status, RunStatus):
            rc.run.status = status

    async def _renew_loop(self, rc: _Run) -> None:
        interval = self._s.lease_s / 3
        while True:
            await asyncio.sleep(interval)
            until = self._clock.now() + timedelta(seconds=self._s.lease_s)
            try:
                ok = await self._store.renew_lease(rc.run.id, self.owner, until)
            except Exception as exc:  # keep renewing through transient DB errors
                rc.log("lease_renew_failed", exc_type=type(exc).__name__)
                continue
            if not ok:
                rc.lease_lost = True
                return

    async def _finish(
        self,
        rc: _Run,
        status: RunStatus,
        *,
        skip_reason: str | None = None,
        error_code: str | None = None,
        output_message_id: str | None = None,
        context_seq: int | None = None,
        **log_fields: object,
    ) -> RunOutcome:
        latency_ms = int((self._clock.monotonic() - rc.t0) * 1000)
        fields: dict[str, object] = {
            "status": status,
            "skip_reason": skip_reason,
            "error_code": error_code,
            "latency_ms": latency_ms,
            "input_tokens": rc.input_tokens,
            "output_tokens": rc.output_tokens,
            "model": rc.model,
        }
        if output_message_id is not None:
            fields["output_message_id"] = output_message_id
        if context_seq is not None:
            fields["context_seq"] = context_seq
        if not await self._store.update_run(rc.run.id, self.owner, **fields):
            rc.log("agent_run_lease_lost", status=status)
        rc.run.status = status
        entry: dict[str, object] = {
            "seq": rc.run.context_seq,
            "latency_ms": latency_ms,
            "skip_reason": skip_reason,
            "error_code": error_code,
            "input_tokens": rc.input_tokens,
            "output_tokens": rc.output_tokens,
            "model": rc.model,
            "attempts": rc.run.attempts,
        }
        entry.update(log_fields)
        rc.log(f"agent_run_{status.value}", **entry)
        return RunOutcome(
            rc.session_id,
            run_id=rc.run.id,
            status=status,
            skip_reason=skip_reason,
            error_code=error_code,
        )

    async def _debit(self, rc: _Run, usage: Usage) -> None:
        """Debit each model call's tokens as soon as it returns, whatever the run's outcome
        (Deep Dive §5 "Every model call ... adds its tokens", §7). The usage endpoint is not
        idempotent, so the client never retries it after the request may have been sent."""
        if usage.total <= 0:
            return
        try:
            await self._chat.record_usage(rc.session_id, rc.run.id, usage.total)
        except ChatError as exc:
            rc.log("usage_debit_failed", tokens=usage.total, code=exc.code)

    async def _ack(self, session_id: str, agent_id: str, seq: int, *, trace_id: str | None) -> None:
        """Advance the agent's read cursor (the amber tick). Never while inactive (D-020)."""
        if seq <= 0 or self.presence.is_inactive(session_id):
            return
        if seq <= self._acked.get(session_id, 0):
            return
        try:
            await self._chat.ack_read(session_id, agent_id, seq)
        except ChatError as exc:
            log_event("read_ack_failed", session_id=session_id, seq=seq, code=exc.code)
            return
        self._acked[session_id] = seq
        log_event("read_ack", session_id=session_id, actor_id=agent_id, seq=seq, trace_id=trace_id)
