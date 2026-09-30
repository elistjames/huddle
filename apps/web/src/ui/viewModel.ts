/**
 * Snapshot -> what the screen shows, following the UI states table (Deep Dive §10 and
 * design/README.md "UI states"). Pure, so every state is testable without a server.
 */
import type { ClientSnapshot } from "../client";
import type { AgentState, BudgetView } from "../contracts";
import { isEmptyThread } from "../core/thread";
import { buildTimeline, type TimelineItem } from "../core/timeline";

export type HeaderConnection = "connected" | "connecting" | "reconnecting" | "closed";
export type ComposerState = "ready" | "connecting" | "reconnecting" | "closed";

export interface AgentVM {
  name: string;
  state: AgentState;
  /** The end of the budget window, shown as "back at {time}" while resting. */
  backAt?: string;
  budget: BudgetView | null;
}

export interface HuddleViewModel {
  title: string;
  headerConnection: HeaderConnection;
  composerState: ComposerState;
  banner: string | null;
  /** ThreadSkeleton: the first connection has not finished loading history yet. */
  loading: boolean;
  empty: boolean;
  closed: boolean;
  showAgentLine: boolean;
  hasEarlier: boolean;
  timeline: TimelineItem[];
  agent: AgentVM;
  members: { name: string; you?: boolean }[];
  cursor: number;
}

export const DEFAULT_AGENT_NAME = "Huddle AI";
export const RECONNECTING_BANNER = "Reconnecting… messages will send when you’re back.";
export const OUTDATED_BANNER = "This version of Huddle is out of date. Reload the page to keep chatting.";

export function buildViewModel(snap: ClientSnapshot): HuddleViewModel {
  const closed = snap.sessionStatus === "closed";
  const conn = snap.connection;
  const firstConnect = conn === "connecting" || conn === "stopped";
  const offline = conn === "reconnecting" || conn === "failed";

  const agentName = snap.agent?.name || DEFAULT_AGENT_NAME;
  const agentState: AgentState = snap.agent?.state ?? "following";
  const agent: AgentVM = { name: agentName, state: agentState, budget: snap.agent?.budget ?? null };
  if (agentState === "resting" && snap.agent?.budget.refills_at) agent.backAt = snap.agent.budget.refills_at;

  const synced = snap.thread.synced;
  const loading = !closed && !synced && firstConnect;

  const members = snap.members
    .filter((m) => m.participant_type === "user" && m.status === "active")
    .map((m) => (m.participant_id === snap.userId ? { name: m.display_name, you: true } : { name: m.display_name }));

  const timeline = buildTimeline(snap.thread, {
    names: snap.names,
    agentId: snap.agent?.id || null,
    agentName,
  });

  let banner: string | null = null;
  if (snap.fatal === "protocol") banner = OUTDATED_BANNER;
  else if (!closed && offline) banner = RECONNECTING_BANNER;

  return {
    title: snap.title || "Huddle",
    headerConnection: closed ? "closed" : conn === "live" ? "connected" : firstConnect ? "connecting" : "reconnecting",
    composerState: closed ? "closed" : conn === "live" ? "ready" : firstConnect ? "connecting" : "reconnecting",
    banner,
    loading,
    empty: synced && isEmptyThread(snap.thread),
    closed,
    showAgentLine: !loading && !closed,
    hasEarlier: snap.thread.hasEarlier,
    timeline,
    agent,
    members,
    cursor: snap.thread.cursor,
  };
}
