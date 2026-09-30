# Repo decisions

Decisions made in this repository after the Notion specs. The spec Decision Log (D-001 to D-018) is in [specs/deep-dive.md](specs/deep-dive.md#decision-log); continue numbering here from R-001.

| ID | Decision | Rationale | Date |
|---|---|---|---|
| R-001 | Design system lives in `design/` at the repo root, copied unmodified from the design export. | Single place for tokens, components and the prototype; referenced by `CLAUDE.md`. | 2026-09-30 |
| R-002 | Notion specs are mirrored to `docs/specs/` as the agents' source of truth. | Agents work from the repo; Notion stays the editing home. | 2026-09-30 |

Still to decide before coding: repo layout (services vs packages), Python tooling, client build tooling, test runner, CI.
