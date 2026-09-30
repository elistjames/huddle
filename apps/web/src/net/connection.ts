/**
 * One WebSocket to the gateway, kept alive (Deep Dive §2 reconnect, §6 connection protocol):
 * - on open: `auth {token, device_id}`; on `auth.ok`: `subscribe {session_id, after_seq}`.
 * - every frame carries `v: 1`; frames with another version are ignored.
 * - heartbeat: `ping` every 20 s; no frame within the pong timeout means the socket is dead.
 * - reconnect with full-jitter exponential backoff (base 500 ms, cap 30 s); the attempt counter
 *   resets once a replay completes (the connection is live again).
 * - `server.draining {reconnect_after_ms}`: reconnect at that time (not counted as a failure).
 * - `TOKEN_EXPIRED`: refresh the token and re-authenticate on the same socket.
 * - `UNAUTHENTICATED` (after one refresh) and `PROTOCOL_UNSUPPORTED` are fatal: no reconnect.
 */
import {
  PROTOCOL_VERSION,
  SERVER_FRAME_TYPES,
  type ClientFrameInput,
  type ServerFrame,
} from "../contracts";
import { backoffDelay } from "../core/backoff";
import { AuthError } from "./auth";

export const HEARTBEAT_MS = 20_000;
export const PONG_TIMEOUT_MS = 10_000;

const OPEN = 1;

/** The part of the browser WebSocket the client uses; fakes implement it in tests and mock mode. */
export interface SocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
}

export type ConnectionStatus = "connecting" | "reconnecting" | "live" | "failed" | "stopped";
export type FatalReason = "unauthenticated" | "protocol";

export interface ConnectionOptions {
  url: string;
  sessionId: string;
  deviceId: string;
  createSocket: (url: string) => SocketLike;
  /** A valid access token (refreshing it first if it is about to expire). */
  getToken: () => Promise<string>;
  /** Force a refresh (after TOKEN_EXPIRED). */
  refreshToken: () => Promise<string>;
  /** The cursor to resume from: the highest contiguous seq the client holds. */
  afterSeq: () => number;
  onFrame: (frame: ServerFrame) => void;
  onStatus: (status: ConnectionStatus) => void;
  /** A subscribe was sent; a replay from `afterSeq` follows as sync.batch frames. */
  onSubscribed: (afterSeq: number) => void;
  onFatal: (reason: FatalReason) => void;
  random?: () => number;
  heartbeatMs?: number;
  pongTimeoutMs?: number;
}

type Timer = ReturnType<typeof setTimeout>;

export class Connection {
  private socket: SocketLike | null = null;
  private status: ConnectionStatus = "stopped";
  private attempt = 0;
  private everLive = false;
  private authed = false;
  private subscribed = false;
  private refreshedForAuth = false;
  private awaitingPong = false;
  private drainAt: number | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private pongTimer: Timer | null = null;
  private reconnectTimer: Timer | null = null;
  private drainTimer: Timer | null = null;
  private readonly o: ConnectionOptions;

  constructor(options: ConnectionOptions) {
    this.o = options;
  }

  get currentStatus(): ConnectionStatus {
    return this.status;
  }

  /** Attempts made since the connection was last live (drives the backoff ceiling). */
  get attempts(): number {
    return this.attempt;
  }

  start(): void {
    if (this.status !== "stopped" && this.status !== "failed") return;
    this.open();
  }

  stop(): void {
    this.clearTimers();
    const s = this.socket;
    this.socket = null;
    this.authed = false;
    if (s) s.close(1000, "client stopped");
    this.setStatus("stopped");
  }

  /** Send a frame if the socket is open and authenticated. Returns false otherwise. */
  send(frame: ClientFrameInput): boolean {
    const s = this.socket;
    if (!s || s.readyState !== OPEN || !this.authed) return false;
    s.send(JSON.stringify({ v: PROTOCOL_VERSION, ...frame }));
    return true;
  }

  /** Drop the current socket and reconnect through the backoff loop (e.g. a dead socket). */
  reconnect(): void {
    const s = this.socket;
    if (!s) return;
    this.socket = null;
    s.close(4000, "reconnect");
    this.handleClose();
  }

  // ---- internals -------------------------------------------------------------------------

  private setStatus(status: ConnectionStatus): void {
    this.status = status;
    this.o.onStatus(status);
  }

  private open(): void {
    this.reconnectTimer = null;
    this.authed = false;
    this.subscribed = false;
    this.refreshedForAuth = false;
    this.setStatus(this.everLive || this.attempt > 0 ? "reconnecting" : "connecting");
    let socket: SocketLike;
    try {
      socket = this.o.createSocket(this.o.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket === socket) void this.authenticate(socket, false);
    };
    socket.onmessage = (ev) => {
      if (this.socket === socket) this.handleMessage(socket, ev.data);
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.handleClose();
    };
    socket.onerror = () => {
      /* a close event follows */
    };
  }

