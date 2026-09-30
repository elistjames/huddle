import { describe, expect, it } from "vitest";
import type { ConnectionStatus } from "../net/connection";
import { ME } from "../test/fixtures";
import { REFILLS_AT, agentView, snapshot, thread } from "../test/snapshot";
import { RECONNECTING_BANNER, buildViewModel } from "./viewModel";

describe("buildViewModel", () => {
  const table: [ConnectionStatus, boolean, string, string, boolean][] = [
    // connection, synced, header dot, composer, skeleton
    ["stopped", false, "connecting", "connecting", true],
    ["connecting", false, "connecting", "connecting", true],
    ["reconnecting", false, "reconnecting", "reconnecting", false],
    ["live", true, "connected", "ready", false],
    ["reconnecting", true, "reconnecting", "reconnecting", false],
    ["failed", true, "reconnecting", "reconnecting", false],
  ];
  it.each(table)("%s (synced=%s) -> dot %s, composer %s, skeleton %s", (connection, synced, dot, comp, skel) => {
    const t = synced ? thread([{ type: "subscribed", afterSeq: 0 }, { type: "synced", messages: [], hasMore: false }]) : thread([]);
    const vm = buildViewModel(snapshot({ connection, thread: t }));
    expect(vm.headerConnection).toBe(dot);
    expect(vm.composerState).toBe(comp);
    expect(vm.loading).toBe(skel);
    expect(vm.banner).toBe(connection === "reconnecting" || connection === "failed" ? RECONNECTING_BANNER : null);
  });

  it("a closed session wins over the connection state and hides the agent line", () => {
    const vm = buildViewModel(snapshot({ sessionStatus: "closed", connection: "reconnecting" }));
    expect(vm).toMatchObject({ headerConnection: "closed", composerState: "closed", banner: null, showAgentLine: false });
  });

  it("resting passes the window end as back-at time", () => {
    const vm = buildViewModel(snapshot({ agent: agentView({ state: "resting" }) }));
    expect(vm.agent.backAt).toBe(REFILLS_AT);
    expect(buildViewModel(snapshot()).agent.backAt).toBeUndefined();
  });

  it("lists active people only, marks you, and leaves the agent out of the people list", () => {
    const s = snapshot();
    const gone = { participant_id: "usr_gone", participant_type: "user" as const, display_name: "Gone", role: "member", status: "left" };
    const members = [...s.members, gone];
    const vm = buildViewModel({ ...s, members });
    expect(vm.members).toEqual([{ name: "Maya Chen" }, { name: "Jordan Reyes" }, { name: "Eli St. James", you: true }]);
    expect(s.userId).toBe(ME);
  });

  it("defaults the agent to Huddle AI, following, before its status arrives", () => {
    const vm = buildViewModel(snapshot({ agent: null }));
    expect(vm.agent).toEqual({ name: "Huddle AI", state: "following", budget: null });
  });
});
