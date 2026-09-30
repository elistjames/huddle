/** Public REST client for the chat service (Deep Dive §6 HTTP endpoints). */
import type {
  AgentPatch,
  AgentView,
  ErrorBody,
  MessagePage,
  SessionView,
  TokenResponse,
} from "../contracts";
import { parseErrorBody } from "../core/errors";
import { AuthError } from "./auth";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: ErrorBody,
  ) {
    super(`${body.code} (HTTP ${status})`);
    this.name = "ApiError";
  }
}

export interface TokenSource {
  accessToken(): Promise<string>;
  refresh(): Promise<string>;
}

export const PAGE_LIMIT = 200; // Deep Dive §6: limit ≤ 200

export class Api {
  private tokens: TokenSource | null = null;

  constructor(
    private readonly baseUrl: string,
    private readonly fetchFn: FetchLike,
  ) {}

  useTokens(tokens: TokenSource): void {
    this.tokens = tokens;
  }

  /** POST /auth/guest {display_name} -> {user_id, access_token, refresh_token} */
  guest(displayName: string): Promise<TokenResponse> {
    return this.request<TokenResponse>("POST", "/auth/guest", { display_name: displayName });
  }

  /**
   * POST /auth/refresh, authenticated by the refresh token (sent as the bearer), no body.
   * Returns the new pair; the refresh token rotates. A rejection means the guest must rejoin.
   */
  async refresh(refreshToken: string): Promise<TokenResponse> {
    try {
      return await this.request<TokenResponse>("POST", "/auth/refresh", undefined, refreshToken);
    } catch (err) {
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        throw new AuthError(err.body.code);
      }
      throw err;
    }
  }

  getSession(sessionId: string): Promise<SessionView> {
    return this.authed<SessionView>("GET", `/sessions/${encodeURIComponent(sessionId)}`);
  }

  getMessages(
    sessionId: string,
    page: { afterSeq?: number; beforeSeq?: number; limit?: number },
  ): Promise<MessagePage> {
    const q = new URLSearchParams();
    if (page.afterSeq !== undefined) q.set("after_seq", String(page.afterSeq));
    if (page.beforeSeq !== undefined) q.set("before_seq", String(page.beforeSeq));
    q.set("limit", String(Math.min(PAGE_LIMIT, page.limit ?? PAGE_LIMIT)));
    return this.authed<MessagePage>(
      "GET",
      `/sessions/${encodeURIComponent(sessionId)}/messages?${q.toString()}`,
    );
  }

  /** PATCH /sessions/{id}/agent {status, name}: pause, resume or rename. Returns the agent. */
  patchAgent(sessionId: string, patch: AgentPatch): Promise<AgentView> {
    return this.authed<AgentView>("PATCH", `/sessions/${encodeURIComponent(sessionId)}/agent`, patch);
  }

  private async authed<T>(method: string, path: string, body?: unknown): Promise<T> {
    const tokens = this.tokens;
    if (!tokens) throw new AuthError("no tokens");
    const token = await tokens.accessToken();
    try {
      return await this.request<T>(method, path, body, token);
    } catch (err) {
      // An expired token gets one refresh and one retry.
      if (err instanceof ApiError && err.status === 401) {
        const fresh = await tokens.refresh();
        return this.request<T>(method, path, body, fresh);
      }
      throw err;
    }
  }

  private async request<T>(method: string, path: string, body?: unknown, bearer?: string): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (bearer !== undefined) headers.Authorization = `Bearer ${bearer}`;
    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await this.fetchFn(`${this.baseUrl}${path}`, init);
    const text = await res.text();
    let json: unknown = null;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        json = null;
      }
    }
    if (!res.ok) throw new ApiError(res.status, parseErrorBody(res.status, json));
    return json as T;
  }
}
