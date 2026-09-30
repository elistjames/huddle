/**
 * In-browser mock of the chat service REST API and the gateway WebSocket, speaking the documented
 * contracts (Deep Dive §2, §6; src/contracts.ts). Dev-only: it powers `npm run dev:mock` and
 * serves as the fake server in tests. It is not a spec of the real services' internals.
 *
 * It simulates two other people (Maya Chen, Jordan Reyes) who receive every message, and the
 * agent: it reads while following (amber tick), sometimes writes a reply, is paused / resting /
 * away on demand, and does not advance its cursor while inactive (D-020).
 */
import {
  MAX_BODY_CHARS,
  PROTOCOL_VERSION,
  RETRYABLE,
  type AgentState,
  type AgentView,
  type BudgetView,
  type ErrorBody,
  type ErrorCode,
  type MemberView,
  type Message,
  type ServerFrame,
  type ServerFrameInput,
  type SessionView,
  type TokenResponse,
} from "../contracts";
import type { SocketLike } from "../net/connection";

export const MOCK_SESSION_ID = "ses_main";
export const MOCK_AGENT_ID = "agt_huddle";
export const MOCK_WS_URL = "ws://mock.huddle/ws";
export const MOCK_PEOPLE = [
  { id: "usr_maya", name: "Maya Chen" },
  { id: "usr_jordan", name: "Jordan Reyes" },
] as const;

const WINDOW_SECONDS = 86_400;
const CAPACITY_TOKENS = 500_000;
const FULL_TURN_TOKENS = 4_000 + 50 + 12_000 + 300; // libs/contracts budget.py
const TOKENS_PER_REPLY = 30_000;
const RATE_LIMIT = 10; // per 10 s per user (Deep Dive §6)
const REPLAY_PAGE = 200;

const AGENT_LINES = [
  "The first two paragraphs say the same thing. Cutting the second gets the intro to about 60 words.",
  "2 works. The changelog link in the last paragraph still points at the old release, so worth fixing first.",
  "One open question from earlier: who is posting it on the team channel?",
  "If it helps, the numbers in the summary match the dashboard as of this morning.",
];
const PEOPLE_LINES = ["much better", "agreed", "on it", "shipping at 2?", "looks good to me"];

export interface MockServerOptions {
  /** One-way network latency in ms (default 40). */
  latencyMs?: number;
  /** Seed a short history (default true). */
  seed?: boolean;
  /** Simulated people ack every message and sometimes reply (default true). */
  people?: boolean;
  /** The simulated agent reads and sometimes replies (default true). */
  agent?: boolean;
  /** Access token lifetime (default 900 s, Deep Dive §8). */
  tokenTtlSec?: number;
  /** A fresh subscribe (after_seq = 0) replays only the last N messages (default 200). */
  windowSize?: number;
}

interface Conn {
  socket: MockSocket;
  userId: string | null;
  needsReauth: boolean;
  subscribed: boolean;
  authTimer: ReturnType<typeof setTimeout> | null;
}

function b64url(s: string): string {
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(s: string): string {
  const b = s.replace(/-/g, "+").replace(/_/g, "/");
  return atob(b.padEnd(b.length + ((4 - (b.length % 4)) % 4), "="));
}

function frame<T extends ServerFrameInput>(f: T): T & { v: number } {
  return { v: PROTOCOL_VERSION, ...f };
}

/** 1-40 chars after whitespace collapse, like the chat service; null when invalid. */
function validName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const n = value.replace(/\s+/g, " ").trim();
  return n.length >= 1 && n.length <= 40 ? n : null;
}

function err(code: ErrorCode, extra: Partial<ErrorBody> = {}): ErrorBody {
  return { code, retryable: RETRYABLE.has(code), message: "", ...extra };
}

type SubmitResult = { ok: true; message_id: string; seq: number } | { ok: false; error: ErrorBody };

