"""WebSocket envelope and events (Deep Dive §6)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from huddle_contracts import PROTOCOL_VERSION
from huddle_contracts.errors import ErrorBody
from huddle_contracts.models import AgentState, BudgetView, Message


class _Frame(BaseModel):
    v: int = PROTOCOL_VERSION


# ---- client -> server ---------------------------------------------------------------------


class Auth(_Frame):
    type: Literal["auth"] = "auth"
    token: str
    device_id: str


class Subscribe(_Frame):
    type: Literal["subscribe"] = "subscribe"
    session_id: str
    after_seq: int = 0


class MessageSend(_Frame):
    type: Literal["message.send"] = "message.send"
    client_msg_id: str
    body: str
    trace_id: str | None = None


class Ack(_Frame):
    """Cumulative delivery ack (drives the second tick)."""

    type: Literal["ack"] = "ack"
    up_to_seq: int


class Read(_Frame):
    type: Literal["read"] = "read"
    up_to_seq: int


class Ping(_Frame):
    type: Literal["ping"] = "ping"


ClientFrame = Annotated[
    Auth | Subscribe | MessageSend | Ack | Read | Ping, Field(discriminator="type")
]


# ---- server -> client ---------------------------------------------------------------------


class AuthOk(_Frame):
    type: Literal["auth.ok"] = "auth.ok"
    user_id: str


class MessageAck(_Frame):
    """First tick: the message is stored."""

    type: Literal["message.ack"] = "message.ack"
    client_msg_id: str
    message_id: str
    seq: int


class MessageCreated(_Frame):
    type: Literal["message.created"] = "message.created"
    event_id: str
    trace_id: str | None = None
    session_id: str
    sequence: int  # == message.seq
    message: Message


class SyncBatch(_Frame):
    type: Literal["sync.batch"] = "sync.batch"
    session_id: str
    messages: list[Message]
    has_more: bool


class ReceiptUpdated(_Frame):
    """Cursors per participant. Clients derive the ticks (Deep Dive §2):
    second tick when any other human's cursor >= seq; amber third tick when the agent's is."""

    type: Literal["receipt.updated"] = "receipt.updated"
    session_id: str
    cursors: dict[str, int]


class AgentStatus(_Frame):
    type: Literal["agent.status"] = "agent.status"
    session_id: str
    state: AgentState
    name: str = "Huddle AI"
    budget: BudgetView


class MemberEvent(_Frame):
    type: Literal["member.joined", "member.left"]
    session_id: str
    participant_id: str
    display_name: str


class SessionUpdated(_Frame):
    type: Literal["session.updated"] = "session.updated"
    session_id: str
    title: str | None = None
    status: str | None = None


class ServerDraining(_Frame):
    type: Literal["server.draining"] = "server.draining"
    reconnect_after_ms: int


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    error: ErrorBody


class Pong(_Frame):
    type: Literal["pong"] = "pong"


ServerFrame = Annotated[
    AuthOk
    | MessageAck
    | MessageCreated
    | SyncBatch
    | ReceiptUpdated
    | AgentStatus
    | MemberEvent
    | SessionUpdated
    | ServerDraining
    | ErrorFrame
    | Pong,
    Field(discriminator="type"),
]

# Close codes
CLOSE_SLOW_CONSUMER = 4008
CLOSE_AUTH_TIMEOUT = 4001


class FanoutEnvelope(BaseModel):
    """Payload published by the chat service on Redis channel gw:{gateway_id}.
    The gateway writes `frame` to each socket in `connection_ids` (dead ones are ignored)."""

    connection_ids: list[str]
    frame: dict[str, Any]
