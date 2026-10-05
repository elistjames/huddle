"""Real-model decider eval: does Huddle AI speak when it should, and stay quiet when it should?

Runs the REAL pipeline (gate, decider prompt, your Ollama models, writer, safety check) on labelled
conversations (`cases.py`) and reports, per case, how often the agent spoke. Nothing is mocked but
the chat store and the clock. It is not part of CI; it needs Ollama with the models pulled:

    uv run python -m huddle_agent.evals.decider                 # decider only, 3 repeats per case
    uv run python -m huddle_agent.evals.decider --with-writer   # also write and check real replies
    uv run python -m huddle_agent.evals.decider --only stuck --repeats 5 -v
    make eval-decider

Cases decided by the deterministic gate (addressing the agent, answering its question, greetings)
never reach the model; they are reported separately so a pass there is not mistaken for the
model's judgement. The numbers that matter are the "decider" rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from huddle_agent.clock import FakeClock
from huddle_agent.config import PolicyConfig, Settings
from huddle_agent.domain import RunRecord, RunStatus, TriggerKind
from huddle_agent.evals.cases import AGENT, CASES, Case
from huddle_agent.memory import MemoryChat, MemoryStore, MemoryWorld
from huddle_agent.models.base import (
    Decision,
    Draft,
    ModelClient,
    ModelRequest,
    SafetyVerdict,
    Stage,
    SummaryText,
    Usage,
)
from huddle_agent.models.ollama import OllamaModelClient
from huddle_agent.policy import SkipReason
from huddle_agent.presence import Presence
from huddle_agent.runner import RunExecutor, clean_draft

MAX_REPLY_CHARS = 600
_BAD_STARTS = ("assistant", "system", "user", "huddle ai:", "ai:")
_BAD_PHRASES = ("as an ai", "i'm just an ai", "<|", "language model")


class RecordingModels:
    """Wraps the real client to record each call's latency and the decider's verdict."""

    def __init__(self, inner: ModelClient, *, real_writer: bool) -> None:
        self._inner = inner
        self._real_writer = real_writer
        self.decisions: list[tuple[bool, str]] = []
        self.raw: list[str] = []
        self.latency: dict[Stage, list[float]] = defaultdict(list)
        self.drafts: list[str] = []

    async def decide(self, req: ModelRequest) -> Decision:
        t0 = time.monotonic()
        try:
            d = await self._inner.decide(req)
        finally:
            self.latency[Stage.DECIDE].append(time.monotonic() - t0)
        self.decisions.append((d.speak, d.reason))
        self.raw.append(d.raw)
        return d

    async def write(self, req: ModelRequest) -> Draft:
        if not self._real_writer:
            return Draft("(writer skipped)", Usage(req.model, 0, 0))
        t0 = time.monotonic()
        try:
            d = await self._inner.write(req)
        finally:
            self.latency[Stage.WRITE].append(time.monotonic() - t0)
        # Production cleans the draft in the runner; record what users would see.
        self.drafts.append(clean_draft(d.text, "Huddle AI"))
        return d

    async def summarize(self, req: ModelRequest) -> SummaryText:
        return await self._inner.summarize(req)

    async def safety_check(self, req: ModelRequest) -> SafetyVerdict:
        if not self._real_writer:
            return SafetyVerdict(True, Usage(req.model, 0, 0))
        return await self._inner.safety_check(req)

    async def ping(self) -> bool:
        return await self._inner.ping()


@dataclass
class Trial:
    spoke: bool
    decided_by: str  # "decider" | "gate"
    reason: str  # the decider's words, or the gate's skip reason
    status: str
    error: str | None = None
    reply: str | None = None
    raw: str = ""  # the decider's unparsed answer
    problems: list[str] = field(default_factory=list[str])


def reply_problems(text: str) -> list[str]:
    out: list[str] = []
    low = text.strip().lower()
    if not low:
        out.append("empty")
    if len(text) > MAX_REPLY_CHARS:
        out.append(f"{len(text)} chars")
    if low.startswith(_BAD_STARTS):
        out.append("starts with a role or name header")
    for phrase in _BAD_PHRASES:
        if phrase in low:
            out.append(f"contains {phrase!r}")
    return out


async def run_trial(
    case: Case, models: ModelClient, args: argparse.Namespace
) -> tuple[Trial, RecordingModels]:
    clock = FakeClock()
    people = list(dict.fromkeys(s for s, _ in case.turns if s != AGENT))
    ids = {name: f"usr_{name.lower()}" for name in people}
    world = MemoryWorld(clock=clock, humans=[(ids[n], n) for n in people])
    world.set_agent_config(
        decider_timeout_s=args.decider_timeout,
        writer_timeout_s=args.writer_timeout,
        decider_max_input_tokens=args.decider_tokens,
    )
    for speaker, text in case.turns:
        if speaker == AGENT:
            world.post_agent(text)
        else:
            world.post_human(ids[speaker], text)
        clock.advance(args.gap)
    clock.advance(args.settle)  # past the cooldown: timing is not what this eval measures
    before = len(world.agent_messages())
    # In the running app every earlier message was already evaluated (and answered or skipped),
    # so only the latest one is new to the gate. Record that, or the gate would see the whole
    # conversation as unread and judge it differently than it does live.
    last_seq = world.messages[world.session_id][-1].seq
    if last_seq > 1:
        world.runs["prior-evaluation"] = RunRecord(
            id="prior-evaluation",
            session_id=world.session_id,
            agent_id=world.agent.id,
            trigger_kind=TriggerKind.MESSAGE,
            trigger_seq=last_seq - 1,
            context_seq=last_seq - 1,
            status=RunStatus.SKIPPED,
            skip_reason=SkipReason.DECIDER_DECLINED.value,
        )

    rec = RecordingModels(models, real_writer=args.with_writer)
    chat = MemoryChat(world)
    executor = RunExecutor(
        store=MemoryStore(world),
        chat=chat,
        models=rec,
        settings=Settings(
            worker_id="decider-eval",
            ollama_url=args.url,
            decider_model=args.decider,
            writer_model=args.writer,
        ),
        clock=clock,
        presence=Presence(chat),
    )
    out = await executor.execute(world.session_id)
    spoke = len(world.agent_messages()) > before
    if rec.decisions:
        decided_by, reason = "decider", rec.decisions[-1][1]
    else:
        decided_by = "gate"
        reason = out.skip_reason or ("forced turn" if spoke else str(out.status))
    error = out.error_code if out.status is not None and out.error_code else None
    reply = rec.drafts[-1] if rec.drafts else None
    trial = Trial(
        spoke=spoke,
        decided_by=decided_by,
        reason=reason,
        status=str(out.status),
        error=error,
        reply=reply,
        raw=rec.raw[-1] if rec.raw else "",
        problems=reply_problems(reply) if reply is not None and spoke else [],
    )
    return trial, rec


def write_json(path: str, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


async def main_async(args: argparse.Namespace) -> int:
    cases = [
        c
        for c in CASES
        if (not args.only or args.only in c.id or args.only == c.category)
        and (not args.category or c.category == args.category)
    ]
    if not cases:
        print("no cases match", file=sys.stderr)
        return 2
    client = OllamaModelClient(args.url)
    if not await client.ping():
        print(
            f"Cannot reach Ollama at {args.url}. Start it (ollama serve) or pass --url.",
            file=sys.stderr,
        )
        return 2
    print(
        f"Ollama {args.url} | decider {args.decider} ({args.decider_tokens} tokens in) | "
        f"writer {args.writer if args.with_writer else 'skipped'} | "
        f"{len(cases)} cases x {args.repeats} repeats"
    )

    if not args.no_warmup:
        print("warming up the model (first call loads it)...", flush=True)
        warm = next((c for c in CASES if c.id == "stuck-topic"), cases[0])
        await run_trial(warm, client, args)

    results: dict[str, list[Trial]] = {}
    lat: dict[Stage, list[float]] = defaultdict(list)
    for case in cases:
        trials: list[Trial] = []
        for _ in range(args.repeats):
            trial, rec = await run_trial(case, client, args)
            trials.append(trial)
            for stage, values in rec.latency.items():
                lat[stage].extend(values)
        results[case.id] = trials
        ok = sum(t.spoke == case.expect_speak and t.error is None for t in trials)
        by = Counter(t.decided_by for t in trials).most_common(1)[0][0]
        mark = "ok  " if ok == len(trials) else ("FAIL" if ok == 0 else "flaky")
        want = "speak " if case.expect_speak else "silent"
        spoke = sum(t.spoke for t in trials)
        print(f"{mark:5} {case.id:30} want {want} spoke {spoke}/{len(trials)}  [{by}]", flush=True)
        if args.verbose or ok != len(trials):
            for t in trials[: (len(trials) if args.verbose else 2)]:
                tail = f"  reply: {t.reply!r}" if t.reply else ""
                if t.raw and (args.verbose or t.reason.startswith(("unparseable", "(answer"))):
                    tail += f"  raw: {t.raw[:160]!r}"
                err = f"  ERROR {t.error}" if t.error else ""
                print(f"        - {'spoke' if t.spoke else 'quiet'}: {t.reason}{err}{tail}")

    # ---- summary --------------------------------------------------------------------------
    print("\n== summary ==")
    for layer in ("decider", "gate"):
        tp = fp = fn = tn = err = 0
        for case in cases:
            for t in results[case.id]:
                if t.decided_by != layer:
                    continue
                if t.error:
                    err += 1
                elif t.spoke and case.expect_speak:
                    tp += 1
                elif t.spoke:
                    fp += 1
                elif case.expect_speak:
                    fn += 1
                else:
                    tn += 1
        n = tp + fp + fn + tn
        print(
            f"{layer:8} trials {n + err:4} | accuracy {pct(tp + tn, n)} | precision "
            f"{pct(tp, tp + fp)} | recall {pct(tp, tp + fn)} | "
            f"missed {fn} | spoke wrongly {fp} | errors {err}"
        )
    print("\nper category (all trials):")
    cats: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        for t in results[case.id]:
            cats[case.category].append(t.spoke == case.expect_speak and t.error is None)
    for cat, oks in sorted(cats.items()):
        print(f"  {cat:14} {pct(sum(oks), len(oks)):>5}  ({sum(oks)}/{len(oks)})")
    for stage, values in lat.items():
        print(
            f"latency {stage.value:7} n={len(values):3} p50 {statistics.median(values):5.1f}s "
            f"p95 {quantile(values, 0.95):5.1f}s max {max(values):5.1f}s"
        )
    bad = [(c.id, t) for c in cases for t in results[c.id] if t.problems]
    if bad:
        print("\nreply problems:")
        for cid, t in bad[:10]:
            print(f"  {cid}: {', '.join(t.problems)}: {t.reply!r}")

    if args.json:
        write_json(args.json, {c.id: [asdict(t) for t in results[c.id]] for c in cases})
        print(f"\nwrote {args.json}")

    # exit code: decider accuracy against --min-accuracy, for scripted use
    dec = [
        (t.spoke == c.expect_speak and t.error is None)
        for c in cases
        for t in results[c.id]
        if t.decided_by == "decider"
    ]
    if args.min_accuracy is not None and dec and sum(dec) / len(dec) < args.min_accuracy:
        print(f"\ndecider accuracy {pct(sum(dec), len(dec))} is below {args.min_accuracy:.0%}")
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = Settings.from_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default=env.ollama_url or os.environ.get("HUDDLE_OLLAMA_URL"))
    p.add_argument("--decider", default=env.decider_model)
    p.add_argument("--writer", default=env.writer_model)
    p.add_argument(
        "--repeats", type=int, default=3, help="runs per case (models are not deterministic)"
    )
    p.add_argument(
        "--with-writer",
        action="store_true",
        help="also run the real writer and safety check (slow on CPU)",
    )
    p.add_argument("--only", help="run cases whose id contains this, or whose category equals it")
    p.add_argument("--category", help="run one category")
    p.add_argument(
        "--decider-timeout",
        type=float,
        default=30.0,
        help="seconds (the spec default is 10; CPU is slower)",
    )
    p.add_argument("--writer-timeout", type=float, default=90.0)
    p.add_argument(
        "--decider-tokens",
        type=int,
        default=PolicyConfig().decider_max_input_tokens,
        help="decider input cap in tokens",
    )
    p.add_argument("--gap", type=float, default=12.0, help="seconds between messages in a case")
    p.add_argument(
        "--settle",
        type=float,
        default=30.0,
        help="quiet seconds before the decision (clears the cooldown)",
    )
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument(
        "--min-accuracy", type=float, help="exit 1 if decider accuracy is below this (0-1)"
    )
    p.add_argument("--json", help="write every trial to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="show every trial's reason")
    return p.parse_args(argv)


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
