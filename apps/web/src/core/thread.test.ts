import { describe, expect, it } from "vitest";
import type { ErrorBody } from "../contracts";
import { AGENT, JORDAN, MAYA, ME, SID, entry, mine, msg, seqs } from "../test/fixtures";
import {
  findGap,
  initialThread,
  isEmptyThread,
  orderedMessages,
  receiptStatus,
  threadReducer,
  type ThreadAction,
  type ThreadState,
} from "./thread";

const run = (actions: ThreadAction[], start: ThreadState = initialThread(SID, ME)) =>
  actions.reduce(threadReducer, start);

/** A thread that has finished a replay of 1..n. */
const synced = (n: number) =>
  run([
    { type: "subscribed", afterSeq: 0 },
    { type: "synced", messages: Array.from({ length: n }, (_, i) => msg(i + 1)), hasMore: false },
  ]);

const rateLimited: ErrorBody = { code: "RATE_LIMITED", retryable: true, retry_after_ms: 2000 };

describe("ordering: render by seq, never by arrival", () => {
  it("orders messages that arrive out of order", () => {
    const s = run([
      { type: "created", message: msg(3) },
      { type: "created", message: msg(1) },
      { type: "created", message: msg(2) },
    ]);
    expect(orderedMessages(s).map((m) => m.seq)).toEqual([1, 2, 3]);
  });

  it("interleaves a replay page and live events by seq", () => {
    const s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "created", message: msg(5) },
      { type: "synced", messages: [msg(1), msg(2), msg(3), msg(4)], hasMore: false },
    ]);
    expect(orderedMessages(s).map((m) => m.seq)).toEqual([1, 2, 3, 4, 5]);
    expect(s.cursor).toBe(5);
  });

  it("ignores arrival timestamps: an earlier created_at with a later seq still renders later", () => {
    const s = run([
      { type: "created", message: msg(2, { created_at: "2020-01-01T00:00:00Z" }) },
      { type: "created", message: msg(1) },
    ]);
    expect(orderedMessages(s).map((m) => m.seq)).toEqual([1, 2]);
  });
});

describe("dedupe by message_id and seq", () => {
  it("keeps one copy of a message delivered twice", () => {
    const once = run([{ type: "created", message: msg(1) }]);
    const twice = threadReducer(once, { type: "created", message: msg(1) });
    expect(twice).toBe(once);
    expect(twice.bySeq.size).toBe(1);
  });

  it("dedupes the overlap between a replay page and live events", () => {
    const s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "created", message: msg(2) },
      { type: "created", message: msg(3) },
      { type: "synced", messages: [msg(1), msg(2), msg(3)], hasMore: false },
      { type: "fetched", messages: [msg(2), msg(3)] },
    ]);
    expect(seqs(s.bySeq)).toEqual([1, 2, 3]);
  });

  it("ignores a known message_id arriving with another seq", () => {
    const s = run([
      { type: "created", message: msg(1) },
      { type: "created", message: msg(2, { id: "msg_1" }) },
    ]);
    expect(seqs(s.bySeq)).toEqual([1]);
  });

  it("keeps the first server copy when another id claims a held seq", () => {
    const s = run([
      { type: "created", message: msg(1, { body: "first" }) },
      { type: "created", message: msg(1, { id: "msg_other", body: "second" }) },
    ]);
    expect(s.bySeq.get(1)?.body).toBe("first");
    expect(s.seqById.has("msg_other")).toBe(false);
  });

  it("ignores messages for another session and invalid seqs", () => {
    const s = run([
      { type: "created", message: msg(1, { session_id: "ses_other" }) },
      { type: "created", message: msg(0) },
      { type: "created", message: msg(-3) },
      { type: "created", message: msg(1.5) },
    ]);
    expect(s.bySeq.size).toBe(0);
  });
});

