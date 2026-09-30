# Technical Design Document

> **Source of truth (high level).** Mirrored from Notion (last edited 2026-09-29): https://www.notion.so/3ea7d720d67f8000962dc439ba6cab06
> Implementation-level contracts, schemas, policies and failure behaviour are in [deep-dive.md](deep-dive.md), which is more detailed and takes precedence where they differ.
> The Notion page embeds a **system architecture diagram** (image) that is not reproduced here. The component table below and Deep Dive §3 describe the same architecture.
> The Notion page's Algorithms, Pseudocode, Test Cases, Bug Fixes and Conclusion sections are empty templates. The inline databases (Chat Message Store, Session Memberships, Connection Registry, User Interface, Updates) are early schema sketches, superseded by Deep Dive §4.

## Project Overview

Ship a lightweight chat application that hosts a real-time chat session with 2 or more users and chat bots. Users can create sessions, add other users, and add AI models as virtual chat participants. Users chat with users as they normally would, with an autonomous AI agent included. The agent is not a prompted model: it sits in the conversation and adds real thoughts when it feels like it, just like a human would.

### Key objectives

- A working real-time chat app; the MVP handles a single session with multiple users.
- Add the agent as a chat participant, harnessed to act like another user.
- Multiple saved chats as the final app structure (with user sign-up, chat creation and invites).

## Requirements

### Functional

- Users connect to a hosted chat thread.
- A user can send a message; when they do, all other users in the chat see it immediately.
- The AI agent is a pre-determined user in the chat and absorbs chat history as context.
- The AI agent chooses when to send a message, constructs it and sends it.
- The agent may send one re-engagement message after a long lull (default 20 min, configurable 10 to 30), and only if the conversation still seems open.
- Agent usage is capped by a per-session token budget that users can see, including when it refills. The models run locally at no cost.
- An agent that restarts, or joins an existing conversation, rebuilds its context from the message store (latest summary plus recent window only).

### Non-functional

- **Consistency over availability.** With an autonomous bot sending messages, all users must see the same state of the chat; otherwise users diverge.
- **Low latency.** Users see the latest message immediately (at least for v1 with small groups).
- **Observability** is a must. We must be able to track metrics on the chat and agent and how they affect each other, to optimize agent behaviour.

## System Architecture

(Diagram not reproduced; see note above.)

### Component details

| Name | Description | Technology | Interactions |
|---|---|---|---|
| Connection gateways | Simple handler of WebSocket connections between client and service. Authenticates clients, sends heartbeats, moves bytes in both directions. | Python FastAPI | On connect, adds a record to the connection registry. Sends heartbeats to detect dead connections; after several misses, closes the connection and cleans the registry. |
| Registry | Connection registry recording which user devices are connected, to which gateway, with a TTL. | Redis | Record added by the gateway on new connection, removed when the gateway closes it. The chat service reads it to know which gateways to push to, rather than fanning out to all gateways. |
| Chat service | The business logic: validates, persists, then delivers. | Pure Python | Exposes one idempotent `submit_message` (keyed on `client_msg_id`) used by both the gateway and the agent. Validates, checks membership, then persists in sequence order in one transaction (assigning the next gapless per-session `seq`). After commit, looks up recipients in the target session, uses the registry to find their connected devices and gateways, pushes to those gateways, and enqueues an agent trigger. Also tracks receipt cursors (the three checkmarks) and the agent's budget. |
| Message store | Records of messages per chat session, ordered by sequence number. Each record carries a `sender_type` (user, agent or system) so the orchestrator can map it to a LangChain message role, and a `client_msg_id` for idempotency. One DB is fine at this scale; long term, shard by chat session. | PostgreSQL | |
| Chat session store | Index of chat session users for easy lookup. When a user is added to a session, a record with the user id and session id is added. Also stores per-user, per-session preferences. | PostgreSQL | |
| Chat agent orchestrator | Owns the LLM loop, context management, and LLM and tool calls. Acts as an AI chat participant that tracks chat history locally and derives context from it. When it decides to speak, it calls the chat service's `submit_message` directly as a tool, so its messages take the same authoritative path as human messages. It never connects to a gateway. The message store is the source of truth: on restart, or when a new agent joins, it rebuilds context from the latest summary plus the recent window only, the same way a new user catches up. | LangChain + Ollama (free local Llama models: ~3B decider, ~8B writer) | Manage context thoroughly: keep recent messages verbatim, summarize the oldest 80% once the window fills. Manage latency and cost with retry and context limits. Compute is capped by a per-session token bucket, measured in tokens, that users can see ("Huddle AI is resting · back at {time}"). Speak policy: debounce, cooldown, 1/N participation share, no two agent messages in a row, and one re-engagement nudge after a long lull if the conversation is still open. Observable logs and evals. |

## Interface Design

### User interface

Defined by the design system in `design/` (see [design-system.md](design-system.md)) and the UI states in Deep Dive §10.

### API

| Endpoint | Method | Request | Response | Description |
|---|---|---|---|---|
| `/auth/guest` | POST | `{display_name}` | `{user_id, access_token, refresh_token}` | MVP guest identity |
| `/auth/refresh` | POST | refresh token | new tokens | Rotates the refresh token |
| `/sessions/{id}` | GET | none | session, members, agent status and budget | Session details panel |
| `/sessions/{id}/messages` | GET | `after_seq` or `before_seq`, `limit` | `{messages, has_more}` | Catch-up after the cursor, and history paging |
| `/sessions/{id}/messages` | POST | `{client_msg_id, body}` | `{message_id, seq}` | Idempotent send; fallback when the socket is down |
| `/sessions/{id}/agent` | GET / PATCH | `{status, name}` | the agent, including budget | Pause, resume or rename the agent |
| `/ws` | WebSocket | `auth`, `subscribe`, `message.send`, `ack` | `message.ack`, `message.created`, `receipt.updated`, `agent.status`… | Real-time channel; full event list in Deep Dive §6 |

## Testing plan

Test strategy areas named in the Notion page: functional, performance, security. The detailed plan is Deep Dive §11.

## Future work

Multiple saved chats with sign-up, chat creation and invites (phase 3), multiple agents, @mentions, and the other items listed as out of scope in the PRD.
