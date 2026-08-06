"use client";

import { useReducer } from "react";

// The windowed workspace core: a set of windows, each a tab area of one or two panes; the
// conversation is a fixed region of the main window, not a tab. A tab lives in exactly one pane
// across all windows. State transitions are a pure reducer (unit-tested without a DOM); `computeTarget`
// resolves a reveal to a pane — the already-open check runs before placement, so a paper is surfaced
// or moved, never duplicated. Pane ids are ephemeral (regenerated on every split); persisted things —
// the strip label mode — key on the window id plus the pane SIDE ('a'/'b'), never a pane id. Rules
// live in docs/design/document-pane.md (§Reveal / §Tabs).

/** A content-kind-agnostic tab. `kind` selects the registry entry; `payload` is that kind's data. The
 *  reducer reasons only over `id`, `pinned`, and placement — the payload is opaque to it. */
export type Tab<P = unknown> = {
  id: string;
  kind: string;
  pinned: boolean;
  payload: P;
};

export const WORKING_DOC_TAB_ID = "doc:working";

export type Edge = "left" | "right" | "top" | "bottom";
export type PaneSide = "a" | "b";

export interface Pane {
  id: string;
  tabs: Tab[];
  activeTabId: string | null;
}

export interface Win {
  id: string;
  panes: [Pane] | [Pane, Pane];
  splitRatio: number;
  activePaneId: string;
}

/** The conversation dock: an edge plus a ratio per orientation, so an edge flip never carries a width
 *  into a height. `ratioH` applies when docked left/right, `ratioV` when top/bottom. */
export interface ConversationState {
  edge: Edge;
  ratioH: number;
  ratioV: number;
}

/** A closed tab retained for reopen. The pure reducer can't reach the content-kind registry to derive
 *  the re-fetch args, so it keeps the raw last `payload`; the controller maps it to those args (via the
 *  kind's `openArgs`) and re-opens fresh, rather than restoring the payload verbatim. */
export interface TabDescriptor {
  id: string;
  kind: string;
  payload: unknown;
}

/** One pane of a consolidate-on-reload rehydration: tabs already re-fetched by the hook (the reducer
 *  stays pure), plus which is active. */
export interface HydrationPane {
  tabs: Tab[];
  activeTabId: string | null;
}

/** A reconstructed single main window handed to the `hydrate` action. The hook re-fetches every tab
 *  and merges child-origin papers before dispatch; the reducer only mints pane ids and installs it. */
export interface Hydration {
  panes: HydrationPane[];
  activePaneSide: PaneSide;
  splitRatio: number;
  closedStack: TabDescriptor[];
}

export interface WorkspaceState {
  windows: Win[];
  mainId: string;
  conversation: ConversationState;
  /** Strip label mode keyed `${winId}:${side}` — the window and pane SIDE, never the ephemeral pane id. */
  labels: Record<string, boolean>;
  /** Per-tab active highlight quote (tab id → quote); transient, not persisted. */
  highlights: Record<string, string>;
  openPapers: string[];
  closedStack: TabDescriptor[];
  /** Monotonic source of ephemeral pane/window ids; an implementation detail of id minting. */
  seq: number;
}

export type Source =
  | { kind: "document"; winId: string; paneId: string }
  | { kind: "conversation" };

/** The content-kind + open args behind a reveal, forwarded so a mirror controller can post the reveal
 *  as a serialisable command (it never runs the registry opener itself). */
export interface OpenIntent {
  kind: string;
  args: unknown;
}

/** Options threaded to `openTab` alongside the id + create-thunk. `forceLocal` is the focus-blocked
 *  reveal fallback; `intent` lets a mirror repost the open as a command. */
export interface OpenTabOpts {
  forceLocal?: boolean;
  intent?: OpenIntent;
  /** A synchronous loading-state tab to place immediately while the async create runs; `open`'s result
   *  is patched onto it. Main-side only (a mirror reposts the intent; main re-derives the placeholder). */
  placeholder?: Tab;
}

export type TargetOp = "open" | "move" | "surface";

/** The reveal target. `paneId` is `null` when the target pane is the sibling to be created by splitting
 *  a single-pane window. `surface` points at the window/pane already holding the paper. */
