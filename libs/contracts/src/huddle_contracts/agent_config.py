"""Agent policy config keys and defaults stored in `agents.config` (Deep Dive §4, §5).

The chat service seeds these when it creates the agent; the agent service reads them into its
`PolicyConfig`. Keys MUST be the agent's `PolicyConfig` field names: unknown keys are ignored
there, so a renamed key silently stops being tunable. `services/agent/tests` asserts that every
key here is a `PolicyConfig` field with the same default.
"""

from __future__ import annotations

from typing import Any

from huddle_contracts.models import AGENT_MAX_BODY_CHARS

DEFAULT_AGENT_CONFIG: dict[str, Any] = {
    "debounce_quiet_s": 3,
    "debounce_cap_s": 8,
    "cooldown_s": 20,
    "cooldown_max_s": 300,
    "share_window": 30,
    "share_cap_factor": 1.25,
    "share_min_messages": 10,
    "share_nudge_factor": 0.5,
    "max_consecutive_agent_messages": 1,
    "nudge_delay_s": 1200,  # configurable from 600 to 1800 (clamped by the agent)
    "max_chars": AGENT_MAX_BODY_CHARS,
    "decider_timeout_s": 10,
    "writer_timeout_s": 45,
}
