import { openViaRegistry, REGISTRY } from "./content-kinds";
import { type DragSession, type LiveSessions, resolveSession } from "./tab-dnd";
import type { WorkspaceModelController } from "./use-workspace-model";
import type {
  ConversationState,
  PaneSide,
  Source,
  Win,
  WorkspaceState,
} from "./workspace-model";

// The N-window mirror protocol. Exactly one window is main and owns the authoritative reducer; every
// other is a thin mirror of one window's tab area. Main broadcasts a whole-workspace snapshot on every
// change (and on a child's request); a child renders its window from the snapshot and posts a command
// for every user action; main applies and re-broadcasts — no split-brain. Only structure and small
// signals cross the channel: never paper bytes, never the conversation transcript (main-only), and for
// the working document only its version + analysisId — each window fetches the body from the BFF keyed
// on that version. See docs/design/document-pane.md §"Windows: one source of truth, N thin mirrors".

/** Query params the opener passes to the /pane route: the BroadcastChannel id and the mirrored window. */
export const CHANNEL_PARAM = "ch";
export const WINDOW_PARAM = "win";

/** The working-document refetch signal — its version + analysisId, never its body. A version bump tells
 *  every window to re-fetch, so a popped working doc re-renders when the agent republishes. */
export interface WorkingDocumentSignal {
  version: number;
  analysisId: string;
}

/** The whole mirrored workspace: structure (windows/panes/tabs) plus small signals. No conversation
 *  transcript, no paper bytes, no working-document body. */
export interface WorkspaceSnapshot {
  windows: Win[];
  mainId: string;
  conversation: ConversationState;
  labels: Record<string, boolean>;
  highlights: Record<string, string>;
  openPapers: string[];
  workingDocument: WorkingDocumentSignal | null;
}

/** A user action in any window, applied by main to the authoritative controller. `openTab` is a
 *  lightweight intent — main runs the registry opener (the fetch, the create-thunk); nothing
 *  un-serialisable crosses the channel. `moveTabToWindow` with `toWinId: null` asks main to open a new
 *  child window (main mints its ids and `window.open`s it). */
export type WorkspaceCommand =
  | { type: "activateTab"; tabId: string }
  | { type: "activatePane"; winId: string; paneId: string }
  | { type: "setSplitRatio"; winId: string; ratio: number }
  | { type: "split"; tabId: string }
  | { type: "reorderTab"; tabId: string; toIndex: number }
  | { type: "moveTabToPane"; tabId: string; toPaneId: string; toIndex?: number }
  | { type: "swapPanes"; winId: string }
  | { type: "moveTabToWindow"; tabId: string; toWinId: string | null }
  | { type: "closeTab"; tabId: string }
  | { type: "setLabel"; winId: string; side: PaneSide; value: boolean }
  | { type: "setHighlight"; tabId: string; quote: string }
  | { type: "patchTab"; tabId: string; payload: Record<string, unknown> }
  | { type: "openTab"; kind: string; args: unknown; src: Source }
  // Move a whole pane's tabs into one new window; the orchestrator opens it and mints its ids.
  | { type: "moveTabsToNewWindow"; tabIds: string[] };

export type WorkspaceMessage =
  | { kind: "state"; snapshot: WorkspaceSnapshot }
  | { kind: "request-state" }
  | { kind: "command"; command: WorkspaceCommand }
  | { kind: "main-closing" }
  // A tab drag beginning in `sourceWinId`, keyed to an opaque session id (also carried in the drag's
  // `text/plain` type). Every other window records it so a drop can correlate the id to the tab.
  | {
      kind: "drag-session";
      sessionId: string;
      tabId: string;
      sourceWinId: string;
    }
  // The drag ended (drop-elsewhere or cancel): every window forgets the session.
  | { kind: "drag-end"; sessionId: string };

/** A window's view of cross-window drags: announce its own (`begin`/`end`, broadcast over the channel)
 *  and resolve a remote one (`resolve`, from the live-session map the channel handler maintains). Both
 *  main and a mirror build one from their channel. */
export interface CrossWindowDrag {
  begin(sessionId: string, tabId: string, sourceWinId: string): void;
  end(sessionId: string): void;
  resolve(sessionId: string | null): DragSession | null;
}

/** Build a `CrossWindowDrag` over a channel `post` and the window's live-session ref. `begin`/`end`
 *  broadcast; `resolve` reads the map the `onmessage` handler folds `drag-session`/`drag-end` into. */
