/**
 * The client's message log: a pure reducer plus selectors. No I/O, no timers.
 *
 * Deep Dive §2 rules implemented here:
 * - Render by `seq`, never by arrival time: stored messages live in a map keyed by `seq`.
 * - Dedupe by `message_id` and by `seq` whenever live events, acks and fetched pages are merged.
 * - Gap detection: `seq` is gapless, so holding 43 and receiving 45 means 44 must be fetched.
 * - Outbox: each own message gets a UUIDv7 `client_msg_id` that is reused on every retry; it
 *   stays in the outbox (pending or failed) until the server confirms it with a `seq`.
 * - Receipts are cursors per participant (`receipt.updated`), merged monotonically (§7: cursors
 *   only move forward) and turned into ticks per message by `receiptStatus`.
 */
import type { ErrorBody, Message } from "../contracts";

export type OutboxStatus = "pending" | "failed";

export interface OutboxEntry {
  clientMsgId: string;
  body: string;
  createdAt: string;
  status: OutboxStatus;
  error: ErrorBody | null;
}

export interface ThreadState {
  sessionId: string;
  /** The signed-in user's participant id; decides which messages are "self". */
  me: string | null;
  bySeq: ReadonlyMap<number, Message>;
  seqById: ReadonlyMap<string, number>;
  /** Seqs whose message was built locally from a `message.ack`; the server copy replaces them. */
  provisional: ReadonlySet<number>;
  /** History at or below `floor` is intentionally not loaded (the server served the last window). */
  floor: number;
  /** Highest seq such that every seq in (floor, cursor] is held. Sent as `after_seq` and acked. */
  cursor: number;
  maxSeq: number;
  receipts: Readonly<Record<string, number>>;
  outbox: readonly OutboxEntry[];
  /** A subscribe replay is in progress (live events meanwhile are merged by seq and deduped). */
  syncing: boolean;
  /** At least one replay has completed, so history is on screen. */
  synced: boolean;
  /** The `after_seq` of the replay in progress (0 = a fresh join, which may start at a window). */
  replayAfter: number;
  awaitingFirstBatch: boolean;
  hasEarlier: boolean;
}

export type ThreadAction =
  | { type: "identity"; me: string }
  | { type: "subscribed"; afterSeq: number }
  | { type: "synced"; messages: readonly Message[]; hasMore: boolean }
  | { type: "fetched"; messages: readonly Message[] }
  | { type: "earlier"; messages: readonly Message[]; hasMore: boolean }
  | { type: "created"; message: Message }
  | { type: "acked"; clientMsgId: string; messageId: string; seq: number }
  | { type: "receipts"; cursors: Readonly<Record<string, number>> }
  | { type: "enqueued"; entry: OutboxEntry }
  | { type: "failed"; clientMsgId: string; error: ErrorBody }
  | { type: "retrying"; clientMsgId: string };

export function initialThread(sessionId: string, me: string | null = null): ThreadState {
  return {
    sessionId,
    me,
    bySeq: new Map(),
    seqById: new Map(),
    provisional: new Set(),
    floor: 0,
    cursor: 0,
    maxSeq: 0,
    receipts: {},
    outbox: [],
    syncing: false,
    synced: false,
    replayAfter: 0,
    awaitingFirstBatch: false,
    hasEarlier: false,
  };
}

function contiguous(bySeq: ReadonlyMap<number, Message>, from: number): number {
  let c = from;
  while (bySeq.has(c + 1)) c++;
  return c;
}

/**
 * Merge server messages. A message is dropped if its `message_id` or its `seq` is already held
 * by a server copy; a provisional (ack-built) copy is replaced. Own messages clear their outbox
 * entry by `client_msg_id`, which also covers messages whose ack was lost.
 */
