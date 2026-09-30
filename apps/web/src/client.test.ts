import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HuddleClient, type ClientDeps } from "./client";
import { failureView } from "./core/errors";
import { orderedMessages } from "./core/thread";
import { buildTimeline } from "./core/timeline";
import { MOCK_AGENT_ID, MOCK_SESSION_ID, MOCK_WS_URL, MockServer, type MockServerOptions } from "./mock/server";
import { REFRESH_TOKEN_KEY } from "./net/auth";

const advance = (ms: number) => vi.advanceTimersByTimeAsync(ms);

let server: MockServer;
let sockets = 0;
let fetched: string[] = [];

function newServer(opts: MockServerOptions = {}): MockServer {
  server = new MockServer({ latencyMs: 5, ...opts });
  return server;
}

function newClient(over: Partial<ClientDeps> = {}): HuddleClient {
  return new HuddleClient(
    { sessionId: MOCK_SESSION_ID, apiBase: "", wsUrl: MOCK_WS_URL },
    {
      fetch: (input, init) => {
        fetched.push(input);
        return server.fetch(input, init);
      },
      createSocket: (url) => {
        sockets++;
        return server.createSocket(url);
      },
      storage: window.localStorage,
      random: () => 0.5,
      isVisible: () => true,
      ...over,
    },
  );
}

async function joined(name = "Eli", over: Partial<ClientDeps> = {}): Promise<HuddleClient> {
  const c = newClient(over);
  c.start();
  await advance(0);
  const p = c.join(name);
  await advance(200);
  await p;
  await advance(200);
  return c;
}

const bodies = (c: HuddleClient) => orderedMessages(c.getSnapshot().thread).map((m) => m.body);

/** The bubble status of my message with this body, as the UI would show it. */
function statusOf(c: HuddleClient, body: string) {
  const s = c.getSnapshot();
  const items = buildTimeline(s.thread, { names: s.names, agentId: s.agent?.id ?? null, agentName: "Huddle AI" });
  for (const i of items) if (i.type === "group") for (const m of i.messages) if (m.body === body) return m.status;
  return undefined;
}

function outboxId(c: HuddleClient, body: string): string {
  const e = c.getSnapshot().thread.outbox.find((x) => x.body === body);
  if (!e) throw new Error(`no outbox entry for ${body}`);
  return e.clientMsgId;
}

const storedWith = (clientMsgId: string) => server.messages.filter((m) => m.client_msg_id === clientMsgId);

let client: HuddleClient | null = null;

beforeEach(() => {
  vi.useFakeTimers();
  sockets = 0;
  fetched = [];
});
afterEach(() => {
  client?.dispose();
  client = null;
  server.dispose();
  vi.useRealTimers();
});

describe("join and connect", () => {
  it("joins as a guest, keeps only the refresh token in storage, loads history and goes live", async () => {
    newServer();
    client = await joined("Eli");
    const s = client.getSnapshot();
    expect(s.phase).toBe("in");
    expect(s.connection).toBe("live");
    expect(s.thread.synced).toBe(true);
    expect(s.title).toBe("Launch post");
    expect(s.thread.cursor).toBe(server.lastSeq);
    expect(bodies(client).at(-1)).toBe("Eli joined");
    expect(Object.keys(window.localStorage).sort()).toEqual(
      ["huddle.device_id", "huddle.display_name", REFRESH_TOKEN_KEY].sort(),
    );
    const stored = Object.values(window.localStorage).join(" ");
    expect(stored).not.toContain("eyJ"); // no JWT in storage
  });

  it("restores a returning guest from the refresh token, and falls back to join when it is rejected", async () => {
    newServer();
    const first = await joined("Eli");
    first.dispose();
    client = newClient();
    client.start();
    await advance(400);
    expect(client.getSnapshot().phase).toBe("in");
    expect(client.getSnapshot().connection).toBe("live");
    client.dispose();

    window.localStorage.setItem(REFRESH_TOKEN_KEY, "rt_bogus");
    client = newClient();
    client.start();
    await advance(400);
    expect(client.getSnapshot().phase).toBe("join");
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
  });

  it("a fresh join loads the last window; Load earlier pages in the rest", async () => {
    newServer({ windowSize: 3 });
    client = await joined();
    expect(client.getSnapshot().thread.hasEarlier).toBe(true);
    expect(bodies(client)).toHaveLength(3);
    const p = client.loadEarlier();
    await advance(50);
    await p;
    expect(bodies(client)).toEqual(server.messages.map((m) => m.body));
    expect(client.getSnapshot().thread.hasEarlier).toBe(false);
  });

  it("acks the rendered cursor", async () => {
    newServer();
    client = await joined();
    const me = client.getSnapshot().userId ?? "";
    client.markRendered();
    await advance(20);
    expect(server.cursors[me]).toBe(server.lastSeq);
  });
});

