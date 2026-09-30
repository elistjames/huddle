"""Core domain value types shared by services (Deep Dive §1, §4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

MAX_BODY_CHARS = 4000  # Deep Dive §8: messages are limited to 4,000 characters
AGENT_MAX_BODY_CHARS = 1000  # Deep Dive §5: agent output over 1,000 chars is rejected


class SenderType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class ParticipantType(StrEnum):
    USER = "user"
    AGENT = "agent"


class MessageKind(StrEnum):
    TEXT = "text"
    SYSTEM = "system"


class AgentState(StrEnum):
    FOLLOWING = "following"
    WRITING = "writing"
    PAUSED = "paused"
    RESTING = "resting"
    AWAY = "away"


class Message(BaseModel):
    id: str  # msg_ + ULID
    session_id: str
    seq: int
    sender_type: SenderType
    sender_id: str
    client_msg_id: str
    kind: MessageKind = MessageKind.TEXT
    body: str | None  # None once redacted
    created_at: datetime


class BudgetView(BaseModel):
    """What users see: percentage used and when the fixed window ends (D-019)."""

    used_pct: int = Field(ge=0, le=100)
    refills_at: datetime


class AgentView(BaseModel):
    id: str  # agt_ + ULID
    name: str  # "Huddle AI" unless renamed
    state: AgentState
    budget: BudgetView


class MemberView(BaseModel):
    participant_id: str
    participant_type: ParticipantType
    display_name: str
    role: str  # owner | member
    status: str  # invited | active | left | removed


class SessionView(BaseModel):
    id: str  # ses_ + ULID
    title: str
    status: str  # active | closed
    last_seq: int
    members: list[MemberView]
    agent: AgentView | None