  private async authenticate(socket: SocketLike, refresh: boolean): Promise<void> {
    let token: string;
    try {
      token = refresh ? await this.o.refreshToken() : await this.o.getToken();
    } catch (err) {
      if (this.socket !== socket) return;
      if (err instanceof AuthError) this.fatal("unauthenticated");
      else this.reconnect();
      return;
    }
    if (this.socket !== socket || socket.readyState !== OPEN) return;
    socket.send(
      JSON.stringify({ v: PROTOCOL_VERSION, type: "auth", token, device_id: this.o.deviceId }),
    );
  }

  private handleMessage(socket: SocketLike, data: unknown): void {
    this.awaitingPong = false;
    const frame = parseFrame(data);
    if (!frame) return;
    // Forward first so the client merges data before any status change it triggers.
    this.o.onFrame(frame);
    if (this.socket !== socket) return;
    switch (frame.type) {
      case "auth.ok":
        this.authed = true;
        this.refreshedForAuth = false;
        if (!this.subscribed) {
          this.subscribed = true;
          const afterSeq = this.o.afterSeq();
          this.o.onSubscribed(afterSeq);
          socket.send(
            JSON.stringify({
              v: PROTOCOL_VERSION,
              type: "subscribe",
              session_id: this.o.sessionId,
              after_seq: afterSeq,
            }),
          );
          this.startHeartbeat();
        } else if (this.status === "live") {
          // Re-authenticated on the same socket (after TOKEN_EXPIRED): ready to send again.
          this.setStatus("live");
        }
        break;
      case "sync.batch":
        if (!frame.has_more && this.status !== "live") {
          this.everLive = true;
          this.attempt = 0;
          this.setStatus("live");
        }
        break;
      case "server.draining":
        this.scheduleDrain(frame.reconnect_after_ms);
        break;
      case "error":
        switch (frame.error.code) {
          case "TOKEN_EXPIRED":
            this.authed = false;
            void this.authenticate(socket, true);
            break;
          case "UNAUTHENTICATED":
            if (this.authed || this.refreshedForAuth) {
              this.fatal("unauthenticated");
            } else {
              this.refreshedForAuth = true;
              void this.authenticate(socket, true);
            }
            break;
          case "PROTOCOL_UNSUPPORTED":
            this.fatal("protocol");
            break;
          default:
            break;
        }
        break;
      default:
        break;
    }
  }

  private handleClose(): void {
    this.stopHeartbeat();
    this.authed = false;
    if (this.status === "stopped" || this.status === "failed") return;
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    let delay: number;
    if (this.drainAt !== null) {
      delay = Math.max(0, this.drainAt - Date.now());
      this.drainAt = null;
      if (this.drainTimer) clearTimeout(this.drainTimer);
      this.drainTimer = null;
    } else {
      delay = backoffDelay(this.attempt, this.o.random);
      this.attempt++;
    }
    this.setStatus("reconnecting");
    this.reconnectTimer = setTimeout(() => {
      this.open();
    }, delay);
  }

  private scheduleDrain(ms: number): void {
    const wait = Math.max(0, Number.isFinite(ms) ? ms : 0);
    this.drainAt = Date.now() + wait;
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.drainTimer = setTimeout(() => {
      this.drainTimer = null;
      if (this.socket) this.reconnect();
    }, wait);
  }

  private fatal(reason: FatalReason): void {
    this.clearTimers();
    const s = this.socket;
    this.socket = null;
    this.authed = false;
    if (s) s.close(1000, reason);
    this.setStatus("failed");
    this.o.onFatal(reason);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    const every = this.o.heartbeatMs ?? HEARTBEAT_MS;
    const timeout = this.o.pongTimeoutMs ?? PONG_TIMEOUT_MS;
    this.heartbeat = setInterval(() => {
      const s = this.socket;
      if (!s || s.readyState !== OPEN) return;
      this.awaitingPong = true;
      s.send(JSON.stringify({ v: PROTOCOL_VERSION, type: "ping" }));
      if (this.pongTimer) clearTimeout(this.pongTimer);
      this.pongTimer = setTimeout(() => {
        this.pongTimer = null;
        if (this.awaitingPong && this.socket === s) this.reconnect();
      }, timeout);
    }, every);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    if (this.pongTimer) clearTimeout(this.pongTimer);
    this.heartbeat = null;
    this.pongTimer = null;
    this.awaitingPong = false;
  }

  private clearTimers(): void {
    this.stopHeartbeat();
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.reconnectTimer = null;
    this.drainTimer = null;
    this.drainAt = null;
  }
}

/** Parse and version-check a server frame. Unknown types and other protocol versions are ignored. */
export function parseFrame(data: unknown): ServerFrame | null {
  if (typeof data !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const o = parsed as { v?: unknown; type?: unknown };
  if (o.v !== undefined && o.v !== PROTOCOL_VERSION) return null;
  if (typeof o.type !== "string" || !SERVER_FRAME_TYPES.has(o.type)) return null;
  return parsed as ServerFrame;
}
