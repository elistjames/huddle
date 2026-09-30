/**
 * Turns the thread into what the screen shows: MessageGroups and SystemNotes in `seq` order,
 * then your unconfirmed (pending or failed) messages, which have no `seq` yet.
 * Grouping follows the MessageGroup README: a new group when the sender or the minute changes;
 * a system note always breaks a group.
 */
import type { ErrorBody, Message } from "../contracts";
import { minuteKey, formatTime } from "./time";
import { orderedMessages, receiptStatus, type ThreadState } from "./thread";

export type GroupKind = "self" | "other" | "agent";
export type BubbleStatus = "sending" | "stored" | "delivered" | "agent-read" | "failed";

export interface TimelineMessage {
  key: string;
  body: string;
  /** Only own messages carry a status. */
  status?: BubbleStatus;
  error?: ErrorBody;
  /** Set for outbox entries, so the UI can retry them. */
  clientMsgId?: string;
}

export type TimelineItem =
  | { type: "note"; key: string; text: string; time: string }
  | {
      type: "group";
      key: string;
      kind: GroupKind;
      author: string;
      time: string;
      messages: TimelineMessage[];
    };

export interface TimelineContext {
  /** participant_id -> display name (includes people who have left). */
  names: ReadonlyMap<string, string>;
  agentId: string | null;
  agentName: string;
}

interface Pending {
  senderKey: string;
  minute: number;
  group: Extract<TimelineItem, { type: "group" }>;
}

export function buildTimeline(state: ThreadState, ctx: TimelineContext): TimelineItem[] {
  const items: TimelineItem[] = [];
  let open: Pending | null = null;

  const push = (senderKey: string, kind: GroupKind, author: string, at: string, msg: TimelineMessage) => {
    const minute = minuteKey(at);
    if (open?.senderKey === senderKey && open.minute === minute) {
      open.group.messages.push(msg);
      return;
    }
    const group: Pending["group"] = {
      type: "group",
      key: `g-${msg.key}`,
      kind,
      author,
      time: formatTime(at),
      messages: [msg],
    };
    items.push(group);
    open = { senderKey, minute, group };
  };

  for (const m of orderedMessages(state)) {
    if (m.body === null) continue; // redacted
    if (m.kind === "system" || m.sender_type === "system") {
      items.push({ type: "note", key: `n-${m.id}`, text: m.body, time: formatTime(m.created_at) });
      open = null;
      continue;
    }
    push(m.sender_id, kindOf(m, state.me), authorOf(m, ctx), m.created_at, stored(m, state, ctx));
  }

  for (const e of state.outbox) {
    const msg: TimelineMessage = {
      key: `c-${e.clientMsgId}`,
      body: e.body,
      status: e.status === "failed" ? "failed" : "sending",
      clientMsgId: e.clientMsgId,
    };
    if (e.error) msg.error = e.error;
    push(state.me ?? "me", "self", ctx.names.get(state.me ?? "") ?? "You", e.createdAt, msg);
  }
  return items;
}

function kindOf(m: Message, me: string | null): GroupKind {
  if (m.sender_type === "agent") return "agent";
  return me !== null && m.sender_id === me ? "self" : "other";
}

function authorOf(m: Message, ctx: TimelineContext): string {
  if (m.sender_type === "agent") return ctx.agentName;
  return ctx.names.get(m.sender_id) ?? "Guest";
}

function stored(m: Message, state: ThreadState, ctx: TimelineContext): TimelineMessage {
  // Keyed by client_msg_id for own messages so the bubble keeps its identity from sending to stored.
  const own = state.me !== null && m.sender_id === state.me && m.sender_type === "user";
  const msg: TimelineMessage = { key: own ? `c-${m.client_msg_id}` : `m-${m.id}`, body: m.body ?? "" };
  if (own) msg.status = receiptStatus(m.seq, state.receipts, state.me, ctx.agentId);
  return msg;
}