describe("receipts follow the cursors", () => {
  it("sending -> ✓ stored -> ✓✓ delivered -> amber ✓ read by the agent", async () => {
    newServer({ agent: true, people: true });
    client = await joined();
    client.send("hello");
    expect(statusOf(client, "hello")).toBe("sending");
    await advance(12);
    expect(statusOf(client, "hello")).toBe("stored");
    await advance(700);
    expect(statusOf(client, "hello")).toBe("delivered");
    await advance(700);
    expect(statusOf(client, "hello")).toBe("agent-read");
  });

  it("a paused agent leaves messages at ✓✓ until it catches up (D-020)", async () => {
    newServer();
    client = await joined();
    server.setAgentPaused(true);
    client.send("while paused");
    await advance(3_000);
    expect(client.getSnapshot().agent?.state).toBe("paused");
    expect(statusOf(client, "while paused")).toBe("delivered");
    server.setAgentPaused(false);
    await advance(1_000);
    expect(statusOf(client, "while paused")).toBe("agent-read");
  });
});

describe("outbox retry idempotency", () => {
  it("a retryable failure shows Retry; the retry reuses client_msg_id and is stored once", async () => {
    newServer({ agent: false });
    client = await joined();
    server.failNextSend("PERSISTENCE_UNAVAILABLE");
    client.send("db was down");
    const id = outboxId(client, "db was down");
    await advance(20);
    const entry = client.getSnapshot().thread.outbox[0];
    expect(entry?.status).toBe("failed");
    expect(entry && entry.error && failureView(entry.error)).toEqual({ errorCode: undefined, retryable: true });
    client.retry(id);
    await advance(20);
    expect(statusOf(client, "db was down")).toBe("stored");
    expect(storedWith(id)).toHaveLength(1);
  });

  it("an ack timeout fails the send; Retry hits the server's idempotency and gets the same seq", async () => {
    newServer({ agent: false, people: false });
    client = await joined();
    server.dropNextAck();
    server.dropNextFanout();
    client.send("lost ack");
    const id = outboxId(client, "lost ack");
    await advance(10_000);
    expect(client.getSnapshot().thread.outbox[0]?.status).toBe("failed");
    const seq = storedWith(id)[0]?.seq;
    client.retry(id);
    await advance(20);
    expect(client.getSnapshot().thread.outbox).toHaveLength(0);
    expect(storedWith(id)).toHaveLength(1);
    expect(server.submissions.filter((s) => s.client_msg_id === id).map((s) => s.duplicate)).toEqual([false, true]);
    expect(orderedMessages(client.getSnapshot().thread).filter((m) => m.client_msg_id === id).map((m) => m.seq)).toEqual([seq]);
  });

  it("a send lost in a network blip stays pending and is re-sent with the same id after reconnect", async () => {
    newServer({ agent: false });
    client = await joined();
    client.send("in the blip");
    const id = outboxId(client, "in the blip");
    server.dropConnections(); // before the frame reaches the server
    await advance(20);
    expect(client.getSnapshot().connection).toBe("reconnecting");
    expect(statusOf(client, "in the blip")).toBe("sending");
    await advance(1_000);
    expect(client.getSnapshot().connection).toBe("live");
    expect(statusOf(client, "in the blip")).not.toBe("sending");
    expect(storedWith(id)).toHaveLength(1);
  });

  it("a send stored just before the blip is not duplicated: the replay or the dedupe settles it", async () => {
    newServer({ agent: false, people: false });
    client = await joined();
    client.send("stored then blip");
    const id = outboxId(client, "stored then blip");
    await advance(7); // the server has stored it; the ack is still on the wire
    expect(storedWith(id)).toHaveLength(1);
    server.dropConnections();
    await advance(1_000);
    expect(client.getSnapshot().connection).toBe("live");
    expect(client.getSnapshot().thread.outbox).toHaveLength(0);
    expect(storedWith(id)).toHaveLength(1);
    expect(bodies(client).filter((b) => b === "stored then blip")).toHaveLength(1);
  });

  it("non-retryable errors fail without Retry", async () => {
    newServer({ agent: false });
    client = await joined();
    server.failNextSend("NOT_MEMBER");
    client.send("nope");
    await advance(20);
    const entry = client.getSnapshot().thread.outbox[0];
    expect(entry?.error?.code).toBe("NOT_MEMBER");
    expect(entry && entry.error && failureView(entry.error).retryable).toBe(false);
  });

  it("RATE_LIMITED fails with 'Sending too fast' and Retry", async () => {
    newServer({ agent: false });
    client = await joined();
    server.failNextSend("RATE_LIMITED");
    client.send("fast");
    await advance(20);
    const e = client.getSnapshot().thread.outbox[0]?.error;
    expect(e && failureView(e)).toEqual({ errorCode: "RATE_LIMITED", retryable: true });
  });
});

