"""Agent presence as seen by users (Deep Dive §1 "Agent presence", §10 UI states).

`following ⇄ writing`, plus `paused` (set by a user), `resting` (budget short) and `away` (model
runtime unreachable, or the global kill switch). Agent errors never reach the thread; they only
show through these states.

The chat service derives `paused` (agents.status) and `resting` (the budget) itself, so the agent
reports only `following`, `writing` and `away` over `PUT .../agent/status`. It still tracks every
state locally, because an inactive agent must not advance its read cursor (D-020).
"""

from __future__ import annotations

from huddle_agent.chat_client import ChatClient, ChatError
from huddle_agent.logs import log_event
from huddle_contracts.models import AgentState

INACTIVE = frozenset({AgentState.PAUSED, AgentState.RESTING, AgentState.AWAY})
REPORTED = frozenset({AgentState.FOLLOWING, AgentState.WRITING, AgentState.AWAY})


class Presence:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat
        self._state: dict[str, AgentState] = {}

    def get(self, session_id: str) -> AgentState | None:
        return self._state.get(session_id)

    def is_inactive(self, session_id: str) -> bool:
        return self._state.get(session_id) in INACTIVE

    def sessions_in(self, *states: AgentState) -> list[str]:
        return [sid for sid, s in self._state.items() if s in states]

    async def set(self, session_id: str, state: AgentState, *, force: bool = False) -> None:
        previous = self._state.get(session_id)
        if previous == state and not force:
            return
        # A chat-owned state is not PUT, but a stale reported `writing` must not outlive it.
        reported = state if state in REPORTED else None
        if reported is None and previous == AgentState.WRITING:
            reported = AgentState.FOLLOWING
        if reported is not None:
            try:
                await self._chat.set_status(session_id, reported)
            except ChatError as exc:
                # Keep the old state so the next transition retries the PUT.
                log_event("agent_status_failed", session_id=session_id, state=state, code=exc.code)
                return
        self._state[session_id] = state
        log_event(
            "agent_status_changed",
            session_id=session_id,
            state=state,
            previous=previous,
            reported=state in REPORTED,
        )
