"""Internal HTTP contracts (private network, `X-Service-Token` header). Deep Dive §3.

Chat service exposes these; gateway and agent call them. Public REST lives in the chat service too
(Deep Dive §6) and is not modelled here except where the web client needs shared shapes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from huddle_contracts.errors import ErrorBody
from huddle_contracts.models import AgentState, BudgetView, Message, SenderType

SERVICE_TOKEN_HEADER = "X-Service-Token"  # noqa: S105  # nosec B105


class Sender(BaseModel):
    type: SenderType
    id: str


# POST /internal/messages  (idempotent on (session_id, sender.id, client_msg_id))
class SubmitMessageRequest(BaseModel):
    session_id: str
    sender: Sender
    client_msg_id: str
    body: str
    trace_id: str | None = None


class SubmitMessageResponse(BaseModel):
    message_id: str
    seq: int
    duplicate: bool = False  # True when an earlier identical submit is being replayed


class SubmitMessageError(BaseModel):
    error: ErrorBody


# POST /internal/acks
class AckDeliveryRequest(BaseModel):
    participant_id: str
    device_id: str | None = None
    session_id: str
    up_to_seq: int
    kind: str = "delivered"  # "delivered" | "read"; agent 'read' produces the amber tick


# GET /internal/sessions/{session_id}/messages?after_seq=&before_seq=&limit=
class MessagePage(BaseModel):
    messages: list[Message]
    has_more: bool


# GET /internal/sessions/{session_id}/members/{participant_id}
class MembershipCheck(BaseModel):
    is_active: bool
    participant_type: str | None = None
    display_name: str | None = None
    last_delivered_seq: int = 0


# PUT /internal/sessions/{session_id}/agent/status  (agent -> chat; chat broadcasts agent.status)
class SetAgentStatusRequest(BaseModel):
    state: AgentState


# POST /internal/sessions/{session_id}/agent/usage  (agent -> chat; debits the fixed-window budget)
class RecordUsageRequest(BaseModel):
    run_id: str
    tokens: int = Field(ge=0)


class BudgetState(BaseModel):
    """Internal, precise view for the agent's gate (users only ever see `BudgetView`)."""

    capacity_tokens: int
    used_tokens: int
    window_seconds: int
    window_started_at: datetime
    refills_at: datetime
    view: BudgetView

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.capacity_tokens - self.used_tokens)


# POST /internal/sessions/{session_id}/receipts  (gateway -> chat on ack/read frames is AckDelivery)


# Redis stream entry (agent:triggers) enqueued by chat service after each committed HUMAN message
class AgentTrigger(BaseModel):
    session_id: str
    seq: int
    message_id: str
    sender_type: SenderType
    trace_id: str | None = None
