import type { Message } from "../contracts";
import type { OutboxEntry } from "../core/thread";

export const SID = "ses_main";
export const ME = "usr_me";
export const MAYA = "usr_maya";
export const JORDAN = "usr_jordan";
export const AGENT = "agt_huddle";

const T0 = Date.parse("2026-09-30T10:42:00Z");

/** A stored message at `seq`; by default from Maya, one second apart. */
export function msg(seq: number, over: Partial<Message> = {}): Message {
  return {
    id: `msg_${seq}`,
    session_id: SID,
    seq,
    sender_type: "user",
    sender_id: MAYA,
    client_msg_id: `cm-${seq}`,
    kind: "text",
    body: `message ${seq}`,
    created_at: new Date(T0 + seq * 1000).toISOString(),
    ...over,
  };
}

export function mine(seq: number, clientMsgId: string, over: Partial<Message> = {}): Message {
  return msg(seq, { sender_id: ME, client_msg_id: clientMsgId, body: `mine ${seq}`, ...over });
}

export function entry(clientMsgId: string, over: Partial<OutboxEntry> = {}): OutboxEntry {
  return {
    clientMsgId,
    body: `body of ${clientMsgId}`,
    createdAt: new Date(T0 + 500_000).toISOString(),
    status: "pending",
    error: null,
    ...over,
  };
}

/** Seqs held, in order. */
export function seqs(bySeq: ReadonlyMap<number, Message>): number[] {
  return [...bySeq.keys()].sort((a, b) => a - b);
}
