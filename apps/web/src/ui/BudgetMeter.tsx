import type { BudgetView } from "../contracts";
import { formatTime } from "../core/time";

/**
 * The AI budget as users see it (D-019, Deep Dive §5): "{used_pct}% used" and "Refills at {time}"
 * (the end of the fixed window). Never raw tokens.
 *
 * Local variant of design/components BudgetMeter, which still shows "312k of 500k tokens" and
 * "Refills continuously" (docs/specs/design-system.md notes the gap). It renders the same markup
 * and hd- classes, so it looks identical; only the words and the meaning of the fill change:
 * the amber fill is the share used, so a resting AI shows a full bar (§10: 100% used).
 */
export function BudgetMeter({ budget, name }: { budget: BudgetView; name: string }) {
  const used = Math.max(0, Math.min(100, Math.round(budget.used_pct)));
  const refills = formatTime(budget.refills_at);
  return (
    <div className="hd-budget">
      <div className="hd-budget-head">
        <span className="hd-budget-label">{name} budget</span>
        <span className="hd-budget-num">{used}% used</span>
      </div>
      <div
        className="hd-meter"
        role="meter"
        aria-label="AI budget used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={used}
        aria-valuetext={`${used}% used`}
      >
        <span style={{ width: `${used}%` }} />
      </div>
      {refills ? <p className="hd-budget-note">Refills at {refills}</p> : null}
    </div>
  );
}
