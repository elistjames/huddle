import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ClientSnapshot } from "../client";
import { formatTime } from "../core/time";
import { AGENT, MAYA, entry, mine, msg } from "../test/fixtures";
import { REFILLS_AT, agentView, snapshot, syncedThread, thread } from "../test/snapshot";
import { HuddleScreen, type HuddleActions } from "./HuddleScreen";
import { OUTDATED_BANNER, RECONNECTING_BANNER, buildViewModel } from "./viewModel";

function actions(): HuddleActions {
  return {
    onSend: vi.fn(),
    onRetry: vi.fn(),
    onPause: vi.fn(),
    onResume: vi.fn(),
    onRename: vi.fn(),
    onExport: vi.fn(),
    onLoadEarlier: vi.fn(),
    onRendered: vi.fn(),
  };
}

function show(over: Partial<ClientSnapshot> = {}) {
  const a = actions();
  const utils = render(<HuddleScreen vm={buildViewModel(snapshot(over))} actions={a} />);
  return { ...utils, a };
}

const composer = () => screen.getByRole("textbox");
const log = () => screen.getByRole("log", { name: "Messages" });
const pageText = () => document.body.textContent;

describe("UI states (Deep Dive §10)", () => {
  it("Connecting: skeleton thread, hollow dot, locked composer", () => {
    show({ connection: "connecting", thread: thread([]) });
    expect(screen.getByRole("status", { name: "Loading messages" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Connecting" })).toBeInTheDocument();
    expect(composer()).toBeDisabled();
    expect(composer()).toHaveAttribute("placeholder", "Connecting…");
    expect(screen.queryByText(/following along/)).toBeNull();
  });

  it("Empty session: the empty state with the agent following along, no Load earlier", () => {
    const notes = [msg(1, { kind: "system", sender_type: "system", sender_id: "system", body: "Eli St. James joined" })];
    show({ thread: syncedThread(notes) });
    expect(screen.getByText("Nobody’s said anything yet.")).toBeInTheDocument();
    expect(screen.getByText("Start the huddle.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Huddle AI is following along");
    expect(screen.getByRole("note")).toHaveTextContent("Eli St. James joined");
    expect(screen.queryByRole("button", { name: /Load earlier/ })).toBeNull();
    expect(composer()).toBeEnabled();
  });

  it("Connected: the live thread with the success dot and an open composer", () => {
    show();
    expect(screen.getByRole("img", { name: "Connected" })).toBeInTheDocument();
    expect(within(log()).getByText("message 1")).toBeInTheDocument();
    expect(composer()).toBeEnabled();
    expect(composer()).toHaveAttribute("placeholder", "Message the huddle");
    expect(screen.getByText("3 people and Huddle AI")).toBeInTheDocument();
  });

  it("Reconnecting: messages stay readable, a worded notice, composer locked", () => {
    show({ connection: "reconnecting" });
    expect(within(log()).getByText("message 1")).toBeInTheDocument();
    expect(screen.getByText(RECONNECTING_BANNER)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Reconnecting" })).toBeInTheDocument();
    expect(composer()).toBeDisabled();
    expect(composer()).toHaveAttribute("placeholder", "Reconnecting…");
  });

  it("Sending, then ✓, ✓✓ and the amber ✓, each with a text label", () => {
    const t = syncedThread([mine(1, "a"), mine(2, "b"), mine(3, "c")], [
      { type: "receipts", cursors: { [MAYA]: 2, [AGENT]: 1 } },
      { type: "enqueued", entry: entry("d", { body: "on its way" }) },
    ]);
    show({ thread: t });
    expect(screen.getByRole("img", { name: "Read by Huddle AI" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Delivered" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Sent" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Sending" })).toBeInTheDocument();
  });

  it("the read receipt names a renamed agent", () => {
    const t = syncedThread([mine(1, "a")], [{ type: "receipts", cursors: { [MAYA]: 1, [AGENT]: 1 } }]);
    show({ thread: t, agent: agentView({ name: "Sage" }) });
    expect(screen.getByRole("img", { name: "Read by Sage" })).toBeInTheDocument();
  });

  it("Delivery failed (retryable): “Didn’t send. Retry” and Retry reuses the entry", async () => {
    const t = syncedThread([msg(1)], [
      { type: "enqueued", entry: entry("x", { body: "db down" }) },
      { type: "failed", clientMsgId: "x", error: { code: "PERSISTENCE_UNAVAILABLE", retryable: true } },
    ]);
    const { a } = show({ thread: t });
    expect(screen.getByText(/Didn’t send/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(a.onRetry).toHaveBeenCalledWith("x");
  });

  it("Delivery failed (rate limited): “Sending too fast” with Retry", () => {
    const t = syncedThread([], [
      { type: "enqueued", entry: entry("x") },
      { type: "failed", clientMsgId: "x", error: { code: "RATE_LIMITED", retryable: true } },
    ]);
    show({ thread: t });
    expect(screen.getByText(/Sending too fast/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it.each([
    ["NOT_MEMBER", "You’re not in this huddle"],
    ["MESSAGE_TOO_LONG", "Too long to send (4,000 characters max)"],
    ["SESSION_CLOSED", "This huddle has ended"],
  ] as const)("Delivery failed (%s): %s, no Retry", (code, copy) => {
    const t = syncedThread([], [
      { type: "enqueued", entry: entry("x") },
      { type: "failed", clientMsgId: "x", error: { code, retryable: false } },
    ]);
    show({ thread: t });
    expect(screen.getByText(copy)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("img", { name: /Sending|Sent/ })).toBeNull();
  });

  it("Agent writing: amber dots and “is writing”; never typing or thinking", () => {
    const { container } = show({ agent: agentView({ state: "writing" }) });
    expect(screen.getByRole("status")).toHaveTextContent("Huddle AI is writing");
    expect(container.querySelector(".hd-dots")).not.toBeNull();
    expect(pageText()).not.toMatch(/typing|thinking/i);
  });

  it("Agent paused: header and indicator say paused; Resume in details", async () => {
    const { a } = show({ agent: agentView({ state: "paused" }) });
    expect(screen.getAllByText(/is paused/)).toHaveLength(2); // header sub-line + indicator
    await userEvent.click(screen.getByRole("button", { name: "Open huddle details" }));
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(a.onResume).toHaveBeenCalled();
  });

  it("Agent resting: “resting · back at {time}”; the budget meter is full, in percent, never tokens", async () => {
    show({ agent: agentView({ state: "resting", budget: { used_pct: 100, refills_at: REFILLS_AT } }) });
    const time = formatTime(REFILLS_AT);
    expect(screen.getAllByText(`is resting · back at ${time}`, { exact: false }).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Open huddle details" }));
    const meter = screen.getByRole("meter", { name: "AI budget used" });
    expect(meter).toHaveAttribute("aria-valuenow", "100");
    expect(meter).toHaveAttribute("aria-valuetext", "100% used");
    expect(screen.getByText("100% used")).toBeInTheDocument();
    expect(screen.getByText(`Refills at ${time}`)).toBeInTheDocument();
    expect(pageText()).not.toMatch(/tokens|continuously/i);
  });

  it("Agent away: “is away right now”", () => {
    show({ agent: agentView({ state: "away" }) });
    expect(screen.getAllByText(/is away right now/).length).toBeGreaterThan(0);
  });

  it("Session closed: read-only thread, “Ended”, composer replaced by the note and Export", async () => {
    const { a } = show({ sessionStatus: "closed" });
    expect(screen.getByText("Ended")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("This huddle has ended")).toBeInTheDocument();
    expect(screen.queryByText(/following along/)).toBeNull();
    expect(within(log()).getByText("message 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(a.onExport).toHaveBeenCalled();
  });

  it("an out-of-date protocol says so in words", () => {
    show({ connection: "failed", fatal: "protocol" });
    expect(screen.getByText(OUTDATED_BANNER)).toBeInTheDocument();
    expect(composer()).toBeDisabled();
  });
});

describe("thread rendering", () => {
  it("groups by sender, labels the agent, and renders system notes", () => {
    const t = syncedThread([
      msg(1),
      msg(2),
      msg(3, { sender_type: "agent", sender_id: AGENT, body: "a thought" }),
      msg(4, { kind: "system", sender_type: "system", sender_id: "system", body: "Maya Chen paused Huddle AI" }),
    ]);
    const { container } = show({ thread: t });
    expect(container.querySelectorAll(".hd-group")).toHaveLength(2);
    expect(container.querySelector(".hd-group-agent .hd-sender")).toHaveTextContent("Huddle AI");
    expect(screen.getByRole("note")).toHaveTextContent("Maya Chen paused Huddle AI");
  });

  it("renders code blocks and inline code; markup in a message stays text", () => {
    const body = "see <b>this</b> and <img src=x onerror=alert(1)>\n```\nconst x = 1;\n```\nrun `npm test`";
    const { container } = show({ thread: syncedThread([msg(1, { body })]) });
    expect(container.querySelector(".hd-bubble b")).toBeNull();
    expect(container.querySelector(".hd-bubble img")).toBeNull();
    expect(within(log()).getByText(/see <b>this<\/b>/)).toBeInTheDocument();
    expect(container.querySelector("pre.msg-pre code")).toHaveTextContent("const x = 1;");
    expect(container.querySelector("code.msg-code")).toHaveTextContent("npm test");
  });

  it("offers Load earlier when history was windowed", async () => {
    const t = thread([
      { type: "subscribed", afterSeq: 0 },
      { type: "synced", messages: [msg(51), msg(52)], hasMore: false },
    ]);
    const { a } = show({ thread: t });
    await userEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
    expect(a.onLoadEarlier).toHaveBeenCalled();
  });

  it("tells the client it rendered, so it can ack", () => {
    const { a } = show();
    expect(a.onRendered).toHaveBeenCalled();
  });
});

describe("composer", () => {
  it("Enter sends, Shift+Enter adds a line", async () => {
    const { a } = show();
    await userEvent.type(composer(), "first line{Shift>}{Enter}{/Shift}second");
    expect(a.onSend).not.toHaveBeenCalled();
    await userEvent.keyboard("{Enter}");
    expect(a.onSend).toHaveBeenCalledWith("first line\nsecond");
    expect(composer()).toHaveValue("");
  });

  it("shows the 4,000-character counter near the limit and blocks sending over it", async () => {
    const { a, container } = show();
    await userEvent.click(composer());
    await userEvent.paste("x".repeat(3_600));
    expect(container.querySelector(".hd-count")).toHaveTextContent("3,600 / 4,000");
    await userEvent.paste("x".repeat(401));
    expect(container.querySelector(".hd-count")).toHaveTextContent("4,001 / 4,000");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.keyboard("{Enter}");
    expect(a.onSend).not.toHaveBeenCalled();
  });
});

describe("details panel", () => {
  it("pause, rename and the people list", async () => {
    const { a } = show();
    await userEvent.click(screen.getByRole("button", { name: "Open huddle details" }));
    const panel = screen.getByRole("complementary", { name: "Huddle details" });
    expect(within(panel).getByText("you")).toBeInTheDocument();
    expect(within(panel).getByText("People (3)")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(a.onPause).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "AI name" });
    await userEvent.clear(input);
    await userEvent.type(input, "Sage{Enter}");
    expect(a.onRename).toHaveBeenCalledWith("Sage");
  });

  it("shows the budget as percentage used and refill time only", async () => {
    show();
    await userEvent.click(screen.getByRole("button", { name: "Open huddle details" }));
    expect(screen.getByText("38% used")).toBeInTheDocument();
    expect(screen.getByText(`Refills at ${formatTime(REFILLS_AT)}`)).toBeInTheDocument();
    expect(pageText()).not.toMatch(/tokens/i);
    await userEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(screen.queryByRole("complementary")).toBeNull();
  });
});
