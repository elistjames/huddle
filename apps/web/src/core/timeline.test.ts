import { describe, expect, it } from "vitest";
import { AGENT, JORDAN, MAYA, ME, SID, entry, mine, msg } from "../test/fixtures";
import { initialThread, threadReducer, type ThreadAction } from "./thread";
import { buildTimeline, type TimelineContext } from "./timeline";

const ctx: TimelineContext = {
  names: new Map([
    [MAYA, "Maya Chen"],
    [JORDAN, "Jordan Reyes"],
    [ME, "Eli St. James"],
  ]),
  agentId: AGENT,
  agentName: "Huddle AI",
};
const run = (actions: ThreadAction[]) => actions.reduce(threadReducer, initialThread(SID, ME));
const at = (min: number, sec = 0) => new Date(Date.UTC(2026, 8, 30, 10, min, sec)).toISOString();

describe("buildTimeline", () => {
  it("groups consecutive messages by sender and minute", () => {
    const s = run([
      { type: "created", message: msg(1, { created_at: at(42, 1) }) },
      { type: "created", message: msg(2, { created_at: at(42, 30) }) },
      { type: "created", message: msg(3, { created_at: at(43, 0) }) },
      { type: "created", message: msg(4, { sender_id: JORDAN, created_at: at(43, 5) }) },
    ]);
    const items = buildTimeline(s, ctx);
    expect(items.map((i) => (i.type === "group" ? [i.author, i.messages.length] : i.text))).toEqual([
      ["Maya Chen", 2],
      ["Maya Chen", 1],
      ["Jordan Reyes", 1],
    ]);
  });

  it("renders system messages as notes that break groups", () => {
    const s = run([
      { type: "created", message: msg(1, { created_at: at(42) }) },
      {
        type: "created",
        message: msg(2, { kind: "system", sender_type: "system", sender_id: "system", body: "Maya Chen paused Huddle AI", created_at: at(42) }),
      },
      { type: "created", message: msg(3, { created_at: at(42) }) },
    ]);
    const items = buildTimeline(s, ctx);
    expect(items.map((i) => i.type)).toEqual(["group", "note", "group"]);
    expect(items[1]).toMatchObject({ type: "note", text: "Maya Chen paused Huddle AI" });
  });

  it("labels the agent with its current name and kind agent", () => {
    const s = run([{ type: "created", message: msg(1, { sender_type: "agent", sender_id: AGENT }) }]);
    expect(buildTimeline(s, { ...ctx, agentName: "Sage" })[0]).toMatchObject({ kind: "agent", author: "Sage" });
  });

  it("gives my stored messages receipt statuses from the cursors, others none", () => {
    const s = run([
      { type: "created", message: mine(1, "a") },
      { type: "created", message: mine(2, "b") },
      { type: "created", message: mine(3, "c") },
      { type: "created", message: msg(4) },
      { type: "receipts", cursors: { [MAYA]: 2, [AGENT]: 1 } },
    ]);
    const items = buildTimeline(s, ctx);
    const self = items[0];
    expect(self?.type === "group" && self.kind).toBe("self");
    expect(self?.type === "group" && self.messages.map((m) => m.status)).toEqual(["agent-read", "delivered", "stored"]);
    const other = items[1];
    expect(other?.type === "group" && other.messages[0]?.status).toBeUndefined();
  });

  it("shows messages from my other devices as mine", () => {
    const s = run([{ type: "created", message: mine(1, "other-device") }]);
    expect(buildTimeline(s, ctx)[0]).toMatchObject({ kind: "self" });
  });

  it("appends pending and failed sends after stored messages, with a stable key", () => {
    const s = run([
      { type: "created", message: msg(1) },
      { type: "enqueued", entry: entry("a") },
      { type: "enqueued", entry: entry("b") },
      { type: "failed", clientMsgId: "b", error: { code: "INTERNAL", retryable: true } },
    ]);
    const items = buildTimeline(s, ctx);
    const last = items[items.length - 1];
    expect(last?.type === "group" && last.messages.map((m) => [m.key, m.status])).toEqual([
      ["c-a", "sending"],
      ["c-b", "failed"],
    ]);
    const acked = threadReducer(s, { type: "acked", clientMsgId: "a", messageId: "msg_2", seq: 2 });
    const stored = buildTimeline(acked, ctx).flatMap((i) => (i.type === "group" ? i.messages : []));
    expect(stored.find((m) => m.key === "c-a")?.status).toBe("stored");
  });

  it("skips redacted messages and names unknown senders Guest", () => {
    const s = run([
      { type: "created", message: msg(1, { body: null }) },
      { type: "created", message: msg(2, { sender_id: "usr_unknown" }) },
    ]);
    const items = buildTimeline(s, ctx);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ author: "Guest" });
  });
});
