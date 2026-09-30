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


def parse_decision(text: str) -> tuple[bool, str]:
    """Parse the decider's `{speak, reason}`. Anything unparseable means "do not speak"."""
    for candidate in [text.strip(), *_JSON_OBJ.findall(text)]:
        try:
            raw: object = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(raw, dict):
            data = cast(dict[str, Any], raw)
            speak = data.get("speak")
            reason = data.get("reason", "")
            if isinstance(speak, str):
                speak = speak.strip().lower() in {"true", "yes"}
            if isinstance(speak, bool):
                return speak, str(reason)[:200]
    return False, "unparseable decider output"


def parse_safety(text: str) -> bool:
    """Safe only on an explicit 'safe' verdict; anything else fails closed."""
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and words[0] == "safe"
