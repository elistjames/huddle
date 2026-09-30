/**
 * Runtime endpoints. Defaults are same-origin (the Vite dev server proxies /auth, /sessions and
 * /ws to the chat service and gateway; see vite.config.ts). Override at build time with
 * VITE_HUDDLE_API_URL, VITE_HUDDLE_WS_URL and VITE_HUDDLE_SESSION_ID.
 */
export interface AppConfig {
  sessionId: string;
  apiBase: string;
  wsUrl: string;
}

export function resolveConfig(env: Record<string, string | undefined>, location: Location): AppConfig {
  const wsDefault = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;
  return {
    // The MVP has one pre-created session (docs/work/README.md: HUDDLE_SESSION_ID, default ses_main).
    sessionId: env.VITE_HUDDLE_SESSION_ID || "ses_main",
    apiBase: (env.VITE_HUDDLE_API_URL ?? "").replace(/\/$/, ""),
    wsUrl: env.VITE_HUDDLE_WS_URL || wsDefault,
  };
}
