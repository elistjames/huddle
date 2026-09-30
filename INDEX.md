# Repository map

Update this file when you add a package or top-level directory (`scripts/check_index.py` enforces it).

| Path | What it is |
|---|---|
| `docs/specs/` | Notion mirrors: PRD, TDD, Deep Dive (source of truth), design page |
| `docs/decisions.md` | Repo-level decisions (R-nnn) |
| `design/` | Huddle design system (tokens, React components, icons, logos, prototype) |
| `libs/contracts` | Shared wire contracts: WS events, internal HTTP models, Redis keys, fixed-window budget math |
| `services/gateway` | WebSocket gateway (FastAPI). Own deployable. |
| `services/chat` | Chat service: public REST, internal API, Postgres, fan-out, receipts, budget. Own deployable. |
| `services/agent` | Agent orchestrator worker (LangChain + Ollama). Own deployable. |
| `apps/web` | React 18 + Vite client built on `design/` |
| `deploy/` | docker-compose (one container per service, simulating separate servers), env examples |
| `scripts/` | `gate.sh` (merge gate), helpers |
| `.github/` | CI, security scanners, templates |
