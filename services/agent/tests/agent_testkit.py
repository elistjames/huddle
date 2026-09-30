"""Shared helpers for the agent service tests (unique module name: pytest prepend mode)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from huddle_agent.clock import FakeClock
from huddle_agent.config import Settings
from huddle_agent.domain import (
    AgentInfo,
    ChatMessage,
    GateSnapshot,
    SessionInfo,
)
from huddle_agent.memory import MemoryChat, MemoryStore, MemoryWorld
from huddle_agent.models.base import ModelClient
from huddle_agent.presence import Presence
from huddle_agent.runner import RunExecutor
from huddle_contracts.models import MessageKind, SenderType

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=FakeClock().now().tzinfo)
AGENT = AgentInfo(
    id="agt_main",
    name="Huddle AI",
    status="active",
    model="llama3.1:8b",
    fallback_model=None,
    prompt_version="v1",
)


def msg(
    seq: int,
    who: str,
    body: str = "hello there",
    *,
    at: datetime | None = None,
    kind: MessageKind = MessageKind.TEXT,
) -> ChatMessage:
    """who: 'agent', 'system', or a user id."""
    if who == "agent":
        st, sid = SenderType.AGENT, AGENT.id
    elif who == "system":
        st, sid, kind = SenderType.SYSTEM, "system", MessageKind.SYSTEM
    else:
        st, sid = SenderType.USER, who
    return ChatMessage(
        id=f"msg_{seq}",
        seq=seq,
        sender_type=st,
        sender_id=sid,
        kind=kind,
        body=body,
        created_at=at or (T0 + timedelta(seconds=seq)),
        client_msg_id=f"00000000-0000-0000-0000-{seq:012d}",
    )


def snapshot(
    recent: list[ChatMessage],
    *,
    n: int = 3,
    agent: AgentInfo = AGENT,
    status: str = "active",
    last_evaluated_seq: int = 0,
    last_human_at: datetime | None = None,
    nudge_done: bool = False,
) -> GateSnapshot:
    text = [m for m in recent if m.is_text]
    last_agent = next((m for m in reversed(text) if m.sender_type == SenderType.AGENT), None)
    humans = [m for m in text if m.is_human]
    return GateSnapshot(
        session=SessionInfo(
            id="ses_main",
            status=status,
            last_seq=recent[-1].seq if recent else 0,
            last_human_message_at=last_human_at or (humans[-1].created_at if humans else None),
        ),
        agent=agent,
        recent=text[-30:],
        active_participants=n,
        last_agent_message=last_agent,
        last_evaluated_seq=last_evaluated_seq,
        nudge_done_for_lull=nudge_done,
    )


def make_executor(
    world: MemoryWorld,
    models: ModelClient,
    settings: Settings | None = None,
) -> RunExecutor:
    chat = MemoryChat(world)
    return RunExecutor(
        store=MemoryStore(world),
        chat=chat,
        models=models,
        settings=settings or Settings(worker_id="worker-1"),
        clock=world.clock,
        presence=Presence(chat),
    )


def require_env(name: str) -> str:
    """Integration store URL, or skip (fail in CI with HUDDLE_REQUIRE_STORES=1)."""
    value = os.environ.get(name)
    if value:
        return value
    if os.environ.get("HUDDLE_REQUIRE_STORES") == "1":
        pytest.fail(f"{name} is not set but HUDDLE_REQUIRE_STORES=1")
    pytest.skip(f'{name} not set; run: eval "$(scripts/dev-stores.sh)"')
