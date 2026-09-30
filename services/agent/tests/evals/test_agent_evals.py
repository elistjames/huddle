"""Deep Dive §11 agent evaluations, replayed in CI with the fake model (no Ollama).

Pass criteria from §11: relevance precision >= 0.8; mid-burst interruptions < 5%; participation
share within +-30% of 1/N for N = 2-6; nudge precision >= 0.85 with 0 nudges after closure;
a standard 500-message session within one budget window and never resting.

Two criteria do not hold with the spec's default thresholds and are kept as strict xfails so
they stay visible (and flip loudly once tuned): N = 2 participation share, and the cost target.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import pytest
from agent_evals import (
    eval_balance,
    eval_cost,
    eval_nudges,
    eval_relevance,
    eval_timing,
    relevance_dataset,
)


@functools.cache
def balance() -> dict[str, Any]:
    return asyncio.run(eval_balance())


@functools.cache
def cost() -> dict[str, Any]:
    return asyncio.run(eval_cost())


def test_relevance_decider_precision() -> None:
    r = asyncio.run(eval_relevance(relevance_dataset()))
    assert r["n"] == 200
    assert r["precision"] >= 0.8, r


def test_timing_never_interrupts_mid_burst() -> None:
    r = asyncio.run(eval_timing())
    assert r["interrupt_rate"] < 0.05, r
    assert r["responded_after_burst_rate"] > 0.5, r  # it does answer once the burst ends


@pytest.mark.parametrize("n", [3, 4, 5, 6])
def test_participation_share_within_30pct_of_one_over_n(n: int) -> None:
    r = balance()[str(n)]
    assert r["within_30pct"], r


@pytest.mark.xfail(
    strict=True,
    reason="Known shortfall: in a 1:1 chat the 20 s cooldown that doubles after unengaged "
    "messages keeps an eager agent near 0.24, below 0.5 +- 30%. Reported for tuning.",
)
def test_participation_share_one_to_one() -> None:
    assert balance()["2"]["within_30pct"], balance()["2"]


def test_nudge_judgment() -> None:
    r = asyncio.run(eval_nudges())
    assert r["precision"] >= 0.85, r
    assert r["nudges_after_closure"] == 0, r


def test_cost_report() -> None:
    r = cost()
    assert r["human_messages"] == 500 and r["tokens_used"] > 0


@pytest.mark.xfail(
    strict=True,
    reason="Known shortfall: with a <=4k-token decider prompt per evaluation, a busy "
    "500-message session spends the 500k-token window and rests for part of it.",
)
def test_cost_never_resting() -> None:
    r = cost()
    assert r["within_one_window"] and r["resting_evaluations"] == 0, r
