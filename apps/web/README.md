# apps/web: the Huddle client

React 18 + Vite + TypeScript (strict) client for the v1 flow: guest join, one live thread with Huddle AI, three-tick receipts, AI states, budget, pause / resume / rename, connection states and export when a huddle ends. Built from the design system in [`design/`](../../design/) (imported unmodified) against the contracts in Deep Dive §2, §6 and §10.

## Run it

Needs Node 22.12+.

```sh
cd apps/web
npm ci
npm run dev:mock     # no backend needed: in-browser mock (see below), http://localhost:5173
npm run dev          # against real services: proxies /auth and /sessions to the chat service, /ws to the gateway
```

`npm run dev` proxies to `http://localhost:8001` (chat) and `ws://localhost:8000` (gateway), the ports in `deploy/compose.yaml`. Override the proxy targets with `HUDDLE_CHAT_URL` / `HUDDLE_GATEWAY_URL` in the environment. A production build talks to its own origin by default; set `VITE_HUDDLE_API_URL`, `VITE_HUDDLE_WS_URL` and `VITE_HUDDLE_SESSION_ID` (default `ses_main`) at build time to point elsewhere.

Checks (the same ones `scripts/gate.sh` and CI run):

```sh
npm run lint && npm run typecheck && npm test && npm run build
```

## Mock mode

`npm run dev:mock` runs Vite with `--mode mock`, which sets `VITE_HUDDLE_MOCK=1` (`.env.mock`). `src/main.tsx` then swaps the real `fetch` and `WebSocket` for [`src/mock/server.ts`](src/mock/server.ts), an in-browser `MockServer` that speaks the documented REST and WebSocket contracts: guest tokens (JWT-shaped, 15 min) with rotating refresh tokens, `subscribe` replay in pages with the last window on a fresh join, idempotent `message.send` keyed on `client_msg_id`, `receipt.updated` cursors, `agent.status` with `budget {used_pct, refills_at}`, system notes for joins, pauses and renames, `server.draining`, `TOKEN_EXPIRED`, rate limits and the error model.

It simulates two other people (Maya Chen, Jordan Reyes) who receive every message (✓✓) and sometimes reply, and the agent, which reads messages while it is following (amber ✓), sometimes writes a reply, and does not read while paused, resting or away (D-020).

- The **Mock backend** button (bottom left) opens controls for every state: network blip, offline for 8 s, gateway drain, a lost fan-out (to see gap recovery), a lost ack, rate-limited / database-down / not-member sends, token expiry, AI writing / paused / budget spent / away, a message from Maya, and ending the huddle.
- `?latency=800` slows the mock network so the connecting skeleton and the sending clock are easy to see.
- `window.huddleMock` is the server, for poking at it from the console.

The mock is dev-only: the branch that loads it is guarded by `import.meta.env.DEV`, so it is not in `npm run build` output. The same `MockServer` is the fake backend in the tests.

## Layout

| Path | What |
|---|---|
| `src/contracts.ts` | Wire types, hand-mirrored from `libs/contracts` (`ws.py`, `models.py`, `errors.py`). Change both in the same PR. |
| `src/core/` | Pure logic, no I/O: `thread.ts` (the message log reducer: ordering, dedupe, gaps, outbox, receipts), `timeline.ts` (grouping for the screen), `backoff.ts`, `errors.ts` (error model to bubble copy), `text.ts` (plain text and code only), `uuidv7.ts`. |
| `src/net/` | `api.ts` (REST), `auth.ts` (tokens), `connection.ts` (the WebSocket: auth, subscribe, heartbeat, backoff, draining). |
| `src/client.ts` | `HuddleClient`: ties them together behind one snapshot that React reads with `useSyncExternalStore`. |
| `src/ui/` | Screens composed from the design components (`design.ts` loads `design/tokens.css`, `bundle.css` and `bundle.js` as `design/docs/USAGE.md` describes). `viewModel.ts` maps the snapshot to the Deep Dive §10 UI states. |
| `src/mock/` | The dev-only mock backend and its controls. |

## How it follows the spec