export class MockServer {
  readonly sessionId = MOCK_SESSION_ID;
  title = "Launch post";
  status: "active" | "closed" = "active";
  readonly messages: Message[] = [];
  readonly cursors: Record<string, number> = {};
  readonly members = new Map<string, MemberView>();
  readonly agent = {
    id: MOCK_AGENT_ID,
    name: "Huddle AI",
    paused: false,
    away: false,
    writing: false,
    usedTokens: 42_000,
  };
  /** Every message.send / POST the server accepted or replayed, for idempotency assertions. */
  readonly submissions: { sender: string; client_msg_id: string; duplicate: boolean }[] = [];

  private readonly opts: Required<MockServerOptions>;
  private readonly conns = new Set<Conn>();
  private readonly refreshTokens = new Map<string, string>();
  private readonly idem = new Map<string, { message_id: string; seq: number }>();
  private readonly rate = new Map<string, number[]>();
  private failNext: ErrorCode[] = [];
  private dropFanouts = 0;
  private dropAcks = 0;
  private offlineUntil = 0;
  private tokensValidAfter = 0;
  private counter = 0;
  private humanCount = 0;
  private lastSender: string | null = null;
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();

  constructor(options: MockServerOptions = {}) {
    this.opts = {
      latencyMs: options.latencyMs ?? 40,
      seed: options.seed ?? true,
      people: options.people ?? true,
      agent: options.agent ?? true,
      tokenTtlSec: options.tokenTtlSec ?? 900,
      windowSize: options.windowSize ?? 200,
    };
    for (const p of MOCK_PEOPLE) this.addMember(p.id, "user", p.name);
    this.addMember(MOCK_AGENT_ID, "agent", this.agent.name);
    if (this.opts.seed) this.seedHistory();
  }

  get lastSeq(): number {
    return this.messages.length;
  }

  // ---- controls (MockControls panel and tests) -----------------------------------------

  /** A network blip: every socket drops (close 1006). Clients back off and resync. */
  dropConnections(): void {
    for (const c of [...this.conns]) this.closeConn(c, 1006);
  }

  /** Refuse connections for `ms` (and drop current ones): the client shows Reconnecting. */
  goOffline(ms: number): void {
    this.offlineUntil = Date.now() + ms;
    this.dropConnections();
  }

  /** `server.draining {reconnect_after_ms}` to every socket, then close them. */
  drain(reconnectAfterMs: number): void {
    for (const c of [...this.conns]) {
      c.socket.deliver(frame({ type: "server.draining", reconnect_after_ms: reconnectAfterMs }));
      this.later(() => {
        this.closeConn(c, 1001);
      }, 200);
    }
  }

  /** The next sends fail with these codes (e.g. RATE_LIMITED, PERSISTENCE_UNAVAILABLE). */
  failNextSend(...codes: ErrorCode[]): void {
    this.failNext.push(...codes);
  }

  /** Skip the fan-out of the next N messages (pub/sub is lossy): clients see a seq gap. */
  dropNextFanout(n = 1): void {
    this.dropFanouts += n;
  }

  /** Store the next N messages but lose their message.ack (the client times out and retries). */
  dropNextAck(n = 1): void {
    this.dropAcks += n;
  }

  /** Access tokens issued so far are now expired; live sockets get TOKEN_EXPIRED. */
  expireAccessTokens(): void {
    this.tokensValidAfter = Date.now() + 1;
    for (const c of this.conns) {
      if (c.userId) {
        c.needsReauth = true;
        c.socket.deliver(frame({ type: "error", error: err("TOKEN_EXPIRED") }));
      }
    }
  }

  setAgentAway(away: boolean): void {
    this.agent.away = away;
    this.broadcastAgent();
    if (!away) this.agentCatchUp();
  }

  exhaustBudget(): void {
    this.agent.usedTokens = CAPACITY_TOKENS;
    this.agent.writing = false;
    this.broadcastAgent();
  }

  refillBudget(): void {
    this.agent.usedTokens = 0;
    this.broadcastAgent();
    this.agentCatchUp();
  }

  setAgentPaused(paused: boolean, by = "Maya Chen"): void {
    if (this.agent.paused === paused) return;
    this.agent.paused = paused;
    this.agent.writing = false;
    this.system(`${by} ${paused ? "paused" : "resumed"} ${this.agent.name}`);
    this.broadcastAgent();
    if (!paused) this.agentCatchUp();
  }

