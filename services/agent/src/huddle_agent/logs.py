"""Structured JSON logs (Deep Dive §9).

Required fields: ts, service, trace_id, session_id, actor_id, message_id, seq, event, latency_ms.
Message bodies, prompts and completions are never logged: `log_event` rejects any field whose
name suggests content, so a careless call cannot leak a body.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

SERVICE = "agent"
LOGGER_NAME = "huddle_agent"
REQUIRED_FIELDS = (
    "ts",
    "service",
    "trace_id",
    "session_id",
    "actor_id",
    "message_id",
    "seq",
    "event",
    "latency_ms",
)
FORBIDDEN_FIELDS = frozenset({"body", "text", "content", "prompt", "completion", "summary"})

logger = logging.getLogger(LOGGER_NAME)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = getattr(record, "huddle", None) or {"event": record.getMessage()}
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "service": SERVICE,
            "level": record.levelname.lower(),
        }
        for name in REQUIRED_FIELDS[2:]:
            doc[name] = fields.get(name)
        for key, value in fields.items():
            if key not in doc:
                doc[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc_type"] = record.exc_info[0].__name__
        return json.dumps(doc, default=str, separators=(",", ":"))


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    bad = FORBIDDEN_FIELDS.intersection(fields)
    if bad:
        raise ValueError(f"refusing to log content fields: {sorted(bad)}")
    logger.log(level, event, extra={"huddle": {"event": event, **fields}})
