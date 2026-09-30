"""Agent evaluation harness (Deep Dive §11 "Agent evaluations", §9).

Replays scripted conversation snapshots through the REAL speak policy and run executor (gate,
debounce, decider -> writer, stale check, share cap, nudge rules) on the in-memory store and a
virtual clock, with a FAKE model. Runs in CI without Ollama:

    uv run python services/agent/tests/evals/agent_evals.py

What the numbers mean with the fake model: the heuristic decider stands in for the ~3B model,
so relevance and nudge precision measure the harness plus the gate around a rule-based decider,
not Llama's judgment. Timing and participation balance are properties of the policy (debounce,
stale check, cooldown, 1/N cap) and are meaningful with any decider of the given propensity
(the balance eval uses an "eager" decider so the policy's caps, not the decider, set the share).
Model latency is simulated (see LATENCY). Token counts are prompt-size estimates (chars / 4).
Swap `HeuristicModel` for `OllamaModelClient` to evaluate a real model (not done in CI).

The pytest wrapper is `test_agent_evals.py` next to this file.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import math
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from huddle_agent.clock import FakeClock
from huddle_agent.config import PolicyConfig, Settings
from huddle_agent.domain import TriggerKind
from huddle_agent.memory import MemoryChat, MemoryStore, MemoryWorld
from huddle_agent.models.base import (
    Decision,
    ModelRequest,
    Stage,
)
from huddle_agent.models.fake import HeuristicModel, is_low_value
from huddle_agent.policy import debounce_due_at
from huddle_agent.presence import Presence
from huddle_agent.runner import RunExecutor
from huddle_contracts.models import SenderType

HUMAN_NAMES = ["Alice", "Bob", "Chen", "Dana", "Eli", "Fatima"]
TOPICS = [
    ("launch", "beta", "rollout"),
    ("venue", "downtown", "the office"),
    ("budget", "Q3", "Q4"),
    ("migration", "Postgres", "MySQL"),
    ("offsite", "June", "September"),
    ("hiring plan", "contractors", "full-time"),
]
LATENCY = {Stage.DECIDE: 1.5, Stage.WRITE: 5.0, Stage.SAFETY: 0.3, Stage.SUMMARIZE: 3.0}


def humans(n: int) -> list[tuple[str, str]]:
    return [(f"usr_{name.lower()}", name) for name in HUMAN_NAMES[:n]]


# ---- a simulation harness around the real executor -----------------------------------------


class SimModel(HeuristicModel):
    """HeuristicModel with virtual latency; delivers scheduled human messages as time passes
    during a model call (so bursts can land mid-generation)."""

    def __init__(self, sim: Sim, *, eager: bool = False) -> None:
        super().__init__(clock=sim.clock, latency_s=dict(LATENCY))
        self.sim = sim
        self.eager = eager

    def _tick(self, req: ModelRequest) -> None:
        super()._tick(req)
        self.sim.deliver_due()

    async def decide(self, req: ModelRequest) -> Decision:
        d = await super().decide(req)
        if not self.eager or req.kind == TriggerKind.NUDGE:
            return d
        # An eager contributor wants to speak on anything that is not small talk, so the
        # policy's caps (not the decider's reluctance) shape its participation share.
        new = [m for m in req.messages if m.is_human]
        speak = bool(new) and not is_low_value(new[-1].body or "")
        return Decision(speak, "eager", d.usage)


@dataclass(order=True)
class Scheduled:
    at: float
    sender_id: str = field(compare=False)
    body: str = field(compare=False)


EPS = 1e-6


class Sim:
    """Event loop: human messages arrive on a schedule, triggers are debounced exactly like the
    worker (3 s quiet, 8 s cap), and each due batch runs one real evaluation."""

    def __init__(
        self,
        n_humans: int,
        *,
        eager: bool = False,
        capacity_tokens: int = 500_000,
    ) -> None:
        self.clock = FakeClock()
        self.t0 = self.clock.monotonic()
        self.world = MemoryWorld(
            clock=self.clock, humans=humans(n_humans), capacity_tokens=capacity_tokens
        )
        self.model = SimModel(self, eager=eager)
        chat = MemoryChat(self.world)
        self.executor = RunExecutor(
            store=MemoryStore(self.world),
            chat=chat,
            models=self.model,
            settings=Settings(worker_id="eval"),
            clock=self.clock,
            presence=Presence(chat),
        )
        self.schedule: list[Scheduled] = []
        self.first_at: float | None = None  # sim seconds
        self.last_at: float | None = None
        self.deferred_at: float | None = None
        self.on_agent_message: Callable[[Sim], None] | None = None

    @property
    def now(self) -> float:
        return self.clock.monotonic() - self.t0

    def advance_to(self, t: float) -> None:
        if t > self.now:
            self.clock.advance(t - self.now)

    def at(self, t: float, sender_id: str, body: str) -> None:
        bisect.insort(self.schedule, Scheduled(t, sender_id, body))

    def deliver_due(self) -> None:
        while self.schedule and self.schedule[0].at <= self.now + EPS:
            s = self.schedule.pop(0)
            self.world.post_human(s.sender_id, s.body)
            if self.first_at is None:
                self.first_at = self.now
            self.last_at = self.now

    def _due(self) -> float:
        deferred = self.deferred_at if self.deferred_at is not None else math.inf
        if self.first_at is None or self.last_at is None:
            return deferred
        policy = self.executor.policies.get(self.world.session_id) or PolicyConfig()
        return min(debounce_due_at(self.first_at, self.last_at, policy), deferred)

    async def run(self) -> None:
        while True:
            next_msg = self.schedule[0].at if self.schedule else math.inf
            due = self._due()
            if next_msg == math.inf and due == math.inf:
                return
            if next_msg <= due:
                self.advance_to(next_msg)
                self.deliver_due()
                continue
            self.advance_to(due)
            self.first_at = self.last_at = self.deferred_at = None
            before = len(self.world.agent_messages())
            out = await self.executor.execute(self.world.session_id)
            if out.retry_after_s is not None:  # like the worker: re-evaluate when cooldown ends
                self.deferred_at = self.now + out.retry_after_s + 0.01
            if len(self.world.agent_messages()) > before and self.on_agent_message is not None:
                self.on_agent_message(self)


# ---- 1. relevance: decider precision / recall on labelled snapshots ------------------------


@dataclass(frozen=True)
class Snapshot:
    messages: tuple[tuple[int, str], ...]  # (human index, text)
    speak: bool
    kind: str
    n_humans: int


def _fill(rng: random.Random, template: str) -> str:
    topic, a, b = rng.choice(TOPICS)
    return template.format(x=topic, a=a, b=b)


SPEAK_Q = [
    "Does anyone know when the {x} is due?",
    "What's the difference between {a} and {b} for the {x}?",
    "How did we handle the {x} last time?",
    "Which is cheaper for the {x}, {a} or {b}?",
    "Is there a hard limit on the {x} this quarter?",
]
SPEAK_MENTION = [
    "Huddle AI, can you summarize where we landed on the {x}?",
    "huddle ai what do you think about {a} vs {b}",
    "@HuddleAI any ideas for the {x}?",
]
TANGLE = [
    "I think we should go with {a} for the {x}",
    "but {a} is slower than {b} in practice",
    "actually {b} has the same issue with the {x}",
    "wait, I thought we agreed on {a} already",
    "no, that was only for the pilot of the {x}",
    "however the numbers for {b} changed since then",
    "I still think {a} is the safer bet overall",
]
SILENT_GREET = ["hi all", "morning!", "hey everyone", "good morning", "hello"]
SILENT_ACK = ["ok", "sounds good", "thanks", "got it", "cool", "yep", "agreed", "perfect"]
SILENT_SMALLTALK = [("how was your weekend?", "good thanks"), ("how's it going?", "not bad")]
SILENT_ANSWERED = [
    ("When is the {x} due?", "Friday at 5, per the doc"),
    ("Who owns the {x}?", "Dana does"),
    ("Are we still on {a}?", "yes, still {a}"),
]
SILENT_HANDLED = ["I'll book the room for the {x}", "sending the invite now", "I'll draft it later"]


def relevance_dataset(n: int = 200, seed: int = 11) -> list[Snapshot]:
    rng = random.Random(seed)
    out: list[Snapshot] = []
    for i in range(n):
        k = rng.randint(2, 5)
        prefix = [
            (rng.randrange(k), _fill(rng, rng.choice(SILENT_HANDLED + SILENT_ACK)))
            for _ in range(rng.randint(0, 3))
        ]
        kind = [
            "question",
            "mention",
            "tangle",
            "greeting",
            "ack",
            "smalltalk",
            "answered",
            "handled",
        ][i % 8]
        speak = kind in ("question", "mention", "tangle")
        if kind == "question":
            body = [(rng.randrange(k), _fill(rng, rng.choice(SPEAK_Q)))]
        elif kind == "mention":
            body = [(rng.randrange(k), _fill(rng, rng.choice(SPEAK_MENTION)))]
        elif kind == "tangle":
            body = [(j % k, _fill(rng, t)) for j, t in enumerate(TANGLE[: rng.randint(6, 7)])]
        elif kind == "greeting":
            body = [(j % k, rng.choice(SILENT_GREET)) for j in range(rng.randint(1, 3))]
        elif kind == "ack":
            body = [(rng.randrange(k), rng.choice(SILENT_ACK))]
        elif kind == "smalltalk":
            q, a = rng.choice(SILENT_SMALLTALK)
            body = [(0, q), (1 % k, a)]
        elif kind == "answered":
            q, a = rng.choice(SILENT_ANSWERED)
            body = [(0, _fill(rng, q)), (1 % k, _fill(rng, a))]
        else:
            body = [(rng.randrange(k), _fill(rng, rng.choice(SILENT_HANDLED)))]
        out.append(Snapshot(tuple(prefix + body), speak, kind, k))
    return out


async def eval_relevance(snaps: Sequence[Snapshot]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for snap in snaps:
        sim = Sim(snap.n_humans)
        ids = [h[0] for h in humans(snap.n_humans)]
        for idx, text in snap.messages:
            sim.world.post_human(ids[idx], text)
            sim.clock.advance(5)
        await sim.executor.execute(sim.world.session_id)
        spoke = bool(sim.world.agent_messages())
        tp += spoke and snap.speak
        fp += spoke and not snap.speak
        fn += (not spoke) and snap.speak
        tn += (not spoke) and not snap.speak
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "n": len(snaps),
        "precision": precision,
        "recall": recall,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


# ---- 2. timing: interrupting mid-burst ----------------------------------------------------


async def eval_timing(bursts: int = 100, seed: int = 23) -> dict[str, Any]:
    rng = random.Random(seed)
    interrupted = responded = 0
    for _ in range(bursts):
        n = rng.randint(2, 5)
        sim = Sim(n)
        ids = [h[0] for h in humans(n)]
        t = 1.0
        times: list[float] = []
        size = rng.randint(5, 12)
        for j in range(size):
            text = _fill(rng, rng.choice(SPEAK_Q)) if j == 0 else _fill(rng, rng.choice(TANGLE))
            sim.at(t, ids[rng.randrange(n)], text)
            times.append(t)
            t += min(rng.expovariate(1 / 1.5), 4.0)  # rapid: mean 1.5 s, never 4+ s apart
        await sim.run()
        burst_seqs = [m.seq for m in sim.world.messages[sim.world.session_id] if m.is_human]
        lo, hi = min(burst_seqs), max(burst_seqs)
        agent_seqs = [m.seq for m in sim.world.agent_messages()]
        if any(lo < s < hi for s in agent_seqs):
            interrupted += 1
        if agent_seqs:
            responded += 1
    return {
        "bursts": bursts,
        "interrupt_rate": interrupted / bursts,
        "responded_after_burst_rate": responded / bursts,
        "assumed_latency_s": {k.value: v for k, v in LATENCY.items()},
    }


# ---- 3. participation balance: simulated day-long sessions -------------------------------

STATEMENTS = [
    "I pushed the draft for the {x} to the shared folder",
    "we should compare {a} and {b} before deciding the {x}",
    "the numbers for {a} look better than last month",
    "I talked to finance about the {x} this morning",
    "{b} might be simpler for the {x} honestly",
    "let's keep {a} as the default for now on the {x}",
]
QUESTIONS = SPEAK_Q
LOW = SILENT_ACK + SILENT_GREET
ENGAGED = [
    "good point about checking the numbers first",
    "agreed, checking the numbers makes sense",
    "fair, I'll pull the numbers and check",
]


def _human_line(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.25:
        return _fill(rng, rng.choice(QUESTIONS))
    if r < 0.75:
        return _fill(rng, rng.choice(STATEMENTS))
    return rng.choice(LOW)


async def eval_balance(
    ns: Sequence[int] = (2, 3, 4, 5, 6), hours: float = 6.0, seed: int = 31
) -> dict[str, Any]:
    per_n: dict[str, Any] = {}
    for n in ns:
        rng = random.Random(seed + n)
        # Budget isolated here (huge capacity) so only the share policy shapes the result.
        sim = Sim(n - 1, eager=True, capacity_tokens=10**12)
        ids = [h[0] for h in humans(n - 1)]
        t = 5.0
        while t < hours * 3600:
            sim.at(t, ids[rng.randrange(n - 1)], _human_line(rng))
            t += rng.expovariate(1 / 40)  # a human message every ~40 s

        def react(s: Sim, rng: random.Random = rng, ids: list[str] = ids) -> None:
            if rng.random() < 0.6:  # humans often engage with what the agent said
                s.at(s.now + rng.uniform(5, 25), ids[rng.randrange(len(ids))], rng.choice(ENGAGED))

        sim.on_agent_message = react
        await sim.run()
        msgs = [m for m in sim.world.messages[sim.world.session_id] if m.is_text]
        agent = sum(1 for m in msgs if m.sender_type == SenderType.AGENT)
        share = agent / len(msgs)
        target = 1 / n
        per_n[str(n)] = {
            "messages": len(msgs),
            "agent_messages": agent,
            "share": share,
            "target": target,
            "relative_error": (share - target) / target,
            "within_30pct": abs(share - target) <= 0.3 * target,
        }
    return per_n


# ---- 4. nudge judgment: lulls labelled open or closed ------------------------------------

OPEN_ENDINGS = [
    "should we book {a} or {b} for the {x}?",
    "we still need to decide the {x} owner",
    "not sure yet about {a} vs {b}, thoughts?",
    "anyone checked the {x} numbers?",
]
CLOSED_ENDINGS = [
    "sounds good, thanks all, bye",
    "decided: {a} it is. thanks!",
    "great, that's settled then. see you tomorrow",
    "perfect, all set. cheers",
]


async def eval_nudges(n: int = 100, seed: int = 47) -> dict[str, Any]:
    rng = random.Random(seed)
    nudged_open = nudged_closed = opened = 0
    for i in range(n):
        is_open = i % 2 == 0
        opened += is_open
        sim = Sim(3)
        ids = [h[0] for h in humans(3)]
        for _ in range(rng.randint(2, 5)):
            sim.world.post_human(ids[rng.randrange(3)], _fill(rng, rng.choice(STATEMENTS)))
            sim.clock.advance(20)
        ending = rng.choice(OPEN_ENDINGS if is_open else CLOSED_ENDINGS)
        sim.world.post_human(ids[rng.randrange(3)], _fill(rng, ending))
        sim.clock.advance(21 * 60)
        await sim.executor.execute(sim.world.session_id, TriggerKind.NUDGE)
        nudged = bool(sim.world.agent_messages())
        nudged_open += nudged and is_open
        nudged_closed += nudged and not is_open
    nudges = nudged_open + nudged_closed
    return {
        "lulls": n,
        "precision": nudged_open / nudges if nudges else 1.0,
        "recall": nudged_open / opened if opened else 1.0,
        "nudges_after_closure": nudged_closed,
    }


# ---- 5. cost (informational): a standard 500-message session ------------------------------


async def eval_cost(messages: int = 500, seed: int = 59) -> dict[str, Any]:
    rng = random.Random(seed)
    sim = Sim(2)  # N = 3, default 500k-token fixed window
    ids = [h[0] for h in humans(2)]
    t = 5.0
    for _ in range(messages):
        sim.at(t, ids[rng.randrange(2)], _human_line(rng))
        t += rng.expovariate(1 / 40)
    await sim.run()
    used = sum(u[2] for u in sim.world.usage)
    runs = list(sim.world.runs.values())
    resting_runs = sum(1 for r in runs if r.skip_reason == "resting")
    return {
        "human_messages": messages,
        "tokens_used": used,
        "capacity_tokens": sim.world.capacity_tokens,
        "within_one_window": used <= sim.world.capacity_tokens,
        "evaluations": len(runs),
        "resting_evaluations": resting_runs,
        "agent_messages": len(sim.world.agent_messages()),
    }


async def run_all() -> dict[str, Any]:
    return {
        "relevance": await eval_relevance(relevance_dataset()),
        "timing": await eval_timing(),
        "balance": await eval_balance(),
        "nudges": await eval_nudges(),
        "cost": await eval_cost(),
    }


def main() -> int:
    report = asyncio.run(run_all())
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
