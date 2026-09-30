"""Agent presence as seen by users (Deep Dive §1 "Agent presence", §10 UI states).

`following ⇄ writing`, plus `paused` (a user paused it, or the global kill switch), `resting`
(budget short) and `away` (model runtime unreachable). Agent errors never reach the thread; they
only show through these states.
"""

from __future__ import annotations

from huddle_agent.chat_client import ChatClient, ChatError
from huddle_agent.logs import log_event
from huddle_contracts.models import AgentState

INACTIVE = frozenset({AgentState.PAUSED, AgentState.RESTING, AgentState.AWAY})


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
        try:
            await self._chat.set_status(session_id, state)
        except ChatError as exc:
            # Keep the old state so the next transition retries the PUT.
            log_event("agent_status_failed", session_id=session_id, state=state, code=exc.code)
            return
        self._state[session_id] = state
        log_event("agent_status_changed", session_id=session_id, state=state, previous=previous)
