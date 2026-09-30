import "./ui/design";
import "./ui/app.css";
import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { HuddleClient, type ClientDeps } from "./client";
import { resolveConfig, type AppConfig } from "./config";
import { App } from "./ui/App";

async function boot(): Promise<void> {
  let config: AppConfig = resolveConfig(import.meta.env, window.location);
  let deps: ClientDeps = {
    fetch: (input, init) => window.fetch(input, init),
    createSocket: (url) => new WebSocket(url),
    storage: window.localStorage,
  };
  let extra: ReactNode = null;

  // Dev-only mock backend. `import.meta.env.DEV` is false in production builds, so this branch
  // and the mock module are removed from the bundle.
  if (import.meta.env.DEV && import.meta.env.VITE_HUDDLE_MOCK === "1") {
    const mock = await import("./mock");
    const m = mock.installMock();
    config = { sessionId: m.sessionId, apiBase: m.apiBase, wsUrl: m.wsUrl };
    deps = { ...deps, fetch: m.fetch, createSocket: m.createSocket };
    extra = <mock.MockControls server={m.server} />;
  }

  const client = new HuddleClient(config, deps);
  const root = document.getElementById("root");
  if (!root) throw new Error("#root missing");
  createRoot(root).render(
    <StrictMode>
      <App client={client} sessionId={config.sessionId} extra={extra} />
    </StrictMode>,
  );
}

void boot();