function merge(state: ThreadState, incoming: readonly Message[]): ThreadState {
  if (incoming.length === 0) return state;
  let bySeq: Map<number, Message> | null = null;
  let seqById: Map<string, number> | null = null;
  let provisional: Set<number> | null = null;
  let outbox = state.outbox;
  let maxSeq = state.maxSeq;
  for (const m of incoming) {
    if (m.session_id !== state.sessionId || !Number.isInteger(m.seq) || m.seq < 1) continue;
    const curSeq = bySeq ?? state.bySeq;
    const curIds = seqById ?? state.seqById;
    const curProv = provisional ?? state.provisional;
    const heldSeqForId = curIds.get(m.id);
    if (heldSeqForId !== undefined && !curProv.has(heldSeqForId)) continue;
    const held = curSeq.get(m.seq);
    if (held && !curProv.has(m.seq)) continue;
    bySeq ??= new Map(state.bySeq);
    seqById ??= new Map(state.seqById);
    provisional ??= new Set(state.provisional);
    if (held) seqById.delete(held.id);
    bySeq.set(m.seq, m);
    seqById.set(m.id, m.seq);
    provisional.delete(m.seq);
    if (m.seq > maxSeq) maxSeq = m.seq;
    if (state.me !== null && m.sender_id === state.me) {
      const next = outbox.filter((e) => e.clientMsgId !== m.client_msg_id);
      if (next.length !== outbox.length) outbox = next;
    }
  }
  if (!bySeq || !seqById || !provisional) return outbox === state.outbox ? state : { ...state, outbox };
  return {
    ...state,
    bySeq,
    seqById,
    provisional,
    outbox,
    maxSeq,
    cursor: contiguous(bySeq, Math.max(state.cursor, state.floor)),
  };
}

function minSeq(messages: readonly Message[]): number {
  let min = Infinity;
  for (const m of messages) if (m.seq < min) min = m.seq;
  return min;
}

export function threadReducer(state: ThreadState, action: ThreadAction): ThreadState {
  switch (action.type) {
    case "identity":
      return state.me === action.me ? state : { ...state, me: action.me };

    case "subscribed":
      return {
        ...state,
        syncing: true,
        replayAfter: action.afterSeq,
        awaitingFirstBatch: true,
      };

    case "synced": {
      let next = state;
      // A fresh join (after_seq = 0) loads "the last window" (Deep Dive §10). If the first page
      // starts above 1, history below it is intentionally not loaded; "Load earlier" pages it in.
      if (
        state.awaitingFirstBatch &&
        state.replayAfter === 0 &&
        !state.synced &&
        state.floor === 0 &&
        state.cursor === 0 &&
        action.messages.length > 0
      ) {
        const first = minSeq(action.messages);
        if (first > 1) next = { ...next, floor: first - 1, cursor: first - 1, hasEarlier: true };
      }
      next = merge(next, action.messages);
      next = { ...next, awaitingFirstBatch: false };
      if (!action.hasMore) next = { ...next, syncing: false, synced: true };
      return next;
    }

    case "fetched":
      return merge(state, action.messages);

    case "earlier": {
      const merged = merge(state, action.messages);
      const newFloor = action.hasMore ? Math.max(0, minSeq(action.messages) - 1) : 0;
      const floor = Math.min(state.floor, Number.isFinite(newFloor) ? newFloor : state.floor);
      return { ...merged, floor, hasEarlier: floor > 0 };
    }

    case "created":
      return merge(state, [action.message]);

    case "acked": {
      const entry = state.outbox.find((e) => e.clientMsgId === action.clientMsgId);
      const outbox = state.outbox.filter((e) => e.clientMsgId !== action.clientMsgId);
      const alreadyHeld = state.seqById.has(action.messageId) || state.bySeq.has(action.seq);
      if (alreadyHeld || !entry || state.me === null) {
        return outbox.length === state.outbox.length ? state : { ...state, outbox };
      }
      // ✓ stored: show the message at its seq now, even before its message.created arrives.
      const local: Message = {
        id: action.messageId,
        session_id: state.sessionId,
        seq: action.seq,
        sender_type: "user",
        sender_id: state.me,
        client_msg_id: entry.clientMsgId,
        kind: "text",
        body: entry.body,
        created_at: entry.createdAt,
      };
      const merged = merge({ ...state, outbox }, [local]);
      const provisional = new Set(merged.provisional);
      provisional.add(action.seq);
      return { ...merged, provisional };
    }

    case "receipts": {
      let changed = false;
      const receipts: Record<string, number> = { ...state.receipts };
      for (const [id, seq] of Object.entries(action.cursors)) {
        if (!Number.isFinite(seq)) continue;
        if (seq > (receipts[id] ?? 0)) {
          receipts[id] = seq;
          changed = true;
        }
      }
      return changed ? { ...state, receipts } : state;
    }

    case "enqueued":
      if (state.outbox.some((e) => e.clientMsgId === action.entry.clientMsgId)) return state;
      return { ...state, outbox: [...state.outbox, action.entry] };

    case "failed": {
      // A late error for a message that is already stored (or unknown) must not mark it failed.
      const idx = state.outbox.findIndex((e) => e.clientMsgId === action.clientMsgId);
      const entry = state.outbox[idx];
      if (!entry || entry.status !== "pending") return state;
      const outbox = state.outbox.slice();
      outbox[idx] = { ...entry, status: "failed", error: action.error };
      return { ...state, outbox };
    }

    case "retrying": {
      const idx = state.outbox.findIndex((e) => e.clientMsgId === action.clientMsgId);
      const entry = state.outbox[idx];
      if (!entry || entry.status !== "failed") return state;
      const outbox = state.outbox.slice();
      // Same clientMsgId: the server dedupes a retry of a message it already stored (§2).
      outbox[idx] = { ...entry, status: "pending", error: null };
      return { ...state, outbox };
    }
  }
}