export interface Target {
  winId: string;
  paneId: string | null;
  op: TargetOp;
}

const MAIN_ID = "main";
const MAIN_PANE_ID = "pane-0";

const WORKING_DOC_TAB: Tab = {
  id: WORKING_DOC_TAB_ID,
  kind: "working-doc",
  pinned: true,
  payload: {},
};

export const INITIAL_WORKSPACE_STATE: WorkspaceState = {
  windows: [
    {
      id: MAIN_ID,
      panes: [
        {
          id: MAIN_PANE_ID,
          tabs: [WORKING_DOC_TAB],
          activeTabId: WORKING_DOC_TAB_ID,
        },
      ],
      splitRatio: 0.5,
      activePaneId: MAIN_PANE_ID,
    },
  ],
  mainId: MAIN_ID,
  conversation: { edge: "left", ratioH: 0.33, ratioV: 0.33 },
  labels: {},
  highlights: {},
  openPapers: [],
  closedStack: [],
  seq: 1,
};

export type WorkspaceAction =
  | { type: "activateTab"; tabId: string }
  | { type: "activatePane"; winId: string; paneId: string }
  | { type: "setSplitRatio"; winId: string; ratio: number }
  | { type: "split"; tabId: string }
  | { type: "reorderTab"; tabId: string; toIndex: number }
  | { type: "moveTabToPane"; tabId: string; toPaneId: string; toIndex?: number }
  // Swap the two panes of a split window (left ↔ right): exchange their contents, keeping the slots.
  | { type: "swapPanes"; winId: string }
  // `toWinId: null` opens a new window. Its ids are minted from `seq` unless the caller supplies them
  // (the orchestrator does, so it knows the window id to `window.open` a child for before dispatch).
  | {
      type: "moveTabToWindow";
      tabId: string;
      toWinId: string | null;
      newWinId?: string;
      newPaneId?: string;
    }
  // Close a child window: its pinned tabs reparent into main's active pane, its closable tabs push onto
  // the reopen stack (recoverable, as a one-at-a-time close would be).
  | { type: "closeWindow"; winId: string }
  | { type: "closeTab"; tabId: string }
  // `forceLocal` ignores the cross-window already-open surface, placing the paper in this source's
  // computed target instead — the reveal fallback when the browser blocks raising the other window.
  | { type: "openTab"; src: Source; tab: Tab; forceLocal?: boolean }
  | { type: "setConversationEdge"; edge: Edge }
  | { type: "hydrate"; hydration: Hydration }
  | { type: "consolidate" }
  // Pop the last closed descriptor. Placement is the controller's job — it re-fetches through the
  // content-kind registry rather than restoring the (possibly loading/failed) payload verbatim.
  | { type: "dropClosed" }
  | { type: "setLabel"; winId: string; side: PaneSide; value: boolean }
  | { type: "setHighlight"; tabId: string; quote: string }
  | { type: "patchTab"; tabId: string; payload: Record<string, unknown> };

export function labelKey(winId: string, side: PaneSide): string {
  return `${winId}:${side}`;
}

function toPanes(arr: Pane[]): [Pane] | [Pane, Pane] {
  if (arr.length === 1) return [arr[0]];
  if (arr.length === 2) return [arr[0], arr[1]];
  throw new Error(`a window holds one or two panes, got ${arr.length}`);
}

interface Located {
  winId: string;
  win: Win;
  pane: Pane;
  paneIndex: number;
  tab: Tab;
}

function locateTab(state: WorkspaceState, tabId: string): Located | null {
  for (const win of state.windows) {
    for (let pi = 0; pi < win.panes.length; pi++) {
      const pane = win.panes[pi];
      const tab = pane.tabs.find((t) => t.id === tabId);
      if (tab) return { winId: win.id, win, pane, paneIndex: pi, tab };
    }
  }
  return null;
}

function getWindow(state: WorkspaceState, winId: string): Win {
  const win = state.windows.find((w) => w.id === winId);
  if (!win) throw new Error(`unknown window: ${winId}`);
  return win;
}

