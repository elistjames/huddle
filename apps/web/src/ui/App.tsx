import { useCallback, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import type { HuddleClient } from "../client";
import { HuddleScreen, type HuddleActions } from "./HuddleScreen";
import { JoinScreen } from "./JoinScreen";
import { buildViewModel } from "./viewModel";

function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function App({ client, sessionId, extra }: { client: HuddleClient; sessionId: string; extra?: ReactNode }) {
  const snap = useSyncExternalStore(client.subscribe, client.getSnapshot);

  // The client lives as long as the page; start() is idempotent (safe under StrictMode).
  useEffect(() => {
    client.start();
  }, [client]);

  const onRendered = useCallback(() => {
    client.markRendered();
  }, [client]);

  const actions = useMemo<HuddleActions>(
    () => ({
      onSend: (text) => {
        client.send(text);
      },
      onRetry: (id) => {
        client.retry(id);
      },
      onPause: () => void client.pause(),
      onResume: () => void client.resume(),
      onRename: (name) => void client.rename(name),
      onExport: () => {
        void client.exportTranscript().then((json) => {
          download(`huddle-${sessionId}.json`, json);
        });
      },
      onLoadEarlier: () => void client.loadEarlier(),
      onRendered,
    }),
    [client, sessionId, onRendered],
  );

  const vm = useMemo(() => buildViewModel(snap), [snap]);

  return (
    <div className="app">
      {snap.phase === "join" || snap.phase === "joining" ? (
        <JoinScreen
          initialName={snap.displayName}
          failed={snap.joinFailed}
          onJoin={(name) => void client.join(name)}
        />
      ) : (
        <HuddleScreen vm={vm} actions={actions} />
      )}
      {extra}
    </div>
  );
}
