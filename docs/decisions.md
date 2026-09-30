# Repo decisions

Decisions made in this repository after the Notion specs. The spec Decision Log (D-001 to D-018) is in [specs/deep-dive.md](specs/deep-dive.md#decision-log); continue numbering here from R-001.

| ID | Decision | Rationale | Date |
|---|---|---|---|
| R-001 | Design system lives in `design/` at the repo root, copied unmodified from the design export. | Single place for tokens, components and the prototype; referenced by `CLAUDE.md`. | 2026-09-30 |
| R-002 | Notion specs are mirrored to `docs/specs/` as the agents' source of truth. | Agents work from the repo; Notion stays the editing home. | 2026-09-30 |
| R-003 | Distributed by design: `gateway`, `chat` and `agent` are separate deployables (own package, Dockerfile, container) that talk only over HTTP, Redis and Postgres. Shared code is limited to `libs/contracts` (wire types, Redis keys, pure budget math). | Scalability; each service can run on its own server. | 2026-09-30 |
| R-004 | Stack: Python 3.11+ uv workspace with one lockfile; ruff, bandit, pyright (strict), pytest; SQLAlchemy Core + asyncpg + Alembic; React 18 + Vite + TypeScript for `apps/web`. | Matches the TDD and the reference harness. | 2026-09-30 |
| R-005 | Agent dev harness adopted from resgraph: `scripts/gate.sh`, CI legs, secret/CVE/workflow scanners, issue-first, independent review. See `docs/harness.md`. | Consistent, controlled agent-driven development. | 2026-09-30 |
| R-006 | Owner answers to the design open questions are recorded as spec decisions D-019 (fixed-window budget shown as a percentage) and D-020 (inactive agent does not advance its read cursor; proposed copy is final). | Keep the spec authoritative. | 2026-09-30 |
| R-007 | Web client readings of spec gaps: the amber tick shows only once ✓✓ is also reached (linear §1 state machine; `Receipt` draws it on top of ✓✓); gaps are fetched with REST `after_seq`; the resume cursor lives in memory (a reload rejoins at `after_seq=0` and gets the last window); a fresh join whose replay starts above seq 1 treats earlier history as "Load earlier", not a gap; unsent messages wait for the socket rather than the REST fallback. Full list in [`apps/web/README.md`](../apps/web/README.md#spec-gaps-and-how-they-were-resolved). | The spec is silent or ambiguous on these client details; each choice is the conservative one and easy to change if the owner decides otherwise. | 2026-09-30 |
