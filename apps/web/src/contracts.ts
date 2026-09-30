/**
 * Wire types for the web client, hand-written to mirror the Python contracts.
 *
 * Source of truth (keep in sync, same PR):
 *   libs/contracts/src/huddle_contracts/__init__.py  PROTOCOL_VERSION
 *   libs/contracts/src/huddle_contracts/models.py    Message, AgentView, SessionView, BudgetView, ...
 *   libs/contracts/src/huddle_contracts/errors.py    ErrorCode, RETRYABLE, ErrorBody
 *   libs/contracts/src/huddle_contracts/ws.py        client and server frames, close codes
 *   libs/contracts/src/huddle_contracts/internal.py  MessagePage (same shape as the public page)
 * and Deep Dive §6 for the public REST shapes that are not modelled in libs/contracts.
 *
 * Datetimes are ISO 8601 strings on the wire (pydantic serialises `datetime` that way).
 */

// ---- __init__.py ---------------------------------------------------------------------------

export const PROTOCOL_VERSION = 1;

// ---- models.py -----------------------------------------------------------------------------

export const MAX_BODY_CHARS = 4000; // Deep Dive §8

export type SenderType = "user" | "agent" | "system";
export type ParticipantType = "user" | "agent";
export type MessageKind = "text" | "system";
export type AgentState = "following" | "writing" | "paused" | "resting" | "away";

export interface Message {
  id: string; // msg_ + ULID
  session_id: string;
  seq: number;
  sender_type: SenderType;
  sender_id: string;
  client_msg_id: string;
  kind: MessageKind;
  body: string | null; // null once redacted
  created_at: string;
}

/** What users see: percentage used and when the fixed window ends (D-019). */
export interface BudgetView {
  used_pct: number; // integer 0..100
  refills_at: string;
}

export interface AgentView {
  id: string; // agt_ + ULID
  name: string;
  state: AgentState;
  budget: BudgetView;
}

export interface MemberView {
  participant_id: string;
  participant_type: ParticipantType;
  display_name: string;
  role: string; // owner | member
  status: string; // invited | active | left | removed
}

export interface SessionView {
  id: string;
  title: string;
  status: string; // active | closed
  last_seq: number;
  members: MemberView[];
  agent: AgentView | null;
}

// ---- errors.py -----------------------------------------------------------------------------

export type ErrorCode =
  | "UNAUTHENTICATED"
  | "TOKEN_EXPIRED"
  | "NOT_MEMBER"
  | "SESSION_CLOSED"
  | "MESSAGE_TOO_LONG"
  | "RATE_LIMITED"
  | "PERSISTENCE_UNAVAILABLE"
  | "PROTOCOL_UNSUPPORTED"
  | "VALIDATION"
  | "FORBIDDEN"
  | "INTERNAL";

/** TOKEN_EXPIRED is retryable only after refreshing the token, so it is not in this set. */
export const RETRYABLE: ReadonlySet<ErrorCode> = new Set<ErrorCode>([
  "RATE_LIMITED",
  "PERSISTENCE_UNAVAILABLE",
  "INTERNAL",
]);

export interface ErrorBody {
  code: ErrorCode;
  retryable: boolean;
  message?: string;
  retry_after_ms?: number | null;
  client_msg_id?: string | null;
}

// ---- ws.py: client -> server ---------------------------------------------------------------

interface Frame {
  v: number;
}

export interface AuthFrame extends Frame {
  type: "auth";
  token: string;
  device_id: string;
}
export interface SubscribeFrame extends Frame {
  type: "subscribe";
  session_id: string;
  after_seq: number;
}
export interface MessageSendFrame extends Frame {
  type: "message.send";
  client_msg_id: string;
  body: string;
  trace_id?: string | null;
}
/** Cumulative delivery ack (drives the second tick). */
export interface AckFrame extends Frame {
  type: "ack";
  up_to_seq: number;
}
export interface ReadFrame extends Frame {
  type: "read";
  up_to_seq: number;
}
export interface PingFrame extends Frame {
  type: "ping";
}