export function makeCrossWindowDrag(
  post: (message: WorkspaceMessage) => void,
  sessions: { readonly current: LiveSessions },
): CrossWindowDrag {
  return {
    begin: (sessionId, tabId, sourceWinId) =>
      post({ kind: "drag-session", sessionId, tabId, sourceWinId }),
    end: (sessionId) => post({ kind: "drag-end", sessionId }),
    resolve: (sessionId) => resolveSession(sessions.current, sessionId),
  };
}

/** The snapshot main broadcasts: the reducer's structure + signals, plus the working-doc signal. */
export function buildSnapshot(
  state: WorkspaceState,
  workingDocument: WorkingDocumentSignal | null,
): WorkspaceSnapshot {
  return {
    windows: state.windows,
    mainId: state.mainId,
    conversation: state.conversation,
    labels: state.labels,
    highlights: state.highlights,
    openPapers: state.openPapers,
    workingDocument,
  };
}

/** The `{analysisId, version}` a window fetches the working-document body with (both null when no
 *  document has been produced). A version bump re-keys the query, so a mirror re-fetches on republish. */
export function documentFetchKey(snapshot: WorkspaceSnapshot): {
  analysisId: string | null;
  version: number | null;
} {
  const wd = snapshot.workingDocument;
  return { analysisId: wd?.analysisId ?? null, version: wd?.version ?? null };
}

/** Apply a command from a mirror to the authoritative controller (main). The new-window case
 *  (`moveTabToWindow` with `toWinId: null`) is handled by the orchestrator, which must `window.open` the
 *  child, so it is skipped here. */
export function applyWorkspaceCommand(
  controller: WorkspaceModelController,
  command: WorkspaceCommand,
): void {
  switch (command.type) {
    case "activateTab":
      controller.activateTab(command.tabId);
      return;
    case "activatePane":
      controller.activatePane(command.winId, command.paneId);
      return;
    case "setSplitRatio":
      controller.setSplitRatio(command.winId, command.ratio);
      return;
    case "split":
      controller.split(command.tabId);
      return;
    case "reorderTab":
      controller.reorderTab(command.tabId, command.toIndex);
      return;
    case "moveTabToPane":
      controller.moveTabToPane(
        command.tabId,
        command.toPaneId,
        command.toIndex,
      );
      return;
    case "swapPanes":
      controller.swapPanes(command.winId);
      return;
    case "moveTabToWindow":
      if (command.toWinId !== null)
        controller.moveTabToWindow(command.tabId, command.toWinId);
      return;
    case "closeTab":
      controller.closeTab(command.tabId);
      return;
    case "setLabel":
      controller.setLabel(command.winId, command.side, command.value);
      return;
    case "setHighlight":
      controller.setHighlight(command.tabId, command.quote);
      return;
    case "patchTab":
      controller.patchTab(command.tabId, command.payload);
      return;
    case "openTab":
      openViaRegistry(controller, {
        kind: command.kind,
        args: command.args,
        src: command.src,
      }).catch(() => {});
      return;
    case "moveTabsToNewWindow":
      // Handled by the orchestrator (it opens the window); intercepted before this apply.
      return;
  }
}

/** The reducer state a mirror renders from — its `.state` for `Pane`/`TabArea` (highlights, labels,
 *  window structure). `closedStack`/`seq` are main-only, so they are empty in a mirror. */
function snapshotState(snapshot: WorkspaceSnapshot): WorkspaceState {
  return {
    windows: snapshot.windows,
    mainId: snapshot.mainId,
    conversation: snapshot.conversation,
    labels: snapshot.labels,
    highlights: snapshot.highlights,
    openPapers: snapshot.openPapers,
    closedStack: [],
    seq: 0,
  };
}

/** A `WorkspaceModelController` for a mirror window: its mutators post commands instead of mutating,
 *  and `openTab` posts the `{kind, args}` intent (a mirror never runs the registry opener). Main-only
 *  actions (the conversation edge, persistence, reopen/consolidate) fail loud — no mirror invokes them. */