/** The tab with this id anywhere in the workspace, or null. Lets the controller decide whether a
 *  reveal must run its async create-thunk (a genuinely new paper) or reuse the open tab. */
export function findTab(state: WorkspaceState, tabId: string): Tab | null {
  return locateTab(state, tabId)?.tab ?? null;
}

/** Drop a tab from a pane, refocusing a neighbour if it was active (null if the pane empties). */
function removeTabFromPane(pane: Pane, tabId: string): Pane {
  const index = pane.tabs.findIndex((t) => t.id === tabId);
  if (index === -1) return pane;
  const tabs = pane.tabs.filter((t) => t.id !== tabId);
  const activeTabId =
    pane.activeTabId === tabId
      ? ((tabs[index] ?? tabs[index - 1] ?? null)?.id ?? null)
      : pane.activeTabId;
  return { ...pane, tabs, activeTabId };
}

/** Add `tab` at `index` (clamped; appended when omitted), unless it is already present. */
function addTabToPane(pane: Pane, tab: Tab, index?: number): Pane {
  if (pane.tabs.some((t) => t.id === tab.id))
    return { ...pane, activeTabId: tab.id };
  const at = Math.max(0, Math.min(index ?? pane.tabs.length, pane.tabs.length));
  const tabs = [...pane.tabs.slice(0, at), tab, ...pane.tabs.slice(at)];
  return { ...pane, tabs, activeTabId: tab.id };
}

/** Collapse a two-pane window with an empty pane back to the surviving pane (now at side 'a'). */
function collapseEmptyPanes(win: Win): Win {
  if (win.panes.length !== 2) return win;
  const [a, b] = win.panes;
  if (a.tabs.length > 0 && b.tabs.length > 0) return win;
  const survivor = a.tabs.length > 0 ? a : b;
  return { ...win, panes: [survivor], activePaneId: survivor.id };
}

/** Collapse empty panes, drop emptied child windows (main stays as a single zero-tab pane), and repair
 *  any `activePaneId` left dangling. */
function prune(state: WorkspaceState): WorkspaceState {
  let windows = state.windows.map(collapseEmptyPanes);
  windows = windows.filter(
    (w) => w.id === state.mainId || w.panes.some((p) => p.tabs.length > 0),
  );
  windows = windows.map((w) =>
    w.panes.some((p) => p.id === w.activePaneId)
      ? w
      : { ...w, activePaneId: w.panes[0].id },
  );
  // Drop label keys (`${winId}:${side}`) for windows this prune removed: they are keyed by window id,
  // reclaimed nowhere else, and window ids never recur, so they would otherwise grow per pop-out/close.
  const liveWinIds = new Set(windows.map((w) => w.id));
  const kept = Object.entries(state.labels).filter(([k]) =>
    liveWinIds.has(k.slice(0, k.lastIndexOf(":"))),
  );
  const labels =
    kept.length === Object.keys(state.labels).length
      ? state.labels
      : Object.fromEntries(kept);
  return { ...state, windows, labels };
}

function stripTab(state: WorkspaceState, tabId: string): WorkspaceState {
  return {
    ...state,
    windows: state.windows.map((w) => ({
      ...w,
      panes: toPanes(w.panes.map((p) => removeTabFromPane(p, tabId))),
    })),
  };
}

/** Remove any existing instance of `tab`, then place it at `target` (splitting when `paneId` is null),
 *  then prune. Tracks a closable tab in `openPapers` and drops any stale closed descriptor for it. */