describe("cursor and gap detection", () => {
  it("advances the contiguous cursor", () => {
    expect(synced(3).cursor).toBe(3);
  });

  it("holding 43 and receiving 45 asks for 44", () => {
    const s = run([{ type: "created", message: msg(45) }], synced(43));
    expect(s.cursor).toBe(43);
    expect(s.maxSeq).toBe(45);
    expect(findGap(s)).toEqual({ afterSeq: 43, missing: [44] });
    const filled = threadReducer(s, { type: "fetched", messages: [msg(44), msg(45)] });
    expect(filled.cursor).toBe(45);
    expect(findGap(filled)).toBeNull();
  });

  it("lists every missing seq up to the next held one", () => {
    const s = run([{ type: "created", message: msg(5) }], synced(1));
    expect(findGap(s)).toEqual({ afterSeq: 1, missing: [2, 3, 4] });
  });

  it("reports no gap while a replay is running (the replay fills it)", () => {
    const s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "created", message: msg(9) },
    ]);
    expect(findGap(s)).toBeNull();
    const done = threadReducer(s, { type: "synced", messages: [msg(1)], hasMore: false });
    expect(findGap(done)).toEqual({ afterSeq: 1, missing: [2, 3, 4, 5, 6, 7, 8] });
  });

  it("reports no gap when caught up", () => {
    expect(findGap(synced(3))).toBeNull();
    expect(findGap(initialThread(SID))).toBeNull();
  });

  it("a fresh join that gets the last window starts its cursor below the window", () => {
    const window = Array.from({ length: 200 }, (_, i) => msg(801 + i));
    const s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: window, hasMore: false },
    ]);
    expect(s.floor).toBe(800);
    expect(s.cursor).toBe(1000);
    expect(s.hasEarlier).toBe(true);
    expect(findGap(s)).toBeNull();
  });

  it("a reconnect replay that skips seqs is a gap, not a window", () => {
    const s = run(
      [
        { type: "subscribed", afterSeq: 3 },
        { type: "synced", messages: [msg(6)], hasMore: false },
      ],
      synced(3),
    );
    expect(s.floor).toBe(0);
    expect(findGap(s)).toEqual({ afterSeq: 3, missing: [4, 5] });
  });

  it("stays syncing across has_more pages and completes on the last", () => {
    let s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: [msg(1), msg(2)], hasMore: true },
    ]);
    expect(s.syncing).toBe(true);
    expect(s.synced).toBe(false);
    s = threadReducer(s, { type: "synced", messages: [msg(3)], hasMore: false });
    expect(s.syncing).toBe(false);
    expect(s.synced).toBe(true);
    expect(s.cursor).toBe(3);
  });

  it("an empty session replays nothing and is synced at 0", () => {
    const s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: [], hasMore: false },
    ]);
    expect(s.synced).toBe(true);
    expect(s.cursor).toBe(0);
    expect(s.hasEarlier).toBe(false);
  });

  it("loading earlier history lowers the floor and never moves the cursor back", () => {
    const window = [msg(11), msg(12)];
    let s = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: window, hasMore: false },
    ]);
    expect(s.floor).toBe(10);
    s = threadReducer(s, { type: "earlier", messages: [msg(6), msg(7), msg(8), msg(9), msg(10)], hasMore: true });
    expect(s.floor).toBe(5);
    expect(s.hasEarlier).toBe(true);
    expect(s.cursor).toBe(12);
    s = threadReducer(s, { type: "earlier", messages: [1, 2, 3, 4, 5].map((n) => msg(n)), hasMore: false });
    expect(s.floor).toBe(0);
    expect(s.hasEarlier).toBe(false);
    expect(orderedMessages(s).map((m) => m.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  });

  it("uses the cursor as the next after_seq after a reconnect replay", () => {
    const s = run(
      [
        { type: "subscribed", afterSeq: 2 },
        { type: "synced", messages: [msg(3), msg(4)], hasMore: false },
      ],
      synced(2),
    );
    expect(s.cursor).toBe(4);
    expect(s.replayAfter).toBe(2);
  });
});

describe("outbox: pending -> stored, failed, retry", () => {
  it("enqueues a pending entry once per client_msg_id", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "enqueued", entry: entry("a") },
    ]);
    expect(s.outbox).toHaveLength(1);
    expect(s.outbox[0]?.status).toBe("pending");
  });

  it("an ack stores the message at its seq before message.created arrives", () => {
    const s = run([
      { type: "enqueued", entry: entry("a", { body: "hello" }) },
      { type: "acked", clientMsgId: "a", messageId: "msg_1", seq: 1 },
    ]);
    expect(s.outbox).toHaveLength(0);
    expect(s.bySeq.get(1)).toMatchObject({ id: "msg_1", sender_id: ME, body: "hello", client_msg_id: "a" });
    expect(s.provisional.has(1)).toBe(true);
    expect(s.cursor).toBe(1);
  });

  it("the server copy replaces the ack-built one without duplicating it", () => {
    const s = run([
      { type: "enqueued", entry: entry("a", { body: "hello" }) },
      { type: "acked", clientMsgId: "a", messageId: "msg_1", seq: 1 },
      { type: "created", message: mine(1, "a", { id: "msg_1", body: "hello", created_at: "2026-09-30T11:00:00Z" }) },
    ]);
    expect(s.bySeq.size).toBe(1);
    expect(s.provisional.size).toBe(0);
    expect(s.bySeq.get(1)?.created_at).toBe("2026-09-30T11:00:00Z");
  });

  it("message.created before the ack clears the entry; the late ack changes nothing", () => {
    const before = run([
      { type: "enqueued", entry: entry("a") },
      { type: "created", message: mine(1, "a") },
    ]);
    expect(before.outbox).toHaveLength(0);
    const after = threadReducer(before, { type: "acked", clientMsgId: "a", messageId: "msg_1", seq: 1 });
    expect(after).toBe(before);
  });

  it("a replay that contains the message clears the entry (its ack was lost)", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: [msg(1), mine(2, "a")], hasMore: false },
    ]);
    expect(s.outbox).toHaveLength(0);
    expect(s.bySeq.get(2)?.client_msg_id).toBe("a");
  });

  it("someone else's message with the same client_msg_id does not clear my entry", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "created", message: msg(1, { client_msg_id: "a" }) },
    ]);
    expect(s.outbox).toHaveLength(1);
  });

  it("ignores an ack it cannot place", () => {
    const s = initialThread(SID, ME);
    expect(threadReducer(s, { type: "acked", clientMsgId: "zz", messageId: "msg_9", seq: 9 })).toBe(s);
  });

  it("marks a pending entry failed with its error", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "failed", clientMsgId: "a", error: rateLimited },
    ]);
    expect(s.outbox[0]).toMatchObject({ status: "failed", error: rateLimited });
  });

  it("a late error for a stored message does not mark it failed", () => {
    const stored = run([
      { type: "enqueued", entry: entry("a") },
      { type: "acked", clientMsgId: "a", messageId: "msg_1", seq: 1 },
    ]);
    expect(threadReducer(stored, { type: "failed", clientMsgId: "a", error: rateLimited })).toBe(stored);
  });

  it("retry puts the same entry (same client_msg_id) back to pending", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "failed", clientMsgId: "a", error: rateLimited },
      { type: "retrying", clientMsgId: "a" },
    ]);
    expect(s.outbox).toEqual([expect.objectContaining({ clientMsgId: "a", status: "pending", error: null })]);
  });

  it("retry is a no-op for pending or unknown entries", () => {
    const s = run([{ type: "enqueued", entry: entry("a") }]);
    expect(threadReducer(s, { type: "retrying", clientMsgId: "a" })).toBe(s);
    expect(threadReducer(s, { type: "retrying", clientMsgId: "b" })).toBe(s);
  });

  it("does not build a message from an ack before it knows who I am", () => {
    const s = run([
      { type: "enqueued", entry: entry("a") },
      { type: "acked", clientMsgId: "a", messageId: "msg_1", seq: 1 },
    ], initialThread(SID, null));
    expect(s.bySeq.size).toBe(0);
    expect(s.outbox).toHaveLength(0);
  });
});

