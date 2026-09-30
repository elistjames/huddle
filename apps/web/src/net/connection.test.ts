import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeSocket } from "../test/fakeSocket";
import { AuthError } from "./auth";
import { Connection, type ConnectionOptions, type ConnectionStatus } from "./connection";

const flush = () => vi.advanceTimersByTimeAsync(0);

function setup(over: Partial<ConnectionOptions> = {}) {
  const statuses: ConnectionStatus[] = [];
  const frames: unknown[] = [];
  const subscribed: number[] = [];
  const fatal: string[] = [];
  let after = 0;
  const getToken = vi.fn(() => Promise.resolve("tok-1"));
  const refreshToken = vi.fn(() => Promise.resolve("tok-2"));
  const conn = new Connection({
    url: "ws://test/ws",
    sessionId: "ses_main",
    deviceId: "dev-1",
    createSocket: (url) => new FakeSocket(url),
    getToken,
    refreshToken,
    afterSeq: () => after,
    onFrame: (f) => frames.push(f),
    onStatus: (s) => statuses.push(s),
    onSubscribed: (a) => subscribed.push(a),
    onFatal: (r) => fatal.push(r),
    random: () => 0.5,
    ...over,
  });
  return {
    conn,
    statuses,
    frames,
    subscribed,
    fatal,
    getToken,
    refreshToken,
    setAfter: (n: number) => {
      after = n;
    },
  };
}

/** Open the latest socket, authenticate and finish the replay. */
async function goLive() {
  const s = FakeSocket.last();
  s.open();
  await flush();
  s.receive({ type: "auth.ok", user_id: "usr_me" });
  s.receive({ type: "sync.batch", session_id: "ses_main", messages: [], has_more: false });
  return s;
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.reset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("connection setup", () => {
  it("authenticates first, then subscribes from the cursor, with v=1 on every frame", async () => {
    const t = setup();
    t.setAfter(42);
    t.conn.start();
    expect(t.statuses).toEqual(["connecting"]);
    const s = FakeSocket.last();
    s.open();
    await flush();
    expect(s.sent).toEqual([{ v: 1, type: "auth", token: "tok-1", device_id: "dev-1" }]);
    s.receive({ type: "auth.ok", user_id: "usr_me" });
    expect(s.sent[1]).toEqual({ v: 1, type: "subscribe", session_id: "ses_main", after_seq: 42 });
    expect(t.subscribed).toEqual([42]);
    expect(t.statuses).toEqual(["connecting"]);
    s.receive({ type: "sync.batch", session_id: "ses_main", messages: [], has_more: true });
    expect(t.statuses).toEqual(["connecting"]);
    s.receive({ type: "sync.batch", session_id: "ses_main", messages: [], has_more: false });
    expect(t.statuses).toEqual(["connecting", "live"]);
  });

  it("only sends once authenticated", async () => {
    const t = setup();
    t.conn.start();
    expect(t.conn.send({ type: "ack", up_to_seq: 1 })).toBe(false);
    const s = FakeSocket.last();
    s.open();
    await flush();
    expect(t.conn.send({ type: "ack", up_to_seq: 1 })).toBe(false);
    s.receive({ type: "auth.ok", user_id: "usr_me" });
    expect(t.conn.send({ type: "message.send", client_msg_id: "c1", body: "hi" })).toBe(true);
    expect(s.sent.at(-1)).toEqual({ v: 1, type: "message.send", client_msg_id: "c1", body: "hi" });
  });

  it("forwards frames and ignores other protocol versions, unknown types and bad JSON", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    const before = t.frames.length;
    s.receive({ v: 2, type: "pong" });
    s.receiveRaw('{"v":1,"type":"surprise"}');
    s.receiveRaw("not json");
    expect(t.frames).toHaveLength(before);
    s.receiveRaw('{"type":"pong"}'); // no v: accepted
    expect(t.frames).toHaveLength(before + 1);
  });
});

describe("heartbeat", () => {
  it("pings every 20 s and reconnects when no frame answers within the pong timeout", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(s.types().filter((x) => x === "ping")).toHaveLength(1);
    s.receive({ type: "pong" });
    await vi.advanceTimersByTimeAsync(20_000);
    expect(s.types().filter((x) => x === "ping")).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(10_000); // no pong
    expect(s.readyState).toBe(3);
    expect(t.statuses.at(-1)).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(250); // backoff(0) with random 0.5
    expect(FakeSocket.created).toHaveLength(2);
  });
});

