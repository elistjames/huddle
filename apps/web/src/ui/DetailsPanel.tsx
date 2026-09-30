import { BudgetMeter } from "./BudgetMeter";
import { Huddle } from "./design";
import type { AgentVM } from "./viewModel";

export interface DetailsPanelProps {
  agent: AgentVM;
  members: { name: string; you?: boolean }[];
  onPause: () => void;
  onResume: () => void;
  onRename: (name: string) => void;
  onClose: () => void;
}

/**
 * The details panel: the design system's SessionDetails (AI controls and people) plus the budget
 * as a percentage (D-019). The agent is passed to SessionDetails without `budget` so its built-in
 * raw-token meter never renders; the local BudgetMeter section follows in the same styles.
 */
export function DetailsPanel({ agent, members, onPause, onResume, onRename, onClose }: DetailsPanelProps) {
  const info = agent.backAt
    ? { name: agent.name, state: agent.state, backAt: agent.backAt }
    : { name: agent.name, state: agent.state };
  return (
    <div className="side">
      <Huddle.SessionDetails
        members={members}
        agent={info}
        onPause={onPause}
        onResume={onResume}
        onRename={onRename}
        onClose={onClose}
      />
      {agent.budget ? (
        <section className="hd-details-sec side-budget" aria-label="Budget">
          <h3 className="hd-sec-title">Budget</h3>
          <BudgetMeter budget={agent.budget} name={agent.name} />
        </section>
      ) : null}
    </div>
  );
}
