/**
 * HuddleClient: auth, REST, the WebSocket and the thread reducer behind one observable snapshot.
 * React reads it with useSyncExternalStore; tests drive it with a mock server and fake timers.
 */
import {
  MAX_BODY_CHARS,
  normalizeName,
  type AgentView,
  type MemberView,
  type Message,
  type ServerFrame,
  type SessionView,
} from "./contracts";
import { ackTimeoutError } from "./core/errors";
import { findGap, initialThread, threadReducer, type ThreadAction, type ThreadState } from "./core/thread";
import { uuidv7 } from "./core/uuidv7";
import { Api, ApiError, PAGE_LIMIT, type FetchLike } from "./net/api";
import {
  AuthError,
  DEVICE_ID_KEY,
  DISPLAY_NAME_KEY,
  TokenStore,
  type KeyValueStore,
} from "./net/auth";
import { Connection, type ConnectionStatus, type FatalReason, type SocketLike } from "./net/connection";

export interface ClientConfig {
  sessionId: string;
  /** Base URL for REST, "" for same-origin. */
  apiBase: string;
  /** Full ws:// or wss:// URL of the gateway's /ws. */
  wsUrl: string;
}

export interface ClientDeps {
  fetch: FetchLike;
  createSocket: (url: string) => SocketLike;
  storage: KeyValueStore;
  random?: () => number;
  now?: () => number;
  isVisible?: () => boolean;
  /** How long a sent message may wait for `message.ack` before it shows as failed. */
  ackTimeoutMs?: number;
  heartbeatMs?: number;
  pongTimeoutMs?: number;
}

export type Phase = "restoring" | "join" | "joining" | "in";

export interface ClientSnapshot {
  phase: Phase;
  joinFailed: boolean;
  /** The last name used on this device, to pre-fill the join form. */
  displayName: string;
  userId: string | null;
  title: string;
  sessionStatus: string;
  members: readonly MemberView[];
  agent: AgentView | null;
  connection: ConnectionStatus;
  fatal: FatalReason | null;
  thread: ThreadState;
  /** participant_id -> display name, kept for people who have left. */
  names: ReadonlyMap<string, string>;
}

export const ACK_TIMEOUT_MS = 10_000;
export const EARLIER_PAGE = 50;

type Timer = ReturnType<typeof setTimeout>;