- **Ordering (§2):** stored messages are keyed by `seq` and rendered in `seq` order. Unconfirmed sends (no `seq` yet) show after them until the server gives them one.
- **Dedupe (§2):** live `message.created`, `sync.batch` replays, catch-up pages and `message.ack` all merge through one function that drops anything whose `message_id` or `seq` is already held.
- **Gaps (§2):** `seq` is gapless, so holding 43 and receiving 45 fetches `GET /sessions/{id}/messages?after_seq=43` and merges the page the same way.
- **Outbox (§2, §7, §10):** each message gets a UUIDv7 `client_msg_id` that every retry reuses, so the server stores it once. A send waits 10 s for `message.ack`, then shows "Didn't send" with Retry. Sends in flight when the socket drops stay "sending" and go again (same id) after the reconnect replay. Retry is offered only for retryable errors.
- **Receipts (§2, D-017, D-020):** from `receipt.updated` cursors: ✓ when stored, ✓✓ once any other human's cursor reaches the message, the amber ✓ once the agent's does too. Every mark has a text label ("Sent", "Delivered", "Read by Huddle AI").
- **Connection (§6):** `auth {token, device_id}` first, then `subscribe {session_id, after_seq}`; `v: 1` on every frame, other versions ignored; `ping` every 20 s and a reconnect if nothing answers within 10 s; full-jitter exponential backoff (base 500 ms, cap 30 s) that resets once a replay completes; `server.draining` reconnects at `reconnect_after_ms`; `TOKEN_EXPIRED` refreshes and re-authenticates on the same socket; `UNAUTHENTICATED` (after one refresh) goes back to the join form; `PROTOCOL_UNSUPPORTED` stops and says the app is out of date. The client sends a cumulative `ack` after rendering and `read` while the page is visible.
- **Auth (§8):** the access token stays in memory; the refresh token is in `localStorage` so a returning guest skips the join form. Refresh tokens rotate and reuse revokes them, so refreshes are serialised: one in flight per tab, plus a `navigator.locks` lock across tabs.
- **Budget (D-019):** only "{n}% used" and "Refills at {time}". The bundled `BudgetMeter` still shows raw tokens, so the details panel passes the agent to `SessionDetails` without `budget` and renders `src/ui/BudgetMeter.tsx`, a local variant with the same markup and `hd-` classes.
- **Rendering (§8):** plain text, inline code and fenced code blocks, all as React text nodes. There is no HTML path (an ESLint rule forbids `dangerouslySetInnerHTML` and `innerHTML`).

### Spec gaps and how they were resolved

Recorded as R-007 in `docs/decisions.md`. Each is small and easy to change if the owner decides otherwise.

1. **Amber tick without ✓✓.** §2 derives ✓✓ and the amber tick independently from cursors, but §1's sender state machine is linear (stored → delivered → agent-seen) and `Receipt` draws the amber tick only on top of ✓✓. The client shows `agent-read` only once the message is also delivered, so ✓✓ never claims a human had it when only the AI did.
2. **How to fetch a gap.** §2 says "fetch 44" without naming the channel. The client uses the REST catch-up endpoint (`after_seq`), which is defined and idempotent, rather than re-sending `subscribe` on a live socket.
3. **"Stored last_seq".** The cursor lives in memory for the page's lifetime and is the `after_seq` on every reconnect. Messages aren't stored locally, so a reload subscribes with `after_seq=0` and gets the last window again (§10 "Join").
4. **The last window.** On a fresh join, if the first replay page starts above seq 1, history below it is treated as intentionally not loaded (not a gap) and "Load earlier messages" pages it in with `before_seq`.
5. **Failed-send copy for other codes.** `MessageBubble` has copy for four codes and decides Retry from the code. Retryable errors show "Didn't send" (or "Sending too fast") with Retry; `VALIDATION` shows the too-long copy; other non-retryable codes show "You're not in this huddle". Retry is never shown for a non-retryable error.
6. **Resend while offline.** §6 lists `POST /sessions/{id}/messages` as a fallback, but the design's reconnecting copy says messages will send when you're back, so the client waits for the socket instead of using the fallback.
7. **Copy the design doesn't cover:** "Couldn't join. Try again." under the join form, and "This version of Huddle is out of date. Reload the page to keep chatting." for `PROTOCOL_UNSUPPORTED`.
8. **Pause and resume PATCH values.** `{status: "paused"}` and `{status: "active"}` (`agents.status`, §4). Names are whitespace-collapsed and capped at 40 characters, as the chat service validates.
9. **System notes come from the log.** Pause, resume, rename and joins show as the server's `kind: "system"` messages (§4). The client never adds its own notes, so everyone sees the same thread.
