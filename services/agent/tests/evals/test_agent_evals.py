"""Deep Dive §11 agent evaluations, replayed in CI with the fake model (no Ollama).

Pass criteria from §11: relevance precision >= 0.8; mid-burst interruptions < 5%; participation
share within +-30% of 1/N for N = 2-6; nudge precision >= 0.85 with 0 nudges after closure;
a standard 500-message session within one budget window and never resting.

Both previously failing criteria (N = 2 participation share, and the 500-message cost target)
now pass after the D-021 tuning; they stay as ordinary tests.
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


@pytest.mark.parametrize("seed", [31, 101, 202])
def test_participation_share_all_group_sizes(seed: int) -> None:
    """Every N = 2-6 within +-30% of 1/N, on several seeds so it cannot pass by luck (D-021)."""
    for n, r in asyncio.run(eval_balance(seed=seed)).items():
        assert r["within_30pct"], (n, r)


def test_nudge_judgment() -> None:
    r = asyncio.run(eval_nudges())
    assert r["precision"] >= 0.85, r
    assert r["nudges_after_closure"] == 0, r


def test_cost_report() -> None:
    r = cost()
    assert 495 <= r["total_messages"] <= 505 and r["tokens_used"] > 0


def test_cost_never_resting() -> None:
    r = cost()
    assert r["within_one_window"] and r["resting_evaluations"] == 0, r
    assert 495 <= r["total_messages"] <= 505, r  # "500-message session" means 500 in total