function placeTabAt(
  state: WorkspaceState,
  tab: Tab,
  target: { winId: string; paneId: string | null; index?: number },
): WorkspaceState {
  const openPapers =
    tab.pinned || state.openPapers.includes(tab.id)
      ? state.openPapers
      : [...state.openPapers, tab.id];
  // Placing a paper by any route (reveal, forceLocal, cross-window move) makes a closed descriptor for
  // it stale, so drop it alongside the openPapers push — the single point every placement flows through.
  const closedStack = state.closedStack.some((d) => d.id === tab.id)
    ? state.closedStack.filter((d) => d.id !== tab.id)
    : state.closedStack;
  const stripped = stripTab({ ...state, openPapers, closedStack }, tab.id);
  let seq = stripped.seq;
  const windows = stripped.windows.map((w) => {
    if (w.id !== target.winId) return w;
    if (target.paneId === null) {
      if (w.panes.length !== 1)
        throw new Error("a split target must be a single-pane window");
      const newPane: Pane = {
        id: `pane-${seq++}`,
        tabs: [tab],
        activeTabId: tab.id,
      };
      return {
        ...w,
        panes: toPanes([w.panes[0], newPane]),
        splitRatio: 0.5,
        activePaneId: newPane.id,
      };
    }
    const paneId = target.paneId;
    if (!w.panes.some((p) => p.id === paneId))
      throw new Error(`unknown pane: ${paneId}`);
    return {
      ...w,
      panes: toPanes(
        w.panes.map((p) =>
          p.id === paneId ? addTabToPane(p, tab, target.index) : p,
        ),
      ),
      activePaneId: paneId,
    };
  });
  return prune({ ...stripped, seq, windows });
}

function activateExisting(
  state: WorkspaceState,
  tabId: string,
): WorkspaceState {
  const loc = locateTab(state, tabId);
  // Activation is a UI focus convenience, not an invariant (see "activatePane"): a click can bubble to
  // the tab strip after an action already removed that tab, or after a consolidate / hydrate /
  // cross-window move retargeted it. A no-op, not a fault.
  if (!loc) return state;
  // Already the active tab of the active pane ⇒ nothing to do; rebuilding would mint a new Win/Pane and
  // break the referential identity a memoization test pins (as "activatePane" guards the same case).
  if (loc.win.activePaneId === loc.pane.id && loc.pane.activeTabId === tabId)
    return state;
  return {
    ...state,
    windows: state.windows.map((w) =>
      w.id !== loc.winId
        ? w
        : {
            ...w,
            activePaneId: loc.pane.id,
            // Only rebuild the target pane, and only if the tab isn't already its active one — focusing
            // the other pane on a split (its tab already active) then changes activePaneId alone.
            panes: toPanes(
              w.panes.map((p) =>
                p.id !== loc.pane.id || p.activeTabId === tabId
                  ? p
                  : { ...p, activeTabId: tabId },
              ),
            ),
          },
    ),
  };
}

/** The pane the placement rules select, ignoring whether the paper is already open. */
function placementTarget(
  state: WorkspaceState,
  src: Source,
): { winId: string; paneId: string | null } {
  if (src.kind === "document") {
    const win = getWindow(state, src.winId);
    const srcIndex = win.panes.findIndex((p) => p.id === src.paneId);
    if (srcIndex === -1) throw new Error(`unknown pane: ${src.paneId}`);
    if (win.panes.length === 2) {
      const other = srcIndex === 0 ? 1 : 0;
      return { winId: win.id, paneId: win.panes[other].id };
    }
    return { winId: win.id, paneId: null };
  }
  const wd = locateTab(state, WORKING_DOC_TAB_ID);
  if (!wd) throw new Error("working document not found");
  if (wd.winId === state.mainId) {
    const main = wd.win;
    if (main.panes.length === 2) {
      const [a, b] = main.panes;
      // A zero-tab second pane does not count as split.
      if (a.tabs.length > 0 && b.tabs.length > 0) {
        const other = wd.paneIndex === 0 ? b : a;
        return { winId: state.mainId, paneId: other.id };
      }
    }
    return { winId: state.mainId, paneId: wd.pane.id };
  }
  const main = getWindow(state, state.mainId);
  return { winId: state.mainId, paneId: main.activePaneId };
}

export function computeTarget(
  state: WorkspaceState,
  src: Source,
  paperId: string,
): Target {
  const existing = locateTab(state, paperId);
  const placementWin = src.kind === "document" ? src.winId : state.mainId;
  if (existing && existing.winId !== placementWin) {
    return { winId: existing.winId, paneId: existing.pane.id, op: "surface" };
  }
  const placement = placementTarget(state, src);
  return { ...placement, op: existing ? "move" : "open" };
}

