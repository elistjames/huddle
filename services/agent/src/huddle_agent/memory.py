"""In-memory `Store` and `ChatClient` over one shared world, for unit tests and evals.

The world mimics the parts of the chat service the agent relies on: gapless `seq`, idempotent
`submit_message` on (session, sender, client_msg_id), cursors, the fixed-window budget (D-019)
and the one-live-run partial unique index. `PostgresStore` is held to the same behaviour by the
shared store contract tests.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ulid import ULID

from huddle_agent.chat_client import ChatError
from huddle_agent.clock import Clock
from huddle_agent.context import message_tokens
from huddle_agent.domain import (
    LIVE_STATUSES,
    AgentInfo,
    ChatMessage,
    GateSnapshot,
    RunRecord,
    RunStatus,
    SessionInfo,
    Summary,
    TriggerKind,
)
from huddle_agent.store import (
    EVALUATED_SKIPS,
    EVALUATED_STATUSES,
    NUDGE_NOT_ATTEMPTED,
    RearmCandidate,
)
from huddle_contracts import budget as fixed_window
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import BudgetState, SubmitMessageResponse
from huddle_contracts.models import (
    MAX_BODY_CHARS,
    AgentState,
    BudgetView,
    MessageKind,
    SenderType,
)

_MUTABLE = (RunStatus.TRIGGERED, *LIVE_STATUSES, RunStatus.COMMITTED)


@dataclass
class _Session:
    id: str
    status: str = "active"
    last_seq: int = 0
    last_human_message_at: datetime | None = None


@dataclass
class _Member:
    participant_id: str
    participant_type: str
    display_name: str
    status: str = "active"
    last_delivered_seq: int = 0
    last_read_seq: int = 0


@dataclass
class _Budget:
    capacity_tokens: int
    used_tokens: int
    window_seconds: int
    window_started_at: datetime


@dataclass
class MemoryWorld:
    clock: Clock
    session_id: str = "ses_main"
    agent: AgentInfo = field(
        default_factory=lambda: AgentInfo(
            id="agt_main",
            name="Huddle AI",
            status="active",
            model="llama3.1:8b",
            fallback_model=None,
            prompt_version="v1",
        )
    )
    humans: Sequence[tuple[str, str]] = (("usr_alice", "Alice"), ("usr_bob", "Bob"))
    capacity_tokens: int = fixed_window.DEFAULT_CAPACITY_TOKENS
    window_seconds: int = fixed_window.DEFAULT_WINDOW_SECONDS

    sessions: dict[str, _Session] = field(default_factory=dict[str, _Session])
    members: dict[str, dict[str, _Member]] = field(default_factory=dict[str, dict[str, _Member]])
    messages: dict[str, list[ChatMessage]] = field(default_factory=dict[str, list[ChatMessage]])
    runs: dict[str, RunRecord] = field(default_factory=dict[str, RunRecord])
    summaries: list[tuple[str, str, Summary]] = field(
        default_factory=list[tuple[str, str, Summary]]
    )
    budgets: dict[str, _Budget] = field(default_factory=dict[str, _Budget])
    status_log: list[tuple[str, AgentState]] = field(default_factory=list[tuple[str, AgentState]])
    acks: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    usage: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    submit_failures: deque[BaseException] = field(default_factory=deque[BaseException])
    lose_submit_response: int = 0  # commit, then fail the response this many times
    submit_calls: int = 0
    fail_budget: bool = False

    def __post_init__(self) -> None:
        sid = self.session_id
        self.sessions[sid] = _Session(sid)
        self.messages[sid] = []
        self.members[sid] = {uid: _Member(uid, "user", name) for uid, name in self.humans}
        self.members[sid][self.agent.id] = _Member(self.agent.id, "agent", self.agent.name)
        now = self.clock.now()
        self.budgets[sid] = _Budget(
            self.capacity_tokens,
            0,
            self.window_seconds,
            fixed_window.window_start(now, self.window_seconds),
        )

    # ---- test drivers ---------------------------------------------------------------------

    def append(
        self,
        sid: str,
        sender_type: SenderType,
        sender_id: str,
        body: str,
        kind: MessageKind,
        client_msg_id: str | None = None,
    ) -> ChatMessage:
        session = self.sessions[sid]
        session.last_seq += 1
        msg = ChatMessage(
            id=f"msg_{ULID()}",
            seq=session.last_seq,
            sender_type=sender_type,
            sender_id=sender_id,
            kind=kind,
            body=body,
            created_at=self.clock.now(),
            client_msg_id=client_msg_id or str(ULID().to_uuid()),
        )
        self.messages[sid].append(msg)
        if sender_type == SenderType.USER and kind == MessageKind.TEXT:
            session.last_human_message_at = msg.created_at
        return msg

    def post_human(self, sender_id: str, body: str, session_id: str | None = None) -> ChatMessage:
        return self.append(
            session_id or self.session_id, SenderType.USER, sender_id, body, MessageKind.TEXT
        )

    def post_system(self, body: str, session_id: str | None = None) -> ChatMessage:
        return self.append(
            session_id or self.session_id, SenderType.SYSTEM, "system", body, MessageKind.SYSTEM
        )

    def post_agent(self, body: str, session_id: str | None = None) -> ChatMessage:
        return self.append(
            session_id or self.session_id, SenderType.AGENT, self.agent.id, body, MessageKind.TEXT
        )

    def set_agent_status(self, status: str) -> None:
        self.agent = replace(self.agent, status=status)

    def set_agent_config(self, **config: Any) -> None:
        self.agent = replace(self.agent, config={**self.agent.config, **config})

    def set_budget_used(self, used: int, session_id: str | None = None) -> None:
        self.budgets[session_id or self.session_id].used_tokens = used

    def agent_messages(self, session_id: str | None = None) -> list[ChatMessage]:
        return [
            m
            for m in self.messages[session_id or self.session_id]
            if m.sender_type == SenderType.AGENT
        ]

    def states(self, session_id: str | None = None) -> list[AgentState]:
        sid = session_id or self.session_id
        return [s for (x, s) in self.status_log if x == sid]

    def agent_cursor(self, session_id: str | None = None) -> int:
        return self.members[session_id or self.session_id][self.agent.id].last_read_seq


class MemoryStore:
    def __init__(self, world: MemoryWorld) -> None:
        self.w = world

    def _agent_for(self, session_id: str) -> AgentInfo | None:
        members = self.w.members.get(session_id, {})
        m = members.get(self.w.agent.id)
        if m is None or m.status != "active":
            return None
        return self.w.agent

    def _last_human_seq(self, session_id: str) -> int:
        for m in reversed(self.w.messages.get(session_id, [])):
            if m.is_human:
                return m.seq
        return 0

    def _nudge_done(self, session_id: str, agent_id: str) -> bool:
        lull = self._last_human_seq(session_id)
        return any(
            r.session_id == session_id
            and r.agent_id == agent_id
            and r.trigger_kind == TriggerKind.NUDGE
            and r.trigger_seq >= lull
            and r.skip_reason not in NUDGE_NOT_ATTEMPTED
            for r in self.w.runs.values()
        )

    async def load_gate_snapshot(self, session_id: str, share_window: int) -> GateSnapshot | None:
        s = self.w.sessions.get(session_id)
        agent = self._agent_for(session_id)
        if s is None or agent is None:
            return None
        msgs = self.w.messages[session_id]
        text = [m for m in msgs if m.is_text]
        last_agent = next(
            (
                m
                for m in reversed(text)
                if m.sender_type == SenderType.AGENT and m.sender_id == agent.id
            ),
            None,
        )
        evaluated = max(
            (
                r.context_seq
                for r in self.w.runs.values()
                if r.session_id == session_id
                and r.agent_id == agent.id
                and r.trigger_kind == TriggerKind.MESSAGE
                and (
                    r.status.value in EVALUATED_STATUSES
                    or (r.status == RunStatus.SKIPPED and r.skip_reason in EVALUATED_SKIPS)
                )
            ),
            default=0,
        )
        active = sum(1 for m in self.w.members[session_id].values() if m.status == "active")
        return GateSnapshot(
            session=SessionInfo(s.id, s.status, s.last_seq, s.last_human_message_at),
            agent=agent,
            recent=text[-share_window:],
            active_participants=active,
            last_agent_message=last_agent,
            last_evaluated_seq=evaluated,
            nudge_done_for_lull=self._nudge_done(session_id, agent.id),
        )

    async def load_window(
        self, session_id: str, agent_id: str, max_tokens: int
    ) -> tuple[Summary | None, list[ChatMessage]]:
        rows = [s for (sid, aid, s) in self.w.summaries if sid == session_id and aid == agent_id]
        summary = max(rows, key=lambda s: s.covers_through_seq, default=None)
        floor = summary.covers_through_seq if summary else 0
        out: list[ChatMessage] = []
        acc = 0
        for m in reversed(self.w.messages.get(session_id, [])):
            if m.seq <= floor:
                break
            out.append(m)
            acc += message_tokens(m)
            if acc > max_tokens:
                break
        out.reverse()
        return summary, out

    async def latest_human_seq(self, session_id: str) -> int:
        return self._last_human_seq(session_id)

    async def latest_seq(self, session_id: str) -> int:
        return self.w.sessions[session_id].last_seq

    async def insert_summary(self, session_id: str, agent_id: str, summary: Summary) -> None:
        self.w.summaries.append((session_id, agent_id, summary))

    async def get_run(self, run_id: str) -> RunRecord | None:
        r = self.w.runs.get(run_id)
        return replace(r) if r else None

    async def find_live_run(self, session_id: str, agent_id: str) -> RunRecord | None:
        for r in self.w.runs.values():
            if r.session_id == session_id and r.agent_id == agent_id and r.status in LIVE_STATUSES:
                return replace(r)
        return None

    async def create_run(self, run: RunRecord, prompt_version: str | None) -> None:
        if run.id in self.w.runs:
            raise ValueError("duplicate run id")
        self.w.runs[run.id] = replace(run, created_at=self.w.clock.now())

    async def promote_to_eligible(self, run_id: str, owner: str) -> bool:
        r = self.w.runs[run_id]
        if r.lease_owner != owner or r.status != RunStatus.TRIGGERED:
            return False
        if await self.find_live_run(r.session_id, r.agent_id) is not None:
            return False  # the partial unique index
        r.status = RunStatus.ELIGIBLE
        return True

    async def takeover_run(
        self, run_id: str, owner: str, now: datetime, lease_until: datetime
    ) -> RunRecord | None:
        r = self.w.runs.get(run_id)
        if r is None or r.status not in LIVE_STATUSES:
            return None
        if r.lease_expires_at is not None and r.lease_expires_at >= now:
            return None
        r.lease_owner = owner
        r.lease_expires_at = lease_until
        r.attempts += 1
        r.status = RunStatus.ELIGIBLE
        return replace(r)

    async def update_run(self, run_id: str, owner: str, **fields: Any) -> bool:
        r = self.w.runs.get(run_id)
        if r is None or r.lease_owner != owner or r.status not in _MUTABLE:
            return False
        for k, v in fields.items():
            if not hasattr(r, k):
                raise ValueError(f"unknown run field {k}")
            setattr(r, k, RunStatus(v) if k == "status" else v)
        return True

    async def renew_lease(self, run_id: str, owner: str, lease_until: datetime) -> bool:
        r = self.w.runs.get(run_id)
        if r is None or r.lease_owner != owner or r.status not in _MUTABLE:
            return False
        r.lease_expires_at = lease_until
        return True

    async def expire_orphans(self, now: datetime) -> int:
        n = 0
        for r in self.w.runs.values():
            if (
                r.status == RunStatus.TRIGGERED
                and r.lease_expires_at is not None
                and r.lease_expires_at < now
            ):
                r.status = RunStatus.TIMED_OUT
                r.error_code = "lease_expired"
                n += 1
        return n

    async def agent_message_for_run(
        self, session_id: str, agent_id: str, run_id: str
    ) -> tuple[str, int] | None:
        for m in self.w.messages.get(session_id, []):
            if m.sender_id == agent_id and m.client_msg_id == run_id:
                return m.id, m.seq
        return None

    async def rearm_candidates(self) -> list[RearmCandidate]:
        out: list[RearmCandidate] = []
        for s in self.w.sessions.values():
            agent = self._agent_for(s.id)
            if s.status != "active" or s.last_human_message_at is None or agent is None:
                continue
            out.append(
                RearmCandidate(
                    s.id, agent, s.last_human_message_at, self._nudge_done(s.id, agent.id)
                )
            )
        return out


class MemoryChat:
    def __init__(self, world: MemoryWorld) -> None:
        self.w = world

    async def submit_message(
        self, session_id: str, agent_id: str, client_msg_id: str, body: str, trace_id: str | None
    ) -> SubmitMessageResponse:
        self.w.submit_calls += 1
        if self.w.submit_failures:
            raise self.w.submit_failures.popleft()
        for m in self.w.messages.get(session_id, []):
            if m.sender_id == agent_id and m.client_msg_id == client_msg_id:
                return SubmitMessageResponse(message_id=m.id, seq=m.seq, duplicate=True)
        session = self.w.sessions.get(session_id)
        if session is None or session.status != "active":
            raise ChatError(ErrorCode.SESSION_CLOSED.value, retryable=False, status=409)
        member = self.w.members[session_id].get(agent_id)
        if member is None or member.status != "active":
            raise ChatError(ErrorCode.NOT_MEMBER.value, retryable=False, status=403)
        if len(body) > MAX_BODY_CHARS:
            raise ChatError(ErrorCode.MESSAGE_TOO_LONG.value, retryable=False, status=422)
        msg = self.w.append(
            session_id, SenderType.AGENT, agent_id, body, MessageKind.TEXT, client_msg_id
        )
        if self.w.lose_submit_response > 0:
            self.w.lose_submit_response -= 1
            raise ChatError("CHAT_UNREACHABLE", retryable=True)
        return SubmitMessageResponse(message_id=msg.id, seq=msg.seq)

    async def ack_read(self, session_id: str, agent_id: str, up_to_seq: int) -> None:
        member = self.w.members[session_id][agent_id]
        member.last_read_seq = max(member.last_read_seq, up_to_seq)
        member.last_delivered_seq = max(member.last_delivered_seq, up_to_seq)
        self.w.acks.append((session_id, agent_id, up_to_seq))

    async def set_status(self, session_id: str, state: AgentState) -> None:
        self.w.status_log.append((session_id, state))

    async def record_usage(self, session_id: str, run_id: str, tokens: int) -> None:
        b = self._rolled(session_id)
        b.used_tokens += tokens
        self.w.usage.append((session_id, run_id, tokens))

    def _rolled(self, session_id: str) -> _Budget:
        b = self.w.budgets[session_id]
        b.window_started_at, b.used_tokens = fixed_window.roll(
            self.w.clock.now(), b.window_started_at, b.used_tokens, b.window_seconds
        )
        return b

    async def get_budget(self, session_id: str) -> BudgetState:
        if self.w.fail_budget:
            raise ChatError("CHAT_UNREACHABLE", retryable=True)
        b = self._rolled(session_id)
        refills = fixed_window.window_end(self.w.clock.now(), b.window_seconds)
        return BudgetState(
            capacity_tokens=b.capacity_tokens,
            used_tokens=b.used_tokens,
            window_seconds=b.window_seconds,
            window_started_at=b.window_started_at,
            refills_at=refills,
            view=BudgetView(
                used_pct=fixed_window.used_pct(b.used_tokens, b.capacity_tokens),
                refills_at=refills,
            ),
        )

    async def display_name(self, session_id: str, participant_id: str) -> str | None:
        m = self.w.members.get(session_id, {}).get(participant_id)
        return m.display_name if m else None