// ---- selectors ---------------------------------------------------------------------------

/** Stored messages in `seq` order. */
export function orderedMessages(state: ThreadState): Message[] {
  return [...state.bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * The first missing range, if any: holding 43 and 45 gives `{afterSeq: 43, missing: [44]}`.
 * Not reported while a replay is running, since the replay fills it.
 */
export function findGap(state: ThreadState): { afterSeq: number; missing: number[] } | null {
  if (state.syncing || state.maxSeq <= state.cursor) return null;
  const missing: number[] = [];
  for (let s = state.cursor + 1; s <= state.maxSeq && missing.length < 200; s++) {
    if (!state.bySeq.has(s)) missing.push(s);
    else break;
  }
  return { afterSeq: state.cursor, missing };
}

export type ReceiptStatus = "stored" | "delivered" | "agent-read";

export function isAgentId(id: string, agentId: string | null): boolean {
  return (agentId !== null && id === agentId) || id.startsWith("agt_");
}

/**
 * Ticks for one of your own stored messages (Deep Dive §2, D-017, D-020):
 * ✓✓ when any other human's cursor is at or past `seq`; the amber third tick when the agent's is
 * too. The sender's client state machine is linear (§1: stored → delivered → agent-seen), and the
 * Receipt component draws the amber tick only on top of ✓✓, so agent-read needs delivered.
 */
export function receiptStatus(
  seq: number,
  receipts: Readonly<Record<string, number>>,
  me: string | null,
  agentId: string | null,
): ReceiptStatus {
  let delivered = false;
  let agentRead = false;
  for (const [id, cursor] of Object.entries(receipts)) {
    if (cursor < seq || id === me) continue;
    if (isAgentId(id, agentId)) agentRead = true;
    else delivered = true;
  }
  if (!delivered) return "stored";
  return agentRead ? "agent-read" : "delivered";
}

/** True when the thread has nothing anyone said (system notes do not count). */
export function isEmptyThread(state: ThreadState): boolean {
  if (state.outbox.length > 0) return false;
  for (const m of state.bySeq.values()) {
    if (m.kind === "text" && m.sender_type !== "system" && m.body !== null) return false;
  }
  return !state.hasEarlier;
}
