"""Service settings (env) and per-agent policy thresholds (`agents.config`, Deep Dive §4/§5).

Every value marked *(tunable)* in Deep Dive §5 lives in `PolicyConfig` so it can be adjusted per
agent from `agents.config` without a deploy (Deep Dive §12 feature flags).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any

from huddle_contracts import redis_keys

NUDGE_DELAY_MIN_S = 10 * 60
NUDGE_DELAY_MAX_S = 30 * 60


@dataclass(frozen=True)
class PolicyConfig:
    """Speak-policy thresholds (Deep Dive §5). Defaults are the spec's starting values."""

    debounce_quiet_s: float = 3.0
    debounce_cap_s: float = 8.0
    cooldown_s: float = 20.0
    cooldown_max_s: float = 300.0
    # A trigger skipped only because of the cooldown is re-evaluated once when the cooldown
    # ends, instead of being dropped until the next human message (spec is silent; see report).
    defer_after_cooldown: bool = True
    engagement_window: int = 3  # an agent message is unengaged if the next 3 messages ignore it
    share_window: int = 30
    share_cap_factor: float = 1.25
    share_nudge_factor: float = 0.5
    max_consecutive_agent_messages: int = 1
    nudge_delay_s: float = 20 * 60
    nudge_enabled: bool = True
    target_chars: int = 600
    max_chars: int = 1000
    decider_timeout_s: float = 10.0
    writer_timeout_s: float = 45.0
    fold_threshold_tokens: int = 12_000
    fold_fraction: float = 0.8
    summary_max_tokens: int = 1_000
    decider_max_input_tokens: int = 700
    writer_max_input_tokens: int = 2_500
    decider_max_output_tokens: int = 50
    writer_max_output_tokens: int = 300

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> PolicyConfig:
        """Build from `agents.config`; unknown keys and badly typed values are ignored."""
        base = cls()
        if not raw:
            return base
        updates: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            default = getattr(base, f.name)
            if isinstance(default, bool):
                if isinstance(value, bool):
                    updates[f.name] = value
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool):
                    updates[f.name] = value
            elif isinstance(value, int | float) and not isinstance(value, bool):
                updates[f.name] = float(value)
        cfg = replace(base, **updates)
        # nudge_delay is configurable from 10 to 30 min (Deep Dive §5).
        clamped = min(max(cfg.nudge_delay_s, NUDGE_DELAY_MIN_S), NUDGE_DELAY_MAX_S)
        # Never two agent messages in a row (Deep Dive §5): a config value cannot loosen it.
        return replace(cfg, nudge_delay_s=clamped, max_consecutive_agent_messages=1)


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str = "postgresql://postgres:postgres@localhost:5432/postgres"
    redis_url: str = "redis://localhost:6379/0"
    service_token: str = ""
    chat_url: str = "http://localhost:8001"
    ollama_url: str = "http://localhost:11434"
    decider_model: str = "llama3.2:3b"
    writer_model: str = "llama3.1:8b"
    session_id: str = "ses_main"
    worker_id: str = field(default_factory=lambda: f"agent-{socket.gethostname()}-{os.getpid()}")
    agents_disabled: bool = False  # global kill switch (Deep Dive §12 runbook)
    health_host: str = "127.0.0.1"
    health_port: int = 8002
    lease_s: float = 30.0
    reclaim_idle_s: float = 60.0
    stale_trigger_s: float = 30.0
    busy_retry_s: float = 1.0
    timer_poll_s: float = 1.0
    reconcile_s: float = 30.0
    stream_block_ms: int = 1000
    triggers_stream: str = redis_keys.AGENT_TRIGGERS_STREAM
    triggers_group: str = redis_keys.AGENT_TRIGGERS_GROUP
    timers_zset: str = redis_keys.AGENT_TIMERS_ZSET

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            postgres_dsn=e.get("HUDDLE_POSTGRES_DSN", d.postgres_dsn),
            redis_url=e.get("HUDDLE_REDIS_URL", d.redis_url),
            service_token=e.get("HUDDLE_SERVICE_TOKEN", d.service_token),
            chat_url=e.get("HUDDLE_CHAT_URL", d.chat_url),
            ollama_url=e.get("HUDDLE_OLLAMA_URL", d.ollama_url),
            decider_model=e.get("HUDDLE_DECIDER_MODEL", d.decider_model),
            writer_model=e.get("HUDDLE_WRITER_MODEL", d.writer_model),
            session_id=e.get("HUDDLE_SESSION_ID", d.session_id),
            worker_id=e.get("HUDDLE_AGENT_WORKER_ID", d.worker_id),
            agents_disabled=_env_bool(e, "HUDDLE_AGENTS_DISABLED"),
            health_host=e.get("HUDDLE_AGENT_HEALTH_HOST", d.health_host),
            health_port=int(e.get("HUDDLE_AGENT_HEALTH_PORT", str(d.health_port))),
            reconcile_s=_env_float(e, "HUDDLE_AGENT_RECONCILE_S", d.reconcile_s),
        )
