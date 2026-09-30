# Technical Design Deep Dive

> **Source of truth.** Mirrored from Notion ("Technical Design Deep Dive", last edited 2026-09-29): https://www.notion.so/a3e803163b2748d3a5680a0f49a555de
> This is the implementation-ready spec: contracts, schemas, agent policy, API/WebSocket events, security, UI states, tests, ops and the Decision Log. If it disagrees with the TDD or PRD, this file wins on implementation detail. If code disagrees with it, change the spec first (see `CLAUDE.md`).

# Purpose
Use this page to turn the high-level technical design into implementation-ready decisions. Fill in each section with explicit contracts, assumptions, constraints, and failure behavior.
Source document: [Technical Design Document](tdd.md) · Product: [PRD](prd.md) · Design (source of truth for UI copy and states): [Design System](../../design/README.md)
> Values marked *(tunable)* are starting points, to be adjusted from beta data. Everything else is a decision recorded in the Decision Log at the bottom.
## 1. Domain Model and State Transitions
### Scope and assumptions
- **In scope for the MVP:**
	- One pre-created session that every user joins on startup.
	- 2 to about 10 human users.
	- One agent, Huddle AI, pre-added to the session.
	- Text messages with three-checkmark receipts.
	- Reconnect with catch-up.
	- Guest identity (a display name plus a signed token).
	- The re-engagement nudge.
	- A visible agent budget.
- **Out of scope (follow-ups):**
	- Multiple saved sessions, sign-up and invites.
	- Multiple agents.
	- @mentions.
	- Typing indicators for humans. The agent's "is writing" indicator is agent status, not a typing indicator, so it stays in scope.
	- Agent tools.
	- Message editing and deleting, reactions, attachments.
	- Push or email notifications (confirmed out of MVP scope).
- **Assumptions:**
	- A user can be connected from several devices at once.
	- The agent is a session member like any user.
	- Clients are untrusted.
	- Groups are small, so every member receives every message.
