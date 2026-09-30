"""Structured JSON logs (Deep Dive §9).

Every line carries the required fields `ts, service, trace_id, session_id, actor_id, message_id,
seq, event, latency_ms` (null when not applicable) plus gateway-specific extras. Message bodies are
never passed to the logger, so they cannot reach application logs.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

SERVICE = "gateway"
REQUIRED_FIELDS = ("trace_id", "session_id", "actor_id", "message_id", "seq", "latency_ms")
_FIELDS_ATTR = "huddle_fields"

logger = logging.getLogger("huddle_gateway")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = getattr(record, _FIELDS_ATTR, None) or {}
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "event": fields.get("event") or record.getMessage(),
        }
        for name in REQUIRED_FIELDS:
            out[name] = fields.get(name)
        for name, value in fields.items():
            if name not in out and value is not None:
                out[name] = value
        if record.name != logger.name and "event" not in fields:
            out["logger"] = record.name
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, separators=(",", ":"))


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event. Callers must never pass message bodies."""
    fields["event"] = event
    logger.log(level, event, extra={_FIELDS_ATTR: fields})


# uvicorn hands "uvicorn.error" to the websockets protocol, which traces every frame (bodies
# included) at DEBUG. Those loggers never go below INFO, whatever the root level.
_FRAME_TRACING_LOGGERS = ("uvicorn.error", "websockets")


def quiet_frame_tracing() -> None:
    for name in _FRAME_TRACING_LOGGERS:
        lg = logging.getLogger(name)
        if lg.getEffectiveLevel() < logging.INFO:
            lg.setLevel(logging.INFO)


def configure(level: int = logging.INFO) -> None:
    """Route the gateway's and uvicorn's loggers through one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    quiet_frame_tracing()
