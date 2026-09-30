import { describe, expect, it, vi } from "vitest";
import type { TokenResponse } from "../contracts";
import { Api, ApiError } from "./api";
import { AuthError, REFRESH_TOKEN_KEY, TokenStore, decodeJwt, type KeyValueStore } from "./auth";

function jwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64(claims)}.sig`;
}

function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => {
      data.set(k, v);
    },
    removeItem: (k) => {
      data.delete(k);
    },
  };
}

const NOW = Date.parse("2026-09-30T12:00:00Z");
const pair = (n: number, expInSec = 900): TokenResponse => ({
  user_id: "usr_me",
  access_token: jwt({ sub: "usr_me", exp: NOW / 1000 + expInSec, n }),
  refresh_token: `rt-${n}`,
});

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("decodeJwt", () => {
  it("reads sub and exp from a base64url payload", () => {
    expect(decodeJwt(jwt({ sub: "usr_é?>", exp: 123 }))).toEqual({ sub: "usr_é?>", exp: 123 });
  });
  it("tolerates garbage", () => {
    expect(decodeJwt("nope")).toEqual({});
    expect(decodeJwt("a.b.c")).toEqual({});
  });
});

describe("TokenStore", () => {
  it("keeps the access token in memory and only the refresh token in storage", () => {
    const storage = memoryStore();
    const tokens = new TokenStore(storage, vi.fn(), () => NOW);
    tokens.set(pair(1));
    expect([...storage.data.entries()]).toEqual([[REFRESH_TOKEN_KEY, "rt-1"]]);
    expect(tokens.userId).toBe("usr_me");
  });

  it("returns the cached access token until it is about to expire, then refreshes (rotating)", async () => {
    const storage = memoryStore();
    let now = NOW;
    const refresh = vi.fn((rt: string) => Promise.resolve(pair(rt === "rt-1" ? 2 : 3)));
    const tokens = new TokenStore(storage, refresh, () => now);
    tokens.set(pair(1));
    const first = await tokens.accessToken();
    expect(refresh).not.toHaveBeenCalled();
    now = NOW + 880_000; // 20 s before exp: inside the 30 s margin
    const second = await tokens.accessToken();
    expect(second).not.toBe(first);
    expect(refresh).toHaveBeenCalledWith("rt-1");
    expect(storage.getItem(REFRESH_TOKEN_KEY)).toBe("rt-2");
  });

  it("shares one refresh between concurrent callers", async () => {
    const storage = memoryStore();
    storage.setItem(REFRESH_TOKEN_KEY, "rt-1");
    const refresh = vi.fn(() => Promise.resolve(pair(2)));
    const tokens = new TokenStore(storage, refresh, () => NOW);
    const [a, b] = await Promise.all([tokens.accessToken(), tokens.refresh()]);
    expect(a).toBe(b);
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("fails with AuthError when there is no refresh token", async () => {
    const tokens = new TokenStore(memoryStore(), vi.fn(), () => NOW);
    await expect(tokens.accessToken()).rejects.toBeInstanceOf(AuthError);
  });

  it("clear() forgets both tokens", () => {
    const storage = memoryStore();
    const tokens = new TokenStore(storage, vi.fn(), () => NOW);
    tokens.set(pair(1));
    tokens.clear();
    expect(tokens.hasRefreshToken).toBe(false);
    expect(tokens.userId).toBeNull();
  });
});

describe("Api", () => {
  it("POST /auth/guest with the display name", async () => {
    const fetch = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(json(200, pair(1))));
    const api = new Api("https://api.test", fetch);
    await expect(api.guest("Eli")).resolves.toEqual(pair(1));
    const [url, init] = fetch.mock.calls[0] ?? [];
    expect(url).toBe("https://api.test/auth/guest");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ display_name: "Eli" }));
  });

  it("POST /auth/refresh authenticates with the refresh token; a 401 is an AuthError", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(json(200, pair(2)))
      .mockResolvedValueOnce(json(401, { error: { code: "TOKEN_EXPIRED", retryable: false } }));
    const api = new Api("", fetch);
    await api.refresh("rt-1");
    const init = fetch.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer rt-1");
    expect(init?.body).toBeUndefined();
    await expect(api.refresh("rt-1")).rejects.toBeInstanceOf(AuthError);
  });

  it("pages messages with after_seq / before_seq and caps limit at 200", async () => {
    const fetch = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(json(200, { messages: [], has_more: false })));
    const api = new Api("", fetch);
    api.useTokens({ accessToken: () => Promise.resolve("a"), refresh: () => Promise.resolve("b") });
    await api.getMessages("ses_main", { afterSeq: 43, limit: 500 });
    await api.getMessages("ses_main", { beforeSeq: 10, limit: 50 });
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/sessions/ses_main/messages?after_seq=43&limit=200",
      "/sessions/ses_main/messages?before_seq=10&limit=50",
    ]);
  });

  it("refreshes once and retries a request that got 401", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(json(401, { error: { code: "TOKEN_EXPIRED", retryable: false } }))
      .mockResolvedValueOnce(json(200, { id: "ses_main" }));
    const api = new Api("", fetch);
    const refresh = vi.fn(() => Promise.resolve("fresh"));
    api.useTokens({ accessToken: () => Promise.resolve("stale"), refresh });
    await expect(api.getSession("ses_main")).resolves.toEqual({ id: "ses_main" });
    expect(refresh).toHaveBeenCalledOnce();
    const auth = fetch.mock.calls.map((c) => new Headers(c[1]?.headers).get("Authorization"));
    expect(auth).toEqual(["Bearer stale", "Bearer fresh"]);
  });

  it("PATCH /sessions/{id}/agent sends {status} or {name}", async () => {
    const fetch = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(json(200, {})));
    const api = new Api("", fetch);
    api.useTokens({ accessToken: () => Promise.resolve("a"), refresh: () => Promise.resolve("b") });
    await api.patchAgent("ses_main", { status: "paused" });
    await api.patchAgent("ses_main", { name: "Sage" });
    expect(fetch.mock.calls.map((c) => [c[0], c[1]?.method, c[1]?.body])).toEqual([
      ["/sessions/ses_main/agent", "PATCH", '{"status":"paused"}'],
      ["/sessions/ses_main/agent", "PATCH", '{"name":"Sage"}'],
    ]);
  });

  it("turns error responses into ApiError with the error model", async () => {
    const fetch = vi.fn(() => Promise.resolve(json(403, { error: { code: "NOT_MEMBER", retryable: false } })));
    const api = new Api("", fetch);
    api.useTokens({ accessToken: () => Promise.resolve("a"), refresh: () => Promise.resolve("b") });
    const err: unknown = await api.getSession("ses_main").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).body.code).toBe("NOT_MEMBER");
  });
});
