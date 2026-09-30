"""Fake model clients for tests and evals (no Ollama needed).

* `ScriptedModel` returns queued results or raises queued errors, per stage.
* `HeuristicModel` is a deterministic rule-based stand-in for the decider and writer, used by the
  eval harness so the full policy pipeline can be replayed in CI. It is not a quality proxy for
  the real Llama models; it only lets the harness (and the gate around the decider) be measured.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from huddle_agent.clock import FakeClock
from huddle_agent.context import estimate_tokens
from huddle_agent.domain import ChatMessage, TriggerKind
from huddle_agent.models.base import (
    Decision,
    Draft,
    ModelRequest,
    SafetyVerdict,
    Stage,
    SummaryText,
    Usage,
)
from huddle_agent.policy import mentions_agent
from huddle_contracts.models import SenderType

Script = object  # a result object, an Exception instance, or a callable(req) -> either


def _usage(req: ModelRequest, output: str) -> Usage:
    est_in = sum(estimate_tokens(str(m.content)) for m in req.prompt)
    return Usage(req.model, est_in, estimate_tokens(output))


@dataclass
class ScriptedModel:
    """Queue results per stage; unscripted calls get a harmless default."""

    clock: FakeClock | None = None
    latency_s: dict[Stage, float] = field(default_factory=dict[Stage, float])
    available: bool = True
    calls: list[ModelRequest] = field(default_factory=list[ModelRequest])
    queues: dict[Stage, deque[Script]] = field(default_factory=dict[Stage, deque[Script]])

    def script(self, stage: Stage, *items: Script) -> ScriptedModel:
        self.queues.setdefault(stage, deque()).extend(items)
        return self

    def stages(self) -> list[Stage]:
        return [c.stage for c in self.calls]

    def _next(self, req: ModelRequest) -> object | None:
        self.calls.append(req)
        if self.clock is not None:
            self.clock.advance(self.latency_s.get(req.stage, 0.0))
        q = self.queues.get(req.stage)
        if not q:
            return None
        item = q.popleft()
        if callable(item):
            item = item(req)
        if isinstance(item, BaseException):
            raise item
        return item

    async def decide(self, req: ModelRequest) -> Decision:
        item = self._next(req)
        if isinstance(item, Decision):
            return item
        if isinstance(item, bool):
            return Decision(item, "scripted", _usage(req, "{}"))
        return Decision(False, "default", _usage(req, "{}"))

    async def write(self, req: ModelRequest) -> Draft:
        item = self._next(req)
        if isinstance(item, Draft):
            return item
        text = item if isinstance(item, str) else "Here is something worth adding."
        return Draft(text, _usage(req, text))

    async def summarize(self, req: ModelRequest) -> SummaryText:
        item = self._next(req)
        if isinstance(item, SummaryText):
            return item
        text = item if isinstance(item, str) else f"summary of {len(req.messages)} messages"
        return SummaryText(text, _usage(req, text))

    async def safety_check(self, req: ModelRequest) -> SafetyVerdict:
        item = self._next(req)
        if isinstance(item, SafetyVerdict):
            return item
        safe = item if isinstance(item, bool) else True
        return SafetyVerdict(safe, _usage(req, "safe"))

    async def ping(self) -> bool:
        return self.available


# ---- heuristic stand-in for evals ----------------------------------------------------------

_GREETING = re.compile(
    r"^\s*(hi|hey|hello|yo|morning|good (morning|night|evening)|bye|cya|see (you|ya)|night)\b",
    re.IGNORECASE,
)
_ACK = re.compile(
    r"^\s*(ok(ay)?|k|sure|yep|yes|yeah|no|nope|cool|nice|lol|haha|thanks|thank you|thx|"
    r"got it|sounds good|agreed|same|true|right|great|perfect|done)\b[\s.!]*$",
    re.IGNORECASE,
)
_SMALLTALK_Q = re.compile(
    r"\b(how are you|how's it going|what's up|how was your (day|weekend)|you there)\b",
    re.IGNORECASE,
)
_CLOSURE = re.compile(
    r"\b(thanks|thank you|bye|see you|sounds good|that's settled|decided|let's go with|"
    r"we're done|all set|good night|cheers)\b",
    re.IGNORECASE,
)
_OPEN = re.compile(
    r"(\?|\b(should we|we still need|not sure|let's decide|anyone|todo|pending|open question|"
    r"what about|haven't decided|undecided)\b)",
    re.IGNORECASE,
)
_TANGLE = re.compile(r"\b(but|disagree|actually|no,|wait|however|i thought)\b", re.IGNORECASE)


def _is_low_value(body: str) -> bool:
    return bool(_GREETING.search(body) or _ACK.match(body) or _SMALLTALK_Q.search(body))


def _since_agent(messages: Sequence[ChatMessage], agent_id: str) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for m in reversed(messages):
        if m.sender_type == SenderType.AGENT and m.sender_id == agent_id:
            break
        if m.is_human:
            out.append(m)
    out.reverse()
    return out


def heuristic_speak(
    messages: Sequence[ChatMessage],
    *,
    agent_id: str,
    agent_name: str,
    encourage: bool,
) -> tuple[bool, str]:
    new = _since_agent(messages, agent_id)
    if not new:
        return False, "nothing new"
    last = new[-1].body or ""
    if any(mentions_agent(m.body, agent_name) for m in new[-3:]):
        return True, "addressed by name"
    if _is_low_value(last):
        return False, "small talk or acknowledgement"
    if "?" in last:
        return True, "unanswered question"
    if len(new) >= 6 and sum(1 for m in new if _TANGLE.search(m.body or "")) >= 3:
        return True, "summarize a tangle"
    if encourage and len(last.split()) >= 8:
        return True, "quiet for a while; substantive topic"
    return False, "people are handling it"


def heuristic_open(messages: Sequence[ChatMessage]) -> tuple[bool, str]:
    humans = [m for m in messages if m.is_human][-4:]
    if not humans:
        return False, "no conversation"
    if _CLOSURE.search(humans[-1].body or ""):
        return False, "conversation reached closure"
    if any(_OPEN.search(m.body or "") for m in humans[-2:]):
        return True, "open thread"
    return False, "nothing pending"


@dataclass
class HeuristicModel:
    clock: FakeClock | None = None
    latency_s: dict[Stage, float] = field(default_factory=dict[Stage, float])
    speak_filter: Callable[[ModelRequest], bool] | None = None
    calls: list[ModelRequest] = field(default_factory=list[ModelRequest])

    def _tick(self, req: ModelRequest) -> None:
        self.calls.append(req)
        if self.clock is not None:
            self.clock.advance(self.latency_s.get(req.stage, 0.0))

    async def decide(self, req: ModelRequest) -> Decision:
        self._tick(req)
        if req.kind == TriggerKind.NUDGE:
            speak, reason = heuristic_open(req.messages)
        else:
            speak, reason = heuristic_speak(
                req.messages,
                agent_id=req.agent_id,
                agent_name=req.agent_name,
                encourage=bool(req.hints.get("encourage")),
            )
        if speak and self.speak_filter is not None:
            speak = self.speak_filter(req)
        out = json.dumps({"speak": speak, "reason": reason})
        return Decision(speak, reason, _usage(req, out))

    async def write(self, req: ModelRequest) -> Draft:
        self._tick(req)
        if req.kind == TriggerKind.NUDGE:
            text = "Picking this back up: we never settled the open question. Any thoughts?"
        else:
            text = "One angle nobody has raised yet: it might be worth checking the numbers first."
        return Draft(text, _usage(req, text))

    async def summarize(self, req: ModelRequest) -> SummaryText:
        self._tick(req)
        text = f"Earlier: {len(req.messages)} messages about the topic at hand."
        return SummaryText(text, _usage(req, text))

    async def safety_check(self, req: ModelRequest) -> SafetyVerdict:
        self._tick(req)
        return SafetyVerdict("UNSAFE" not in req.draft, _usage(req, "safe"))

    async def ping(self) -> bool:
        return True