  setAgentWriting(writing: boolean): void {
    this.agent.writing = writing;
    this.broadcastAgent();
  }

  endSession(): void {
    this.status = "closed";
    this.broadcast(frame({ type: "session.updated", session_id: this.sessionId, status: "closed" }));
  }

  /** A simulated person says something. */
  postAs(personId: string, body: string): void {
    this.submit("user", personId, `sim-${++this.counter}`, body);
  }

  dispose(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    for (const c of [...this.conns]) this.closeConn(c, 1001);
  }

  // ---- views -----------------------------------------------------------------------------

  budgetView(now = Date.now()): BudgetView {
    const epoch = Math.floor(now / 1000);
    const end = (epoch - (epoch % WINDOW_SECONDS) + WINDOW_SECONDS) * 1000;
    const used = this.agent.usedTokens;
    const pct = Math.min(100, Math.max(0, Math.ceil((used * 100) / CAPACITY_TOKENS)));
    return { used_pct: pct, refills_at: new Date(end).toISOString() };
  }

  agentState(): AgentState {
    if (this.agent.paused) return "paused";
    if (this.agent.away) return "away";
    if (CAPACITY_TOKENS - this.agent.usedTokens < FULL_TURN_TOKENS) return "resting";
    return this.agent.writing ? "writing" : "following";
  }

  agentView(): AgentView {
    return { id: this.agent.id, name: this.agent.name, state: this.agentState(), budget: this.budgetView() };
  }

  sessionView(): SessionView {
    return {
      id: this.sessionId,
      title: this.title,
      status: this.status,
      last_seq: this.lastSeq,
      members: [...this.members.values()],
      agent: this.agentView(),
    };
  }

  // ---- REST ------------------------------------------------------------------------------

