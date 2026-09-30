/**
 * The Huddle design system, used unmodified from design/ (design/docs/USAGE.md):
 * tokens and component styles, then the component bundle, which registers `window.Huddle`.
 * Types come from design/components/index.d.ts.
 */
import "./react-global";
import "../../../../design/tokens.css";
import "../../../../design/components/bundle.css";
import "../../../../design/components/bundle.js";

export const Huddle = window.Huddle;

export type {
  AgentInfo,
  AgentState as DesignAgentState,
  ErrorCode as DesignErrorCode,
  Member,
  ReceiptStatus,
} from "../../../../design/components/index";
