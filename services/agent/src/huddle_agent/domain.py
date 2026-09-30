"""Agent-side domain records (read models of Deep Dive §4 rows)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from huddle_contracts.models import MessageKind, SenderType


class RunStatus(StrEnum):
    """Agent run states (Deep Dive §1). The partial unique index covers LIVE_STATUSES."""

    TRIGGERED = "triggered"
    ELIGIBLE = "eligible"
    SKIPPED = "skipped"
    CONTEXT_BUILT = "context_built"
    GENERATING = "generating"
    VALIDATED = "validated"
    COMMITTED = "committed"
    OBSERVED = "observed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


LIVE_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.ELIGIBLE,
    RunStatus.CONTEXT_BUILT,
    RunStatus.GENERATING,
    RunStatus.VALIDATED,
)
TERMINAL_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.SKIPPED,
    RunStatus.OBSERVED,
    RunStatus.CANCELLED,
    RunStatus.SUPERSEDED,
    RunStatus.TIMED_OUT,
    RunStatus.FAILED,
)


class TriggerKind(StrEnum):
    MESSAGE = "message"
    NUDGE = "nudge"


@dataclass(frozen=True)
class ChatMessage:
    id: str
    seq: int
    sender_type: SenderType
    sender_id: str
    kind: MessageKind
    body: str | None
    created_at: datetime
    client_msg_id: str = ""

    @property
    def is_text(self) -> bool:
        return self.kind == MessageKind.TEXT

    @property
    def is_human(self) -> bool:
        return self.sender_type == SenderType.USER and self.kind == MessageKind.TEXT


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    status: str  # active | paused | removed
    model: str
    fallback_model: str | None
    prompt_version: str
    config: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class SessionInfo:
    id: str
    status: str  # active | closed
    last_seq: int
    last_human_message_at: datetime | None


@dataclass(frozen=True)
class Summary:
    covers_through_seq: int
    summary: str
    token_count: int
    model: str


@dataclass(frozen=True)
class GateSnapshot:
    """Everything the deterministic gate needs; one cheap read per evaluation."""

    session: SessionInfo
    agent: AgentInfo
    recent: list[ChatMessage]  # last `share_window` TEXT messages, oldest first
    active_participants: int  # N, includes agents (Deep Dive §5)
    last_agent_message: ChatMessage | None
    last_evaluated_seq: int  # context_seq of the last run that reached the decider
    nudge_done_for_lull: bool  # a nudge run already happened since last_human_message_at


@dataclass
class RunRecord:
    id: str
    session_id: str
    agent_id: str
    trigger_kind: TriggerKind
    trigger_seq: int
    context_seq: int
    status: RunStatus
    attempts: int = 1
    skip_reason: str | None = None
    error_code: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None
    output_message_id: str | None = None
    created_at: datetime | None = None
