"""Fixed-window token budget math (D-019). Pure functions; no I/O.

Windows are fixed and aligned to the UTC epoch: window k covers
[k*window_seconds, (k+1)*window_seconds). At each boundary usage resets to zero. There is no
continuous or sliding refill.
"""

from __future__ import annotations

from datetime import UTC, datetime

DEFAULT_CAPACITY_TOKENS = 500_000
DEFAULT_WINDOW_SECONDS = 86_400
# One full turn = decider (4k in + 50 out) + writer (12k in + 300 out)
FULL_TURN_TOKENS = 4_000 + 50 + 12_000 + 300


def window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=UTC)


def window_end(now: datetime, window_seconds: int) -> datetime:
    start = window_start(now, window_seconds)
    return datetime.fromtimestamp(int(start.timestamp()) + window_seconds, tz=UTC)


def roll(
    now: datetime, window_started_at: datetime, used_tokens: int, window_seconds: int
) -> tuple[datetime, int]:
    """Return (window_started_at, used_tokens) after applying any boundary reset."""
    current = window_start(now, window_seconds)
    if current > window_started_at:
        return current, 0
    return window_started_at, used_tokens


def used_pct(used_tokens: int, capacity_tokens: int) -> int:
    if capacity_tokens <= 0:
        return 100
    return min(100, max(0, (used_tokens * 100 + capacity_tokens - 1) // capacity_tokens))


def is_resting(used_tokens: int, capacity_tokens: int) -> bool:
    """Resting when fewer than one full turn is left in the current window."""
    return capacity_tokens - used_tokens < FULL_TURN_TOKENS
