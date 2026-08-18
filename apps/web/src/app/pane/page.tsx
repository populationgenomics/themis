"use client";

import { useSearchParams } from "next/navigation";
import { type ReactNode, Suspense, useEffect, useRef, useState } from "react";
import type { Citation } from "@/components/workbench/markdown";
import { revealCitation } from "@/components/workbench/reveal";
import { TabArea } from "@/components/workbench/tab-area";
import {
  addDragSession,
  type LiveSessions,
  removeDragSession,
} from "@/components/workbench/tab-dnd";
import { WorkspaceDataProvider } from "@/components/workbench/workspace-context";
import { WORKING_DOC_TAB_ID } from "@/components/workbench/workspace-model";
import {
  CHANNEL_PARAM,
  documentFetchKey,
  makeCrossWindowDrag,
  mirrorWindowActions,
  mirrorWorkspace,
  WINDOW_PARAM,
  type WorkspaceCommand,
  type WorkspaceMessage,
  type WorkspaceSnapshot,
} from "@/components/workbench/workspace-sync";
import { useDocument } from "@/lib/queries";

// A mirror window: the tab area of one window in the main workspace, and nothing else — no conversation
// region (main-only). It renders from the broadcast snapshot and posts a command for every user action;
// main stays authoritative. The working-document body does NOT ride the channel — this window fetches it
// from the BFF keyed on the snapshot's {analysisId, version}, so a popped doc re-renders on republish.
// A `main-closing` message closes this window; main detects this one going away by its window handle.

export default function PanePage(): React.ReactElement {
  return (
    <Suspense fallback={<PaneShell>{<Center text="Connecting…" />}</PaneShell>}>
      <MirrorWindow />
    </Suspense>
  );
}

function MirrorWindow(): React.ReactElement {
  const params = useSearchParams();
  const channelId = params.get(CHANNEL_PARAM);
  const winId = params.get(WINDOW_PARAM);
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const channelRef = useRef<BroadcastChannel | null>(null);
  // Live cross-window drags this window learns of from other windows' broadcasts (never its own).
  const sessionsRef = useRef<LiveSessions>({});

  useEffect(() => {
    if (!channelId || !winId) return;
    const channel = new BroadcastChannel(channelId);
    channelRef.current = channel;
    channel.onmessage = (event: MessageEvent<WorkspaceMessage>) => {
      const message = event.data;
      if (message.kind === "state") setSnapshot(message.snapshot);
      else if (message.kind === "main-closing") window.close();
      else if (message.kind === "drag-session")
        sessionsRef.current = addDragSession(
          sessionsRef.current,
          message.sessionId,
          message.tabId,
          message.sourceWinId,
        );
      else if (message.kind === "drag-end")
        sessionsRef.current = removeDragSession(
          sessionsRef.current,
          message.sessionId,
        );
    };
    channel.postMessage({ kind: "request-state" } satisfies WorkspaceMessage);
    // No close signal is posted: main detects a genuinely gone child by its window handle, which a
    // reload leaves open. A reloaded child re-registers here via `request-state`.
    return () => {
      channel.close();
      channelRef.current = null;
    };
  }, [channelId, winId]);

  const win = snapshot?.windows.find((w) => w.id === winId) ?? null;
  // The working-document body, fetched by this window from the BFF keyed on the broadcast version — the
  // body never crosses the channel. Only when this window actually holds the working-doc tab: the
  // version signal reaches every mirror, but a window popped out to hold a paper has nowhere to render
  // it, so fetching (and re-fetching on each version bump) would be pure waste.
  const holdsWorkingDoc =
    win?.panes.some((p) => p.tabs.some((t) => t.id === WORKING_DOC_TAB_ID)) ??
    false;
  const fetchKey =
    snapshot && holdsWorkingDoc
      ? documentFetchKey(snapshot)
      : { analysisId: null, version: null };
  const doc = useDocument(fetchKey.analysisId, fetchKey.version);
  const workingDocument = doc.data?.document ?? null;
  // Close only once this window has appeared in a snapshot and then left it (main reparented/pruned it).
  // A first snapshot can predate the window's creation — it lacks this window too — so don't self-close
  // then; wait for the corrective broadcast that follows main's move-to-new-window dispatch.
  const seenRef = useRef(false);
  if (win) seenRef.current = true;
  useEffect(() => {
    if (snapshot && !win && seenRef.current) window.close();
  }, [snapshot, win]);

  if (!channelId || !winId)
    return (
      <PaneShell>{<Center text="Missing window parameters." />}</PaneShell>
    );
  if (!snapshot || !win)
    return (
      <PaneShell>{<Center text="Connecting to the workbench…" />}</PaneShell>
    );

  const send = (command: WorkspaceCommand): void => {
    channelRef.current?.postMessage({
      kind: "command",
      command,
    } satisfies WorkspaceMessage);
  };
  const workspace = mirrorWorkspace(snapshot, winId, send);
  const windowActions = mirrorWindowActions(snapshot, send);
  const crossWindowDrag = makeCrossWindowDrag(
    (message) => channelRef.current?.postMessage(message),
    sessionsRef,
  );
  const onCitation = (
    wId: string,
    paneId: string,
    citation: Citation,
  ): void => {
    void revealCitation(
      workspace,
      { kind: "document", winId: wId, paneId },
      citation,
    );
  };

  return (
    <PaneShell>
      <WorkspaceDataProvider
        value={{
          events: [],
          workingDocument,
          documentSignal: snapshot.workingDocument,
          documentError: doc.isError,
        }}
      >
        <TabArea
          win={win}
          controller={workspace}
          windowActions={windowActions}
          crossWindowDrag={crossWindowDrag}
          onCitation={onCitation}
        />
      </WorkspaceDataProvider>
    </PaneShell>
  );
}

function PaneShell({ children }: { children: ReactNode }): React.ReactElement {
  return (
    <div className="flex h-svh flex-col overflow-hidden bg-surface-doc-pane">
      {children}
    </div>
  );
}

function Center({ text }: { text: string }): React.ReactElement {
  return (
    <div className="flex flex-1 items-center justify-center text-[13px] text-ink-faintest">
      {text}
    </div>
  );
}
