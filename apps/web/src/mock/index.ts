/** Dev-only mock backend wiring (see apps/web/README.md "Mock mode"). */
import { MockServer, MOCK_SESSION_ID, MOCK_WS_URL } from "./server";

export { MockControls } from "./MockControls";
export { MockServer };

/** `?latency=800` slows the mock network so the connecting and sending states are visible. */
export function installMock(search: string = window.location.search) {
  const latency = Number(new URLSearchParams(search).get("latency"));
  const server = new MockServer(Number.isFinite(latency) && latency > 0 ? { latencyMs: latency } : {});
  (window as unknown as { huddleMock?: MockServer }).huddleMock = server; // for the console
  return {
    server,
    sessionId: MOCK_SESSION_ID,
    apiBase: "",
    wsUrl: MOCK_WS_URL,
    fetch: server.fetch,
    createSocket: server.createSocket,
  };
}