### Core entities
| Entity | Purpose | Key fields | Relationships |
|---|---|---|---|
| User | A human identity | `id (usr_…)`, `display_name`, `created_at` | Has many memberships and device connections |
| Chat session | One conversation; the authority for its ordering | `id (ses_…)`, `title`, `status`, `last_seq`, `created_by` | Has many memberships, messages and summaries; has one agent budget |
| Session membership | Links a participant to a session and holds their receipt cursors | `(session_id, participant_id)`, `participant_type`, `role`, `status`, `last_delivered_seq`, `last_read_seq`, `preferences` | Belongs to a session and to a user or an agent |
| Message | An immutable entry in the session log | `id (msg_…)`, `session_id`, `seq`, `sender_type`, `sender_id`, `client_msg_id`, `kind`, `body`, `agent_run_id?` | Belongs to a session; may be produced by an agent run |
| AI agent | A participant identity and its settings | `id (agt_…)`, `name`, `avatar`, `model`, `fallback_model`, `prompt_version`, `config`, `status` | Has a membership; has many runs |
| Device connection | One live socket, kept in Redis | `connection_id`, `user_id`, `device_id`, `gateway_id`, `last_heartbeat`, TTL | Belongs to a user; routed through one gateway |
| Agent run | One cycle of evaluating and possibly speaking | See §4 | Belongs to a session and an agent |
| Session summary | Compacted older context for the agent | `covers_through_seq`, `summary` | Belongs to a session and an agent |
| Agent budget | A fixed-window token budget that caps agent load; shown to users as a percentage | `capacity_tokens`, `used_tokens`, `window_seconds`, `window_started_at` | One per session and agent |
### State machines
- **Chat session:** `active → closed`. `archived` is a follow-up. The MVP session never closes.
- **Session membership:** `invited → active → left | removed`. In the MVP, joining goes straight to `active`.
- **Message (server):** `received → validated → persisted-in-sequence → published → delivered → acknowledged`. Persisting and sequencing are one atomic stage in one transaction. A message that fails validation ends in `rejected`.
- **Message (sender's client):** `pending → ✓ stored → ✓✓ delivered → ✓✓✓ agent-seen`, or `pending → failed`.
- **Device connection:** `connecting → authenticated → live → stale (missed heartbeats) → closed`.
- **Agent run:** `triggered → eligible | skipped → context-built → generating → validated → committed → observed`. Terminal alternatives are `cancelled`, `superseded`, `timed_out` and `failed`.
- **Agent presence:** `following ⇄ writing`, plus `paused` (set by a user), `resting` (budget empty) and `away` (model runtime unavailable).
### Open decisions
- None. Scope was confirmed on 2026-09-29.
## 2. Message Ordering, Idempotency, and Reconnects
### Message lifecycle
```plain text
received → validated → persisted-in-sequence (one txn) → published → delivered → acknowledged
```
- After a message is sent, there are three checkmarks:
	- **✓ Stored:** the message is committed. The sender receives `message.ack {client_msg_id, message_id, seq}`.
	- **✓✓ Delivered:** at least **one** other human member (any one, not all) has a device that acked `seq ≥ this`.
	- **✓✓✓ Agent received:** the orchestrator has taken the message into its context. This means the agent read it, not that it replied. The third check is **amber**, because amber means AI in the Huddle design, which makes it distinguishable for the sender.
- Receipts are **monotonic cursors per membership** (`last_delivered_seq`), not rows per message. An ack for seq N covers everything before N.
- When a cursor advances, the server broadcasts `receipt.updated {cursors: {participant_id: last_delivered_seq}}`. Each client works out its own checks from the cursors:
	- ✓✓ when any other human's cursor is at or past the message's `seq`.
	- The amber ✓ when the agent's cursor is at or past it.
- An offline member never holds ✓✓ back, as long as someone else has received the message.
### Ordering model
- **Source of truth for ordering:** a gapless, monotonic sequence number per session. Clients render by `seq`, never by arrival time. When two sends race for the next slot, either winner is fine, because every viewer sees the same order.
- **Scope:** ordering is always per session.
- **Concurrent messages:** they are serialized by the row lock on `chat_sessions`. The client remembers the last `seq` it has seen per session and, on reconnect, fetches everything after it.
- **Who assigns `seq`:** it is centralized in Postgres, inside the chat service transaction: `UPDATE chat_sessions SET last_seq = last_seq + 1 WHERE id = $1 RETURNING last_seq`, followed by the insert. `UNIQUE(session_id, seq)` is the backstop.
- **Why gapless:** a rolled-back transaction also rolls back the counter. A Postgres SEQUENCE would leave gaps, which is why it isn't used. Gaplessness lets a client detect a missed message: if it holds 43 and receives 45, it fetches 44.
### Delivery semantics
- **Guarantee:** effectively once. Delivery is at-least-once, and idempotency makes it effectively once.
- **Duplicates:** duplicates can occur on the wire, but they are **never visible to users**. Two layers enforce this:
	- **Server:** `submit_message` is idempotent on `(session_id, sender_id, client_msg_id)`. The client generates a UUIDv7 per message and reuses it on every retry. A repeat returns the original `message_id` and `seq`.
	- **Client:** the client dedupes by `message_id` or `seq` whenever it merges live events with fetched history.
- **Missing messages:** messages are stored first, then pushed. Missing messages are detected by a gap in `seq`, or recovered on reconnect: the client asks for everything after its cursor and dedupes any overlap.
### Reconnect and catch-up protocol
- **The cursor:** the client stores `last_seq` per session. On reconnect it sends `subscribe {session_id, after_seq}`, and the server replays from Postgres in pages of 200. Live events that arrive during replay are buffered, merged by `seq`, and deduped.
- **What the cursor recovers:** messages sent while the client was offline, messages missed in a network blip, and messages whose delivery ack was lost.
- **Client backoff:** jittered exponential backoff (full jitter, base 500 ms, cap 30 s) is always on in the client, because it costs nothing.
- **Server-side draining:** this is only needed at large scale (the scenario where 500k connections restart at once). The server sends `server.draining {reconnect_after_ms}` with staggered delays.
- **The same protocol applies to newcomers.** An agent restart, and in the multi-agent future a new agent joining an existing conversation, follows the same protocol as a new user. It pulls from the store, which is the source of truth, and fetches only the last window of history rather than all of it. See §5.
### Open decisions
- None. Q1 resolved: ✓✓ needs one other human, not all of them.
## 3. Service Boundaries and Internal Interfaces
### Component responsibilities
| Component | Owns | Does not own | Inputs | Outputs |
|---|---|---|---|---|
| Connection gateway (FastAPI) | Sockets, auth handshake, heartbeats, per-connection send queue, registry entries | Business rules, ordering, persistence | Client frames; the `gw:{id}` pub/sub channel | Calls to the chat service; frames to clients |
| Connection registry (Redis) | Which devices are connected, and to which gateway, with a TTL | Any durable state | Gateway writes and heartbeat refreshes | Lookups from user to connections to gateways |
| Chat service (Python) | Validation, authorization, sequencing, persistence, fan-out, receipts, budget accounting | Sockets, LLM calls | `submit_message`, `ack_delivery` | Postgres writes; publishes to gateways; agent triggers |
| Message store (Postgres) | The ordered message log | Delivery state beyond the cursors | Inserts from the chat service | Replay and window queries |
| Session store (Postgres) | Sessions, memberships, cursors, preferences, agent budgets | Messages | The chat service | Membership lookups |
| Agent orchestrator (LangChain workers) | The speak policy, context and summaries, LLM calls, run lifecycle, nudge timers | Sequencing and delivery | The `agent:triggers` stream; the `agent:timers` sorted set; windowed reads from Postgres | The `submit_message` tool; `ack_delivery`; agent status |
### Internal contracts
- **Gateway → chat service** (internal HTTP):
	- `submit_message(session_id, sender{type,id}, client_msg_id, body, trace_id) → {message_id, seq} | error`. This call is **idempotent** on `client_msg_id`.
	- `ack_delivery(participant_id, device_id, session_id, up_to_seq)`.
- **Chat service → message store:** one transaction that checks membership, increments `last_seq`, and inserts the message.
- **Chat service → connection registry:** `get_connections(participant_ids) → [(connection_id, gateway_id)]`.
- **Chat service → gateway fan-out:** `PUBLISH gw:{gateway_id} {targets:[connection_ids], envelope}`. Pub/sub can drop messages; the cursor recovers them.
- **Chat service → agent orchestrator:** `XADD agent:triggers {session_id, seq, message_id, sender_type}`. This is a Redis Stream with a consumer group, so delivery is at-least-once.
- **Agent orchestrator → chat service:**
	- The agent calls `submit_message` **directly, as a LangChain tool**, with `sender_type=agent` and `client_msg_id=run_id`. Because the call is idempotent, a retried commit can't post twice.
	- The orchestrator also calls `ack_delivery`, which produces the amber check, and `set_agent_status(following|writing|paused|resting|away)`.
	- It never opens a websocket.
### Authoritative message path
Every message, whether from a human or the agent, goes through `submit_message`. That function runs, in order:
1. The authenticated identity.
2. A membership check.
3. Length and rate limits.
4. The sequence-and-insert transaction.
5. After commit: fan-out to gateways and an agent trigger. Agent messages don't create triggers.
Nothing reaches a client without a `seq`. Moderation of agent output happens in the orchestrator, before it calls the tool.
### Open decisions
- None.
## 4. Postgres and Redis Schemas
### Chat sessions
| Field | Type | Required | Constraints / indexes | Notes |
|---|---|---|---|---|
| id | text | ✓ | PK | `ses_`  • ULID |
| title | text | ✓ |  |  |
| status | text | ✓ | CHECK in (`active`, `closed`) |  |
| last_seq | bigint | ✓ | default 0 | Row-locked on every send |
| last_human_message_at | timestamptz |  |  | Used for nudge eligibility |
| created_by | text |  | FK users |  |
| created_at / closed_at | timestamptz | ✓ / – |  |  |
### Session memberships
| Field | Type | Required | Constraints / indexes | Notes |
|---|---|---|---|---|
| session_id | text | ✓ | PK part 1, FK |  |
| participant_id | text | ✓ | PK part 2; index on its own | A user or agent ID. The composite PK replaces the old `Membership Key` string. |
| participant_type | text | ✓ | CHECK in (`user`, `agent`) |  |
| role | text | ✓ | `owner` or `member` | Used from phase 3 |
| status | text | ✓ | `invited`, `active`, `left`, `removed` |  |
| last_delivered_seq / last_read_seq | bigint | ✓ | default 0 | Receipt cursors |
| preferences | jsonb | ✓ | default `{}` |  |
| joined_at / left_at | timestamptz | ✓ / – |  |  |
### Messages
| Field | Type | Required | Constraints / indexes | Notes |
|---|---|---|---|---|
| id | text | ✓ | PK | `msg_`  • ULID |
| session_id, seq | text, bigint | ✓ | `UNIQUE(session_id, seq)` | This index also serves replay and window queries |
| sender_type | text | ✓ | CHECK in (`user`, `agent`, `system`) | Maps to LangChain message roles: `user` → HumanMessage with the speaker's name, `agent` → AIMessage, `system` → a system line |
| sender_id | text | ✓ |  |  |
| client_msg_id | uuid | ✓ | `UNIQUE(session_id, sender_id, client_msg_id)` | Idempotency key. For agent messages it is the `run_id`. |
| kind | text | ✓ | `text` or `system` | System messages record joins, leaves, pauses and similar events |
| body | text | – | CHECK length ≤ 4000 | Set to null when redacted |
| agent_run_id | text | – | FK agent_runs |  |
| created_at / redacted_at | timestamptz | ✓ / – |  |  |
### Agent configuration and runs
| Field | Type | Required | Constraints / indexes | Notes |
|---|---|---|---|---|
| [agents.id](http://agents.id) / name / avatar_url | text | ✓ | PK on `id` | The default name is "Huddle AI" |
| agents.model / fallback_model / prompt_version | text | ✓ |  | Each run records its `prompt_version` |
| agents.config | jsonb | ✓ |  | The policy thresholds from §5 |
| agents.status | text | ✓ | `active`, `paused` or `removed` | `resting` and `away` are derived states, not stored |
| agent_[runs.id](http://runs.id) / session_id / agent_id | text | ✓ | Partial UNIQUE on `(session_id, agent_id)` WHERE status is `eligible`, `context_built`, `generating` or `validated` | Allows only one live run per session |
| agent_runs.trigger_kind | text | ✓ | `message` or `nudge` |  |
| agent_runs.trigger_seq / context_seq | bigint | ✓ |  | `context_seq` is the last `seq` the run read, used for the staleness check |
| agent_runs.status / skip_reason / error_code | text | ✓ / – / – |  |  |
| agent_[runs.lease](http://runs.lease)_owner / lease_expires_at / attempts | text / timestamptz / int | – |  | The lease is 30 s and is renewed while the run is working |
| agent_runs.model / input_tokens / output_tokens / latency_ms | mixed | – |  |  |
| agent_runs.output_message_id | text | – | FK messages |  |
| session_summaries | row | ✓ | Index on `(session_id, agent_id, created_at desc)` | Fields: `covers_through_seq`, `summary`, `token_count`, `model`. The latest row wins. |
| agent_budgets | row | ✓ | PK `(session_id, agent_id)` | Fields: `capacity_tokens`, `used_tokens`, `window_seconds`, `window_started_at`. **Fixed window (D-019):** when `now >= window_started_at + window_seconds` the row resets on the next read (`used_tokens = 0`, `window_started_at` advances to the current window boundary). There is no continuous or sliding refill. |
| audit_events | row | ✓ | Index on `(session_id, created_at)` | Fields: `actor_id`, `action`, `target`, `payload`. Records membership and agent configuration changes. |
### Connection registry
- **Redis key structure:**
	- `conn:{connection_id}` is a hash.
	- `user_conns:{user_id}` is a sorted set whose members are `connection_id`s, scored by expiry time.
	- `gw_alive:{gateway_id}` marks a live gateway.
- **Value format:** the hash holds `{user_id, device_id, gateway_id, connected_at, last_heartbeat}`.
- **TTL and refresh interval:** the TTL is 60 s, refreshed on each heartbeat every 20 s. `gw_alive` has a 30 s TTL.
- **Heartbeat behavior:** three missed heartbeats means the gateway closes the socket and the entry expires.
- **Cleanup behavior:** on a clean close, the gateway deletes the entry. When the chat service reads a user's connections, it drops expired members with `ZREMRANGEBYSCORE`. The chat service skips any gateway whose `gw_alive` key has expired.
- **Multi-device behavior:** each device has its own entry, and every device receives every fan-out. ✓✓ counts once any device of a user has acked.
- **Stale-entry handling:** a push to a dead socket does nothing on the gateway. The client recovers the message through its cursor.
- **Other Redis state:** the `gw:{id}` pub/sub channels, the `agent:triggers` stream, the `agent:timers` sorted set (nudges), and the `rl:*` rate-limit counters.
### Data lifecycle
- **Retention policy:** indefinite for the MVP.
- **Deletion and redaction behavior:** redaction sets `body` to null and fills `redacted_at`. The `seq` is kept so ordering stays intact.
- **Session export requirements:** a JSON dump of the session's messages in `seq` order.
- **Backup and restore expectations:**
	- Postgres uses managed daily snapshots plus point-in-time recovery, with an RPO of 5 min or less.
	- Redis is ephemeral and needs no backup. Reconnects rebuild the registry, and nudge timers are re-armed from `last_human_message_at`.
- **Migration strategy:** Alembic, with expand-then-contract migrations.
### Open decisions
- None.
## 5. Agent Orchestration and Participation Policy
### Agent identity and permissions
- **What the agent represents:** itself, Huddle AI (or its configured name). It does not represent any user.
- **User controls:**
	- In the MVP, any member can pause, resume or rename the agent.
	- From phase 3, only the owner can configure or remove it.
- **Session access:** only sessions where the agent has an active membership.
- **Tools:** one only, the `submit_message` tool, which goes to the chat service. There are no external tools in v1.
### Agent wake-up triggers
- **Message triggers:** every committed *human* message enqueues a trigger. Messages from the agent itself never do.
- **Debounce:** triggers are coalesced. The orchestrator waits for 3 s of quiet, capped at 8 s, then evaluates the whole batch once *(tunable)*. This imitates a person reading before replying.
- **Nudge timer:** each human message resets `agent:timers[session] = now + nudge_delay`. See the re-engagement rule below.
- **Duplicate prevention:**
	- Only one live run per session is allowed, enforced by a lease and the partial unique index.
	- Triggers that arrive during a run set a `dirty` flag. When the run ends, the orchestrator re-evaluates once against the latest `seq`.
### Speak / do-not-speak policy
The policy has two stages:
1. **Deterministic gate.** Cheap, rule-based checks.
2. **Decider.** A small model that returns `{speak, reason}`.
Only if the decider says yes does the writer model compose the message.
| Rule | Policy *(tunable)* |
|---|---|
| Relevance | The decider speaks only when it can add something new: a missing fact, a different angle, a summary of a tangle, or an answer to a question nobody has answered. |
| Cooldown | At least 20 s since the agent's last message. Bypassed (D-021) when a human reply engages the agent's last message, and when the only human in the session is the one talking to it (N = 2). |
| Participation share | Over the last 30 messages, the agent may speak only if its share **including the message it is about to post** is at most 1.25 × 1/N (an agent that has not spoken in the window is never over).  If its share falls below 0.5 × 1/N, the decider is nudged toward speaking. N counts all active participants, including agents. This rule enforces the PRD's primary metric. |
| Conversation interruption | The agent never speaks during a burst of messages; the debounce handles this. Before committing, a stale check runs (see the run lifecycle). |
| Direct mentions | @mentions are out of scope. If the agent's name appears in a message, or a question is clearly aimed at it, the cooldown and share cap are bypassed. The consecutive-message cap still applies. |
| Silence after low-value turns | The decider skips greetings, acknowledgements and small talk between humans; a batch of only short greetings or acknowledgements (at most four words from a closed set, no question, no mention) is skipped by the gate without a decider call (D-021). If one of the agent's messages gets no human engagement within the next 3 messages, its cooldown doubles, up to a 5 min cap; the doubling applies only while the agent's share is at least 1/N (D-021). |
| Maximum consecutive agent messages | 1. The agent never posts twice without a human message in between. This includes nudges. |
| Session inactivity (re-engagement nudge) | After a lull with no human messages, the agent is **encouraged** to post one message that draws people back. The lull is `nudge_delay`, which defaults to 20 min and is configurable from 10 to 30 min. Conditions:<br>  • **Once per lull.** Only a new human message re-arms the nudge.<br>  • **Only if the conversation is still open,** as judged by the decider: an unanswered question, a pending decision, or an idea nobody followed up on. Signs of closure ("sounds good", "thanks, bye", a decision that was made) cancel the nudge.<br>  • **Not if the agent was the last speaker.**<br>  • **Not if the budget can't cover a turn.** |
| Length | The prompt asks for at most 3 sentences or about 600 characters. Validation rejects anything over 1,000 characters, regenerates once, then discards. |
| Budget | No evaluation runs if the bucket holds less than one full turn (decider plus writer). The agent's state becomes `resting` (see Cost and latency budgets). |
### Agent run lifecycle
```plain text
triggered → eligible → context-built → generating → validated → committed → observed
```
- **Timeouts:** 10 s for the decider and 45 s for the writer, sized for local hardware *(tunable)*.
- **Retries:** one retry on a transient error, using the fallback model. A run that still fails ends as `failed`.
- **Lease:** 30 s, renewed while the run is working. If a worker crashes, the lease expires and another worker takes over, rerunning from a fresh context.
- **Duplicate runs:** the partial unique index allows one live run per session. The commit is idempotent because `client_msg_id = run_id`.
- **Stale check before commit:** if new human messages arrived after `context_seq`, the run re-plans once with them. If it is still stale, it ends as `superseded`.
- **Cancellation:** a run is cancelled if the agent is paused or removed, the session closes, or a nudge run sees a human message arrive.
- **Observed:** token usage, cost and latency are written to the run, and the tokens are debited from the budget.
### Context and memory
- **Recent message window:** the newest messages, up to about 6k tokens, kept verbatim.
- **Older-message summarization:** when the verbatim window grows past about 12k tokens, the oldest 80% is folded into the rolling summary and the newest 20% stays verbatim (the PRD rule).
- **Summary storage and regeneration:** summaries are stored in `session_summaries` with `covers_through_seq`. Each one is regenerated incrementally from the previous summary plus the newly aged-out messages.
- **Session events included in context:** joins, leaves, pauses and nudges, as system lines. `sender_type` maps each message to a LangChain role, and the speaker's name is kept.
- **Agent private working memory:** none in v1, apart from the local context cache.
- **Context overflow behavior:** the oldest verbatim messages are summarized first. If the context is still too large, the summary itself is re-compressed.
- **Restart and recovery behavior:**
	- State is tracked locally for speed, but **the store is the source of truth**.
	- On a restart, or when a new agent joins an existing conversation (the multi-agent future), the agent rebuilds context the same way a new client catches up. It loads the latest summary and then only the recent window: messages after `covers_through_seq`, capped at the window size. It never loads the full history.
	- After rebuilding, it acks its cursor.
### Cost and latency budgets
- **Maximum response latency** (local hardware, *tunable*):
	- Decider p95 under 3 s.
	- Trigger-to-message p95 under 20 s, measured after the debounce.
- **Token budget per turn:**
	- Decider: at most 4k tokens in and 50 out. The default input cap is **700** tokens (D-021).
	- Writer: at most 12k tokens in and 300 out. The default input cap is **2.5k** tokens (D-021).
	- "One full turn" for the resting threshold is the defaults' sum (700 + 50 + 2,500 + 300 tokens).
- **Token budget per session:** a **fixed-window token budget** for each session and agent, measured in **tokens, never dollars** (D-016, D-019). The models run locally at no cost, so the budget's job is to keep the local machine responsive and the agent's behavior predictable. The default capacity is 500k tokens per 24 h window *(tunable)*. Windows are fixed and aligned (window k covers `[k*window_seconds, (k+1)*window_seconds)` from the UTC epoch); at each window boundary usage resets to zero. It does not refill gradually and is not a sliding window. Every model call (decider, writer and summarizer) adds its tokens to `used_tokens`.
	- **Resting:** when the tokens left in the current window are fewer than one full turn, the agent is `resting` until the window ends.
	- **Transparency:** users always see the budget, **as a percentage used**, not raw tokens.
		- The details panel shows a meter with "{used_pct}% used" and "Refills at {time}" (the window end).
		- When the agent is resting, the header shows "Huddle AI is resting · back at \{time\}". `{time}` is the end of the current window.
		- `agent.status` events carry `budget {used_pct, refills_at}` (`used_pct` is an integer 0 to 100; `refills_at` is the window end).
- **Model selection and fallback policy.** This is a prototype with **no model spend**, so the models are free and local:
	- **Runtime:** Ollama on the host machine, called through LangChain's `ChatOllama`.
	- **Decider, summarizer and safety check:** a small open instruct model of about 3B parameters, such as Llama 3.2 3B.
	- **Writer:** an open model of about 8B parameters, such as Llama 3.1 8B.
	- **Context size:** `num_ctx` is set explicitly to 16k, because Ollama's default is much smaller and would silently cut off the prompt.
	- **Fallback:** an optional free-tier hosted endpoint, off by default. Free tiers have rate limits, and some use prompts for training, so check the terms before turning one on. With no fallback, the agent shows as `away` when Ollama is unreachable.
	- **Why not Claude:** a Claude Pro or Max subscription doesn't include API access (the API is billed separately), so it can't power the agent without new spend.
- **Retry limits:** one retry per model call. One re-plan per run.
### Open decisions
- None. Models and budget units are decided (D-015, D-016).
## 6. API and WebSocket Contracts
### HTTP endpoints
| Endpoint | Method | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| `/auth/guest` | POST | none | `{display_name}` | `{user_id, access_token, refresh_token}` | `VALIDATION` |
| `/auth/refresh` | POST | refresh token | none | New tokens | `TOKEN_EXPIRED` |
| `/sessions` | GET / POST (phase 3) | bearer | POST: `{title}` | A list of sessions / one session | `FORBIDDEN` |
| `/sessions/{id}` | GET | member | none | The session, its members, and the agent's status and budget | `NOT_MEMBER` |
| `/sessions/{id}/messages` | GET | member | `after_seq` or `before_seq`, `limit ≤ 200` | `{messages, has_more}` | `NOT_MEMBER` |
| `/sessions/{id}/messages` | POST | member | `{client_msg_id, body}` | `{message_id, seq}` | A fallback for when the socket is down; idempotent like the socket path |
| `/sessions/{id}/members` | POST / DELETE (phase 3) | owner | `{user_id}` | The membership | `FORBIDDEN` |
| `/sessions/{id}/agent` | GET / PATCH / DELETE (phase 3) | member / owner | PATCH: `{status, name}` | The agent, including `budget` | `FORBIDDEN` |
### WebSocket message envelope
```json
{
  "v": 1,
  "type": "message.created",
  "event_id": "evt_789",
  "trace_id": "00-…",
  "session_id": "sess_123",
  "sequence": 42,
  "message": {
    "id": "msg_456",
    "client_msg_id": "0192…",
    "sender_type": "user",
    "sender_id": "user_123",
    "kind": "text",
    "body": "...",
    "created_at": "2026-09-29T22:00:00Z"
  }
}
```
**Client-to-server events:**
- `auth {token, device_id}`
- `subscribe {session_id, after_seq}`
- `message.send {client_msg_id, body}`
- `ack {up_to_seq}`
- `read {up_to_seq}`
- `ping`
**Server-to-client events:**
- `auth.ok`
- `message.ack {client_msg_id, message_id, seq}` (✓)
- `message.created`
- `sync.batch {messages, has_more}`
- `receipt.updated {cursors: {participant_id: last_delivered_seq}}`
- `agent.status {state: following|writing|paused|resting|away, budget {used_pct, refills_at}}`
- `member.joined`, `member.left`
- `session.updated`
- `server.draining {reconnect_after_ms}`
- `error`
- `pong`
### Connection protocol
- **Authentication during connection setup:** the `auth` frame must arrive within 5 s of connecting, or the socket closes. If the token expires mid-connection, the server sends `TOKEN_EXPIRED` and the client re-authenticates on the same socket.
- **Authorization checks:** membership is checked on `subscribe` and again inside every send transaction.
- **Heartbeats:** a ping every 20 s. Three misses closes the socket.
- **Client acknowledgements:** the client sends `ack` after rendering. Acks are cumulative.
- **Reconnect behavior:** backoff, then `subscribe` with `after_seq`, then dedupe the overlap. See §2.
- **Protocol versioning:** the envelope carries `v`. An unsupported version gets `PROTOCOL_UNSUPPORTED`.
- **Backpressure:** each connection's outbound queue holds at most 500 events or 1 MB. On overflow the server closes with code 4008 ("slow consumer"), and the client reconnects and syncs.
- **Rate limits:** 10 messages per 10 s per user per session, and 5 connections per user.
### Error model
Every error has a stable `code`, a `retryable` flag, an optional `retry_after_ms`, and the `client_msg_id` it refers to, if any.
| Code | Retryable |
|---|---|
| `UNAUTHENTICATED` | no |
| `TOKEN_EXPIRED` | after refreshing the token |
| `NOT_MEMBER` | no |
| `SESSION_CLOSED` | no |
| `MESSAGE_TOO_LONG` | no |
| `RATE_LIMITED` | yes |
| `PERSISTENCE_UNAVAILABLE` | yes |
| `PROTOCOL_UNSUPPORTED` | no |
| `INTERNAL` | yes |
User-facing copy comes from the Huddle voice, for example "Didn't send. Retry". Every error is logged with its `trace_id`.
### Open decisions
- None.
## 7. Failure Handling and Concurrency
### Failure scenarios
| Scenario | Expected behavior | Recovery | Observability |
|---|---|---|---|
| Gateway crash during delivery | Its sockets drop. The messages are already persisted. | Clients back off, reconnect to another gateway and sync from their cursor. Registry entries expire. | Gateway-down alert; spike in reconnects |
| Database write succeeds but delivery fails | The sender has ✓; recipients don't have the message yet. | The next message's `seq` gap, or a reconnect, triggers a fetch. | `fanout_publish_errors` |
| Delivery succeeds but acknowledgement is lost | ✓✓ is delayed. | The next ack, or a reconnect, advances the cursor. Cursors only move forward. | Receipt lag |
| Stale Redis connection | A push goes to a dead socket and does nothing. | TTL expiry plus lazy cleanup of the sorted set. | Stale-hit counter |
| Agent worker crashes mid-generation | The run's lease expires. | Another worker takes over and rebuilds context from the store. The commit is idempotent (`client_msg_id = run_id`). | `agent_run_timed_out` |
| Model response arrives after session closure | `submit_message` rejects it with `SESSION_CLOSED`. | The run is marked `cancelled`, and its tokens are still debited. | Run status counts |
| Duplicate agent trigger | Blocked by the lease or the unique index; sets the `dirty` flag. | One re-evaluation after the current run. | `trigger_coalesced` |
| Postgres unavailable | Sends are rejected with `PERSISTENCE_UNAVAILABLE`. This chooses consistency over availability. | The client keeps its outbox and retries with the same `client_msg_id`. The composer shows the failure. | Page immediately |
| User reconnects from multiple devices | Every device receives every message. | Each device keeps its own cursor. | Connections per user |
| Member removed while a message is in flight | The membership check inside the transaction rejects the message. | The client shows "Didn't send", and the socket is unsubscribed. | An audit event |
| Nudge timer lost (Redis flush) | No nudge is sent for that lull. | On startup, the orchestrator re-arms timers from `last_human_message_at`. | `nudge_rearmed` |
### Concurrency control
- **Per-session ordering mechanism:** a row lock on `chat_sessions` while `last_seq` is incremented.
- **Agent run lease or lock:** a 30 s lease, with the partial unique index as a backstop.
- **Duplicate work prevention:** idempotent `submit_message`, coalesced triggers, and one live run per session.
- **Cancellation semantics:** cooperative. The orchestrator checks for cancellation before generating, and again before committing.
- **Transaction boundaries:** the membership check, the sequence increment and the insert happen in one transaction. Fan-out and triggers happen after commit.
- **Queue and retry behavior:**
	- Triggers go through a Redis Stream consumer group, with a pending-entry reclaim after 60 s.
	- A trigger lost between commit and `XADD` only delays the agent until the next message, because every run reads the store from its last processed `seq`.
	- A transactional outbox is deferred to later.
### Backpressure
- **What happens when a client is slow?** It is disconnected (code 4008), then syncs on reconnect.
- **What happens when a gateway is overloaded?** It refuses new connections at its cap, and the load balancer routes them to other gateways.
- **What happens when the agent queue grows?** Only the latest trigger per session matters. If queue lag exceeds 30 s, stale triggers are skipped.
### Open decisions
- None.
## 8. Security and Authorization
### Authentication
- **User authentication method:** a guest token issued from a display name for the MVP. Email magic link or OAuth in phase 3.
- **WebSocket authentication:** the token goes in the first frame.
- **Token expiration and refresh:** the access token is a JWT that lasts 15 min. The refresh token lasts 30 days and rotates on each use.
- **Device/session identity:** `device_id` is a random UUID stored on the client.
### Authorization
- **Who can create sessions?** Any authenticated user (phase 3).
- **Who can invite or remove members?** The owner.
- **Who can change agent settings?**
	- Pause and resume: any member.
	- Configure and remove: the owner.
- **How are permissions checked on every message?** Inside the send transaction, the chat service checks that the sender's membership status is `active`.
### Trust boundaries
- **Client → gateway:** the server ignores any `sender_id`, `seq` or timestamp supplied by the client.
- **Gateway → internal services:** the gateway stamps the authenticated identity. Internal services are reachable only on the private network, with a service token.
- **User content → agent context:**
	- All chat content is untrusted data, wrapped with speaker labels.
	- The system prompt states that chat content cannot change the agent's instructions.
- **Agent → tools or external services:**
	- Only the `submit_message` tool is available.
	- The agent passes through the same validation as a human sender.
	- There is no external access in v1.
- **Service → database and Redis:** least-privilege credentials, Redis AUTH, and neither is publicly reachable.
### Abuse and safety controls
- **Rate limiting:** as set out in §6.
- **Spam prevention:**
	- Messages are limited to 4,000 characters.
	- Each user may have at most 5 connections.
- **Message moderation:**
	- Agent output passes a local safety check before commit, such as Llama Guard running in Ollama, so it costs nothing.
	- Human messages are not moderated in v1, on the assumption of small trusted groups (D-011).
- **Prompt injection handling:** the trust-boundary rules above, and a tool surface limited to `submit_message`.
- **Agent name:** any member can rename the agent and the name enters its system prompt, so it is limited to 1-40 letters, digits, spaces and `. ' -`, and generic labels ("Assistant", "Bot") are rejected (independent review, 2026-09-30).
- **Tool allowlists:** `submit_message` is the only tool.
- **Audit logging:** `audit_events` records membership changes, agent configuration changes, and pause and resume.
- **Data retention and deletion:** see §4.
- **Rendering:** clients render plain text and code blocks only; no raw HTML.
### Open decisions
- None.
## 9. Observability and Evaluation
### Metrics
| Area | Metric | Definition | Target / threshold *(tunable)* |
|---|---|---|---|
| Messaging | Ingest latency | From the gateway receiving a message to the commit ack | p95 \< 100 ms |
| Messaging | Fan-out latency | From commit to the message being written to recipients' sockets | p95 \< 150 ms; send-to-render end to end \< 300 ms |
| Messaging | Delivery failure rate | The share of messages where an online recipient didn't ack within 5 s | \< 0.1% |
| Reliability | Reconnect rate | Reconnects per connection-hour | Alert at 3× baseline |
| Agent | Participation share | Agent messages ÷ all messages, per session per day | 1/N ± 30% |
| Agent | Response latency | From trigger to committed message | p95 under 20 s on local hardware |
| Agent | Model error rate | Failed LLM calls ÷ all LLM calls | \< 2% |
| Agent | Token usage | Per run and per session-day; time spent resting | Resting under 10% of active hours |
| Agent | Speak rate | Speak decisions ÷ evaluations | Tracked only |
| Agent | Nudge revival rate | The share of nudges followed by a human message within 30 min | Baseline to be set during beta |
| Product | Messages per session | Messages in the first 24 h after the session is created | Baseline to be set during beta |
### Logging and tracing
- **Correlation ID format:** a W3C `traceparent`, carried through the gateway, the chat service, Redis payloads and agent runs.
- **Required structured log fields:** `ts`, `service`, `trace_id`, `session_id`, `actor_id`, `message_id`, `seq`, `event`, `latency_ms`.
- **Sensitive data redaction:** message bodies are never written to application logs. Prompts and completions go only to LangChain tracing, which keeps them for 30 days behind access controls.
- **Trace boundaries:** one trace per message send. One trace per agent run, linked to the message that triggered it.
- **Sampling policy:** 100% of errors and agent runs; 10% of message traces.
### Agent evaluations
- **Relevance, timing, participation balance and safety:** see the table in §11.
- **Helpfulness:** a weekly LLM-as-judge pass over a sample of agent messages, rated 1 to 5.
- **Token efficiency:** tokens spent per message the agent actually sends.
- **User engagement after agent messages:** the share of agent messages that get a human reply within 2 min.
- **Nudges:** the revival rate defined above.
### Dashboards and alerts
- **Which dashboards are required?** Messaging health, agent behavior (share, speak rate, nudges, resting) and token usage.
- **Which thresholds page someone?**
	- Pages someone: Postgres down, ingest error rate above 1% for 5 min, or zero healthy gateways.
	- Opens a ticket: agent share out of band for a day, or unusual budget burn.
- **Who owns each alert?** The project owner, for now.
### Open decisions
- None.
## 10. UI States and User Experience
All copy and visuals follow [Design System](../../design/README.md), which is the source of truth.
### Core flows
- **Create a session:** phase 3. In the MVP the session already exists.
- **Invite a user:** phase 3.
- **Join a session:** enter a display name, receive a guest token, subscribe with `after_seq=0`, and load the last window of messages.
- **Send a message:** the bubble shows its sending state, then ✓, ✓✓, and the amber ✓.
- **Reconnect after a network interruption:** the composer shows "Reconnecting…", then the client syncs from its cursor and the composer re-enables.
- **Add, pause, configure, or remove the agent:** pause and resume are available in the MVP from the session details panel, which also shows the budget meter. Configure and remove come in phase 3.
- **Leave a session:** phase 3.
### UI states
| State | User sees | Available actions | Backend condition |
|---|---|---|---|
| Empty session | "Nobody's said anything yet. Start the huddle." with "Huddle AI is following along" | Send | `last_seq` = 0 |
| Connecting | A skeleton thread; the composer is disabled | None | Before `auth.ok` and the first sync |
| Connected | The live thread, with the success dot | All | Socket live and the cursor caught up |
| Reconnecting | Messages stay readable; the composer shows "Reconnecting…" and is disabled | Read | The backoff loop is running |
| Sending | The bubble's sending state, then ✓, ✓✓, and the amber ✓ | None | Waiting for `message.ack` |
| Delivery failed | "Didn't send. Retry" in the danger colour | Retry, which reuses the same `client_msg_id` | An error, or the ack timed out |
| Agent writing | Amber dots and "Huddle AI is writing". The word "thinking" is never used. | Send | The writer stage only, never during the decider. Otherwise the indicator would often appear and then vanish without a message. |
| Agent paused | "Huddle AI is paused" in muted text | Resume | `agents.status = paused` |
| Agent resting (budget) | "Huddle AI is resting · back at \{time\}" in muted text; the budget meter is full (100% used) | View the budget | Fewer than one full turn of tokens are left in the current window |
| Agent unavailable | "Huddle AI is away right now" in muted text | None | Ollama is unreachable, and the optional fallback is off or also failed |
| Session closed | A read-only thread; a system note replaces the composer | Export | `status = closed` |
### Agent presentation
- **Agent name and avatar:** "Huddle AI" (or its configured name) with the amber circle avatar. It is never called "Assistant" or "Bot".
- **How agent messages are labelled:** the bubble has the same shape and size as everyone else's, with an amber tint. There is no extra badge.
- **Typing or thinking indicators:** the AgentIndicator shows "is following along" or "is writing".
- **User controls:** pause and resume, plus the budget meter (percentage used) with its refill time.
- **Visibility of agent errors or unavailable states:** errors never appear in the thread. They show only as the paused, resting or away state.
### Open decisions
- None.
## 11. Testing Strategy
### Unit tests
- **Sequence assignment:** gapless under concurrent sends; the counter rolls back with the transaction.
- **Idempotency:** a repeated `client_msg_id` returns the same `seq`, including for agent `run_id`s.
- **Authorization:** removed members are rejected inside the transaction.
- **Agent eligibility:** every gate rule, including the nudge conditions and the budget threshold.
- **Context construction:** window size, summary folding at 80%, mapping `sender_type` to LangChain roles.
- **Retry and timeout behavior:** the fallback model, lease expiry, superseded runs.
- **Token budget:** fixed-window reset at the boundary, `used_pct` rounding, and the `refills_at` calculation.
### Integration tests
- **Postgres persistence:** replay pagination and the redaction path.
- **Redis registry and TTL behavior:** expiry after missed heartbeats; sorted-set cleanup.
- **Gateway connections:** auth timeout, and closing with code 4008 on overflow.
- **Fan-out behavior:** multiple devices per user; skipping dead gateways.
- **Agent-to-chat-service integration:** the tool call goes through the same path; triggers and acks work.
- **Nudge timers:** re-armed after a Redis flush.
### End-to-end tests
- Multiple clients observe the same ordered conversation.
- Concurrent messages converge to one history.
- Gateway restart and client reconnect.
- Agent response appears through the normal message path.
- Member removal during an active conversation.
- An agent worker restarts mid-conversation and rebuilds context from the store (window only).
- The nudge fires once after a lull in an open conversation, and not at all after a closed one.
- An empty budget shows the resting state with the correct refill time.
### Agent evaluations
| Evaluation | Dataset / scenario | Metric | Pass criteria |
|---|---|---|---|
| Relevance | 200 replayed conversation snapshots, labelled speak or stay silent | Decider precision and recall | Precision ≥ 0.8 |
| Timing | Scripted bursts of rapid human messages | Rate of interrupting mid-burst | \< 5% |
| Participation balance | Simulated day-long sessions with N = 2–6 | Share against 1/N | Within ±30% |
| Nudge judgment | 100 lulls, labelled open or closed | Precision of nudging open conversations | ≥ 0.85; 0 nudges after clear closure |
| Safety | Prompt-injection and abuse conversations | Policy violations | 0 |
| Cost | A standard 500-message session | Tokens used and time spent resting | Within one bucket; never resting |
### Load and chaos testing
- **Expected concurrent sessions:** 1 for the MVP; 10k in phase 3.
- **Expected connections per session:** up to 10 for the MVP; 50k connections in total in phase 3.
- **Messages per second:** 2 at peak for the MVP; 500 in phase 3.
- **Agent generation concurrency:** 200 concurrent runs in phase 3.
- **Gateway failure tests:** kill a gateway; a reconnect-storm test to validate backoff and draining (the 500k-connection scenario).
- **Database and Redis failure tests:** pause Postgres; flush Redis; kill an agent worker mid-generation.
## 12. Deployment, Scaling, and Operations
### Runtime topology
- **Environments:** local (docker-compose), staging and production.
- **Service replicas:** at least 2 each of gateways, chat service and agent workers.
- **Load balancing:** an L7 load balancer with websocket support.
- **WebSocket connection routing:** any gateway can take any connection. There are no sticky sessions, because the registry routes messages.
- **Queue or event infrastructure:** Redis pub/sub for fan-out; a Redis Stream for agent triggers; a Redis sorted set for nudge timers.
### Scaling plan
- **Gateway scaling:** horizontal, by connection count.
- **Chat service scaling:** stateless and horizontal. The per-session row lock caps throughput per session, which is fine for chat.
- **Agent worker scaling:** by queue lag. In the MVP there is one worker per local GPU, and model calls are serialized. Going past a handful of sessions means hosted models, which would reopen D-015.
- **Database scaling:** one primary plus a read replica for history. Shard by `session_id` later.
- **Redis scaling:** one primary plus a replica, moving to Redis Cluster later.
### Reliability targets
- **Availability target:** 99.5% for the MVP.
- **Recovery time objective:** 30 min.
- **Recovery point objective:** 5 min.
- **Maximum tolerable message loss:** zero for any message that received ✓.
### Release and migration strategy
- **Database migrations:** Alembic, expand then contract.
- **Protocol compatibility:** the envelope carries `v`, and the server supports N and N-1.
- **Rollbacks:** stateless services roll back to the previous image. Migrations are backward compatible.
- **Feature flags:** agent policy thresholds, the nudge on or off, and bucket capacity.
- **Model or prompt version rollout:** `prompt_version` is stored on the agent and on every run. New versions are canaried on a subset of sessions and compared on the §11 evaluations.
### Runbooks
| Runbook | Response |
|---|---|
| Gateway outage | Drain and replace the instance. Clients reconnect on their own. |
| Database outage | Fail over to the replica. Sends fail with retry until it recovers. |
| Redis outage | Restore Redis. Reconnects rebuild the registry, and nudge timers are re-armed. |
| Model runtime outage | Restart Ollama and check the models are pulled. If the free-tier fallback is configured, it takes over; otherwise the agent shows as "away". |
| Agent runaway behavior | Pause every agent with a global kill-switch flag, then inspect recent runs. |
| Elevated latency | Check Postgres lock waits on `chat_sessions` first, then gateway queue depth. |
### Open decisions
- None.
## Decision Log
| ID | Decision | Options considered | Rationale | Owner | Date | Status |
|---|---|---|---|---|---|---|
| D-001 | Gapless per-session `seq` from `chat_sessions.last_seq` | Postgres SEQUENCE; Redis INCR | Atomic with the insert, and gapless so clients can detect gaps | Owner | 2026-09-29 | Accepted |
| D-002 | Effectively-once delivery: idempotent `submit_message` plus client dedupe | At-most-once | No lost and no visible duplicate messages | Owner | 2026-09-29 | Accepted |
| D-003 | Receipts as monotonic cursors per membership | A receipt row per message | Cheap, and matches the cursor model | Owner | 2026-09-29 | Accepted |
| D-004 | The agent calls the chat service directly as a tool | The agent as a websocket client of a gateway | One authoritative path, with no socket inside the workers | Owner | 2026-09-29 | Accepted |
| D-005 | Two-stage agent: gate plus decider, then writer | One call that can reply "no reply" | Most evaluations stay cheap | Owner | 2026-09-29 | Accepted |
| D-006 | 1/N share enforced as a rolling-window cap and nudge | Measuring only | Enforces the PRD's primary metric | Owner | 2026-09-29 | Accepted |
| D-007 | Pub/sub for fan-out; Streams for triggers | Kafka; a single mechanism for both | Fan-out can be lossy thanks to cursors; triggers should not be lossy | Owner | 2026-09-29 | Accepted |
| D-008 | Reject sends when Postgres is down | Buffer sends server-side | Consistency over availability | Owner | 2026-09-29 | Accepted |
| D-009 | Agent tools limited to `submit_message` in v1 | External tools | Smallest prompt-injection blast radius | Owner | 2026-09-29 | Accepted |
| D-010 | Guest auth for the MVP | Full sign-up | The single-session MVP doesn't need accounts | Owner | 2026-09-29 | Accepted |
| D-011 | No moderation of human messages in v1 | Moderate everything | Small trusted groups; revisit in phase 3 | Owner | 2026-09-29 | Accepted |
| D-012 | The store is the source of truth for agent context; rebuild from the summary plus the last window | Agent-local state only | Survives restarts, and lets new agents join a conversation | Owner | 2026-09-29 | Accepted |
| D-013 | One re-engagement nudge per lull, only if the conversation is open | Stay silent when inactive; repeated nudges | Improves retention without nagging | Owner | 2026-09-29 | Accepted |
| D-014 | A visible per-session token bucket with a refill time | A hidden cost cap | Users understand why the agent is resting | Owner | 2026-09-29 | Accepted |
| D-015 | Free local models: Ollama with a \~3B decider and an \~8B writer; optional free-tier fallback | Claude API; paid hosted models | No model spend for the prototype. A Claude subscription doesn't include API access. | Owner | 2026-09-29 | Accepted |
| D-016 | The budget is measured in tokens, not dollars | A dollar cap | No model spend, so tokens are the real constraint | Owner | 2026-09-29 | Accepted |
| D-017 | ✓✓ fires when any one other human has received the message | Wait for every member | Offline members don't hold the sender's receipt back | Owner | 2026-09-29 | Accepted |
| D-018 | Push and email notifications are out of MVP scope | Ship them with the nudge | Keeps the MVP small; nudges reach people who have the app open | Owner | 2026-09-29 | Accepted |
| D-019 | The budget is a fixed-window token budget shown to users as a percentage used, with a refill time at the window end. Supersedes the continuous-refill bucket wording in D-014 and the "312k of 500k tokens" display. | Continuous or sliding refill; showing raw tokens | Simpler to implement, explain and test; the UI needs only a percentage | Owner | 2026-09-30 | Accepted |
| D-021 | Tuning to meet the §11 evals: share is checked after speaking; cooldown doubling only at or above fair share; cooldown bypass for replies that engage the agent and in 1:1 chats; gate-level skip of trivial batches; smaller default prompt caps (decider 700, writer 2.5k tokens) and a matching resting threshold; engagement matching uses light stemming. "500-message session" means 500 messages in total, humans and agent. | Leave the evals failing; raise the budget capacity | Meets N = 2-6 within ±30% and a 500-message session in one window without resting, using only *(tunable)* values and clarifications | Owner | 2026-09-30 | Accepted |
| D-020 | A paused, resting or away agent does not advance its read cursor, so messages sent meanwhile stay at ✓✓ until it catches up. The AI-state, error-line and system-note wording proposed in the design system is final for the MVP. | Advance the cursor while inactive | Keeps the amber tick honest: it means the agent read the message | Owner | 2026-09-30 | Accepted |
## Open Questions
- None open. Q1 → D-017, Q2 → D-015, Q3 → D-016, Q4 → D-018 (all resolved 2026-09-29).
## Final Review Checklist
- [x] Domain model is explicit
- [x] Message ordering and reconnect behavior are defined
- [x] Service boundaries and contracts are documented
- [x] Database schemas and indexes are documented
- [x] Agent participation policy is implementable
- [x] API and WebSocket contracts are complete
- [x] Failure and concurrency behavior is defined
- [x] Security model is documented
- [x] Observability and evaluation metrics are measurable
- [x] UI states cover success and failure paths
- [x] Tests cover multi-client consistency
- [x] Deployment and operational runbooks exist