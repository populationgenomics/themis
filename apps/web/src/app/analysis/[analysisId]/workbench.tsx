"use client";

import { useCallback, useEffect, useMemo } from "react";
import { AppBar } from "@/components/app-bar";
import { BackLink } from "@/components/back-link";
import { ReaderTime } from "@/components/reader-time";
import { useGroupRef } from "@/components/ui/resizable";
import { REGISTRY } from "@/components/workbench/content-kinds";
import { ConversationDock } from "@/components/workbench/conversation-dock";
import { ConversationPane } from "@/components/workbench/conversation-pane";
import type { Citation } from "@/components/workbench/markdown";
import { revealCitation } from "@/components/workbench/reveal";
import { SteerComposer } from "@/components/workbench/steer-composer";
import { TabArea } from "@/components/workbench/tab-area";
import { useSteering } from "@/components/workbench/use-steering";
import { useWorkspaceModelController } from "@/components/workbench/use-workspace-model";
import { useWorkspaceWindow } from "@/components/workbench/use-workspace-window";
import {
  CONVERSATION_PANEL_ID,
  ConversationSplit,
  edgeToOrientation,
  TAB_AREA_PANEL_ID,
} from "@/components/workbench/workbench-layout";
import { WorkspaceDataProvider } from "@/components/workbench/workspace-context";
import {
  computeTarget,
  findTab,
  pinnedDocumentVersion,
  type Source,
  WORKING_DOC_TAB_ID,
} from "@/components/workbench/workspace-model";
import { useDocument, usePoll } from "@/lib/queries";

/** The Analysis as its page resolved it — identity fixed for the life of the page, so it arrives as
 *  props rather than through a query the browser repeats. */
export interface AnalysisIdentity {
  id: string;
  /** The scenario's one-line name for this Analysis — the variant, or the opening of a free-form
   *  instruction (lib/scenario.ts). */
  title: string;
  /** The scenario's prose, shown as the title's hover: the clinical picture, or the whole
   *  instruction a free-form title was cut from. */
  detail: string;
  scenario: string;
  /** When the Analysis was created, with the page's pinned render of it — what the markup carries
   *  until `ReaderTime` reformats it on the reader's clock. */
  created: { iso: string; pinnedLabel: string; pinnedTitle: string };
  projectId: string;
  projectName: string;
}

