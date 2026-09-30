import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { HuddleClient } from "../client";
import { MOCK_SESSION_ID, MOCK_WS_URL, MockServer } from "../mock/server";
import { REFRESH_TOKEN_KEY } from "../net/auth";
import { App } from "./App";

let server: MockServer | null = null;
let client: HuddleClient | null = null;

function mount(options: ConstructorParameters<typeof MockServer>[0] = {}) {
  server = new MockServer({ latencyMs: 2, ...options });
  client = new HuddleClient(
    { sessionId: MOCK_SESSION_ID, apiBase: "", wsUrl: MOCK_WS_URL },
    { fetch: server.fetch, createSocket: server.createSocket, storage: window.localStorage, isVisible: () => true },
  );
  render(<App client={client} sessionId={MOCK_SESSION_ID} />);
  return server;
}

afterEach(() => {
  client?.dispose();
  server?.dispose();
  client = null;
  server = null;
});

describe("App against the mock backend", () => {
  it("join, load history, send, and watch the receipt reach the amber tick", async () => {
    mount();
    expect(await screen.findByText("No account needed. This is the name others will see.")).toBeInTheDocument();
    const join = screen.getByRole("button", { name: "Join" });
    expect(join).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your name"), "Eli St. James");
    await userEvent.click(join);

    const log = await screen.findByRole("log", { name: "Messages" });
    expect(await within(log).findByText("reading now")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Connected" })).toBeInTheDocument();
    expect(window.localStorage.getItem(REFRESH_TOKEN_KEY)).toMatch(/^rt_/);

    const box = await screen.findByRole("textbox", { name: "Message the huddle" });
    await userEvent.type(box, "agreed, cutting it{Enter}");
    expect(await within(log).findByText("agreed, cutting it")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Delivered" }, { timeout: 3000 })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Read by Huddle AI" }, { timeout: 3000 })).toBeInTheDocument();
    // The simulated agent writes, then answers through the normal message path.
    expect(await screen.findByText("is writing", { exact: false }, { timeout: 3000 })).toBeInTheDocument();
    expect(await within(log).findByText(/still points at the old release/, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(await screen.findByText("is following along", { exact: false })).toBeInTheDocument();
  }, 15_000);

  it("shows the empty state for a brand-new huddle", async () => {
    mount({ seed: false, agent: false, people: false });
    await userEvent.type(await screen.findByLabelText("Your name"), "Eli");
    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(await screen.findByText("Nobody’s said anything yet.")).toBeInTheDocument();
    expect(screen.getByText("Eli joined")).toBeInTheDocument();
  });
});
