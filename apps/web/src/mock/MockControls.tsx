import { useState } from "react";
import type { ErrorCode } from "../contracts";
import { MOCK_PEOPLE, type MockServer } from "./server";

/**
 * Dev-only panel for the mock backend (like the prototype's state bar): drive the server into
 * each UI state. Not part of the product; only loaded by `npm run dev:mock`.
 */
export function MockControls({ server }: { server: MockServer }) {
  const [open, setOpen] = useState(false);
  const act = (fn: () => void) => () => {
    fn();
  };
  const fail = (code: ErrorCode) => act(() => {
    server.failNextSend(code);
  });
  return (
    <div className="mock-panel" role="region" aria-label="Mock backend controls">
      <button type="button" className="mock-toggle" onClick={() => { setOpen((o) => !o); }} aria-expanded={open}>
        Mock backend
      </button>
      {open ? (
        <div className="mock-actions">
          <button type="button" onClick={act(() => { server.dropConnections(); })}>Network blip</button>
          <button type="button" onClick={act(() => { server.goOffline(8_000); })}>Offline 8 s</button>
          <button type="button" onClick={act(() => { server.drain(1_500); })}>Drain gateway</button>
          <button type="button" onClick={act(() => { server.dropNextFanout(); })}>Lose next fan-out (gap)</button>
          <button type="button" onClick={act(() => { server.dropNextAck(); })}>Lose next ack</button>
          <button type="button" onClick={fail("RATE_LIMITED")}>Next send: rate limited</button>
          <button type="button" onClick={fail("PERSISTENCE_UNAVAILABLE")}>Next send: DB down</button>
          <button type="button" onClick={fail("NOT_MEMBER")}>Next send: not member</button>
          <button type="button" onClick={act(() => { server.expireAccessTokens(); })}>Expire token</button>
          <button type="button" onClick={act(() => { server.setAgentWriting(true); })}>AI writing</button>
          <button type="button" onClick={act(() => { server.setAgentPaused(!server.agent.paused); })}>Toggle AI paused</button>
          <button type="button" onClick={act(() => { server.exhaustBudget(); })}>AI budget spent</button>
          <button type="button" onClick={act(() => { server.refillBudget(); })}>Refill budget</button>
          <button type="button" onClick={act(() => { server.setAgentAway(!server.agent.away); })}>Toggle AI away</button>
          <button type="button" onClick={act(() => { server.postAs(MOCK_PEOPLE[0].id, "quick question: are we still on for 2?"); })}>
            Maya says something
          </button>
          <button type="button" onClick={act(() => { server.endSession(); })}>End huddle</button>
        </div>
      ) : null}
    </div>
  );
}
