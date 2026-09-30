"""The real-model decider eval (huddle_agent.evals) is itself tested here with a scripted model:
the harness must run, and every case the deterministic gate decides must match its label, so a
wrong label or a gate regression shows up in CI without Ollama."""

from __future__ import annotations

import pytest

from huddle_agent.evals.cases import AGENT, CASES, Case
from huddle_agent.evals.decider import parse_args, reply_problems, run_trial
from huddle_agent.models.fake import HeuristicModel

GATE_DECIDED = {"addressed", "answered", "trivial", "not-addressed"}


def test_cases_are_well_formed_and_unique() -> None:
    assert len({c.id for c in CASES}) == len(CASES)
    for c in CASES:
        assert c.turns, c.id
        assert c.turns[-1][0] != AGENT, f"{c.id}: the last turn must be a person"
        assert any(s != AGENT for s, _ in c.turns), c.id
    assert sum(c.expect_speak for c in CASES) >= 10
    assert sum(not c.expect_speak for c in CASES) >= 10


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.category in GATE_DECIDED], ids=lambda c: c.id
)
async def test_gate_decided_cases_match_their_labels(case: Case) -> None:
    args = parse_args([])
    args.url = "http://unused"
    trial, _ = await run_trial(case, HeuristicModel(), args)
    assert trial.spoke is case.expect_speak, (trial.reason, trial.status)
    if case.category in {"addressed", "answered"}:
        assert trial.decided_by == "gate"  # no model call: the gate treats it as a turn


async def test_every_case_runs_to_completion_with_a_scripted_model() -> None:
    args = parse_args([])
    args.url = "http://unused"
    for case in CASES:
        trial, rec = await run_trial(case, HeuristicModel(), args)
        assert trial.status != "None", case.id
        assert trial.error is None, (case.id, trial.error)
        assert len(rec.decisions) <= 1


def test_reply_problems_flags_template_leaks_and_long_replies() -> None:
    assert reply_problems("assistant\n\nHi") == ["starts with a role or name header"]
    assert reply_problems("Sounds good, let's pick Friday.") == []
    assert "empty" in reply_problems("   ")
    assert any("chars" in p for p in reply_problems("x" * 700))
    assert any("as an ai" in p for p in reply_problems("As an AI, I think so"))
