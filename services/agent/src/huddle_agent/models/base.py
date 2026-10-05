"""Model runtime interface (Deep Dive §5 "Model selection and fallback policy").

The orchestrator only talks to a `ModelClient`. Production uses `OllamaModelClient`; tests and
evals use the fakes in `huddle_agent.models.fake`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage

from huddle_agent.domain import ChatMessage, TriggerKind


class Stage(StrEnum):
    DECIDE = "decide"
    WRITE = "write"
    SUMMARIZE = "summarize"
    SAFETY = "safety"


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelRequest:
    """One model call. `prompt` is all a real model sees; the structured fields are carried for
    observability and so fakes can script behaviour without parsing prompts."""

    stage: Stage
    prompt: Sequence[BaseMessage]
    model: str
    timeout_s: float
    max_output_tokens: int
    kind: TriggerKind = TriggerKind.MESSAGE
    agent_name: str = "Huddle AI"
    agent_id: str = ""
    messages: Sequence[ChatMessage] = ()
    hints: Mapping[str, Any] = field(default_factory=dict[str, Any])
    draft: str = ""  # safety stage: the text being checked


@dataclass(frozen=True)
class Decision:
    speak: bool
    reason: str
    usage: Usage
    raw: str = ""  # the model's unparsed answer, kept for evals and debugging


@dataclass(frozen=True)
class Draft:
    text: str
    usage: Usage


@dataclass(frozen=True)
class SummaryText:
    text: str
    usage: Usage


@dataclass(frozen=True)
class SafetyVerdict:
    safe: bool
    usage: Usage


class ModelError(Exception):
    """A failed model call. `transient` errors are retried once with the fallback model."""

    code = "model_error"

    def __init__(self, message: str = "", *, transient: bool = True) -> None:
        super().__init__(message or self.code)
        self.transient = transient


class ModelUnavailable(ModelError):
    """The model runtime is unreachable (or the model is not pulled): the agent shows `away`."""

    code = "model_unavailable"


class ModelTimeout(ModelError):
    code = "model_timeout"


class ModelClient(Protocol):
    async def decide(self, req: ModelRequest) -> Decision: ...

    async def write(self, req: ModelRequest) -> Draft: ...

    async def summarize(self, req: ModelRequest) -> SummaryText: ...

    async def safety_check(self, req: ModelRequest) -> SafetyVerdict: ...

    async def ping(self) -> bool:
        """True when the runtime is reachable (used to leave the `away` state)."""
        ...


_JSON_OBJ = re.compile(r"\{.*?\}", re.DOTALL)
_SPEAK_FIELD = re.compile(r"""["']?speak["']?\s*[:=]\s*["']?(true|false|yes|no)\b""", re.IGNORECASE)
_REASON_FIELD = re.compile(r'"reason"\s*:\s*"([^"]*)', re.IGNORECASE)

# What the decider must return. Passed to Ollama as the structured-output schema so a small model
# cannot wander off the shape.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"speak": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["speak", "reason"],
}

# Asked a yes/no question, a 3B model often commits to the boolean first and then writes a
# reason that contradicts it ("speak": true, "reason": "already answered"). So for ordinary
# messages the decider picks ONE label from a closed list and the code turns the label into a
# decision (D-024). The labels are the spec's speak policy (Deep Dive §5) made explicit.
SPEAK_SITUATIONS: tuple[str, ...] = (
    "unanswered_question",
    "wants_help",
    "stuck",
    "looks_wrong",
    "tangle",
)
QUIET_SITUATIONS: tuple[str, ...] = (
    "already_answered",
    "plans_or_status",
    "small_talk",
    "finished",
    "nothing_to_add",
)
SITUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "situation": {"type": "string", "enum": [*SPEAK_SITUATIONS, *QUIET_SITUATIONS]},
        "reason": {"type": "string"},
    },
    "required": ["situation", "reason"],
}
_SITUATION_FIELD = re.compile(r'"situation"\s*:\s*"([a-z_]+)', re.IGNORECASE)


def parse_decision(text: str) -> tuple[bool, str]:
    """Read the decider's answer: `{situation, reason}` (messages) or `{speak, reason}` (the
    re-engagement nudge). Anything unreadable means "do not speak".

    A small model's answer is often cut off at the output cap or wrapped in prose, so after a
    strict JSON parse this falls back to reading the field directly. In manual testing 8 of 35
    cases scored as "stay quiet" only because the answer was not valid JSON."""
    for candidate in [text.strip(), *_JSON_OBJ.findall(text)]:
        try:
            raw: object = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(raw, dict):
            data = cast(dict[str, Any], raw)
            reason = str(data.get("reason", ""))
            situation = data.get("situation")
            if isinstance(situation, str) and situation in (*SPEAK_SITUATIONS, *QUIET_SITUATIONS):
                return situation in SPEAK_SITUATIONS, f"{situation}: {reason}"[:200]
            speak = data.get("speak")
            if isinstance(speak, str):
                speak = speak.strip().lower() in {"true", "yes"}
            if isinstance(speak, bool):
                return speak, reason[:200]
    reason_match = _REASON_FIELD.search(text)
    reason = reason_match.group(1) if reason_match else "(answer was cut off)"
    label = _SITUATION_FIELD.search(text)
    if label is not None and label.group(1).lower() in (*SPEAK_SITUATIONS, *QUIET_SITUATIONS):
        name = label.group(1).lower()
        return name in SPEAK_SITUATIONS, f"{name}: {reason}"[:200]
    found = _SPEAK_FIELD.search(text)
    if found is not None:
        return found.group(1).lower() in {"true", "yes"}, reason[:200]
    return False, "unparseable decider output"


def parse_safety(text: str) -> bool:
    """Safe only on an explicit 'safe' verdict; anything else fails closed."""
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and words[0] == "safe"
