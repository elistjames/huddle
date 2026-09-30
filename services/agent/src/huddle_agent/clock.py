"""Injectable clock so policy, leases and timers are testable without sleeping."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Wall-clock time (UTC, tz-aware)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Manually advanced clock for tests and evals."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        self._mono = 1_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
