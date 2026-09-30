"""Observability: structured JSON logs and W3C traceparent propagation (Deep Dive §9).

Every log line carries the §9 fields: ts, service, trace_id, session_id, actor_id, message_id,
seq, event, latency_ms. Message bodies are never logged: `log_event` has no body parameter and
drops any field whose name suggests message content.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SERVICE = "chat"
LOGGER_NAME = "huddle_chat"
_FIELDS = ("session_id", "actor_id", "message_id", "seq", "latency_ms")
_FORBIDDEN = frozenset({"body", "text", "content", "prompt", "completion"})
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str  # 32 hex chars
    span_id: str  # 16 hex chars
    flags: str = "01"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    def child(self) -> TraceContext:
        return TraceContext(self.trace_id, secrets.token_hex(8), self.flags)

    @classmethod
    def new(cls) -> TraceContext:
        return cls(secrets.token_hex(16), secrets.token_hex(8))

    @classmethod
    def parse(cls, value: str | None) -> TraceContext | None:
        if not value:
            return None
        m = _TRACEPARENT.match(value.strip().lower())
        if not m or m.group(1) == "ff":
            return None
        trace_id, span_id = m.group(2), m.group(3)
        if trace_id == "0" * 32 or span_id == "0" * 16:
            return None
        return cls(trace_id, span_id, m.group(4))

    @classmethod
    def continue_from(cls, value: str | None) -> TraceContext:
        """A child span of an incoming traceparent, or a fresh trace if it is missing/invalid."""
        parent = cls.parse(value)
        return parent.child() if parent else cls.new()


current_trace: ContextVar[TraceContext | None] = ContextVar("huddle_chat_trace", default=None)


def trace() -> TraceContext:
    ctx = current_trace.get()
    if ctx is None:
        ctx = TraceContext.new()
        current_trace.set(ctx)
    return ctx


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = current_trace.get()
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "trace_id": ctx.trace_id if ctx else None,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        for name in _FIELDS:
            data[name] = getattr(record, name, None)
        extra: dict[str, Any] = getattr(record, "fields", None) or {}
        for key, value in extra.items():
            if key not in data and key not in _FORBIDDEN:
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # our middleware logs requests


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    session_id: str | None = None,
    actor_id: str | None = None,
    message_id: str | None = None,
    seq: int | None = None,
    latency_ms: float | None = None,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    for name in _FORBIDDEN:
        fields.pop(name, None)
    logging.getLogger(LOGGER_NAME).log(
        level,
        event,
        exc_info=exc_info,
        extra={
            "event": event,
            "session_id": session_id,
            "actor_id": actor_id,
            "message_id": message_id,
            "seq": seq,
            "latency_ms": None if latency_ms is None else round(latency_ms, 2),
            "fields": fields,
        },
    )