  fetch = async (input: string, init?: RequestInit): Promise<Response> => {
    await this.sleep(this.opts.latencyMs);
    const url = new URL(input, "http://mock.huddle");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    const bearer = (headers.get("Authorization") ?? "").replace(/^Bearer /, "");
    const body: unknown = typeof init?.body === "string" && init.body ? JSON.parse(init.body) : null;
    const path = url.pathname;
    const res = this.route(method, path, url.searchParams, bearer, body);
    await this.sleep(this.opts.latencyMs);
    return new Response(JSON.stringify(res.body), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  };

  private route(
    method: string,
    path: string,
    q: URLSearchParams,
    bearer: string,
    body: unknown,
  ): { status: number; body: unknown } {
    const fail = (status: number, e: ErrorBody) => ({ status, body: { error: e } });
    if (method === "POST" && path === "/auth/guest") {
      const name = validName((body as { display_name?: unknown } | null)?.display_name);
      if (name === null) return fail(400, err("VALIDATION"));
      const id = `usr_${(++this.counter).toString(36)}${Math.random().toString(36).slice(2, 8)}`;
      this.addMember(id, "user", name);
      this.broadcast(frame({ type: "member.joined", session_id: this.sessionId, participant_id: id, display_name: name }));
      this.system(`${name} joined`);
      return { status: 200, body: this.issue(id) };
    }
    if (method === "POST" && path === "/auth/refresh") {
      const user = this.refreshTokens.get(bearer);
      if (!user) return fail(401, err("TOKEN_EXPIRED"));
      this.refreshTokens.delete(bearer); // rotates on use (Deep Dive §8)
      return { status: 200, body: this.issue(user) };
    }
    const m = /^\/sessions\/([^/]+)(\/messages|\/agent)?$/.exec(path);
    if (!m) return fail(404, err("VALIDATION", { message: "not found" }));
    const auth = this.checkAccess(bearer);
    if (!auth.ok) return fail(401, err(auth.code));
    if (m[1] !== this.sessionId || !this.members.has(auth.user)) return fail(403, err("NOT_MEMBER"));
    const sub = m[2];
    if (!sub && method === "GET") return { status: 200, body: this.sessionView() };
    if (sub === "/messages" && method === "GET") {
      const limit = Math.min(REPLAY_PAGE, Number(q.get("limit") ?? 50));
      const before = q.get("before_seq");
      const afterParam = q.get("after_seq");
      if (before !== null || afterParam === null) {
        // before_seq pages backwards; no cursor at all returns the latest window.
        const b = before !== null ? Number(before) : this.lastSeq + 1;
        const start = Math.max(0, b - 1 - limit);
        return { status: 200, body: { messages: this.messages.slice(start, b - 1), has_more: start > 0 } };
      }
      const after = Number(afterParam);
      const page = this.messages.slice(after, after + limit);
      return { status: 200, body: { messages: page, has_more: after + limit < this.lastSeq } };
    }
    if (sub === "/messages" && method === "POST") {
      const b = body as { client_msg_id?: string; body?: string } | null;
      const r = this.submit("user", auth.user, b?.client_msg_id ?? "", b?.body ?? "");
      return r.ok ? { status: 200, body: { message_id: r.message_id, seq: r.seq } } : fail(400, r.error);
    }
    if (sub === "/agent" && method === "PATCH") {
      const p = (body ?? {}) as { status?: string; name?: unknown };
      const who = this.members.get(auth.user)?.display_name ?? "Someone";
      const newName = p.name === undefined ? undefined : validName(p.name);
      if (newName === null) return fail(400, err("VALIDATION"));
      if (p.status !== undefined && !["paused", "active", "following"].includes(p.status)) {
        return fail(400, err("VALIDATION"));
      }
      if (newName !== undefined && newName !== this.agent.name) {
        this.agent.name = newName;
        const member = this.members.get(MOCK_AGENT_ID);
        if (member) member.display_name = this.agent.name;
        this.system(`${who} renamed the AI to ${this.agent.name}`);
        this.broadcastAgent();
      }
      if (p.status !== undefined) this.setAgentPaused(p.status === "paused", who);
      return { status: 200, body: this.agentView() };
    }
    return fail(405, err("VALIDATION"));
  }

  private issue(userId: string): TokenResponse {
    const now = Date.now();
    const payload = { sub: userId, exp: Math.floor(now / 1000) + this.opts.tokenTtlSec, iat_ms: now };
    const access = `${b64url(JSON.stringify({ alg: "none", typ: "JWT" }))}.${b64url(JSON.stringify(payload))}.mock`;
    const refresh = `rt_${(++this.counter).toString(36)}_${Math.random().toString(36).slice(2)}`;
    this.refreshTokens.set(refresh, userId);
    return { user_id: userId, access_token: access, refresh_token: refresh };
  }

  private checkAccess(token: string): { ok: true; user: string } | { ok: false; code: ErrorCode } {
    const part = token.split(".")[1];
    if (!part) return { ok: false, code: "UNAUTHENTICATED" };
    try {
      const c = JSON.parse(unb64url(part)) as { sub?: string; exp?: number; iat_ms?: number };
      if (!c.sub || !this.members.has(c.sub)) return { ok: false, code: "UNAUTHENTICATED" };
      if ((c.exp ?? 0) * 1000 <= Date.now() || (c.iat_ms ?? 0) < this.tokensValidAfter) {
        return { ok: false, code: "TOKEN_EXPIRED" };
      }
      return { ok: true, user: c.sub };
    } catch {
      return { ok: false, code: "UNAUTHENTICATED" };
    }
  }

  // ---- WebSocket -------------------------------------------------------------------------

  createSocket = (url: string): SocketLike => {
    const socket = new MockSocket(url, this.opts.latencyMs);
    if (Date.now() < this.offlineUntil) {
      socket.failToOpen();
      return socket;
    }
    const conn: Conn = { socket, userId: null, needsReauth: false, subscribed: false, authTimer: null };
    socket.onServerOpen = () => {
      this.conns.add(conn);
      conn.authTimer = this.later(() => {
        if (!conn.userId) this.closeConn(conn, 4001); // auth frame within 5 s (Deep Dive §6)
      }, 5_000);
    };
    socket.onServerMessage = (data) => {
      this.receive(conn, data);
    };
    socket.onServerClose = () => {
      this.conns.delete(conn);
    };
    socket.startOpen();
    return socket;
  };

  private closeConn(c: Conn, code: number): void {
    this.conns.delete(c);
    c.socket.serverClose(code);
  }

  private receive(c: Conn, data: string): void {
    let f: { v?: number; type?: string; [k: string]: unknown };
    try {
      f = JSON.parse(data) as typeof f;
    } catch {
      return;
    }
    if (f.v !== PROTOCOL_VERSION) {
      c.socket.deliver(frame({ type: "error", error: err("PROTOCOL_UNSUPPORTED") }));
      return;
    }
    if (f.type === "auth") {
      const a = this.checkAccess(String(f.token));
      if (!a.ok) {
        c.socket.deliver(frame({ type: "error", error: err(a.code) }));
        return;
      }
      c.userId = a.user;
      c.needsReauth = false;
      c.socket.deliver(frame({ type: "auth.ok", user_id: a.user }));
      return;
    }
    if (!c.userId) {
      c.socket.deliver(frame({ type: "error", error: err("UNAUTHENTICATED") }));
      return;
    }
    const user = c.userId;
    switch (f.type) {
      case "subscribe": {
        const requested = Number(f.after_seq ?? 0);
        const after = requested === 0 ? Math.max(0, this.lastSeq - this.opts.windowSize) : requested;
        const pages: Message[][] = [];
        for (let i = after; i < this.lastSeq; i += REPLAY_PAGE) pages.push(this.messages.slice(i, i + REPLAY_PAGE));
        if (pages.length === 0) pages.push([]);
        pages.forEach((messages, i) => {
          c.socket.deliver(
            frame({ type: "sync.batch", session_id: this.sessionId, messages, has_more: i < pages.length - 1 }),
          );
        });
        c.subscribed = true;
        c.socket.deliver(frame({ type: "receipt.updated", session_id: this.sessionId, cursors: { ...this.cursors } }));
        c.socket.deliver(this.agentFrame());
        return;
      }
      case "message.send": {
        const cmid = String(f.client_msg_id);
        if (c.needsReauth) {
          c.socket.deliver(frame({ type: "error", error: err("TOKEN_EXPIRED", { client_msg_id: cmid }) }));
          return;
        }
        const r = this.submit("user", user, cmid, typeof f.body === "string" ? f.body : "");
        if (!r.ok) {
          c.socket.deliver(frame({ type: "error", error: { ...r.error, client_msg_id: cmid } }));
        } else if (this.dropAcks > 0) {
          this.dropAcks--;
        } else {
          c.socket.deliver(frame({ type: "message.ack", client_msg_id: cmid, message_id: r.message_id, seq: r.seq }));
        }
        return;
      }
      case "ack":
        this.advance(user, Number(f.up_to_seq));
        return;
      case "read":
        return;
      case "ping":
        c.socket.deliver(frame({ type: "pong" }));
        return;
      default:
        return;
    }
  }

  // ---- the one write path ------------------------------------------------------------------

  private submit(senderType: "user" | "agent", senderId: string, cmid: string, body: string): SubmitResult {
    const key = `${senderId}:${cmid}`;
    const prior = this.idem.get(key);
    if (prior) {
      this.submissions.push({ sender: senderId, client_msg_id: cmid, duplicate: true });
      return { ok: true, ...prior };
    }
    if (this.status === "closed") return { ok: false, error: err("SESSION_CLOSED") };
    if (body.length > MAX_BODY_CHARS) return { ok: false, error: err("MESSAGE_TOO_LONG") };
    if (!body.trim()) return { ok: false, error: err("VALIDATION") };
    const forced = this.failNext.shift();
    if (forced) return { ok: false, error: err(forced, forced === "RATE_LIMITED" ? { retry_after_ms: 2_000 } : {}) };
    if (senderType === "user") {
      const now = Date.now();
      const recent = (this.rate.get(senderId) ?? []).filter((t) => now - t < 10_000);
      if (recent.length >= RATE_LIMIT) {
        return { ok: false, error: err("RATE_LIMITED", { retry_after_ms: 10_000 - (now - (recent[0] ?? now)) }) };
      }
      recent.push(now);
      this.rate.set(senderId, recent);
    }
    const seq = this.lastSeq + 1;
    const message: Message = {
      id: `msg_${seq.toString().padStart(6, "0")}`,
      session_id: this.sessionId,
      seq,
      sender_type: senderType,
      sender_id: senderId,
      client_msg_id: cmid,
      kind: "text",
      body,
      created_at: new Date().toISOString(),
    };
    this.messages.push(message);
    this.idem.set(key, { message_id: message.id, seq });
    this.submissions.push({ sender: senderId, client_msg_id: cmid, duplicate: false });
    this.cursors[senderId] = Math.max(this.cursors[senderId] ?? 0, seq);
    this.lastSender = senderId;
    this.fanout(message);
    if (senderType === "user") this.afterHuman(senderId);
    return { ok: true, message_id: message.id, seq };
  }

  private system(text: string): void {
    const seq = this.lastSeq + 1;
    const message: Message = {
      id: `msg_${seq.toString().padStart(6, "0")}`,
      session_id: this.sessionId,
      seq,
      sender_type: "system",
      sender_id: "system",
      client_msg_id: `sys-${seq}`,
      kind: "system",
      body: text,
      created_at: new Date().toISOString(),
    };
    this.messages.push(message);
    this.fanout(message);
  }

  private fanout(message: Message): void {
    if (this.dropFanouts > 0) {
      this.dropFanouts--;
      return;
    }
    this.broadcast(
      frame({
        type: "message.created",
        event_id: `evt_${message.seq}`,
        session_id: this.sessionId,
        sequence: message.seq,
        message,
      }),
    );
  }

  private broadcast(f: ServerFrame): void {
    for (const c of this.conns) if (c.subscribed) c.socket.deliver(f);
  }

  private agentFrame(): ServerFrame {
    return frame({
      type: "agent.status",
      session_id: this.sessionId,
      state: this.agentState(),
      name: this.agent.name,
      budget: this.budgetView(),
    });
  }

  private broadcastAgent(): void {
    this.broadcast(this.agentFrame());
  }

  private advance(participant: string, upTo: number): void {
    const next = Math.min(upTo, this.lastSeq);
    if (!(next > (this.cursors[participant] ?? 0))) return;
    this.cursors[participant] = next;
    this.broadcast(frame({ type: "receipt.updated", session_id: this.sessionId, cursors: { [participant]: next } }));
  }

  // ---- simulated participants --------------------------------------------------------------

  private agentActive(): boolean {
    const s = this.agentState();
    return s === "following" || s === "writing";
  }

  /** The agent catches up after becoming active again (D-020: no reads while inactive). */
  private agentCatchUp(): void {
    if (!this.opts.agent) return;
    this.later(() => {
      if (this.agentActive()) this.advance(MOCK_AGENT_ID, this.lastSeq);
    }, 900);
  }

  private afterHuman(senderId: string): void {
    const simulated = MOCK_PEOPLE.some((p) => p.id === senderId);
    if (this.opts.people) {
      this.later(() => {
        for (const p of MOCK_PEOPLE) if (p.id !== senderId) this.advance(p.id, this.lastSeq);
      }, 600);
    }
    if (!this.opts.agent) return;
    this.later(() => {
      if (this.agentActive()) this.advance(MOCK_AGENT_ID, this.lastSeq);
    }, 1_300);
    if (simulated) return;
    this.humanCount++;
    if (this.humanCount % 2 === 1) {
      const runId = crypto.randomUUID();
      this.later(() => {
        if (this.agentActive() && this.lastSender !== MOCK_AGENT_ID) this.setAgentWriting(true);
      }, 2_000);
      this.later(() => {
        if (!this.agent.writing) return;
        this.agent.writing = false;
        this.agent.usedTokens = Math.min(CAPACITY_TOKENS, this.agent.usedTokens + TOKENS_PER_REPLY);
        const line = AGENT_LINES[(Math.floor(this.humanCount / 2) + 1) % AGENT_LINES.length] ?? "";
        // The agent's client_msg_id is its run_id (Deep Dive §3), so a retried commit can't post twice.
        this.submit("agent", MOCK_AGENT_ID, runId, line);
        this.broadcastAgent();
      }, 4_200);
    } else if (this.opts.people && this.humanCount % 3 === 0) {
      this.later(() => {
        const who = MOCK_PEOPLE[this.humanCount % MOCK_PEOPLE.length] ?? MOCK_PEOPLE[0];
        this.postAs(who.id, PEOPLE_LINES[this.humanCount % PEOPLE_LINES.length] ?? "ok");
      }, 2_500);
    }
  }

  private seedHistory(): void {
    const base = Date.now() - 40 * 60_000;
    const at = (min: number) => new Date(base + min * 60_000).toISOString();
    const add = (sender_type: Message["sender_type"], sender_id: string, body: string, min: number) => {
      const seq = this.lastSeq + 1;
      this.messages.push({
        id: `msg_${seq.toString().padStart(6, "0")}`,
        session_id: this.sessionId,
        seq,
        sender_type,
        sender_id,
        client_msg_id: `seed-${seq}`,
        kind: sender_type === "system" ? "system" : "text",
        body,
        created_at: at(min),
      });
    };
    add("system", "system", "Maya Chen started the huddle", 0);
    add("system", "system", "Huddle AI joined", 0);
    add("user", "usr_maya", "Draft of the launch post is in the doc", 4);
    add("user", "usr_maya", "mostly worried the intro is too long", 4);
    add("user", "usr_jordan", "reading now", 6);
    add(
      "agent",
      MOCK_AGENT_ID,
      "The first two paragraphs say the same thing. Cutting the second gets the intro to about 60 words, and the “why now” line moves up where people will see it.",
      7,
    );
    add("user", "usr_jordan", "Here is the snippet for the changelog link:\n```\nhttps://example.com/changelog#v2\n```", 9);
    for (const p of MOCK_PEOPLE) this.cursors[p.id] = this.lastSeq;
    this.cursors[MOCK_AGENT_ID] = this.lastSeq;
    this.lastSender = "usr_jordan";
  }

  private addMember(id: string, type: "user" | "agent", name: string): void {
    this.members.set(id, {
      participant_id: id,
      participant_type: type,
      display_name: name,
      role: "member",
      status: "active",
    });
  }

  private later(fn: () => void, ms: number): ReturnType<typeof setTimeout> {
    const t = setTimeout(() => {
      this.timers.delete(t);
      fn();
    }, ms);
    this.timers.add(t);
    return t;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      this.later(resolve, ms);
    });
  }
}

