import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import type { UserConfig } from "vite";
import type { InlineConfig } from "vitest/node";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));

// The design system lives at the repo root (design/) and is imported unmodified, so the dev
// server must be allowed to read it. In dev, REST and the WebSocket are proxied to the chat
// service and the gateway (ports from deploy/compose.yaml), so the app is same-origin.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "HUDDLE_");
  const chat = env.HUDDLE_CHAT_URL || "http://localhost:8001";
  const gateway = env.HUDDLE_GATEWAY_URL || "ws://localhost:8000";
  const config: UserConfig & { test: InlineConfig } = {
    plugins: [react()],
    server: {
      fs: { allow: [repoRoot] },
      proxy: {
        "/auth": chat,
        "/sessions": chat,
        "/ws": { target: gateway, ws: true },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
    },
  };
  return config;
});
