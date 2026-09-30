import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { failureView, ackTimeoutError } from "../core/errors";
import type { TimelineItem, TimelineMessage } from "../core/timeline";
import { DetailsPanel } from "./DetailsPanel";
import { Huddle } from "./design";
import type { DesignErrorCode, ReceiptStatus } from "./design";
import { MessageText } from "./MessageText";
import type { HuddleViewModel } from "./viewModel";

export interface HuddleActions {
  onSend: (text: string) => void;
  onRetry: (clientMsgId: string) => void;
  onPause: () => void;
  onResume: () => void;
  onRename: (name: string) => void;
  onExport: () => void;
  onLoadEarlier: () => void;
  /** After each render of new messages: the client sends the cumulative ack (Deep Dive §6). */
  onRendered: () => void;
}

type GroupMessage = {
  text: ReactNode;
  status?: ReceiptStatus | "failed";
  errorCode?: DesignErrorCode;
  onRetry?: () => void;
};

function toGroupMessage(m: TimelineMessage, onRetry: (id: string) => void): GroupMessage {
  const out: GroupMessage = { text: <MessageText body={m.body} /> };
  if (m.status === "failed") {
    const view = failureView(m.error ?? ackTimeoutError(m.clientMsgId ?? ""));
    out.status = "failed";
    if (view.errorCode) out.errorCode = view.errorCode;
    const id = m.clientMsgId;
    if (view.retryable && id) out.onRetry = () => { onRetry(id); };
  } else if (m.status) {
    out.status = m.status;
  }
  return out;
}

const NEAR_BOTTOM_PX = 96;

export function HuddleScreen({ vm, actions }: { vm: HuddleViewModel; actions: HuddleActions }) {
  const [details, setDetails] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const { onRendered } = actions;

  // Keep the newest message in view unless the reader has scrolled up; keep the reading position
  // when earlier messages are prepended.
  useLayoutEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    const a = anchor.current;
    if (a) {
      el.scrollTop = a.top + (el.scrollHeight - a.height);
      anchor.current = null;
    } else if (stick.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [vm.timeline, vm.loading, details]);

  useEffect(() => {
    onRendered();
  }, [vm.cursor, vm.headerConnection, onRendered]);

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") onRendered();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [onRendered]);

  const loadEarlier = () => {
    const el = threadRef.current;
    if (el) anchor.current = { height: el.scrollHeight, top: el.scrollTop };
    actions.onLoadEarlier();
  };

  const agentInfo = vm.agent.backAt
    ? { name: vm.agent.name, state: vm.agent.state, backAt: vm.agent.backAt }
    : { name: vm.agent.name, state: vm.agent.state };

  return (
    <div className="stage">
      <div className="col">
        <Huddle.ThreadHeader
          title={vm.title}
          connection={vm.headerConnection}
          members={vm.members}
          agent={agentInfo}
          onOpenDetails={() => {
            setDetails((d) => !d);
          }}
        />
        {vm.banner ? (
          <div className="banner" role="status">
            {vm.banner}
          </div>
        ) : null}
        <div
          className="thread"
          role="log"
          aria-label="Messages"
          ref={threadRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
          }}
        >
          {vm.loading ? (
            <div className="thread-inner">
              <Huddle.ThreadSkeleton />
            </div>
          ) : (
            <div className="thread-inner">
              {vm.hasEarlier && !vm.empty ? (
                <Huddle.Button className="earlier" variant="ghost" size="sm" icon="chevron-down" onClick={loadEarlier}>
                  Load earlier messages
                </Huddle.Button>
              ) : null}
              {vm.timeline.map((item) => renderItem(item, vm.agent.name, actions.onRetry))}
              {vm.empty ? <Huddle.EmptyState /> : null}
            </div>
          )}
        </div>
        <div className="foot">
          <div className="foot-inner">
            {vm.showAgentLine ? <Huddle.AgentIndicator {...agentInfo} /> : null}
            <Huddle.Composer state={vm.composerState} onSend={actions.onSend} onExport={actions.onExport} />
          </div>
        </div>
      </div>
      {details ? (
        <DetailsPanel
          agent={vm.agent}
          members={vm.members}
          onPause={actions.onPause}
          onResume={actions.onResume}
          onRename={actions.onRename}
          onClose={() => {
            setDetails(false);
          }}
        />
      ) : null}
    </div>
  );
}

function renderItem(item: TimelineItem, agentName: string, onRetry: (id: string) => void) {
  if (item.type === "note") {
    return (
      <Huddle.SystemNote key={item.key} time={item.time}>
        {item.text}
      </Huddle.SystemNote>
    );
  }
  return (
    <Huddle.MessageGroup
      key={item.key}
      author={item.author}
      kind={item.kind}
      time={item.time}
      agentName={agentName}
      messages={item.messages.map((m) => toGroupMessage(m, onRetry))}
    />
  );
}
