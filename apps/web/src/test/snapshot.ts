import type { ClientSnapshot } from "../client";
import type { AgentView } from "../contracts";
import { initialThread, threadReducer, type ThreadAction, type ThreadState } from "../core/thread";
import { AGENT, JORDAN, MAYA, ME, SID, msg } from "./fixtures";

export const REFILLS_AT = "2026-10-01T00:00:00Z";

export function agentView(over: Partial<AgentView> = {}): AgentView {
  return {
    id: AGENT,
    name: "Huddle AI",
    state: "following",
    budget: { used_pct: 38, refills_at: REFILLS_AT },
    ...over,
  };
}

export function thread(actions: ThreadAction[]): ThreadState {
  return actions.reduce(threadReducer, initialThread(SID, ME));
}

/** A thread that finished its first replay with these messages. */
export function syncedThread(messages = [msg(1), msg(2, { sender_id: JORDAN })], more: ThreadAction[] = []): ThreadState {
  return thread([{ type: "subscribed", afterSeq: 0 }, { type: "synced", messages, hasMore: false }, ...more]);
}

export function snapshot(over: Partial<ClientSnapshot> = {}): ClientSnapshot {
  return {
    phase: "in",
    joinFailed: false,
    displayName: "Eli St. James",
    userId: ME,
    title: "Launch post",
    sessionStatus: "active",
    members: [
      { participant_id: MAYA, participant_type: "user", display_name: "Maya Chen", role: "member", status: "active" },
      { participant_id: JORDAN, participant_type: "user", display_name: "Jordan Reyes", role: "member", status: "active" },
      { participant_id: ME, participant_type: "user", display_name: "Eli St. James", role: "member", status: "active" },
      { participant_id: AGENT, participant_type: "agent", display_name: "Huddle AI", role: "member", status: "active" },
    ],
    agent: agentView(),
    connection: "live",
    fatal: null,
    thread: syncedThread(),
    names: new Map([
      [MAYA, "Maya Chen"],
      [JORDAN, "Jordan Reyes"],
      [ME, "Eli St. James"],
    ]),
    ...over,
  };
}