export function Workbench({
  userEmail,
  analysis,
}: {
  userEmail: string;
  analysis: AnalysisIdentity;
}) {
  const analysisId = analysis.id;
  const workspace = useWorkspaceModelController();
  const mainWindow = workspace.state.windows.find(
    (w) => w.id === workspace.state.mainId,
  );
  if (!mainWindow) throw new Error("main window not found");
  // An empty tab area (its sole pane holds no tabs — e.g. the working document moved to a child) shows
  // as nothing rather than an empty panel: the conversation fills the window. A reveal or reparent adds
  // a tab and the split returns. The reducer keeps the zero-tab pane as the reveal/reparent target.
  const tabAreaEmpty =
    mainWindow.panes.length === 1 && mainWindow.panes[0].tabs.length === 0;
  const edge = workspace.state.conversation.edge;
  const orientation = edgeToOrientation(edge);

  // The outer split ratio persists per orientation (via the controller's store). It loads
  // post-mount so SSR and the first client render match the reducer default, and re-applies on an edge
  // flip so a width % never carries into a height %. react-resizable-panels manages the live layout;
  // the controller only reads/writes the persisted fraction.
  const groupRef = useGroupRef();
  const { readOuterRatio, writeOuterRatio } = workspace;
  const defaultConversationRatio =
    orientation === "horizontal"
      ? workspace.state.conversation.ratioH
      : workspace.state.conversation.ratioV;
  // Apply the persisted ratio for the current orientation (or its default when none is saved) whenever
  // the group (re)mounts or the edge flips. The group unmounts while the tab area is empty and remounts
  // at its `defaultSize` when tabs return, so `tabAreaEmpty` is a trigger; without the reset on an edge
  // flip a width % would carry into a height %, as react-resizable-panels keeps the last layout.
  useEffect(() => {
    if (tabAreaEmpty) return;
    let cancelled = false;
    void (async () => {
      const ratio =
        (await readOuterRatio(orientation)) ?? defaultConversationRatio;
      if (cancelled) return;
      groupRef.current?.setLayout({
        [CONVERSATION_PANEL_ID]: ratio * 100,
        [TAB_AREA_PANEL_ID]: (1 - ratio) * 100,
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [
    orientation,
    readOuterRatio,
    groupRef,
    defaultConversationRatio,
    tabAreaEmpty,
  ]);
  const onLayoutChanged = useCallback(
    (layout: Record<string, number>, meta: { isUserInteraction: boolean }) => {
      if (!meta.isUserInteraction) return;
      const conversation = layout[CONVERSATION_PANEL_ID];
      if (typeof conversation === "number")
        writeOuterRatio(orientation, conversation / 100);
    },
    [orientation, writeOuterRatio],
  );

  const poll = usePoll(analysisId);
  const workingDocumentVersion = poll.data?.workingDocumentVersion ?? null;
  const pinnedVersion = pinnedDocumentVersion(
    findTab(workspace.state, WORKING_DOC_TAB_ID)?.payload,
    analysisId,
  );
  const doc = useDocument(analysisId, pinnedVersion ?? workingDocumentVersion);
  const workingDocument = doc.data?.document ?? null;
  const events = poll.data?.events ?? [];
  // The projection, not the fallback: a turn cannot be placed against a run that has
  // not loaded, and steering is disabled until it has.
  const steering = useSteering(analysisId, poll.data?.events);

  // The working-document refetch signal every window shares: version + analysisId, never the body.
  // Each window (main and children) fetches its own body keyed on this, so a popped doc re-renders when
  // the agent republishes.
  const workingDocumentSignal = useMemo(
    () =>
      workingDocumentVersion !== null
        ? { analysisId, version: workingDocumentVersion }
        : null,
    [analysisId, workingDocumentVersion],
  );
  const { windowActions, crossWindowDrag, focusWindow } = useWorkspaceWindow(
    workspace,
    workingDocumentSignal,
  );

  // A `:paper`/`:quote` click reveals the paper beside its source (see reveal.ts). When the paper is
  // already open in another window, raise that window and activate it there (surface, not yank); if the
  // browser blocks the raise, fall back to moving it into this window's computed target.
  const reveal = useCallback(
    (src: Source, citation: Citation) => {
      const paperId = REGISTRY.paper.id({ docId: citation.docId });
      const target = computeTarget(workspace.state, src, paperId);
      if (target.op === "surface") {
        if (focusWindow(target.winId)) {
          workspace.activateTab(paperId);
          if (citation.kind === "quote")
            workspace.setHighlight(paperId, citation.quote);
          return;
        }
        void revealCitation(workspace, src, citation, { forceLocal: true });
        return;
      }
      void revealCitation(workspace, src, citation);
    },
    [workspace, focusWindow],
  );

  return (
    <div className="flex h-svh flex-col overflow-hidden">
      <AppBar
        userEmail={userEmail}
        left={
          <>
            <BackLink href={`/project/${analysis.projectId}`}>
              {analysis.projectName}
            </BackLink>
            <span className="flex min-w-0 max-w-[560px] flex-col leading-[1.25]">
              <span className="flex items-center gap-[7px] text-[10.5px] text-ink-faintest">
                <span>{analysis.scenario}</span>
                <span
                  className="size-[3px] rounded-full bg-separator-dot"
                  aria-hidden
                />
                <ReaderTime
                  className="font-mono"
                  iso={analysis.created.iso}
                  pinnedLabel={analysis.created.pinnedLabel}
                  pinnedTitle={analysis.created.pinnedTitle}
                />
              </span>
              <span
                className="truncate font-mono text-[13px] font-medium text-ink-primary"
                title={analysis.detail}
              >
                {analysis.title}
              </span>
            </span>
          </>
        }
        right={
          <ConversationDock
            edge={edge}
            onEdge={workspace.setConversationEdge}
          />
        }
      />
      <div className="relative flex min-h-0 flex-1 flex-col">
        <WorkspaceDataProvider
          value={{
            events,
            workingDocument,
            documentSignal: workingDocumentSignal,
            documentError: doc.isError,
          }}
        >
          {(() => {
            const conversation = (
              <ConversationPane
                analysisId={analysisId}
                events={events}
                pending={steering.pending}
                onCitation={(citation) =>
                  reveal({ kind: "conversation" }, citation)
                }
                composer={<SteerComposer steering={steering} />}
              />
            );
            if (tabAreaEmpty)
              return <div className="flex min-h-0 flex-1">{conversation}</div>;
            return (
              <ConversationSplit
                edge={edge}
                groupRef={groupRef}
                onLayoutChanged={onLayoutChanged}
                defaultConversationRatio={defaultConversationRatio}
                conversation={conversation}
                tabArea={
                  <TabArea
                    win={mainWindow}
                    controller={workspace}
                    windowActions={windowActions}
                    crossWindowDrag={crossWindowDrag}
                    onCitation={(winId, paneId, citation) =>
                      reveal({ kind: "document", winId, paneId }, citation)
                    }
                  />
                }
              />
            );
          })()}
        </WorkspaceDataProvider>
      </div>
    </div>
  );
}