/** A WebSocket stand-in wired to MockServer. Frames are JSON strings, like the real socket. */
export class MockSocket implements SocketLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onServerOpen: (() => void) | null = null;
  onServerMessage: ((data: string) => void) | null = null;
  onServerClose: (() => void) | null = null;

  constructor(
    readonly url: string,
    private readonly latency: number,
  ) {}

  startOpen(): void {
    setTimeout(() => {
      if (this.readyState !== 0) return;
      this.readyState = 1;
      this.onServerOpen?.();
      this.onopen?.(new Event("open"));
    }, this.latency);
  }

  failToOpen(): void {
    setTimeout(() => {
      this.readyState = 3;
      this.onerror?.(new Event("error"));
      this.onclose?.(new CloseEvent("close", { code: 1006 }));
    }, this.latency);
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error("socket not open");
    setTimeout(() => {
      if (this.readyState === 1) this.onServerMessage?.(data);
    }, this.latency);
  }

  close(code = 1000): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onServerClose?.();
    setTimeout(() => {
      this.onclose?.(new CloseEvent("close", { code }));
    }, 0);
  }

  /** Server -> client. Dropped if the socket closes before it arrives. */
  deliver(f: ServerFrame): void {
    const data = JSON.stringify(f);
    setTimeout(() => {
      if (this.readyState === 1) this.onmessage?.(new MessageEvent("message", { data }));
    }, this.latency);
  }

  serverClose(code: number): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onServerClose?.();
    setTimeout(() => {
      this.onclose?.(new CloseEvent("close", { code }));
    }, this.latency);
  }
}