function clampSplit(ratio: number): number {
  return Number.isFinite(ratio) && ratio > 0 && ratio < 1 ? ratio : 0.5;
}

/** Replace `windows` with a single main window rebuilt from an already-fetched rehydration: mint fresh
 *  (ephemeral) pane ids, restore the active pane by side and the split ratio, and derive `openPapers`
 *  from the placed closable tabs. `highlights` are transient, so they reset. */
function hydrate(state: WorkspaceState, h: Hydration): WorkspaceState {
  const placed = new Set<string>();
  for (const p of h.panes)
    for (const t of p.tabs) {
      if (placed.has(t.id))
        throw new Error(`hydrate: tab ${t.id} appears in more than one pane`);
      placed.add(t.id);
    }
  // The working document is a pinned singleton every placement rule assumes exists (placementTarget
  // throws without it); a hydration that dropped it would fault later, in render, far from here.
  if (!placed.has(WORKING_DOC_TAB_ID))
    throw new Error("hydrate: the working document is not in any pane");
  let seq = state.seq;
  const panes: Pane[] = h.panes.map((p) => ({
    id: `pane-${seq++}`,
    tabs: p.tabs,
    activeTabId:
      p.activeTabId !== null && p.tabs.some((t) => t.id === p.activeTabId)
        ? p.activeTabId
        : (p.tabs[p.tabs.length - 1]?.id ?? null),
  }));
  const activeIndex = h.activePaneSide === "b" && panes.length === 2 ? 1 : 0;
  const win: Win = {
    id: state.mainId,
    panes: toPanes(panes),
    splitRatio: clampSplit(h.splitRatio),
    activePaneId: panes[activeIndex].id,
  };
  const openPapers = panes
    .flatMap((p) => p.tabs)
    .filter((t) => !t.pinned)
    .map((t) => t.id);
  return prune({
    ...state,
    windows: [win],
    openPapers,
    // A paper re-fetched into a pane can't also be pending reopen — drop any descriptor for it.
    closedStack: h.closedStack.filter((d) => !placed.has(d.id)),
    highlights: {},
    seq,
  });
}

function consolidate(state: WorkspaceState): WorkspaceState {
  const main = getWindow(state, state.mainId);
  const ordered = [main, ...state.windows.filter((w) => w.id !== state.mainId)];
  const all: Tab[] = [];
  const seen = new Set<string>();
  for (const w of ordered)
    for (const p of w.panes)
      for (const t of p.tabs)
        if (!seen.has(t.id)) {
          seen.add(t.id);
          all.push(t);
        }
  const activePane =
    main.panes.find((p) => p.id === main.activePaneId) ?? main.panes[0];
  const activeTabId = activePane.activeTabId ?? all[0]?.id ?? null;
  const paneId = main.panes[0].id;
  const win: Win = {
    id: state.mainId,
    panes: [{ id: paneId, tabs: all, activeTabId }],
    splitRatio: 0.5,
    activePaneId: paneId,
  };
  return { ...state, windows: [win] };
}

