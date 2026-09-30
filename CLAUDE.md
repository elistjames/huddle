# Huddle: instructions for coding agents

Huddle is a real-time group chat where people and one autonomous AI agent ("Huddle AI") share a single thread. The agent reads the same context as everyone and speaks up only when it has something worth adding.

**Status: repo scaffolding only. No application code exists yet.** Build to the specs below.

> Naming: the product is **Huddle**. (It was once referred to as "Hubble"; the Notion specs and design system all say Huddle.)

## Source of truth, in priority order

1. `docs/specs/deep-dive.md`: implementation-ready contracts, schemas, agent policy, API/WebSocket events, failure handling, security, UI states, tests, ops, and the **Decision Log** (D-001 to D-018).
2. `docs/specs/tdd.md`: architecture and component overview, API table.
3. `docs/specs/prd.md`: scope (in/out), objectives, success metrics.
4. `design/` and `docs/specs/design-system.md`: UI. Read `design/CLAUDE.md` before touching any UI.

These are mirrors of the Notion pages (links at the top of each file). If they disagree, the higher one wins. If you think a spec is wrong or silent on something you need, **stop and ask**; do not silently invent behaviour. Any decision you get answered should be written back into the specs (and the Notion page should be updated by the owner).

## Rules that are easy to get wrong

- **Scope is v1.** One pre-created session everyone joins, 2 to ~10 humans, one agent, guest auth, text only. Do not build: multiple sessions, sign-up/invites, multi-agent, @mentions, human typing indicators, presence, attachments, reactions, edit/delete, push/email. (Phase 3 items exist only as components in `design/`.)
- **One write path.** Every message, human or agent, goes through the chat service's idempotent `submit_message`. The agent calls it as a tool and never opens a WebSocket. Nothing reaches a client without a `seq`.
- **Ordering:** gapless per-session `seq` assigned in the same Postgres transaction as the insert (`chat_sessions.last_seq`), not a Postgres SEQUENCE. Clients render by `seq`.
- **Idempotency:** keyed on `(session_id, sender_id, client_msg_id)`; agent messages use `run_id` as `client_msg_id`. Clients dedupe by `message_id`/`seq`.
- **Receipts are cursors,** not per-message rows. ✓ stored, ✓✓ any one other human has it, amber ✓ the agent's cursor has passed it (means read, not "will reply").
- **Consistency over availability:** reject sends when Postgres is down; do not buffer.
- **The message store is the agent's source of truth.** Rebuild context from the latest summary plus the recent window.
- **Agent policy:** two stages (gate + ~3B decider, then ~8B writer), debounce, cooldown, 1/N participation share, never two agent messages in a row, one re-engagement nudge per lull only if the conversation is open. Models are free and local (Ollama). Budget is a visible token bucket, in tokens never dollars.
- **Agent errors never appear in the thread;** they show only as the paused / resting / away state. Never say "typing" or "thinking"; the agent is "Huddle AI", never "Assistant" or "Bot".
- **UI:** amber means AI and only AI. Use the tokens and components in `design/`; do not hardcode colours or restyle components.

## Stack (from the TDD)

Python (FastAPI gateways, pure-Python chat service), PostgreSQL (Alembic migrations, expand-then-contract), Redis (connection registry, pub/sub fan-out, Streams for agent triggers, sorted set for nudge timers), LangChain + Ollama (Llama models) for the agent orchestrator, React 18 for the client (see `design/docs/USAGE.md`). Local dev via docker-compose.

The repository layout, package manager, test runner and client build tooling are **not yet decided**. Propose them and record the choice in `docs/decisions.md` before scaffolding.

## Repository layout

```
CLAUDE.md            this file
README.md            project overview
docs/
  README.md          index of the docs
  decisions.md       repo-level decisions made after the Notion specs (append here)
  specs/             prd.md, tdd.md, deep-dive.md, design-system.md (Notion mirrors)
design/              the Huddle design system (tokens, React components, icons, logos, prototype)
```

## Working agreements

- Develop on the branch you were given; small, focused commits; do not open PRs unless asked.
- Tests follow Deep Dive §11 (sequence gaplessness, idempotency, authorization, agent gate rules, token bucket, reconnect, multi-client consistency, agent evals).
- Keep observability in mind from the start (Deep Dive §9): structured logs with `trace_id`, agent run records.