function readStore(storage: KeyValueStore, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(storage: KeyValueStore, key: string, value: string): void {
  try {
    storage.setItem(key, value);
  } catch {
    /* storage unavailable */
  }
}

export class HuddleClient {
  readonly api: Api;
  readonly tokens: TokenStore;
  private snap: ClientSnapshot;
  private readonly listeners = new Set<() => void>();
  private conn: Connection | null = null;
  private readonly inFlight = new Map<string, Timer>();
  private gapFetching = false;
  private started = false;
  private lastAck = 0;
  private lastRead = 0;
  private readonly deviceId: string;
  private readonly now: () => number;

  constructor(
    private readonly config: ClientConfig,
    private readonly deps: ClientDeps,
  ) {
    this.now = deps.now ?? Date.now;
    this.api = new Api(config.apiBase, deps.fetch);
    this.tokens = new TokenStore(deps.storage, (rt) => this.api.refresh(rt), this.now);
    this.api.useTokens(this.tokens);
    let device = readStore(deps.storage, DEVICE_ID_KEY);
    if (!device) {
      device = crypto.randomUUID();
      writeStore(deps.storage, DEVICE_ID_KEY, device);
    }
    this.deviceId = device;
    this.snap = this.blank("restoring");
  }

  // ---- store interface -----------------------------------------------------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): ClientSnapshot => this.snap;

  private update(patch: Partial<ClientSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const l of this.listeners) l();
  }

  private dispatch(action: ThreadAction): void {
    const thread = threadReducer(this.snap.thread, action);
    if (thread !== this.snap.thread) this.update({ thread });
  }

  private blank(phase: Phase): ClientSnapshot {
    return {
      phase,
      joinFailed: false,
      displayName: readStore(this.deps.storage, DISPLAY_NAME_KEY) ?? "",
      userId: null,
      title: "",
      sessionStatus: "active",
      members: [],
      agent: null,
      connection: "stopped",
      fatal: null,
      thread: initialThread(this.config.sessionId),
      names: new Map(),
    };
  }

  // ---- lifecycle -------------------------------------------------------------------------

  /** Start once per page: restore a returning guest or show the join form. Idempotent. */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.restore();
  }

  /** On page load: a stored refresh token skips the join form. */
  async restore(): Promise<void> {
    if (!this.tokens.hasRefreshToken) {
      this.update({ phase: "join" });
      return;
    }
    this.update({ phase: "restoring" });
    try {
      await this.tokens.refresh();
    } catch (err) {
      if (err instanceof AuthError) {
        this.tokens.clear();
        this.update({ phase: "join" });
        return;
      }
      // Server unreachable: enter anyway; the connection keeps retrying with backoff.
    }
    this.enter();
  }

  /** JoinForm: POST /auth/guest, then connect. */
  async join(name: string): Promise<void> {
    const displayName = normalizeName(name);
    if (!displayName || this.snap.phase === "joining" || this.snap.phase === "in") return;
    this.update({ phase: "joining", joinFailed: false, displayName });
    try {
      const tokens = await this.api.guest(displayName);
      this.tokens.set(tokens);
      writeStore(this.deps.storage, DISPLAY_NAME_KEY, displayName);
    } catch {
      this.update({ phase: "join", joinFailed: true });
      return;
    }
    this.enter();
  }

  dispose(): void {
    this.conn?.stop();
    this.conn = null;
    this.clearInFlight();
  }

  private enter(): void {
    const userId = this.tokens.userId;
    this.update({
      phase: "in",
      userId,
      thread: initialThread(this.config.sessionId, userId),
    });
    this.conn = new Connection({
      url: this.config.wsUrl,
      sessionId: this.config.sessionId,
      deviceId: this.deviceId,
      createSocket: this.deps.createSocket,
      getToken: () => this.tokens.accessToken(),
      refreshToken: () => this.tokens.refresh(),
      afterSeq: () => this.snap.thread.cursor,
      onFrame: (f) => {
        this.onFrame(f);
      },
      onStatus: (s) => {
        this.onStatus(s);
      },
      onSubscribed: (afterSeq) => {
        this.dispatch({ type: "subscribed", afterSeq });
        void this.loadSession();
      },
      onFatal: (reason) => {
        this.onFatal(reason);
      },
      ...(this.deps.random ? { random: this.deps.random } : {}),
      ...(this.deps.heartbeatMs ? { heartbeatMs: this.deps.heartbeatMs } : {}),
      ...(this.deps.pongTimeoutMs ? { pongTimeoutMs: this.deps.pongTimeoutMs } : {}),
    });
    this.conn.start();
  }

  private logout(): void {
    this.dispose();
    this.tokens.clear();
    this.snap = this.blank("join");
    for (const l of this.listeners) l();
  }

  private onFatal(reason: FatalReason): void {
    if (reason === "unauthenticated") this.logout();
    else this.update({ fatal: reason });
  }

  private async loadSession(): Promise<void> {
    try {
      this.applySession(await this.api.getSession(this.config.sessionId));
    } catch (err) {
      if (err instanceof AuthError) this.logout();
      // Otherwise keep what we have; the next (re)subscribe loads it again.
    }
  }

  private applySession(s: SessionView): void {
    const names = new Map(this.snap.names);
    for (const m of s.members) names.set(m.participant_id, m.display_name);
    this.update({
      title: s.title,
      sessionStatus: s.status,
      members: s.members,
      agent: s.agent,
      names,
    });
  }

  // ---- frames ----------------------------------------------------------------------------

  private onFrame(f: ServerFrame): void {
    if ("session_id" in f && f.session_id !== this.config.sessionId) return;
    switch (f.type) {
      case "auth.ok":
        this.update({ userId: f.user_id });
        this.dispatch({ type: "identity", me: f.user_id });
        break;
      case "sync.batch":
        this.dispatch({ type: "synced", messages: f.messages, hasMore: f.has_more });
        if (!f.has_more) void this.fillGaps();
        break;
      case "message.created":
        this.dispatch({ type: "created", message: f.message });
        void this.fillGaps();
        break;
      case "message.ack":
        this.settle(f.client_msg_id);
        this.dispatch({
          type: "acked",
          clientMsgId: f.client_msg_id,
          messageId: f.message_id,
          seq: f.seq,
        });
        void this.fillGaps();
        break;
      case "receipt.updated":
        this.dispatch({ type: "receipts", cursors: f.cursors });
        break;
      case "agent.status": {
        const prev = this.snap.agent;
        this.update({
          agent: { id: prev?.id ?? "", name: f.name, state: f.state, budget: f.budget },
        });
        break;
      }
      case "member.joined":
      case "member.left":
        this.applyMember(f.type === "member.joined", f.participant_id, f.display_name);
        break;
      case "session.updated": {
        const patch: Partial<ClientSnapshot> = {};
        if (f.title) patch.title = f.title;
        if (f.status) patch.sessionStatus = f.status;
        this.update(patch);
        break;
      }
      case "error": {
        const e = f.error;
        if (e.code === "SESSION_CLOSED") this.update({ sessionStatus: "closed" });
        const id = e.client_msg_id;
        if (!id) break;
        this.settle(id);
        // TOKEN_EXPIRED: the connection refreshes and re-authenticates; the message stays
        // pending and is re-sent (same client_msg_id) once the socket is ready again.
        if (e.code !== "TOKEN_EXPIRED" && e.code !== "UNAUTHENTICATED") {
          this.dispatch({ type: "failed", clientMsgId: id, error: e });
        }
        break;
      }
      case "server.draining":
      case "pong":
        break;
    }
  }

  private applyMember(joined: boolean, participantId: string, displayName: string): void {
    const names = new Map(this.snap.names);
    if (displayName) names.set(participantId, displayName);
    const members = this.snap.members.filter((m) => m.participant_id !== participantId);
    const existing = this.snap.members.find((m) => m.participant_id === participantId);
    members.push({
      participant_id: participantId,
      participant_type: existing?.participant_type ?? (participantId.startsWith("agt_") ? "agent" : "user"),
      display_name: displayName || existing?.display_name || "",
      role: existing?.role ?? "member",
      status: joined ? "active" : "left",
    });
    this.update({ members, names });
  }

  private onStatus(status: ConnectionStatus): void {
    if (status !== "live") {
      // Unconfirmed messages stay pending and are re-sent after the next replay.
      this.clearInFlight();
      this.lastAck = 0;
      this.lastRead = 0;
    }
    this.update({ connection: status });
    if (status === "live") {
      this.flushOutbox();
      void this.fillGaps();
    }
  }

  // ---- sending ---------------------------------------------------------------------------

  /** Composer onSend. The message gets a UUIDv7 client_msg_id that every retry reuses. */
  send(text: string): void {
    const body = text.trim();
    if (!body || body.length > MAX_BODY_CHARS || this.snap.sessionStatus === "closed") return;
    const clientMsgId = uuidv7(this.now());
    this.dispatch({
      type: "enqueued",
      entry: {
        clientMsgId,
        body,
        createdAt: new Date(this.now()).toISOString(),
        status: "pending",
        error: null,
      },
    });
    this.transmit(clientMsgId);
  }

  /** The Retry action on a failed bubble: same client_msg_id, so the server dedupes. */
  retry(clientMsgId: string): void {
    this.dispatch({ type: "retrying", clientMsgId });
    this.transmit(clientMsgId);
  }

  private transmit(clientMsgId: string): void {
    if (this.inFlight.has(clientMsgId) || this.snap.connection !== "live") return;
    const entry = this.snap.thread.outbox.find((e) => e.clientMsgId === clientMsgId);
    if (entry?.status !== "pending") return;
    if (!this.conn?.send({ type: "message.send", client_msg_id: clientMsgId, body: entry.body })) return;
    const timer = setTimeout(() => {
      this.inFlight.delete(clientMsgId);
      this.dispatch({ type: "failed", clientMsgId, error: ackTimeoutError(clientMsgId) });
    }, this.deps.ackTimeoutMs ?? ACK_TIMEOUT_MS);
    this.inFlight.set(clientMsgId, timer);
  }

  private flushOutbox(): void {
    for (const e of this.snap.thread.outbox) {
      if (e.status === "pending") this.transmit(e.clientMsgId);
    }
  }

  private settle(clientMsgId: string): void {
    const t = this.inFlight.get(clientMsgId);
    if (t) clearTimeout(t);
    this.inFlight.delete(clientMsgId);
  }

  private clearInFlight(): void {
    for (const t of this.inFlight.values()) clearTimeout(t);
    this.inFlight.clear();
  }

  // ---- gaps, history, receipts -----------------------------------------------------------

  /**
   * Gap fill (Deep Dive §2): holding 43 and receiving 45 fetches after_seq=43 through the REST
   * catch-up endpoint and merges the page through the same dedupe path as a replay.
   * Loops while it makes progress; a gap that cannot be filled waits for the next event.
   */
  private async fillGaps(): Promise<void> {
    if (this.gapFetching || this.snap.phase !== "in") return;
    this.gapFetching = true;
    try {
      for (let guard = 0; guard < 50; guard++) {
        const gap = findGap(this.snap.thread);
        if (!gap) break;
        const before = this.snap.thread.cursor;
        const page = await this.api.getMessages(this.config.sessionId, {
          afterSeq: gap.afterSeq,
          limit: PAGE_LIMIT,
        });
        this.dispatch({ type: "fetched", messages: page.messages });
        if (this.snap.thread.cursor <= before) break;
      }
    } catch (err) {
      if (err instanceof AuthError) this.logout();
    } finally {
      this.gapFetching = false;
    }
  }

  /** "Load earlier messages": page backwards from the oldest message held. */
  async loadEarlier(): Promise<void> {
    const t = this.snap.thread;
    if (!t.hasEarlier) return;
    try {
      const page = await this.api.getMessages(this.config.sessionId, {
        beforeSeq: t.floor + 1,
        limit: EARLIER_PAGE,
      });
      this.dispatch({ type: "earlier", messages: page.messages, hasMore: page.has_more });
    } catch (err) {
      if (err instanceof AuthError) this.logout();
    }
  }

  /** Called after the thread has rendered: cumulative `ack` (and `read` when visible), §6. */
  markRendered(): void {
    const cursor = this.snap.thread.cursor;
    const conn = this.conn;
    if (!conn || cursor <= 0) return;
    if (cursor > this.lastAck && conn.send({ type: "ack", up_to_seq: cursor })) this.lastAck = cursor;
    const visible = this.deps.isVisible ? this.deps.isVisible() : document.visibilityState === "visible";
    if (visible && cursor > this.lastRead && conn.send({ type: "read", up_to_seq: cursor })) {
      this.lastRead = cursor;
    }
  }

  // ---- agent controls (Deep Dive §5 user controls; any member in the MVP) ----------------

  pause(): Promise<void> {
    return this.patchAgent({ status: "paused" });
  }

  resume(): Promise<void> {
    return this.patchAgent({ status: "active" });
  }

  rename(name: string): Promise<void> {
    const n = normalizeName(name);
    return n ? this.patchAgent({ name: n }) : Promise.resolve();
  }

  private async patchAgent(patch: { status?: "active" | "paused"; name?: string }): Promise<void> {
    try {
      const agent = await this.api.patchAgent(this.config.sessionId, patch);
      this.update({ agent });
    } catch (err) {
      if (err instanceof AuthError) this.logout();
      // Agent errors never appear in the thread (Deep Dive §10); the state comes from agent.status.
      else if (!(err instanceof ApiError)) throw err;
    }
  }

  // ---- export (Deep Dive §4: a JSON dump of the session's messages in seq order) ---------

  async exportTranscript(): Promise<string> {
    const messages: Message[] = [];
    let after = 0;
    for (let guard = 0; guard < 10_000; guard++) {
      const page = await this.api.getMessages(this.config.sessionId, { afterSeq: after, limit: PAGE_LIMIT });
      messages.push(...page.messages);
      const last = page.messages.reduce((m, x) => Math.max(m, x.seq), after);
      if (!page.has_more || last <= after) break;
      after = last;
    }
    const seen = new Set<number>();
    const ordered = messages
      .sort((a, b) => a.seq - b.seq)
      .filter((m) => (seen.has(m.seq) ? false : (seen.add(m.seq), true)));
    return JSON.stringify(
      {
        session_id: this.config.sessionId,
        title: this.snap.title,
        exported_at: new Date(this.now()).toISOString(),
        messages: ordered,
      },
      null,
      2,
    );
  }
}