describe("receipts: cursors -> ticks", () => {
  it("merges cursors monotonically", () => {
    let s = run([{ type: "receipts", cursors: { [MAYA]: 5, [AGENT]: 3 } }]);
    s = threadReducer(s, { type: "receipts", cursors: { [MAYA]: 2, [JORDAN]: 4 } });
    expect(s.receipts).toEqual({ [MAYA]: 5, [AGENT]: 3, [JORDAN]: 4 });
  });

  it("returns the same state when nothing moves forward", () => {
    const s = run([{ type: "receipts", cursors: { [MAYA]: 5 } }]);
    expect(threadReducer(s, { type: "receipts", cursors: { [MAYA]: 5 } })).toBe(s);
    expect(threadReducer(s, { type: "receipts", cursors: { [MAYA]: Number.NaN } })).toBe(s);
  });

  const cases: [string, Record<string, number>, string][] = [
    ["no cursors: stored", {}, "stored"],
    ["only my own devices: stored", { [ME]: 10 }, "stored"],
    ["another human behind: stored", { [MAYA]: 4 }, "stored"],
    ["any one other human at seq: delivered", { [MAYA]: 5, [JORDAN]: 0 }, "delivered"],
    ["another human past seq: delivered", { [JORDAN]: 9 }, "delivered"],
    ["agent read, no human yet: stored (linear state machine)", { [AGENT]: 5 }, "stored"],
    ["human and agent: agent-read", { [MAYA]: 5, [AGENT]: 5 }, "agent-read"],
    ["agent behind: delivered", { [MAYA]: 5, [AGENT]: 4 }, "delivered"],
  ];
  it.each(cases)("%s", (_name, cursors, expected) => {
    expect(receiptStatus(5, cursors, ME, AGENT)).toBe(expected);
  });

  it("recognises the agent by its agt_ id even before the agent id is known", () => {
    expect(receiptStatus(5, { [MAYA]: 5, agt_other: 5 }, ME, null)).toBe("agent-read");
  });
});

describe("empty thread", () => {
  it("is empty with only system notes", () => {
    const s = run([{ type: "created", message: msg(1, { kind: "system", sender_type: "system", body: "Maya Chen started the huddle" }) }]);
    expect(isEmptyThread(s)).toBe(true);
  });

  it("is not empty with a message, a pending send or earlier history", () => {
    expect(isEmptyThread(synced(1))).toBe(false);
    expect(isEmptyThread(run([{ type: "enqueued", entry: entry("a") }]))).toBe(false);
    const windowed = run([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: [msg(5, { kind: "system", sender_type: "system" })], hasMore: false },
    ]);
    expect(isEmptyThread(windowed)).toBe(false);
  });

  it("ignores redacted messages", () => {
    expect(isEmptyThread(run([{ type: "created", message: msg(1, { body: null }) }]))).toBe(true);
  });

  it("identity sets who I am once", () => {
    const s = initialThread(SID);
    const a = threadReducer(s, { type: "identity", me: ME });
    expect(a.me).toBe(ME);
    expect(threadReducer(a, { type: "identity", me: ME })).toBe(a);
  });
});
