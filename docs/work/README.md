# Work briefs

Per-component build briefs for parallel agents. Each brief is scoped to one deployable so agents don't collide. Read `CLAUDE.md` and `docs/harness.md` first.

Shared inter-service interface (owned by `libs/contracts`, do not fork it):

| From → To | Channel | Shape |
|---|---|---|
| client → chat | HTTPS REST | Deep Dive §6 (`/auth/guest`, `/auth/refresh`, `/sessions/{id}`, `/sessions/{id}/messages`, `/sessions/{id}/agent`). JWT HS256 signed with `HUDDLE_JWT_SECRET`, claims `sub` (user id), `exp`. |
| client ↔ gateway | WebSocket `/ws` | `huddle_contracts.ws` frames, envelope `v: 1`. |
| gateway → chat | HTTP, `X-Service-Token: $HUDDLE_SERVICE_TOKEN` | `POST /internal/messages` (SubmitMessageRequest → SubmitMessageResponse), `POST /internal/acks` (AckDeliveryRequest), `GET /internal/sessions/{sid}/messages?after_seq&before_seq&limit` (MessagePage), `GET /internal/sessions/{sid}/members/{participant_id}` (MembershipCheck). Errors: JSON `SubmitMessageError` with HTTP 4xx/5xx. |
| chat → gateway | Redis pub/sub `gw:{gateway_id}` | `FanoutEnvelope`. Registry keys in `redis_keys.py`. |
| chat → agent | Redis Stream `agent:triggers`, group `agent-workers` | `AgentTrigger` fields, only for committed HUMAN messages. |
| agent → chat | HTTP, same service token | `POST /internal/messages` (sender agent, `client_msg_id = run_id`), `POST /internal/acks` (kind `read`), `PUT /internal/sessions/{sid}/agent/status` (SetAgentStatusRequest), `POST /internal/sessions/{sid}/agent/usage` (RecordUsageRequest), `GET /internal/sessions/{sid}/budget` (BudgetState). |
| agent → Postgres | direct, own credentials | reads `messages`, `session_memberships`, `agents`, `chat_sessions`; writes `agent_runs`, `session_summaries`. **The chat service owns all Alembic migrations** for the full Deep Dive §4 schema. |

Env vars (names in `deploy/.env.example`): `HUDDLE_POSTGRES_DSN`, `HUDDLE_REDIS_URL`, `HUDDLE_JWT_SECRET`, `HUDDLE_SERVICE_TOKEN`, `HUDDLE_CHAT_URL`, `HUDDLE_GATEWAY_ID`, `HUDDLE_OLLAMA_URL`, `HUDDLE_DECIDER_MODEL`, `HUDDLE_WRITER_MODEL`. The MVP has one pre-created session; its id comes from `HUDDLE_SESSION_ID` (default `ses_main`), created with its agent on chat startup.
