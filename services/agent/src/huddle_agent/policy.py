"""Deterministic speak gate and debounce (Deep Dive §5). Pure functions; no I/O.

The gate is stage one of the two-stage policy (D-005): cheap rule checks run before any model
call. Stage two (the decider) only runs when the gate says the agent is eligible.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from huddle_agent.config import PolicyConfig
from huddle_agent.domain import ChatMessage, GateSnapshot, TriggerKind
from huddle_contracts.budget import is_resting
from huddle_contracts.models import AgentState, SenderType


class SkipReason(StrEnum):
    DISABLED = "disabled"  # global kill switch
    PAUSED = "paused"
    REMOVED = "removed"
    SESSION_CLOSED = "session_closed"
    RESTING = "resting"
    ALREADY_EVALUATED = "already_evaluated"
    CONSECUTIVE = "consecutive"
    COOLDOWN = "cooldown"
    SHARE_CAP = "share_cap"
    NUDGE_DISABLED = "nudge_disabled"
    NO_LULL = "no_lull"
    LULL_NOT_OVER = "lull_not_over"
    NUDGE_DONE = "nudge_done"
    AGENT_SPOKE_LAST = "agent_spoke_last"
    DECIDER_DECLINED = "decider_declined"
    CONVERSATION_CLOSED = "conversation_closed"
    COALESCED = "coalesced"


# Skip reasons that mean the agent is inactive: its read cursor must not advance (D-020).
INACTIVE_REASONS: dict[SkipReason, AgentState] = {
    SkipReason.DISABLED: AgentState.AWAY,  # kill switch: users cannot resume it
    SkipReason.PAUSED: AgentState.PAUSED,
    SkipReason.REMOVED: AgentState.PAUSED,
    SkipReason.RESTING: AgentState.RESTING,
}


@dataclass(frozen=True)
class GateResult:
    eligible: bool
    skip_reason: SkipReason | None = None
    encourage: bool = False  # share below 0.5 x 1/N: nudge the decider toward speaking
    mentioned: bool = False
    share: float = 0.0
    cooldown_s: float = 0.0

    @property
    def inactive_state(self) -> AgentState | None:
        if self.skip_reason is None:
            return None
        return INACTIVE_REASONS.get(self.skip_reason)


def _skip(reason: SkipReason, *, share: float = 0.0, cooldown_s: float = 0.0) -> GateResult:
    return GateResult(eligible=False, skip_reason=reason, share=share, cooldown_s=cooldown_s)


# ---- helpers --------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    "about above after again also because been before being between both could does doing "
    "down during each from further have having here into itself just more most much must "
    "only other ought over same should some such than that their them then there these they "
    "this those through under until very what when where which while will with would your "
    "yours yourself okay yeah sure maybe really think thing things know like just good great "
    "thanks thank".split()
)


def mentions_agent(text: str | None, agent_name: str) -> bool:
    """True if the agent's name appears in `text` (case-insensitive, whole words).

    @mentions are out of scope (Deep Dive §5): this is a plain name match. The name also matches
    with its spaces removed ("HuddleAI") and with a leading "@".
    """
    if not text or not agent_name.strip():
        return False
    words = [re.escape(w) for w in agent_name.split()]
    spaced = r"\s+".join(words)
    joined = "".join(words)
    pattern = rf"(?<![\w])@?(?:{spaced}|{joined})(?![\w])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _content_words(text: str | None) -> set[str]:
    if not text:
        return set()
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 4 and w not in _STOPWORDS}


def is_engaged(agent_msg: ChatMessage, following: Sequence[ChatMessage], agent_name: str) -> bool:
    """Did any human engage with `agent_msg` in the messages that follow it?

    Deep Dive §5 does not define "engagement" and v1 has no replies or reactions, so a human
    message counts as engagement when it names the agent or shares at least two content words
    with the agent's message.
    """
    agent_words = _content_words(agent_msg.body)
    for m in following:
        if not m.is_human:
            continue
        if mentions_agent(m.body, agent_name):
            return True
        if len(agent_words & _content_words(m.body)) >= 2:
            return True
    return False


def unengaged_streak(
    recent: Sequence[ChatMessage], agent_id: str, agent_name: str, window: int
) -> int:
    """Consecutive most-recent agent messages that got no human engagement within `window`
    following messages. Agent messages with fewer than `window` followers and no engagement yet
    are still pending and neither count nor break the streak."""
    streak = 0
    for i in range(len(recent) - 1, -1, -1):
        msg = recent[i]
        if msg.sender_type != SenderType.AGENT or msg.sender_id != agent_id:
            continue
        following = recent[i + 1 : i + 1 + window]
        if is_engaged(msg, following, agent_name):
            break
        if len(following) >= window:
            streak += 1
    return streak


def effective_cooldown(streak: int, policy: PolicyConfig) -> float:
    """20 s, doubling per unengaged agent message, capped at 5 min (Deep Dive §5)."""
    return min(policy.cooldown_s * (2 ** min(streak, 16)), policy.cooldown_max_s)


def participation_share(recent: Sequence[ChatMessage], agent_id: str) -> float:
    text = [m for m in recent if m.is_text]
    if not text:
        return 0.0
    mine = sum(1 for m in text if m.sender_type == SenderType.AGENT and m.sender_id == agent_id)
    return mine / len(text)


def agent_spoke_last(recent: Sequence[ChatMessage]) -> bool:
    text = [m for m in recent if m.is_text]
    return bool(text) and text[-1].sender_type == SenderType.AGENT


def _mentioned_since_last_agent_message(
    recent: Sequence[ChatMessage], agent_id: str, agent_name: str
) -> bool:
    for m in reversed(recent):
        if m.sender_type == SenderType.AGENT and m.sender_id == agent_id:
            return False
        if m.is_human and mentions_agent(m.body, agent_name):
            return True
    return False


# ---- the gate -------------------------------------------------------------------------------


def evaluate_gate(
    snap: GateSnapshot,
    *,
    now: datetime,
    kind: TriggerKind,
    budget_used: int,
    budget_capacity: int,
    disabled: bool,
    policy: PolicyConfig,
) -> GateResult:
    """Stage one of the speak policy. Order matters: inactive states first (no run, no read ack),
    then the rules that apply to both trigger kinds, then the kind-specific rules."""
    if disabled:
        return _skip(SkipReason.DISABLED)
    if snap.agent.status == "removed":
        return _skip(SkipReason.REMOVED)
    if snap.agent.status == "paused":
        return _skip(SkipReason.PAUSED)
    if snap.session.status != "active":
        return _skip(SkipReason.SESSION_CLOSED)
    if is_resting(budget_used, budget_capacity):
        return _skip(SkipReason.RESTING)

    if kind == TriggerKind.NUDGE:
        return _nudge_gate(snap, now=now, policy=policy)

    recent = snap.recent
    humans = [m for m in recent if m.is_human]
    if not humans or humans[-1].seq <= snap.last_evaluated_seq:
        return _skip(SkipReason.ALREADY_EVALUATED)

    # Never two agent messages without a human in between; mentions do not bypass this.
    if agent_spoke_last(recent):
        return _skip(SkipReason.CONSECUTIVE)

    agent = snap.agent
    mentioned = _mentioned_since_last_agent_message(recent, agent.id, agent.name)
    share = participation_share(recent, agent.id)
    n = max(snap.active_participants, 1)
    streak = unengaged_streak(recent, agent.id, agent.name, policy.engagement_window)
    cooldown = effective_cooldown(streak, policy)

    if not mentioned:
        last = snap.last_agent_message
        if last is not None and now - last.created_at < timedelta(seconds=cooldown):
            return _skip(SkipReason.COOLDOWN, share=share, cooldown_s=cooldown)
        if share > policy.share_cap_factor / n:
            return _skip(SkipReason.SHARE_CAP, share=share, cooldown_s=cooldown)

    return GateResult(
        eligible=True,
        encourage=share < policy.share_nudge_factor / n,
        mentioned=mentioned,
        share=share,
        cooldown_s=cooldown,
    )


def _nudge_gate(snap: GateSnapshot, *, now: datetime, policy: PolicyConfig) -> GateResult:
    """Re-engagement nudge conditions (Deep Dive §5, D-013). Cooldown and share do not apply:
    the rule lists its own conditions, and the lull guarantees the cooldown has passed."""
    if not policy.nudge_enabled:
        return _skip(SkipReason.NUDGE_DISABLED)
    last_human = snap.session.last_human_message_at
    if last_human is None:
        return _skip(SkipReason.NO_LULL)
    if now - last_human < timedelta(seconds=policy.nudge_delay_s):
        return _skip(SkipReason.LULL_NOT_OVER)
    if snap.nudge_done_for_lull:
        return _skip(SkipReason.NUDGE_DONE)
    if agent_spoke_last(snap.recent):
        return _skip(SkipReason.AGENT_SPOKE_LAST)
    return GateResult(eligible=True)


# ---- debounce -------------------------------------------------------------------------------


def debounce_due_at(first_at: float, last_at: float, policy: PolicyConfig) -> float:
    """Evaluate after `debounce_quiet_s` of quiet, but never later than `debounce_cap_s` after
    the first trigger of the batch (Deep Dive §5)."""
    return min(last_at + policy.debounce_quiet_s, first_at + policy.debounce_cap_s)
