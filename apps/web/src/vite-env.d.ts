/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_HUDDLE_MOCK?: string;
  readonly VITE_HUDDLE_API_URL?: string;
  readonly VITE_HUDDLE_WS_URL?: string;
  readonly VITE_HUDDLE_SESSION_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
