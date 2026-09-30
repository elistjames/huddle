"""Deterministic gate (Deep Dive §5, §11 "every gate rule, including the nudge conditions and
the budget threshold")."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from agent_testkit import AGENT, T0, msg, snapshot

from huddle_agent.config import PolicyConfig
from huddle_agent.domain import ChatMessage, TriggerKind
from huddle_agent.policy import (
    GateResult,
    SkipReason,
    debounce_due_at,
    effective_cooldown,
    evaluate_gate,
    is_engaged,
    mentions_agent,
    participation_share,
    unengaged_streak,
)
from huddle_contracts.budget import FULL_TURN_TOKENS
from huddle_contracts.models import AgentState

P = PolicyConfig()
CAP = 500_000


def gate(
    recent: list[ChatMessage],
    *,
    now: datetime | None = None,
    kind: TriggerKind = TriggerKind.MESSAGE,
    used: int = 0,
    disabled: bool = False,
    policy: PolicyConfig = P,
    **snap_kw: object,
) -> GateResult:
    snap = snapshot(recent, **snap_kw)  # type: ignore[arg-type]
    return evaluate_gate(
        snap,
        now=now or (recent[-1].created_at + timedelta(seconds=5) if recent else T0),
        kind=kind,
        budget_used=used,
        budget_capacity=CAP,
        disabled=disabled,
        policy=policy,
    )


def pad(k: int = 12) -> list[ChatMessage]:
    """Old human history (negative seqs, an hour earlier) so one agent message is a small share
    of the window: the gate now checks the share *after* speaking (D-021)."""
    return [
        msg(-k + i, "usr_alice" if i % 2 else "usr_bob", "x", at=T0 - timedelta(hours=1))
        for i in range(k)
    ]


def humans(
    start: int, count: int, who: str = "usr_alice", body: str = "we should ship it"
) -> list[ChatMessage]:
    return [msg(start + i, who if i % 2 == 0 else "usr_bob", body) for i in range(count)]


# ---- inactive states and the budget threshold -----------------------------------------------


def test_kill_switch_skips_and_shows_away() -> None:
    r = gate([msg(1, "usr_alice")], disabled=True)
    assert r.skip_reason == SkipReason.DISABLED
    assert r.inactive_state == AgentState.AWAY  # users cannot resume an operator kill switch


@pytest.mark.parametrize(
    ("status", "reason"), [("paused", SkipReason.PAUSED), ("removed", SkipReason.REMOVED)]
)
def test_paused_or_removed_agent_never_runs(status: str, reason: SkipReason) -> None:
    r = gate(
        [msg(1, "usr_alice", "Huddle AI, what do you think?")], agent=replace(AGENT, status=status)
    )
    assert r.skip_reason == reason
    assert r.inactive_state == AgentState.PAUSED


def test_closed_session_skips() -> None:
    assert gate([msg(1, "usr_alice")], status="closed").skip_reason == SkipReason.SESSION_CLOSED


def test_budget_threshold_is_one_full_turn() -> None:
    recent = [msg(1, "usr_alice", "what is the deadline?")]
    short = gate(recent, used=CAP - FULL_TURN_TOKENS + 1)
    assert short.skip_reason == SkipReason.RESTING
    assert short.inactive_state == AgentState.RESTING
    assert gate(recent, used=CAP - FULL_TURN_TOKENS).eligible


def test_budget_blocks_even_a_mention() -> None:
    r = gate([msg(1, "usr_alice", "Huddle AI?")], used=CAP)
    assert r.skip_reason == SkipReason.RESTING


# ---- message-trigger rules ------------------------------------------------------------------


def test_eligible_on_fresh_human_message() -> None:
    r = gate([msg(1, "usr_alice", "what is the deadline?")])
    assert r.eligible and r.skip_reason is None


def test_already_evaluated_dedupes_redeliveries() -> None:
    recent = [msg(1, "usr_alice"), msg(2, "usr_bob")]
    assert gate(recent, last_evaluated_seq=2).skip_reason == SkipReason.ALREADY_EVALUATED
    assert gate(recent, last_evaluated_seq=1).eligible


def test_never_two_agent_messages_in_a_row() -> None:
    recent = [msg(1, "usr_alice"), msg(2, "agent", "my point")]
    assert gate(recent).skip_reason == SkipReason.CONSECUTIVE
    # A system line in between does not count as a human message.
    recent = [msg(1, "usr_alice"), msg(2, "agent"), msg(3, "system", "Bob joined")]
    assert gate(recent).skip_reason == SkipReason.CONSECUTIVE


def test_consecutive_cap_is_configurable() -> None:
    old = T0 - timedelta(hours=1)
    recent = [msg(1, "usr_alice", "Huddle AI, what now?", at=old), msg(2, "agent", "x", at=old)]
    p2 = PolicyConfig(max_consecutive_agent_messages=2)  # from_json clamps this to 1
    assert gate(recent, policy=p2, now=T0, n=1).eligible  # n=1: keep the share cap out of it
    recent.append(msg(3, "agent", "y", at=old))
    assert gate(recent, policy=p2, now=T0, n=1).skip_reason == SkipReason.CONSECUTIVE


def test_mention_does_not_bypass_consecutive_cap() -> None:
    # The human mention came before the agent's own last message.
    recent = [msg(1, "usr_alice", "Huddle AI, thoughts?"), msg(2, "agent", "Sure: ...")]
    assert gate(recent).skip_reason == SkipReason.CONSECUTIVE


def test_cooldown_20s_since_last_agent_message() -> None:
    at = T0
    recent = [
        *pad(),
        msg(1, "usr_alice", at=at),
        msg(2, "agent", "a point", at=at + timedelta(seconds=1)),
        msg(3, "usr_bob", "ok then", at=at + timedelta(seconds=5)),
    ]
    blocked = gate(recent, now=at + timedelta(seconds=20))
    assert blocked.skip_reason == SkipReason.COOLDOWN
    assert blocked.cooldown_s == 20
    assert gate(recent, now=at + timedelta(seconds=21, milliseconds=1)).eligible


def test_name_mention_bypasses_cooldown() -> None:
    recent = [
        msg(1, "usr_alice", at=T0),
        msg(2, "agent", "a point", at=T0 + timedelta(seconds=1)),
        msg(3, "usr_bob", "huddle ai can you expand?", at=T0 + timedelta(seconds=3)),
    ]
    r = gate(recent, now=T0 + timedelta(seconds=5))
    assert r.eligible and r.mentioned


def _unengaged_block(start: int, at: datetime) -> list[ChatMessage]:
    """An agent message followed by three human messages that ignore it."""
    return [
        msg(start, "agent", "consider the latency budget carefully", at=at),
        msg(start + 1, "usr_alice", "lunch at noon?", at=at + timedelta(seconds=1)),
        msg(start + 2, "usr_bob", "sure pizza", at=at + timedelta(seconds=2)),
        msg(start + 3, "usr_alice", "cool", at=at + timedelta(seconds=3)),
    ]


def test_cooldown_doubles_after_unengaged_agent_messages_up_to_5_min() -> None:
    # n=6: the agent's share (1/5 = 0.2) is above its fair share 1/6, so the doubling applies.
    recent = [msg(0, "usr_alice", at=T0), *_unengaged_block(1, T0)]
    r = gate(recent, n=6, now=T0 + timedelta(seconds=30))
    assert r.skip_reason == SkipReason.COOLDOWN and r.cooldown_s == 40
    assert gate(recent, n=6, now=T0 + timedelta(seconds=41)).skip_reason != SkipReason.COOLDOWN

    recent2 = [
        msg(0, "usr_alice", at=T0),
        *_unengaged_block(1, T0),
        *_unengaged_block(5, T0 + timedelta(seconds=100)),
    ]
    r2 = gate(recent2, n=6, now=T0 + timedelta(seconds=150))
    assert r2.cooldown_s == 80
    assert effective_cooldown(10, P) == 300


def test_engagement_resets_the_doubling() -> None:
    at = T0
    recent = [
        msg(0, "usr_alice", at=at),
        msg(1, "agent", "consider the latency budget carefully", at=at),
        msg(2, "usr_bob", "good call on the latency budget", at=at + timedelta(seconds=2)),
        msg(3, "usr_alice", "yes", at=at + timedelta(seconds=3)),
        msg(4, "usr_bob", "ok", at=at + timedelta(seconds=4)),
    ]
    assert gate(recent, now=at + timedelta(seconds=25)).cooldown_s == 20


def test_pending_agent_message_neither_counts_nor_breaks_streak() -> None:
    # Only two messages after the latest agent message: still pending.
    recent = [
        msg(0, "usr_alice", at=T0),
        *_unengaged_block(1, T0),
        msg(5, "agent", "another angle on scheduling", at=T0 + timedelta(seconds=10)),
        msg(6, "usr_bob", "lol", at=T0 + timedelta(seconds=11)),
    ]
    assert unengaged_streak(recent, AGENT.id, AGENT.name, 3) == 1


def test_participation_share_cap_1_25_over_n() -> None:
    # N=3 -> cap = 0.4167. 30 messages, 13 agent messages (0.433) spread out, all old.
    old = T0 - timedelta(hours=1)
    recent: list[ChatMessage] = []
    seq = 0
    for i in range(30):
        seq += 1
        who = "agent" if i % 2 == 1 and i < 26 else ("usr_alice" if i % 4 == 0 else "usr_bob")
        recent.append(msg(seq, who, "topic words here", at=old + timedelta(seconds=seq)))
    recent[-1] = msg(30, "usr_alice", "new question?", at=T0)
    share = participation_share(recent, AGENT.id)
    assert share == pytest.approx(13 / 30)
    r = gate(recent, now=T0 + timedelta(seconds=5), n=3)
    assert r.skip_reason == SkipReason.SHARE_CAP
    # Four participants: cap = 0.3125, still blocked; two participants: cap 0.625, allowed.
    assert gate(recent, now=T0 + timedelta(seconds=5), n=4).skip_reason == SkipReason.SHARE_CAP
    assert gate(recent, now=T0 + timedelta(seconds=5), n=2).eligible


def test_share_is_checked_after_speaking() -> None:
    # N=4 -> cap 1.25/4 = 0.3125. 4 agent messages in 15 -> speaking again makes 5/16 = 0.3125.
    old = T0 - timedelta(hours=1)
    agent_at = {2, 5, 8, 11}
    recent = [
        msg(s, "agent" if s in agent_at else "usr_alice", "x", at=old + timedelta(seconds=s))
        for s in range(1, 16)
    ]
    recent.append(msg(16, "usr_bob", "y", at=T0))
    assert participation_share(recent, AGENT.id) == pytest.approx(4 / 16)
    assert gate(recent, n=4).eligible  # one more message would land exactly on the cap
    one_more = [*recent[:-2], msg(15, "agent", "x", at=old), msg(16, "usr_bob", "y", at=T0)]
    assert gate(one_more, n=4).skip_reason == SkipReason.SHARE_CAP  # 6/17 > cap


def test_agent_that_has_not_spoken_is_never_over_its_share() -> None:
    assert gate([msg(1, "usr_alice", "what is the plan?")], n=10).eligible


def test_mention_bypasses_share_cap() -> None:
    old = T0 - timedelta(hours=1)
    recent = [
        msg(i + 1, "agent" if i % 2 else "usr_alice", "x", at=old + timedelta(seconds=i))
        for i in range(29)
    ]
    recent.append(msg(30, "usr_bob", "@huddleai what do you think?", at=T0))
    r = gate(recent, n=5)
    assert r.eligible and r.mentioned


def test_encourage_below_half_of_one_over_n() -> None:
    recent = [msg(i + 1, "usr_alice" if i % 2 else "usr_bob", "x") for i in range(10)]
    r = gate(recent, n=3)
    assert r.eligible and r.encourage and r.share == 0
    with_some = [
        *recent[:-1],
        msg(10, "agent", "x", at=T0 - timedelta(hours=1)),
        msg(11, "usr_bob"),
    ]
    # 1/11 = 0.09 < 0.5/3 = 0.167 -> still encouraged; with N=10 it is not (0.05).
    assert gate(with_some, n=3).encourage
    assert not gate(with_some, n=10).encourage


def test_n_counts_agents_too() -> None:
    # 2 humans + 1 agent: N=3 (the snapshot counts active memberships including the agent).
    recent = [msg(1, "usr_alice"), msg(2, "agent", at=T0 - timedelta(hours=1)), msg(3, "usr_bob")]
    recent = sorted(recent, key=lambda m: m.seq)
    r = gate([*pad(), *recent], n=3, now=T0 + timedelta(hours=1))
    assert r.share == pytest.approx(1 / 15)
    assert r.eligible  # (1+1)/16 <= 1.25/3


def test_one_human_with_the_agent_bypasses_the_cooldown() -> None:
    # N=2: everything the human says is addressed to the agent (D-021).
    recent = [
        *pad(),
        msg(1, "usr_alice", at=T0),
        msg(2, "agent", "a point", at=T0 + timedelta(seconds=1)),
        msg(3, "usr_alice", "and the deadline?", at=T0 + timedelta(seconds=3)),
    ]
    assert gate(recent, n=2, now=T0 + timedelta(seconds=6)).eligible
    assert gate(recent, n=3, now=T0 + timedelta(seconds=6)).skip_reason == SkipReason.COOLDOWN


def test_reply_engaging_the_agents_last_message_bypasses_the_cooldown() -> None:
    recent = [
        *pad(),
        msg(1, "usr_alice", at=T0),
        msg(2, "agent", "consider the latency budget carefully", at=T0 + timedelta(seconds=1)),
        msg(3, "usr_bob", "good call on the latency budget", at=T0 + timedelta(seconds=3)),
    ]
    assert gate(recent, n=3, now=T0 + timedelta(seconds=6)).eligible


def test_below_fair_share_cooldown_does_not_double() -> None:
    recent = [*pad(), msg(0, "usr_alice", at=T0), *_unengaged_block(1, T0)]
    assert gate(recent, n=3, now=T0 + timedelta(seconds=30)).cooldown_s == 20  # share < 1/3
    busy = [
        *[msg(-40 + i, "agent", "x", at=T0 - timedelta(hours=1)) for i in range(0, 40, 4)],
        *recent,
    ]
    assert gate(busy, n=3, now=T0 + timedelta(seconds=30)).cooldown_s > 20


def test_trivial_batches_skip_the_decider() -> None:
    for body in ["thanks", "agreed!", "hey everyone", "sounds good, thanks", "ok"]:
        assert gate([msg(1, "usr_alice", body)], n=3).skip_reason == SkipReason.TRIVIAL, body
    for body in ["thanks, but what about the budget?", "hey, can you look at the plan"]:
        assert gate([msg(1, "usr_alice", body)], n=3).eligible, body
    # A mention is never trivial.
    assert gate([msg(1, "usr_alice", "thanks Huddle AI")], n=3).eligible


# ---- nudge conditions -----------------------------------------------------------------------

LULL = timedelta(minutes=20)


def nudge(recent: list[ChatMessage], after: timedelta = LULL, **kw: object) -> GateResult:
    last_human = max(m.created_at for m in recent if m.is_human)
    return gate(recent, now=last_human + after, kind=TriggerKind.NUDGE, **kw)  # type: ignore[arg-type]


def test_nudge_eligible_after_lull() -> None:
    assert nudge([msg(1, "usr_alice", "should we pick a date?")]).eligible


def test_nudge_requires_the_full_lull() -> None:
    r = nudge([msg(1, "usr_alice")], after=LULL - timedelta(seconds=1))
    assert r.skip_reason == SkipReason.LULL_NOT_OVER


def test_nudge_delay_is_configurable_10_to_30_min() -> None:
    p = PolicyConfig.from_json({"nudge_delay_s": 12 * 60})
    assert nudge([msg(1, "usr_alice")], after=timedelta(minutes=12), policy=p).eligible
    assert PolicyConfig.from_json({"nudge_delay_s": 60}).nudge_delay_s == 600
    assert PolicyConfig.from_json({"nudge_delay_s": 99_999}).nudge_delay_s == 1800


def test_nudge_once_per_lull() -> None:
    assert nudge([msg(1, "usr_alice")], nudge_done=True).skip_reason == SkipReason.NUDGE_DONE


def test_no_nudge_if_agent_spoke_last() -> None:
    recent = [msg(1, "usr_alice", at=T0), msg(2, "agent", "bye", at=T0 + timedelta(seconds=5))]
    assert nudge(recent).skip_reason == SkipReason.AGENT_SPOKE_LAST


def test_no_nudge_if_budget_cannot_cover_a_turn() -> None:
    r = nudge([msg(1, "usr_alice")], used=CAP - FULL_TURN_TOKENS + 1)
    assert r.skip_reason == SkipReason.RESTING


def test_no_nudge_while_paused_or_disabled() -> None:
    assert nudge([msg(1, "usr_alice")], agent=replace(AGENT, status="paused")).skip_reason == (
        SkipReason.PAUSED
    )
    assert nudge([msg(1, "usr_alice")], disabled=True).skip_reason == SkipReason.DISABLED


def test_nudge_can_be_switched_off() -> None:
    p = PolicyConfig.from_json({"nudge_enabled": False})
    assert nudge([msg(1, "usr_alice")], policy=p).skip_reason == SkipReason.NUDGE_DISABLED


def test_nudge_ignores_cooldown_and_share() -> None:
    # Agent share is high, but the nudge has its own conditions.
    recent = [
        msg(i + 1, "agent" if i % 2 else "usr_alice", "x", at=T0 + timedelta(seconds=i))
        for i in range(9)
    ]
    assert nudge(recent, n=4).eligible


def test_no_lull_without_human_messages() -> None:
    snap_recent: list[ChatMessage] = []
    r = gate(snap_recent, kind=TriggerKind.NUDGE, now=T0)
    assert r.skip_reason == SkipReason.NO_LULL


# ---- helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Huddle AI what do you think", True),
        ("hey huddle ai!", True),
        ("@HuddleAI thoughts?", True),
        ("HUDDLE   AI?", True),
        ("the huddle was fun", False),
        ("Huddle AIs are neat", False),
        # D-022: the ways people actually address it (QA: "Hey huddle are you there?")
        ("Hey huddle are you there?", True),
        ("Maybe the AI has an idea", True),
        ("what does the AI think", True),
        ("huddle, what do you think", True),
        ("thanks huddle", True),
        ("ask huddle", True),
        ("@huddle", True),
        ("huddle", True),
        ("hey ai", True),
        # ... and the ordinary uses of the same words
        ("Start the huddle", False),
        ("I love this huddle", False),
        ("We should do a huddle later", False),
        ("Huddle is cool", False),
        ("this AI stuff is wild", False),
        ("I like AI", False),
        ("an ai model", False),
        ("why is he not responding?", False),
        ("", False),
        (None, False),
    ],
)
def test_mentions_agent(text: str | None, hit: bool) -> None:
    assert mentions_agent(text, "Huddle AI") is hit


def test_engagement_by_overlap_or_name() -> None:
    a = msg(1, "agent", "The deployment window closes Friday evening")
    assert is_engaged(a, [msg(2, "usr_bob", "Friday evening deployment works for me")], "Huddle AI")
    assert is_engaged(a, [msg(2, "usr_bob", "thanks huddle ai")], "Huddle AI")
    assert not is_engaged(a, [msg(2, "usr_bob", "anyone for lunch?")], "Huddle AI")


def test_debounce_quiet_3s_capped_at_8s() -> None:
    assert debounce_due_at(0.0, 0.0, P) == 3.0
    assert debounce_due_at(0.0, 2.0, P) == 5.0
    assert debounce_due_at(0.0, 7.0, P) == 8.0  # cap: never wait more than 8 s from the first


def test_share_ignores_system_lines() -> None:
    recent = [msg(1, "usr_alice"), msg(2, "system", "Bob joined"), msg(3, "agent")]
    assert participation_share(recent, AGENT.id) == pytest.approx(0.5)


def test_a_renamed_agent_answers_to_its_new_name_only() -> None:
    assert mentions_agent("hey Sage, thoughts?", "Sage")
    assert not mentions_agent("hey huddle are you there?", "Sage")
    assert not mentions_agent("what does the AI think", "Sage")


def test_nickname_mention_bypasses_the_cooldown_and_reaches_the_decider() -> None:
    recent = [
        *pad(),
        msg(1, "usr_alice", at=T0),
        msg(2, "agent", "a point", at=T0 + timedelta(seconds=1)),
        msg(3, "usr_bob", "Hey huddle are you there?", at=T0 + timedelta(seconds=3)),
    ]
    r = gate(recent, n=3, now=T0 + timedelta(seconds=6))
    assert r.eligible and r.mentioned


def test_no_share_cap_in_a_short_chat() -> None:
    # 3 of 6 messages are the agent's: far over 1.25/3, but six messages say nothing about share.
    recent = [
        msg(1, "usr_alice", at=T0 - timedelta(hours=1)),
        msg(2, "agent", "x", at=T0 - timedelta(hours=1)),
        msg(3, "usr_bob", "what about the budget?", at=T0 - timedelta(hours=1)),
        msg(4, "agent", "y", at=T0 - timedelta(hours=1)),
        msg(5, "usr_alice", "and the plan?", at=T0 - timedelta(hours=1)),
        msg(6, "usr_bob", "what about the venue?", at=T0),
    ]
    assert gate(recent, n=3).eligible
    # Ten messages, four of them the agent's: now the share counts (5/11 > 1.25/3).
    old = T0 - timedelta(hours=1)
    long_chat = [
        msg(s, "agent" if s in {2, 4, 6, 8} else "usr_alice", "x", at=old + timedelta(seconds=s))
        for s in range(1, 10)
    ]
    long_chat.append(msg(10, "usr_bob", "what about the venue?", at=T0))
    assert gate(long_chat, n=3).skip_reason == SkipReason.SHARE_CAP


def test_answering_the_agents_question_is_a_turn_even_when_trivial_or_in_cooldown() -> None:
    """QA: the agent asked a question, the person answered, and the agent stayed silent."""
    recent = [
        *pad(),
        msg(1, "usr_alice", "I am not sure", at=T0),
        msg(
            2,
            "agent",
            "Was it a specific topic or a general suggestion?",
            at=T0 + timedelta(seconds=1),
        ),
        msg(3, "usr_alice", "general suggestion", at=T0 + timedelta(seconds=3)),
    ]
    r = gate(recent, n=3, now=T0 + timedelta(seconds=6))
    assert r.eligible and r.answering and not r.mentioned
    # "yes" is an answer, not small talk
    yes = [*recent[:-1], msg(3, "usr_alice", "yes", at=T0 + timedelta(seconds=3))]
    assert gate(yes, n=3, now=T0 + timedelta(seconds=6)).answering
    assert gate(yes, n=3, now=T0 + timedelta(seconds=6)).skip_reason is None


def test_a_reply_to_a_statement_is_not_an_answer() -> None:
    recent = [
        *pad(),
        msg(1, "agent", "One angle: check the numbers first.", at=T0),
        msg(2, "usr_alice", "thanks", at=T0 + timedelta(seconds=3)),
    ]
    r = gate(recent, n=3, now=T0 + timedelta(seconds=6))
    assert not r.answering and r.skip_reason == SkipReason.TRIVIAL


def test_an_old_agent_question_is_not_answered_by_later_chatter() -> None:
    recent = [
        *pad(),
        msg(1, "agent", "Which venue?", at=T0),
        msg(2, "usr_alice", "a", at=T0 + timedelta(seconds=1)),
        msg(3, "usr_bob", "b", at=T0 + timedelta(seconds=2)),
        msg(4, "usr_alice", "c", at=T0 + timedelta(seconds=3)),
        msg(5, "usr_bob", "unrelated chatter", at=T0 + timedelta(seconds=4)),
    ]
    from huddle_agent.policy import answers_agent_question

    assert answers_agent_question(recent, AGENT.id)  # a person replied within three messages
    assert not answers_agent_question([*pad(), msg(1, "agent", "Which venue?", at=T0)], AGENT.id)
