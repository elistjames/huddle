/**
 * Guest tokens (Deep Dive §8): a 15-minute JWT access token kept in memory only, and a 30-day
 * refresh token that rotates on every use, kept in localStorage so a returning guest skips the
 * join form. The access token is refreshed shortly before it expires and whenever the server
 * says TOKEN_EXPIRED.
 */
import type { TokenResponse } from "../contracts";

/** The credentials were rejected: the guest has to join again. */
export class AuthError extends Error {
  constructor(message = "not authenticated") {
    super(message);
    this.name = "AuthError";
  }
}

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export const REFRESH_TOKEN_KEY = "huddle.refresh_token";
export const DISPLAY_NAME_KEY = "huddle.display_name";
export const DEVICE_ID_KEY = "huddle.device_id";
/** Refresh this long before `exp`, so a token never expires in flight. */
export const EXPIRY_MARGIN_MS = 30_000;

export interface JwtClaims {
  sub?: string;
  exp?: number;
}

/** Read (not verify) the claims of a JWT. Verification is the server's job. */
export function decodeJwt(token: string): JwtClaims {
  const part = token.split(".")[1];
  if (!part) return {};
  try {
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const json = atob(b64.padEnd(b64.length + ((4 - (b64.length % 4)) % 4), "="));
    const claims = JSON.parse(json) as unknown;
    if (!claims || typeof claims !== "object") return {};
    const c = claims as Record<string, unknown>;
    const out: JwtClaims = {};
    if (typeof c.sub === "string") out.sub = c.sub;
    if (typeof c.exp === "number") out.exp = c.exp;
    return out;
  } catch {
    return {};
  }
}

export type RefreshFn = (refreshToken: string) => Promise<TokenResponse>;

/** Runs `fn` while holding an exclusive lock shared by every tab of this origin. */
export type LockFn = <T>(name: string, fn: () => Promise<T>) => Promise<T>;
export const REFRESH_LOCK = "huddle.refresh";

/** navigator.locks when the browser has it; otherwise no cross-tab lock (single-tab safety only). */
export function browserLock(): LockFn | null {
  const locks = typeof navigator === "undefined" ? undefined : (navigator as Partial<Navigator>).locks;
  if (!locks) return null;
  return (name, fn) => locks.request(name, fn);
}

export class TokenStore {
  private access: string | null = null;
  private user: string | null = null;
  private inflight: Promise<string> | null = null;

  /**
   * Refresh tokens rotate, and reusing a rotated one revokes the whole family (chat service).
   * So refreshes are serialised: one in-flight refresh per tab, and a cross-tab lock around
   * read-token -> refresh -> store, so no two tabs ever send the same refresh token.
   */
  constructor(
    private readonly storage: KeyValueStore,
    private readonly refreshFn: RefreshFn,
    private readonly now: () => number = Date.now,
    private readonly lock: LockFn | null = browserLock(),
  ) {}

  get userId(): string | null {
    return this.user;
  }

  get hasRefreshToken(): boolean {
    return this.readRefresh() !== null;
  }

  /** Store a fresh pair. Only the refresh token is persisted. */
  set(tokens: TokenResponse): void {
    this.access = tokens.access_token;
    this.user = tokens.user_id ?? decodeJwt(tokens.access_token).sub ?? this.user;
    try {
      this.storage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
    } catch {
      /* storage unavailable: the session lasts until reload */
    }
  }

  clear(): void {
    this.access = null;
    this.user = null;
    this.inflight = null;
    try {
      this.storage.removeItem(REFRESH_TOKEN_KEY);
    } catch {
      /* ignore */
    }
  }

  /** A usable access token, refreshing first when it is missing or about to expire. */
  async accessToken(): Promise<string> {
    const token = this.access;
    if (token !== null) {
      const { exp } = decodeJwt(token);
      if (exp === undefined || exp * 1000 - this.now() > EXPIRY_MARGIN_MS) return token;
    }
    return this.refresh();
  }

  /** Rotate the refresh token. Concurrent callers share one request. */
  refresh(): Promise<string> {
    this.inflight ??= this.doRefresh().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
  }

  private doRefresh(): Promise<string> {
    return this.lock ? this.lock(REFRESH_LOCK, () => this.rotate()) : this.rotate();
  }

  /** Read the newest refresh token (another tab may have just rotated it) and rotate it. */
  private async rotate(): Promise<string> {
    const rt = this.readRefresh();
    if (rt === null) throw new AuthError("no refresh token");
    const tokens = await this.refreshFn(rt); // AuthError when rejected; other errors propagate
    this.set(tokens);
    return tokens.access_token;
  }

  private readRefresh(): string | null {
    try {
      return this.storage.getItem(REFRESH_TOKEN_KEY);
    } catch {
      return null;
    }
  }
}
