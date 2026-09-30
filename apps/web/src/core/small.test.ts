import { describe, expect, it } from "vitest";
import type { ErrorBody, ErrorCode } from "../contracts";
import { BACKOFF_CAP_MS, backoffCeiling, backoffDelay } from "./backoff";
import { ackTimeoutError, failureView, parseErrorBody } from "./errors";
import { parseBody } from "./text";
import { UUID_V7_PATTERN, uuidv7 } from "./uuidv7";

describe("backoff: full jitter, base 500 ms, cap 30 s", () => {
  it("doubles the ceiling per attempt up to the cap", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 7, 50].map(backoffCeiling)).toEqual([
      500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });

  it("draws uniformly from [0, ceiling)", () => {
    expect(backoffDelay(0, () => 0)).toBe(0);
    expect(backoffDelay(0, () => 0.999)).toBe(499);
    expect(backoffDelay(3, () => 0.5)).toBe(2000);
    expect(backoffDelay(20, () => 0.9999)).toBeLessThan(BACKOFF_CAP_MS);
  });

  it("stays within bounds for random draws", () => {
    for (let a = 0; a < 12; a++) {
      for (let i = 0; i < 50; i++) {
        const d = backoffDelay(a);
        expect(d).toBeGreaterThanOrEqual(0);
        expect(d).toBeLessThan(backoffCeiling(a));
      }
    }
  });
});

describe("uuidv7", () => {
  it("is a version 7, variant 10 UUID", () => {
    for (let i = 0; i < 20; i++) expect(uuidv7()).toMatch(UUID_V7_PATTERN);
  });

  it("encodes the millisecond timestamp in the first 48 bits", () => {
    const id = uuidv7(0x0192_3456_789a, (b) => b.fill(0));
    expect(id).toBe("01923456-789a-7000-8000-000000000000");
  });

  it("sorts by time", () => {
    expect(uuidv7(1000) < uuidv7(2000)).toBe(true);
  });
});

describe("failureView: Retry only when retryable", () => {
  const body = (code: ErrorCode, retryable = false): ErrorBody => ({ code, retryable });
  const cases: [ErrorBody, ReturnType<typeof failureView>][] = [
    [body("RATE_LIMITED", true), { errorCode: "RATE_LIMITED", retryable: true }],
    [body("PERSISTENCE_UNAVAILABLE", true), { errorCode: undefined, retryable: true }],
    [body("INTERNAL", true), { errorCode: undefined, retryable: true }],
    [body("MESSAGE_TOO_LONG"), { errorCode: "MESSAGE_TOO_LONG", retryable: false }],
    [body("VALIDATION"), { errorCode: "MESSAGE_TOO_LONG", retryable: false }],
    [body("NOT_MEMBER"), { errorCode: "NOT_MEMBER", retryable: false }],
    [body("FORBIDDEN"), { errorCode: "NOT_MEMBER", retryable: false }],
    [body("SESSION_CLOSED"), { errorCode: "SESSION_CLOSED", retryable: false }],
    // The code table wins if a server forgets the flag.
    [body("RATE_LIMITED", false), { errorCode: "RATE_LIMITED", retryable: true }],
  ];
  it.each(cases)("%j", (err, expected) => {
    expect(failureView(err)).toEqual(expected);
  });

  it("never pairs a Retry-less bubble copy with a retryable error or vice versa", () => {
    const noRetryCopy = new Set(["MESSAGE_TOO_LONG", "NOT_MEMBER", "SESSION_CLOSED"]);
    for (const [err] of cases) {
      const v = failureView(err);
      expect(v.errorCode !== undefined && noRetryCopy.has(v.errorCode)).toBe(!v.retryable);
    }
  });

  it("treats an ack timeout as a retryable 'Didn't send'", () => {
    expect(failureView(ackTimeoutError("x"))).toEqual({ errorCode: undefined, retryable: true });
  });
});

describe("parseErrorBody", () => {
  it("reads {error: ErrorBody}, a bare body and FastAPI detail", () => {
    expect(parseErrorBody(429, { error: { code: "RATE_LIMITED", retryable: true, retry_after_ms: 5 } })).toMatchObject({
      code: "RATE_LIMITED",
      retryable: true,
      retry_after_ms: 5,
    });
    expect(parseErrorBody(403, { code: "NOT_MEMBER", retryable: false }).code).toBe("NOT_MEMBER");
    expect(parseErrorBody(401, { detail: { code: "TOKEN_EXPIRED" } }).code).toBe("TOKEN_EXPIRED");
    expect(parseErrorBody(401, { detail: { error: { code: "TOKEN_EXPIRED" } } }).code).toBe("TOKEN_EXPIRED");
  });

  it("falls back on the HTTP status", () => {
    expect(parseErrorBody(401, null).code).toBe("UNAUTHENTICATED");
    expect(parseErrorBody(503, "nope")).toMatchObject({ code: "INTERNAL", retryable: true });
    expect(parseErrorBody(422, { detail: "bad" }).code).toBe("VALIDATION");
  });
});

describe("parseBody: plain text and code only", () => {
  it("keeps plain text as text, including markup", () => {
    expect(parseBody("<b>hi</b> & <script>x</script>")).toEqual([
      { kind: "text", text: "<b>hi</b> & <script>x</script>" },
    ]);
  });

  it("splits inline code", () => {
    expect(parseBody("run `npm test` now")).toEqual([
      { kind: "text", text: "run " },
      { kind: "code", text: "npm test" },
      { kind: "text", text: " now" },
    ]);
  });

  it("splits fenced blocks with a language", () => {
    expect(parseBody("before\n```ts\nconst a = 1;\n```\nafter")).toEqual([
      { kind: "text", text: "before\n" },
      { kind: "block", text: "const a = 1;", lang: "ts" },
      { kind: "text", text: "\nafter" },
    ]);
  });

  it("leaves an unclosed fence as text", () => {
    expect(parseBody("```\nno end")).toEqual([{ kind: "text", text: "```\nno end" }]);
  });
});