export function workspaceModelReducer(
  state: WorkspaceState,
  action: WorkspaceAction,
): WorkspaceState {
  switch (action.type) {
    case "activateTab":
      return activateExisting(state, action.tabId);
    case "activatePane": {
      const win = state.windows.find((w) => w.id === action.winId);
      // Activation is a UI focus convenience, not an invariant: a click can bubble to a pane's
      // activation handler after an action already removed that pane (or window), and activating the
      // already-active pane changes nothing. All three are no-ops, not faults.
      if (
        !win ||
        win.activePaneId === action.paneId ||
        !win.panes.some((p) => p.id === action.paneId)
      )
        return state;
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.id === action.winId ? { ...w, activePaneId: action.paneId } : w,
        ),
      };
    }
    case "setSplitRatio": {
      const win = getWindow(state, action.winId);
      // Nothing to size on a single-pane window; the ratio applies only to the inner divider.
      if (win.panes.length !== 2) return state;
      const ratio = clampSplit(action.ratio);
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.id === action.winId ? { ...w, splitRatio: ratio } : w,
        ),
      };
    }
    case "split": {
      const loc = locateTab(state, action.tabId);
      if (!loc) throw new Error(`split: unknown tab ${action.tabId}`);
      // At most two panes; nothing to separate from a lone tab.
      if (loc.win.panes.length === 2) return state;
      if (loc.pane.tabs.length <= 1) return state;
      return placeTabAt(state, loc.tab, { winId: loc.winId, paneId: null });
    }
    case "reorderTab": {
      const loc = locateTab(state, action.tabId);
      if (!loc) throw new Error(`reorderTab: unknown tab ${action.tabId}`);
      const from = loc.pane.tabs.findIndex((t) => t.id === action.tabId);
      // `toIndex` is the destination slot in the pane's resulting tab order.
      const to = Math.max(
        0,
        Math.min(action.toIndex, loc.pane.tabs.length - 1),
      );
      if (to === from) return state;
      const tabs = [...loc.pane.tabs];
      const [moved] = tabs.splice(from, 1);
      tabs.splice(to, 0, moved);
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.id !== loc.winId
            ? w
            : {
                ...w,
                panes: toPanes(
                  w.panes.map((p) =>
                    p.id === loc.pane.id ? { ...p, tabs } : p,
                  ),
                ),
              },
        ),
      };
    }
    case "moveTabToPane": {
      const loc = locateTab(state, action.tabId);
      if (!loc) throw new Error(`moveTabToPane: unknown tab ${action.tabId}`);
      const target = state.windows.find((w) =>
        w.panes.some((p) => p.id === action.toPaneId),
      );
      if (!target)
        throw new Error(`moveTabToPane: unknown pane ${action.toPaneId}`);
      return placeTabAt(state, loc.tab, {
        winId: target.id,
        paneId: action.toPaneId,
        index: action.toIndex,
      });
    }
    case "swapPanes": {
      const win = getWindow(state, action.winId);
      if (win.panes.length !== 2) return state;
      const [p0, p1] = win.panes;
      // Exchange contents, not the slots: each pane keeps its id (so react-resizable-panels keeps the
      // slot's width) and takes the other's tabs and active tab. `activePaneId` stays on its slot.
      const panes: [Pane, Pane] = [
        { ...p0, tabs: p1.tabs, activeTabId: p1.activeTabId },
        { ...p1, tabs: p0.tabs, activeTabId: p0.activeTabId },
      ];
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.id === action.winId ? { ...w, panes } : w,
        ),
      };
    }
    case "moveTabToWindow": {
      const loc = locateTab(state, action.tabId);
      if (!loc) throw new Error(`moveTabToWindow: unknown tab ${action.tabId}`);
      if (action.toWinId === null) {
        // The orchestrator supplies the ids (it needs the window id before dispatch to open the child)
        // from its own namespace; reject a collision rather than mint a duplicate — pane/window ids are
        // addressed by id alone (`getWindow`, `Source.paneId`, first-match pane lookup).
        if (
          action.newWinId &&
          state.windows.some((w) => w.id === action.newWinId)
        )
          throw new Error(
            `moveTabToWindow: window id in use: ${action.newWinId}`,
          );
        if (
          action.newPaneId &&
          state.windows.some((w) =>
            w.panes.some((p) => p.id === action.newPaneId),
          )
        )
          throw new Error(
            `moveTabToWindow: pane id in use: ${action.newPaneId}`,
          );
        const stripped = stripTab(state, action.tabId);
        let seq = stripped.seq;
        const paneId = action.newPaneId ?? `pane-${seq++}`;
        const winId = action.newWinId ?? `win-${seq++}`;
        const win: Win = {
          id: winId,
          panes: [{ id: paneId, tabs: [loc.tab], activeTabId: loc.tab.id }],
          splitRatio: 0.5,
          activePaneId: paneId,
        };
        return prune({ ...stripped, seq, windows: [...stripped.windows, win] });
      }
      const target = getWindow(state, action.toWinId);
      return placeTabAt(state, loc.tab, {
        winId: target.id,
        paneId: target.activePaneId,
      });
    }
    case "closeWindow": {
      if (action.winId === state.mainId) return state;
      const win = state.windows.find((w) => w.id === action.winId);
      if (!win) return state;
      const tabs = win.panes.flatMap((p) => p.tabs);
      const closable = tabs.filter((t) => !t.pinned);
      const closableIds = closable.map((t) => t.id);
      const pinned = tabs.filter((t) => t.pinned);
      const highlights = { ...state.highlights };
      for (const id of closableIds) delete highlights[id];
      const descriptors: TabDescriptor[] = closable.map((t) => ({
        id: t.id,
        kind: t.kind,
        payload: t.payload,
      }));
      let next: WorkspaceState = {
        ...state,
        highlights,
        windows: state.windows.filter((w) => w.id !== action.winId),
        openPapers: state.openPapers.filter((id) => !closableIds.includes(id)),
        closedStack: [...state.closedStack, ...descriptors],
      };
      const targetPaneId = getWindow(next, next.mainId).activePaneId;
      for (const tab of pinned)
        next = placeTabAt(next, tab, {
          winId: next.mainId,
          paneId: targetPaneId,
        });
      return prune(next);
    }
    case "closeTab": {
      const loc = locateTab(state, action.tabId);
      if (!loc) throw new Error(`closeTab: unknown tab ${action.tabId}`);
      if (loc.tab.pinned) return state;
      const highlights = { ...state.highlights };
      delete highlights[action.tabId];
      const descriptor: TabDescriptor = {
        id: loc.tab.id,
        kind: loc.tab.kind,
        payload: loc.tab.payload,
      };
      const stripped = stripTab(
        {
          ...state,
          highlights,
          openPapers: state.openPapers.filter((id) => id !== action.tabId),
          closedStack: [...state.closedStack, descriptor],
        },
        action.tabId,
      );
      return prune(stripped);
    }
    case "openTab": {
      const existing = locateTab(state, action.tab.id);
      const tab = existing ? existing.tab : action.tab;
      if (action.forceLocal)
        return placeTabAt(state, tab, placementTarget(state, action.src));
      const target = computeTarget(state, action.src, action.tab.id);
      // Already in the destination pane ⇒ activate in place. Re-placing would strip and re-append it,
      // silently reordering the strip; an already-open paper is surfaced, never shuffled.
      if (target.op === "surface" || existing?.pane.id === target.paneId)
        return activateExisting(state, action.tab.id);
      return placeTabAt(state, tab, target);
    }
    case "setConversationEdge":
      return {
        ...state,
        conversation: { ...state.conversation, edge: action.edge },
      };
    case "hydrate":
      return hydrate(state, action.hydration);
    case "consolidate":
      return consolidate(state);
    case "dropClosed": {
      if (state.closedStack.length === 0) return state;
      return { ...state, closedStack: state.closedStack.slice(0, -1) };
    }
    case "setLabel":
      return {
        ...state,
        labels: {
          ...state.labels,
          [labelKey(action.winId, action.side)]: action.value,
        },
      };
    case "setHighlight":
      return {
        ...state,
        highlights: { ...state.highlights, [action.tabId]: action.quote },
      };
    case "patchTab": {
      // Rebuild only the window and pane that hold the tab: every other reducer case preserves the
      // identity of untouched panes (a test pins that memoization), and a patch is one tab in one pane.
      const loc = locateTab(state, action.tabId);
      if (!loc) return state;
      const patch = (pane: Pane): Pane => ({
        ...pane,
        tabs: pane.tabs.map((t) => {
          if (t.id !== action.tabId) return t;
          if (typeof t.payload !== "object" || t.payload === null)
            throw new Error(`patchTab: tab ${t.id} has a non-object payload`);
          return { ...t, payload: { ...t.payload, ...action.payload } };
        }),
      });
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.id !== loc.winId
            ? w
            : {
                ...w,
                panes: toPanes(
                  w.panes.map((p) => (p.id === loc.pane.id ? patch(p) : p)),
                ),
              },
        ),
      };
    }
  }
}

export function useWorkspaceModel(): [
  WorkspaceState,
  React.Dispatch<WorkspaceAction>,
] {
  return useReducer(workspaceModelReducer, INITIAL_WORKSPACE_STATE);
}