export type ClientFrame =
  | AuthFrame
  | SubscribeFrame
  | MessageSendFrame
  | AckFrame
  | ReadFrame
  | PingFrame;

/** Distributes Omit over the union so each member keeps its own fields. */
export type ClientFrameInput = ClientFrame extends infer F
  ? F extends ClientFrame
    ? Omit<F, "v">
    : never
  : never;

// ---- ws.py: server -> client ---------------------------------------------------------------

export interface AuthOkFrame extends Frame {
  type: "auth.ok";
  user_id: string;
}
/** First tick: the message is stored. */
export interface MessageAckFrame extends Frame {
  type: "message.ack";
  client_msg_id: string;
  message_id: string;
  seq: number;
}
export interface MessageCreatedFrame extends Frame {
  type: "message.created";
  event_id: string;
  trace_id?: string | null;
  session_id: string;
  sequence: number; // == message.seq
  message: Message;
}
export interface SyncBatchFrame extends Frame {
  type: "sync.batch";
  session_id: string;
  messages: Message[];
  has_more: boolean;
}
/** Cursors per participant (last_delivered_seq). Clients derive the ticks (Deep Dive §2). */
export interface ReceiptUpdatedFrame extends Frame {
  type: "receipt.updated";
  session_id: string;
  cursors: Record<string, number>;
}
export interface AgentStatusFrame extends Frame {
  type: "agent.status";
  session_id: string;
  state: AgentState;
  name: string;
  budget: BudgetView;
}
export interface MemberEventFrame extends Frame {
  type: "member.joined" | "member.left";
  session_id: string;
  participant_id: string;
  display_name: string;
}
export interface SessionUpdatedFrame extends Frame {
  type: "session.updated";
  session_id: string;
  title?: string | null;
  status?: string | null;
}
export interface ServerDrainingFrame extends Frame {
  type: "server.draining";
  reconnect_after_ms: number;
}
export interface ErrorFrame extends Frame {
  type: "error";
  error: ErrorBody;
}
export interface PongFrame extends Frame {
  type: "pong";
}

export type ServerFrame =
  | AuthOkFrame
  | MessageAckFrame
  | MessageCreatedFrame
  | SyncBatchFrame
  | ReceiptUpdatedFrame
  | AgentStatusFrame
  | MemberEventFrame
  | SessionUpdatedFrame
  | ServerDrainingFrame
  | ErrorFrame
  | PongFrame;

export const SERVER_FRAME_TYPES: ReadonlySet<string> = new Set<ServerFrame["type"]>([
  "auth.ok",
  "message.ack",
  "message.created",
  "sync.batch",
  "receipt.updated",
  "agent.status",
  "member.joined",
  "member.left",
  "session.updated",
  "server.draining",
  "error",
  "pong",
]);

// Close codes
export const CLOSE_SLOW_CONSUMER = 4008;
export const CLOSE_AUTH_TIMEOUT = 4001;

// ---- REST (Deep Dive §6; not modelled in libs/contracts) -----------------------------------

/** POST /auth/guest */
export interface GuestAuthRequest {
  display_name: string;
}
/** POST /auth/guest and POST /auth/refresh ("new tokens"; the refresh token rotates, §8). */
export interface TokenResponse {
  user_id?: string;
  access_token: string;
  refresh_token: string;
}
/** GET /sessions/{id}/messages (same shape as internal.py MessagePage). */
export interface MessagePage {
  messages: Message[];
  has_more: boolean;
}
/** POST /sessions/{id}/messages */
export interface SubmitMessageResponse {
  message_id: string;
  seq: number;
}
/** PATCH /sessions/{id}/agent. `status` is agents.status (Deep Dive §4): active | paused. */
export interface AgentPatch {
  status?: "active" | "paused";
  name?: string;
}