export function mirrorWorkspace(
  snapshot: WorkspaceSnapshot,
  _winId: string,
  send: (command: WorkspaceCommand) => void,
): WorkspaceModelController {
  const mainOnly = (name: string): never => {
    throw new Error(`${name} is main-only and cannot run in a mirror window`);
  };
  return {
    state: snapshotState(snapshot),
    activateTab: (tabId) => send({ type: "activateTab", tabId }),
    activatePane: (winId, paneId) =>
      send({ type: "activatePane", winId, paneId }),
    setSplitRatio: (winId, ratio) =>
      send({ type: "setSplitRatio", winId, ratio }),
    split: (tabId) => send({ type: "split", tabId }),
    reorderTab: (tabId, toIndex) =>
      send({ type: "reorderTab", tabId, toIndex }),
    moveTabToPane: (tabId, toPaneId, toIndex) =>
      send({ type: "moveTabToPane", tabId, toPaneId, toIndex }),
    swapPanes: (winId) => send({ type: "swapPanes", winId }),
    moveTabToWindow: (tabId, toWinId) =>
      send({ type: "moveTabToWindow", tabId, toWinId }),
    // A mirror can ask for a new window but not mint its ids — main opens it and mints them.
    moveTabToNewWindow: (tabId) =>
      send({ type: "moveTabToWindow", tabId, toWinId: null }),
    // Main reparents a gone mirror when its window handle closes (polled), not on a command.
    closeWindow: () => mainOnly("closeWindow"),
    closeTab: (tabId) => send({ type: "closeTab", tabId }),
    openTab: async (src, _id, _create, opts) => {
      if (!opts?.intent)
        throw new Error("a mirror openTab needs an intent to repost");
      send({
        type: "openTab",
        kind: opts.intent.kind,
        args: opts.intent.args,
        src,
      });
    },
    setConversationEdge: () => mainOnly("setConversationEdge"),
    setLabel: (winId, side, value) =>
      send({ type: "setLabel", winId, side, value }),
    setHighlight: (tabId, quote) =>
      send({ type: "setHighlight", tabId, quote }),
    patchTab: (tabId, payload) => send({ type: "patchTab", tabId, payload }),
    reopenClosed: () => mainOnly("reopenClosed"),
    consolidate: () => mainOnly("consolidate"),
    readOuterRatio: () => mainOnly("readOuterRatio"),
    writeOuterRatio: () => mainOnly("writeOuterRatio"),
  };
}

/** Since windows have no user title, a move-to-window menu names each destination by its active (or
 *  pinned) tab — the design's accessibility rule. */
export interface WindowDestination {
  winId: string;
  label: string;
}

/** Window-lifecycle actions the tab menu drives, distinct from the model controller: which other
 *  windows a tab can move to, and moving it there or to a fresh window. Main opens/closes real windows;
 *  a mirror posts commands. */
export interface WindowActions {
  /** Whether this window can open a *new* browser window. True only in main: a mirror's new-window
   *  request would run `window.open` in main's channel handler with no user activation and be blocked,
   *  so the menu hides the action rather than offer one that silently fails. Move-to-*existing*-window
   *  stays available in a mirror. */
  canOpenWindow: boolean;
  destinations(currentWinId: string): WindowDestination[];
  moveToWindow(tabId: string, winId: string): void;
  moveToNewWindow(tabId: string): void;
  /** Move a whole pane's tabs into one new window (the pane-menu action). */
  moveTabsToNewWindow(tabIds: string[]): void;
}

/** A window's label for the move-to-window menu: its active tab's title, else a pinned tab's, else the
 *  first tab's; "Empty window" when it holds none. */
export function windowLabel(win: Win): string {
  const pane = win.panes.find((p) => p.id === win.activePaneId) ?? win.panes[0];
  const tab =
    pane.tabs.find((t) => t.id === pane.activeTabId) ??
    pane.tabs.find((t) => t.pinned) ??
    pane.tabs[0];
  if (!tab) return "Empty window";
  return REGISTRY[tab.kind].label(tab.payload);
}

/** The move-to-window destinations: every window but the current one, each named by `windowLabel`. */
export function windowDestinations(
  windows: Win[],
  currentWinId: string,
): WindowDestination[] {
  return windows
    .filter((w) => w.id !== currentWinId)
    .map((w) => ({ winId: w.id, label: windowLabel(w) }));
}

/** `WindowActions` for a mirror window: destinations from the snapshot, moves posted as commands. */
export function mirrorWindowActions(
  snapshot: WorkspaceSnapshot,
  send: (command: WorkspaceCommand) => void,
): WindowActions {
  return {
    // A mirror cannot open a new window (see WindowActions.canOpenWindow); the menu hides the action, so
    // `moveToNewWindow`/`moveTabsToNewWindow` are never invoked in a mirror. They are here only to
    // satisfy the `WindowActions` interface, not because any mirror path reaches them.
    canOpenWindow: false,
    destinations: (currentWinId) =>
      windowDestinations(snapshot.windows, currentWinId),
    moveToWindow: (tabId, winId) =>
      send({ type: "moveTabToWindow", tabId, toWinId: winId }),
    moveToNewWindow: (tabId) =>
      send({ type: "moveTabToWindow", tabId, toWinId: null }),
    moveTabsToNewWindow: (tabIds) =>
      send({ type: "moveTabsToNewWindow", tabIds }),
  };
}
