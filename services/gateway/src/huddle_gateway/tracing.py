"""W3C `traceparent` handling (Deep Dive §9: one trace per message send)."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


@dataclass(frozen=True, slots=True)
class Trace:
    trace_id: str  # 32 lowercase hex chars, shared by every span of the trace
    span_id: str  # 16 lowercase hex chars, this gateway's span
    flags: str = "01"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"


def _new_span_id() -> str:
    span = secrets.token_hex(8)
    return span if span != _ZERO_SPAN else _new_span_id()


def start_span(incoming: str | None = None) -> Trace:
    """Continue the client's trace if it sent a valid traceparent, else start a new trace."""
    if incoming:
        m = _TRACEPARENT.match(incoming.strip().lower())
        if m and m.group(1) != _ZERO_TRACE and m.group(2) != _ZERO_SPAN:
            return Trace(trace_id=m.group(1), span_id=_new_span_id(), flags=m.group(3))
    trace_id = secrets.token_hex(16)
    while trace_id == _ZERO_TRACE:  # pragma: no cover - 2^-128
        trace_id = secrets.token_hex(16)
    return Trace(trace_id=trace_id, span_id=_new_span_id())
