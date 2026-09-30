# Huddle: instructions for coding agents

Huddle is a real-time group chat where people and one autonomous AI agent ("Huddle AI") share a single thread. The agent reads the same context as everyone and speaks up only when it has something worth adding.

**Status: v1 services and web client are built and merged; see `docs/status.md` for verification gaps and open decisions.**

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
- **Agent policy:** two stages (gate + ~3B decider, then ~8B writer), debounce, cooldown, 1/N participation share, never two agent messages in a row, one re-engagement nudge per lull only if the conversation is open. Models are free and local (Ollama). Budget is a visible fixed-window token budget (D-019), measured in tokens never dollars, shown as a percentage.
- **Agent errors never appear in the thread;** they show only as the paused / resting / away state. Never say "typing" or "thinking"; the agent is "Huddle AI", never "Assistant" or "Bot".
- **UI:** amber means AI and only AI. Use the tokens and components in `design/`; do not hardcode colours or restyle components.

## Architecture: distributed, one deployable per service

`services/gateway` (WebSocket), `services/chat` (public REST + internal API, owns Postgres writes, fan-out, receipts, budget), `services/agent` (orchestrator worker), `apps/web` (client). Each service has its own package, `pyproject.toml`, Dockerfile and container; they run on separate servers and talk **only** over HTTP (`X-Service-Token`), Redis (pub/sub fan-out, `agent:triggers` stream, `agent:timers`, connection registry) and Postgres. **No service imports another service's code.** The only shared code is `libs/contracts` (pydantic wire types, Redis key names, pure fixed-window budget math). If you need a new cross-service shape, add it to `libs/contracts` and to Deep Dive §3/§6 in the same PR.

Stack: Python 3.11+ (uv workspace, single `uv.lock`), FastAPI, SQLAlchemy Core + asyncpg + Alembic (expand-then-contract), redis-py, LangChain + Ollama, React 18 + Vite + TypeScript for the client. See `docs/decisions.md` (R-003 to R-006).

## The development loop (harness; details in `docs/harness.md`)

1. Work from an issue that cites spec sections and D-numbers.
2. Implement against the spec. Cite D-numbers. Never contradict a locked decision silently.
3. **Run `scripts/gate.sh` before every commit.** It runs ruff format + lint, bandit, pyright (strict), pytest and the INDEX check, all legs every time. Do not commit with it red. Do not weaken a check to pass it; use a narrowly scoped, justified `# nosec BXXX` / `# noqa` only.
4. Integration tests are marked `@pytest.mark.integration`. Start local stores with `eval "$(scripts/dev-stores.sh)"` (real Postgres 16 + Redis, no Docker). Without the env vars they skip locally; CI sets `HUDDLE_REQUIRE_STORES=1` so they fail instead.
5. Update `INDEX.md` when adding a package or directory.
6. Review is done by a different agent/session that sees only the spec and the diff.

Commands: `uv sync`, `uv run pytest -m "not integration"`, `uv run pyright`, `uv run ruff format . && uv run ruff check . --fix`, `scripts/gate.sh`.

## Repository layout

See `INDEX.md`.

## Working agreements

- Develop on the branch you were given; small, focused commits; do not open PRs unless asked.
- Tests follow Deep Dive §11 (sequence gaplessness, idempotency, authorization, agent gate rules, budget window, reconnect, multi-client consistency, agent evals).
- Observability from the start (Deep Dive §9): structured JSON logs with `ts, service, trace_id, session_id, actor_id, message_id, seq, event, latency_ms`; never log message bodies.
- Budget (D-019): fixed UTC-aligned 24 h window, shown to users only as a percentage used plus a refill time. Never show raw tokens.
- Inactive agent (paused, resting, away) does not advance its read cursor (D-020).