describe("reconnect backoff (full jitter, base 500 ms, cap 30 s)", () => {
  it("waits random * min(cap, 500 * 2^attempt) between failed attempts", async () => {
    const t = setup();
    t.conn.start();
    await goLive();
    const expected = [250, 500, 1000, 2000, 4000, 8000, 15_000, 15_000];
    for (const delay of expected) {
      const count = FakeSocket.created.length;
      FakeSocket.last().drop();
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(FakeSocket.created).toHaveLength(count);
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeSocket.created).toHaveLength(count + 1);
    }
    expect(t.statuses.at(-1)).toBe("reconnecting");
  });

  it("resets the attempts once the replay after a reconnect completes", async () => {
    const t = setup();
    t.conn.start();
    await goLive();
    FakeSocket.last().drop();
    await vi.advanceTimersByTimeAsync(250);
    FakeSocket.last().drop();
    await vi.advanceTimersByTimeAsync(500);
    expect(t.conn.attempts).toBe(2);
    await goLive();
    expect(t.conn.attempts).toBe(0);
    expect(t.statuses.at(-1)).toBe("live");
  });

  it("resubscribes from the current cursor after a slow-consumer close (4008)", async () => {
    const t = setup();
    t.conn.start();
    await goLive();
    t.setAfter(77);
    FakeSocket.last().drop(4008);
    await vi.advanceTimersByTimeAsync(250);
    const s = FakeSocket.last();
    s.open();
    await flush();
    s.receive({ type: "auth.ok", user_id: "usr_me" });
    expect(s.sent[1]).toMatchObject({ type: "subscribe", after_seq: 77 });
    expect(t.statuses).toContain("reconnecting");
  });

  it("the first connection failing shows reconnecting (the backoff loop is running)", async () => {
    const t = setup();
    t.conn.start();
    FakeSocket.last().drop();
    expect(t.statuses).toEqual(["connecting", "reconnecting"]);
    await vi.advanceTimersByTimeAsync(250);
    expect(t.statuses.at(-1)).toBe("reconnecting");
  });

  it("ignores events from a socket it has replaced", async () => {
    const t = setup();
    t.conn.start();
    const old = await goLive();
    old.drop();
    await vi.advanceTimersByTimeAsync(250);
    const count = t.frames.length;
    old.receive({ type: "pong" });
    expect(t.frames).toHaveLength(count);
  });

  it("stop() closes the socket and never reconnects", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    t.conn.stop();
    expect(s.readyState).toBe(3);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.created).toHaveLength(1);
    expect(t.statuses.at(-1)).toBe("stopped");
  });
});

describe("server.draining", () => {
  it("reconnects at reconnect_after_ms without counting a failure", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    s.receive({ type: "server.draining", reconnect_after_ms: 3_000 });
    await vi.advanceTimersByTimeAsync(2_999);
    expect(s.readyState).toBe(1);
    expect(t.statuses.at(-1)).toBe("live");
    await vi.advanceTimersByTimeAsync(1);
    expect(s.readyState).toBe(3);
    await vi.advanceTimersByTimeAsync(1); // fake timers run a 0 ms timer set during a tick 1 ms later
    expect(FakeSocket.created).toHaveLength(2);
    expect(t.conn.attempts).toBe(0);
  });

  it("waits out the delay when the server closes first", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    s.receive({ type: "server.draining", reconnect_after_ms: 3_000 });
    await vi.advanceTimersByTimeAsync(1_000);
    s.drop(1001);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(FakeSocket.created).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.created).toHaveLength(2);
    expect(t.conn.attempts).toBe(0);
  });
});

describe("auth errors", () => {
  it("TOKEN_EXPIRED: refreshes and re-authenticates on the same socket without resubscribing", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    s.receive({ type: "error", error: { code: "TOKEN_EXPIRED", retryable: false } });
    expect(t.conn.send({ type: "ack", up_to_seq: 1 })).toBe(false);
    await flush();
    expect(t.refreshToken).toHaveBeenCalledOnce();
    expect(s.sent.at(-1)).toEqual({ v: 1, type: "auth", token: "tok-2", device_id: "dev-1" });
    s.receive({ type: "auth.ok", user_id: "usr_me" });
    expect(s.types().filter((x) => x === "subscribe")).toHaveLength(1);
    expect(FakeSocket.created).toHaveLength(1);
    expect(t.statuses.slice(-2)).toEqual(["live", "live"]);
    expect(t.conn.send({ type: "ack", up_to_seq: 1 })).toBe(true);
  });

  it("UNAUTHENTICATED on the first auth: one refresh, then fatal", async () => {
    const t = setup();
    t.conn.start();
    const s = FakeSocket.last();
    s.open();
    await flush();
    s.receive({ type: "error", error: { code: "UNAUTHENTICATED", retryable: false } });
    await flush();
    expect(t.refreshToken).toHaveBeenCalledOnce();
    s.receive({ type: "error", error: { code: "UNAUTHENTICATED", retryable: false } });
    expect(t.fatal).toEqual(["unauthenticated"]);
    expect(t.statuses.at(-1)).toBe("failed");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.created).toHaveLength(1);
  });

  it("a rejected token refresh is fatal (back to join)", async () => {
    const t = setup({ getToken: () => Promise.reject(new AuthError()) });
    t.conn.start();
    FakeSocket.last().open();
    await flush();
    expect(t.fatal).toEqual(["unauthenticated"]);
  });

  it("an unreachable token endpoint just backs off and retries", async () => {
    const getToken = vi.fn().mockRejectedValueOnce(new TypeError("fetch failed")).mockResolvedValue("tok-1");
    const t = setup({ getToken });
    t.conn.start();
    FakeSocket.last().open();
    await flush();
    expect(t.fatal).toEqual([]);
    expect(t.statuses.at(-1)).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(250);
    FakeSocket.last().open();
    await flush();
    expect(FakeSocket.last().sent[0]).toMatchObject({ type: "auth", token: "tok-1" });
  });

  it("PROTOCOL_UNSUPPORTED is fatal and stops reconnecting", async () => {
    const t = setup();
    t.conn.start();
    const s = await goLive();
    s.receive({ type: "error", error: { code: "PROTOCOL_UNSUPPORTED", retryable: false } });
    expect(t.fatal).toEqual(["protocol"]);
    expect(t.statuses.at(-1)).toBe("failed");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.created).toHaveLength(1);
  });
});