describe("gaps, draining and token expiry", () => {
  it("holding N and receiving N+2 fetches N+1 through the catch-up endpoint", async () => {
    newServer({ agent: false, people: false });
    client = await joined();
    server.dropNextFanout();
    server.postAs("usr_maya", "this one is lost on the wire");
    server.postAs("usr_maya", "this one arrives");
    await advance(100);
    expect(bodies(client).slice(-2)).toEqual(["this one is lost on the wire", "this one arrives"]);
    expect(client.getSnapshot().thread.cursor).toBe(server.lastSeq);
    expect(fetched).toContain(
      `/sessions/${MOCK_SESSION_ID}/messages?after_seq=${server.lastSeq - 2}&limit=200`,
    );
  });

  it("server.draining: reconnects after the delay and resyncs without duplicates", async () => {
    newServer({ agent: false });
    client = await joined();
    const before = bodies(client);
    server.drain(1_000);
    await advance(300);
    server.postAs("usr_jordan", "said while draining");
    await advance(2_000);
    expect(client.getSnapshot().connection).toBe("live");
    expect(bodies(client)).toEqual([...before, "said while draining"]);
    expect(sockets).toBe(2);
  });

  it("TOKEN_EXPIRED mid-connection: refresh and re-auth on the same socket, then sending works", async () => {
    newServer({ agent: false });
    client = await joined();
    const rt = window.localStorage.getItem(REFRESH_TOKEN_KEY);
    server.expireAccessTokens();
    client.send("after expiry");
    await advance(200);
    expect(sockets).toBe(1);
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).not.toBe(rt); // rotated
    expect(statusOf(client, "after expiry")).not.toBe("sending");
    expect(client.getSnapshot().thread.outbox).toHaveLength(0);
  });
});

describe("agent controls, session end, export", () => {
  it("pause, resume and rename via PATCH; the system notes arrive through the log", async () => {
    newServer({ agent: false });
    client = await joined("Eli");
    let p = client.pause();
    await advance(50);
    await p;
    expect(client.getSnapshot().agent?.state).toBe("paused");
    p = client.resume();
    await advance(50);
    await p;
    p = client.rename("Sage");
    await advance(50);
    await p;
    expect(client.getSnapshot().agent).toMatchObject({ name: "Sage", state: "following", id: MOCK_AGENT_ID });
    expect(bodies(client).slice(-3)).toEqual(["Eli paused Huddle AI", "Eli resumed Huddle AI", "Eli renamed the AI to Sage"]);
  });

  it("agent.status carries the budget as a percentage and refill time; resting when spent", async () => {
    newServer({ agent: false });
    client = await joined();
    server.exhaustBudget();
    await advance(20);
    const a = client.getSnapshot().agent;
    expect(a?.state).toBe("resting");
    expect(a?.budget.used_pct).toBe(100);
    expect(Date.parse(a?.budget.refills_at ?? "") % 86_400_000).toBe(0); // UTC window boundary
  });

  it("a closed session stops sending", async () => {
    newServer({ agent: false });
    client = await joined();
    server.endSession();
    await advance(20);
    expect(client.getSnapshot().sessionStatus).toBe("closed");
    client.send("too late");
    expect(client.getSnapshot().thread.outbox).toHaveLength(0);
  });

  it("export is a JSON dump of every message in seq order", async () => {
    newServer({ agent: false, windowSize: 2 });
    client = await joined();
    const p = client.exportTranscript();
    await advance(100);
    const dump = JSON.parse(await p) as { session_id: string; messages: { seq: number }[] };
    expect(dump.session_id).toBe(MOCK_SESSION_ID);
    expect(dump.messages.map((m) => m.seq)).toEqual(server.messages.map((m) => m.seq));
  });
});
