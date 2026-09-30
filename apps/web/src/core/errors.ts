import { RETRYABLE, type ErrorBody, type ErrorCode } from "../contracts";

/** The error codes MessageBubble has copy for (design/components/MessageBubble/README.md). */
export type BubbleErrorCode = "RATE_LIMITED" | "MESSAGE_TOO_LONG" | "NOT_MEMBER" | "SESSION_CLOSED";

export interface FailureView {
  /** Passed to MessageBubble; `undefined` renders "Didn't send". */
  errorCode: BubbleErrorCode | undefined;
  /** Whether a Retry action is offered. MessageBubble shows Retry exactly when this is true. */
  retryable: boolean;
}

const ERROR_CODES: ReadonlySet<string> = new Set<ErrorCode>([
  "UNAUTHENTICATED",
  "TOKEN_EXPIRED",
  "NOT_MEMBER",
  "SESSION_CLOSED",
  "MESSAGE_TOO_LONG",
  "RATE_LIMITED",
  "PERSISTENCE_UNAVAILABLE",
  "PROTOCOL_UNSUPPORTED",
  "VALIDATION",
  "FORBIDDEN",
  "INTERNAL",
]);

export function isErrorCode(value: unknown): value is ErrorCode {
  return typeof value === "string" && ERROR_CODES.has(value);
}

export function isRetryable(err: ErrorBody): boolean {
  return err.retryable || RETRYABLE.has(err.code);
}

/**
 * Map an error (Deep Dive §6 error model) to the failed-bubble copy from the design system.
 *
 * The bubble decides whether to show Retry from `errorCode` alone (no code or RATE_LIMITED:
 * Retry; the other three: no Retry). So the mapping keeps "Retry only when retryable":
 *   retryable     -> RATE_LIMITED keeps its copy ("Sending too fast"), everything else "Didn't send".
 *   not retryable -> MESSAGE_TOO_LONG / VALIDATION: "Too long to send (4,000 characters max)"
 *                    (length is the only message validation the composer can't prevent),
 *                    SESSION_CLOSED: "This huddle has ended",
 *                    anything else (NOT_MEMBER, FORBIDDEN, ...): "You're not in this huddle".
 */
export function failureView(err: ErrorBody): FailureView {
  if (isRetryable(err)) {
    return { errorCode: err.code === "RATE_LIMITED" ? "RATE_LIMITED" : undefined, retryable: true };
  }
  switch (err.code) {
    case "MESSAGE_TOO_LONG":
    case "VALIDATION":
      return { errorCode: "MESSAGE_TOO_LONG", retryable: false };
    case "SESSION_CLOSED":
      return { errorCode: "SESSION_CLOSED", retryable: false };
    default:
      return { errorCode: "NOT_MEMBER", retryable: false };
  }
}

/** Deep Dive §10 "Delivery failed ... an error, or the ack timed out": a timeout is retryable. */
export function ackTimeoutError(clientMsgId: string): ErrorBody {
  return { code: "INTERNAL", retryable: true, message: "ack timed out", client_msg_id: clientMsgId };
}

/** Parse an error from a REST response body: `{error: ErrorBody}`, a bare ErrorBody, or FastAPI's `{detail}`. */
export function parseErrorBody(status: number, json: unknown): ErrorBody {
  const candidates: unknown[] = [];
  if (json && typeof json === "object") {
    const o = json as Record<string, unknown>;
    candidates.push(o.error, o, o.detail);
    if (o.detail && typeof o.detail === "object") {
      candidates.push((o.detail as Record<string, unknown>).error);
    }
  }
  for (const c of candidates) {
    if (c && typeof c === "object" && isErrorCode((c as Record<string, unknown>).code)) {
      const e = c as Partial<ErrorBody> & { code: ErrorCode };
      return {
        code: e.code,
        retryable: typeof e.retryable === "boolean" ? e.retryable : RETRYABLE.has(e.code),
        message: typeof e.message === "string" ? e.message : "",
        retry_after_ms: e.retry_after_ms ?? null,
        client_msg_id: e.client_msg_id ?? null,
      };
    }
  }
  const code: ErrorCode =
    status === 401
      ? "UNAUTHENTICATED"
      : status === 403
        ? "FORBIDDEN"
        : status === 413
          ? "MESSAGE_TOO_LONG"
          : status === 429
            ? "RATE_LIMITED"
            : status >= 500 || status === 0
              ? "INTERNAL"
              : "VALIDATION";
  return { code, retryable: RETRYABLE.has(code), message: `HTTP ${status}` };
}
