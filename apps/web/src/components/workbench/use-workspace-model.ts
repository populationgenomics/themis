"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import type { Orientation } from "@/components/ui/resizable";
import { openViaRegistry, REGISTRY } from "./content-kinds";
import {
  type Edge,
  findTab,
  type Hydration,
  type HydrationPane,
  type OpenTabOpts,
  type PaneSide,
  type Source,
  type Tab,
  type TabDescriptor,
  useWorkspaceModel,
  type Win,
  WORKING_DOC_TAB_ID,
  type WorkspaceState,
} from "./workspace-model";

// The controller around the pure windowed reducer (workspace-model.ts). It exposes typed action
// methods, keeps the reducer pure, and owns all persistence: the conversation edge, the
// orientation-specific outer ratios (a width % docked left/right, a height % docked top/bottom, kept
// under separate keys so an edge flip never carries one into the other), and the per-pane-side strip
// label mode. Persisted values load post-mount so SSR and the first client render match the reducer
// defaults. Papers open through a create-thunk: the id is resolved up front, the async fetch runs only
// for a genuinely new tab, so nothing un-serialisable reaches the reducer.

const EDGE_KEY = "workbench:conversationEdge";
const RATIO_H_KEY = "workbench:ratioH";
const RATIO_V_KEY = "workbench:ratioV";
const LABEL_KEY_PREFIX = "workbench:tabLabels:";
const LAYOUT_KEY = "workbench:layout";

/** localStorage access that never throws. A blocked store (Safari private mode, storage disabled)
 *  throws on the setItem/getItem call, and merely *touching* `window.localStorage` throws
 *  `SecurityError`; persistence here is best-effort UI state, never load-bearing, so a failure falls
 *  back to the reducer default rather than taking the workbench mount down. */
function readStore(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {}
}

/** The persisted-value parsers reject anything the reducer's types forbid, so a hand-edited or stale
 *  `localStorage` entry falls back to the default rather than corrupting state. */
export function parseEdge(raw: string | null): Edge | null {
  return raw === "left" || raw === "right" || raw === "top" || raw === "bottom"
    ? raw
    : null;
}

export function parseRatio(raw: string | null): number | null {
  if (raw === null) return null;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 && n < 1 ? n : null;
}

export function serializeLabelMode(value: boolean): string {
  return value ? "titles" : "icons";
}

export function parseLabelMode(raw: string | null): boolean | null {
  if (raw === "titles") return true;
  if (raw === "icons") return false;
  return null;
}

function ratioKey(orientation: Orientation): string {
  return orientation === "horizontal" ? RATIO_H_KEY : RATIO_V_KEY;
}

// Consolidate-on-reload layout persistence. Only main's tab area persists (per-pane ordered tab
// descriptors, active tab, active pane side, split ratio), plus the descriptors of papers open in child
// windows (child-origin, no persisted pane) and the reopen-last-closed stack. Window distribution,
// runtime handles, and `highlights` (transient) never persist. A tab persists as `{kind, args}` — the
// minimal re-open args, not the fetched payload — so rehydration re-fetches fresh (docs/design/
// document-pane.md §"Persistence and restore").

/** A persisted tab: its content kind and the args to re-fetch it. `args` is null for the pinned working
 *  document (reconstructed, never re-fetched); its slot persists only to keep its pane position. */
interface PersistedTab {
  kind: string;
  args: unknown;
}

interface PersistedPane {
  tabs: PersistedTab[];
  activeTabId: string | null;
}

export interface PersistedLayout {
  panes: PersistedPane[];
  activePaneSide: PaneSide;
  splitRatio: number;
  /** Descriptors of tabs open in non-main windows; they merge into main's active pane on rehydration. */
  childTabs: PersistedTab[];
  closedStack: TabDescriptor[];
}

function tabDescriptor(tab: Tab): PersistedTab | null {
  const kind = REGISTRY[tab.kind];
  if (!kind) return null;
  return {
    kind: tab.kind,
    args: kind.openArgs ? kind.openArgs(tab.payload) : null,
  };
}

export function serializeLayout(
  windows: Win[],
  mainId: string,
  closedStack: TabDescriptor[],
): string {
  const main = windows.find((w) => w.id === mainId);
  if (!main) throw new Error("main window not found");
  const isTab = (d: PersistedTab | null): d is PersistedTab => d !== null;
  const panes: PersistedPane[] = main.panes.map((p) => ({
    tabs: p.tabs.map(tabDescriptor).filter(isTab),
    activeTabId: p.activeTabId,
  }));
  const activeIndex = main.panes.findIndex((p) => p.id === main.activePaneId);
  const childTabs: PersistedTab[] = windows
    .filter((w) => w.id !== mainId)
    .flatMap((w) => w.panes.flatMap((p) => p.tabs))
    .map(tabDescriptor)
    .filter(isTab);
  const layout: PersistedLayout = {
    panes,
    activePaneSide: activeIndex === 1 ? "b" : "a",
    splitRatio: main.splitRatio,
    childTabs,
    closedStack,
  };
  return JSON.stringify(layout);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function isPersistedTab(v: unknown): v is PersistedTab {
  return isRecord(v) && typeof v.kind === "string";
}

function isDescriptor(v: unknown): v is TabDescriptor {
  return (
    isRecord(v) &&
    typeof v.id === "string" &&
    typeof v.kind === "string" &&
    // `reopenClosed` dereferences `payload` (via the kind's `openArgs`); a hand-edited or cross-version
    // entry missing it would throw there, so reject it at the parse boundary.
    isRecord(v.payload)
  );
}

/** Parse a persisted layout, falling back to null on anything the shape forbids. The entry is
 *  user-editable and cross-version, so a malformed one restores the default rather than throwing. */
export function parseLayout(raw: string | null): PersistedLayout | null {
  if (raw === null) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  if (
    !Array.isArray(value.panes) ||
    value.panes.length < 1 ||
    value.panes.length > 2
  )
    return null;
  const panes: PersistedPane[] = [];
  for (const p of value.panes) {
    if (!isRecord(p) || !Array.isArray(p.tabs) || !p.tabs.every(isPersistedTab))
      return null;
    if (p.activeTabId !== null && typeof p.activeTabId !== "string")
      return null;
    panes.push({ tabs: p.tabs, activeTabId: p.activeTabId ?? null });
  }
  if (value.activePaneSide !== "a" && value.activePaneSide !== "b") return null;
  if (!Array.isArray(value.childTabs) || !value.childTabs.every(isPersistedTab))
    return null;
  if (
    !Array.isArray(value.closedStack) ||
    !value.closedStack.every(isDescriptor)
  )
    return null;
  return {
    panes,
    activePaneSide: value.activePaneSide,
    splitRatio: typeof value.splitRatio === "number" ? value.splitRatio : 0.5,
    childTabs: value.childTabs,
    closedStack: value.closedStack,
  };
}

/** Re-fetch a persisted tab via the registry. A pinned kind (the working document) is reconstructed
 *  without a fetch; a stale/failed fetch resolves to null so one bad id never wedges the restore. */
async function fetchDescriptor(desc: PersistedTab): Promise<Tab | null> {
  const kind = REGISTRY[desc.kind];
  if (!kind) return null;
  if (kind.pinned)
    return {
      id: kind.id(desc.args),
      kind: desc.kind,
      pinned: true,
      payload: {},
    };
  if (!kind.open) return null;
  try {
    return await kind.open(desc.args);
  } catch {
    return null;
  }
}

/** Re-fetch every persisted tab and assemble the single main window: main's panes restore in order, and
 *  child-origin papers append into the active pane after the restored tabs (they had no persisted pane).
 *  The working document is a pinned singleton, so it is reconstructed if a failed/absent descriptor left
 *  it out. */
export async function hydrationFromLayout(
  layout: PersistedLayout,
): Promise<Hydration> {
  const keep = (t: Tab | null): t is Tab => t !== null;
  const rawPanes = await Promise.all(
    layout.panes.map(async (p) => ({
      tabs: (await Promise.all(p.tabs.map(fetchDescriptor))).filter(keep),
      activeTabId: p.activeTabId,
    })),
  );
  // A tab id must appear in at most one pane — the reducer throws on a duplicate, which gates the
  // persistence effect off for the whole session (the corrupt entry then never gets overwritten). A
  // stale/hand-edited layout can repeat the slot-keeping working-doc entry across both panes; keep the
  // first occurrence and drop the rest, the same shape as the `childTabs` dedupe below.
  const seen = new Set<string>();
  const dedupe = (tab: Tab): boolean => {
    if (seen.has(tab.id)) return false;
    seen.add(tab.id);
    return true;
  };
  const panes: HydrationPane[] = rawPanes.map((p) => {
    const tabs = p.tabs.filter(dedupe);
    return {
      tabs,
      activeTabId: tabs.some((t) => t.id === p.activeTabId)
        ? p.activeTabId
        : (tabs[tabs.length - 1]?.id ?? null),
    };
  });
  const childTabs = (
    await Promise.all(layout.childTabs.map(fetchDescriptor))
  ).filter(keep);

  const activeIndex =
    layout.activePaneSide === "b" && panes.length === 2 ? 1 : 0;
  const appended: Tab[] = [];
  for (const t of childTabs)
    if (!seen.has(t.id)) {
      seen.add(t.id);
      appended.push(t);
    }
  if (!seen.has(WORKING_DOC_TAB_ID))
    appended.unshift({
      id: WORKING_DOC_TAB_ID,
      kind: "working-doc",
      pinned: true,
      payload: {},
    });
  const active = panes[activeIndex];
  panes[activeIndex] = {
    tabs: [...active.tabs, ...appended],
    activeTabId:
      active.activeTabId ?? appended[appended.length - 1]?.id ?? null,
  };
  return {
    panes,
    activePaneSide: layout.activePaneSide,
    splitRatio: layout.splitRatio,
    // Drop closed-stack entries whose kind the registry no longer has (a since-removed or hand-edited
    // kind), the same way `fetchDescriptor` drops unknown pane tabs — otherwise "Reopen closed tab"
    // carries a resurrectable phantom that reopen would silently no-op.
    closedStack: layout.closedStack.filter((d) => d.kind in REGISTRY),
  };
}

export interface WorkspaceModelController {
  state: WorkspaceState;
  activateTab(tabId: string): void;
  activatePane(winId: string, paneId: string): void;
  /** Write back the inner two-pane divider ratio (pane 'a's fraction) for `winId` — persisted for main,
   *  broadcast to mirrors. */
  setSplitRatio(winId: string, ratio: number): void;
  split(tabId: string): void;
  /** Reorder a tab within its own pane to `toIndex` (the destination slot in the resulting order). */
  reorderTab(tabId: string, toIndex: number): void;
  moveTabToPane(tabId: string, toPaneId: string, toIndex?: number): void;
  /** Swap the two panes of a split window (left ↔ right). */
  swapPanes(winId: string): void;
  /** Move a tab into an existing window's active pane. */
  moveTabToWindow(tabId: string, toWinId: string): void;
  /** Move a tab into a fresh window whose ids the caller supplies (so it can `window.open` the child
   *  before the reducer mints them). */
  moveTabToNewWindow(tabId: string, newWinId: string, newPaneId: string): void;
  /** Close a child window: pinned tabs reparent into main's active pane, closable tabs drop. */
  closeWindow(winId: string): void;
  closeTab(tabId: string): void;
  /** Open (or surface/move, if already open) `id` beside `src`. The async `create` runs only when the
   *  paper is not already open — an open tab is reused, never re-fetched. `opts.forceLocal` overrides a
   *  cross-window surface (the focus-blocked fallback); `opts.intent` is ignored here (mirror-only). */
  openTab(
    src: Source,
    id: string,
    create: () => Promise<Tab>,
    opts?: OpenTabOpts,
  ): Promise<void>;
  setConversationEdge(edge: Edge): void;
  setLabel(winId: string, side: PaneSide, value: boolean): void;
  setHighlight(tabId: string, quote: string): void;
  /** Merge a partial payload into a tab (the paper representation toggle). */
  patchTab(tabId: string, payload: Record<string, unknown>): void;
  reopenClosed(): void;
  consolidate(): void;
  /** The persisted outer ratio (conversation's fraction) for this orientation, or null if unsaved. */
  readOuterRatio(orientation: Orientation): number | null;
  writeOuterRatio(orientation: Orientation, ratio: number): void;
}

export function useWorkspaceModelController(): WorkspaceModelController {
  const [state, dispatch] = useWorkspaceModel();

  // Latest state for the async open, which reads placement before deciding to run the create-thunk.
  const stateRef = useRef(state);
  stateRef.current = state;

  // Gates the persistence effect until the initial rehydration has run (or been found absent), so the
  // default first render never overwrites a saved layout before it is read.
  const hydratedRef = useRef(false);

  // Load persisted edge + label modes post-mount, then consolidate the saved layout into the single main
  // window. A malformed entry is ignored (parser returns null), leaving the reducer default. The layout
  // re-fetch is async (registry openers); the reducer stays pure — the `hydrate` action installs the
  // already-fetched tabs. `highlights` are not restored (transient).
  useEffect(() => {
    const edge = parseEdge(readStore(EDGE_KEY));
    if (edge) dispatch({ type: "setConversationEdge", edge });
    for (const side of ["a", "b"] as PaneSide[]) {
      const mode = parseLabelMode(readStore(LABEL_KEY_PREFIX + side));
      if (mode !== null)
        dispatch({
          type: "setLabel",
          winId: stateRef.current.mainId,
          side,
          value: mode,
        });
    }
    const layout = parseLayout(readStore(LAYOUT_KEY));
    if (!layout) {
      hydratedRef.current = true;
      return;
    }
    let cancelled = false;
    void hydrationFromLayout(layout).then((hydration) => {
      if (cancelled) return;
      dispatch({ type: "hydrate", hydration });
      hydratedRef.current = true;
    });
    return () => {
      cancelled = true;
    };
  }, [dispatch]);

  // Persist the main-window layout + child-origin papers + reopen stack on every durable change, once
  // the initial rehydration has settled.
  useEffect(() => {
    if (!hydratedRef.current) return;
    writeStore(
      LAYOUT_KEY,
      serializeLayout(state.windows, state.mainId, state.closedStack),
    );
  }, [state.windows, state.mainId, state.closedStack]);

  const setConversationEdge = useCallback(
    (edge: Edge) => {
      dispatch({ type: "setConversationEdge", edge });
      writeStore(EDGE_KEY, edge);
    },
    [dispatch],
  );

  const setLabel = useCallback(
    (winId: string, side: PaneSide, value: boolean) => {
      dispatch({ type: "setLabel", winId, side, value });
      if (winId === stateRef.current.mainId)
        writeStore(LABEL_KEY_PREFIX + side, serializeLabelMode(value));
    },
    [dispatch],
  );

  const openTab = useCallback(
    async (
      src: Source,
      id: string,
      create: () => Promise<Tab>,
      opts?: OpenTabOpts,
    ) => {
      const existing = findTab(stateRef.current, id);
      if (existing) {
        dispatch({
          type: "openTab",
          src,
          tab: existing,
          forceLocal: opts?.forceLocal,
        });
        return;
      }
      // Show the tab immediately in its loading state, then patch in the fetched payload — so a reveal
      // opens the tab at once rather than after the round-trip. A failed fetch patches the tab to an
      // error state (fail loud, visible) rather than closing it: `patchTab` no-ops if the user already
      // closed the loading tab (unlike `closeTab`, which throws on an absent tab). Closing a still-loading
      // tab does stack a descriptor, but reopen re-fetches through the registry, so it comes back fresh.
      const placeholder = opts?.placeholder;
      if (placeholder) {
        dispatch({
          type: "openTab",
          src,
          tab: placeholder,
          forceLocal: opts?.forceLocal,
        });
        try {
          const tab = await create();
          dispatch({
            type: "patchTab",
            tabId: id,
            payload: tab.payload as Record<string, unknown>,
          });
        } catch (error) {
          dispatch({
            type: "patchTab",
            tabId: id,
            payload: { loading: false, error: true },
          });
          throw error;
        }
        return;
      }
      const tab = await create();
      dispatch({ type: "openTab", src, tab, forceLocal: opts?.forceLocal });
    },
    [dispatch],
  );

  const activateTab = useCallback(
    (tabId: string) => dispatch({ type: "activateTab", tabId }),
    [dispatch],
  );
  const activatePane = useCallback(
    (winId: string, paneId: string) =>
      dispatch({ type: "activatePane", winId, paneId }),
    [dispatch],
  );
  const setSplitRatio = useCallback(
    (winId: string, ratio: number) =>
      dispatch({ type: "setSplitRatio", winId, ratio }),
    [dispatch],
  );
  const split = useCallback(
    (tabId: string) => dispatch({ type: "split", tabId }),
    [dispatch],
  );
  const reorderTab = useCallback(
    (tabId: string, toIndex: number) =>
      dispatch({ type: "reorderTab", tabId, toIndex }),
    [dispatch],
  );
  const moveTabToPane = useCallback(
    (tabId: string, toPaneId: string, toIndex?: number) =>
      dispatch({ type: "moveTabToPane", tabId, toPaneId, toIndex }),
    [dispatch],
  );
  const swapPanes = useCallback(
    (winId: string) => dispatch({ type: "swapPanes", winId }),
    [dispatch],
  );
  const moveTabToWindow = useCallback(
    (tabId: string, toWinId: string) =>
      dispatch({ type: "moveTabToWindow", tabId, toWinId }),
    [dispatch],
  );
  const moveTabToNewWindow = useCallback(
    (tabId: string, newWinId: string, newPaneId: string) =>
      dispatch({
        type: "moveTabToWindow",
        tabId,
        toWinId: null,
        newWinId,
        newPaneId,
      }),
    [dispatch],
  );
  const closeWindow = useCallback(
    (winId: string) => dispatch({ type: "closeWindow", winId }),
    [dispatch],
  );
  const closeTab = useCallback(
    (tabId: string) => dispatch({ type: "closeTab", tabId }),
    [dispatch],
  );
  const setHighlight = useCallback(
    (tabId: string, quote: string) =>
      dispatch({ type: "setHighlight", tabId, quote }),
    [dispatch],
  );
  const patchTab = useCallback(
    (tabId: string, payload: Record<string, unknown>) =>
      dispatch({ type: "patchTab", tabId, payload }),
    [dispatch],
  );
  const reopenClosed = useCallback(() => {
    const descriptor = stateRef.current.closedStack.at(-1);
    if (!descriptor) return;
    dispatch({ type: "dropClosed" });
    const kind = REGISTRY[descriptor.kind];
    if (!kind?.open) return;
    // Re-fetch through the registry rather than restoring the descriptor's (possibly loading or
    // failed) payload verbatim, so a tab closed mid-load reopens fresh instead of stuck on a spinner.
    void openViaRegistry(
      { openTab },
      {
        kind: descriptor.kind,
        args: kind.openArgs ? kind.openArgs(descriptor.payload) : null,
        src: { kind: "conversation" },
      },
    ).catch(() => {});
  }, [openTab, dispatch]);
  const consolidate = useCallback(
    () => dispatch({ type: "consolidate" }),
    [dispatch],
  );

  const readOuterRatio = useCallback(
    (orientation: Orientation) => parseRatio(readStore(ratioKey(orientation))),
    [],
  );
  const writeOuterRatio = useCallback(
    (orientation: Orientation, ratio: number) =>
      writeStore(ratioKey(orientation), String(ratio)),
    [],
  );

  return useMemo(
    () => ({
      state,
      activateTab,
      activatePane,
      setSplitRatio,
      split,
      reorderTab,
      moveTabToPane,
      swapPanes,
      moveTabToWindow,
      moveTabToNewWindow,
      closeWindow,
      closeTab,
      openTab,
      setConversationEdge,
      setLabel,
      setHighlight,
      patchTab,
      reopenClosed,
      consolidate,
      readOuterRatio,
      writeOuterRatio,
    }),
    [
      state,
      activateTab,
      activatePane,
      setSplitRatio,
      split,
      reorderTab,
      moveTabToPane,
      swapPanes,
      moveTabToWindow,
      moveTabToNewWindow,
      closeWindow,
      closeTab,
      openTab,
      setConversationEdge,
      setLabel,
      setHighlight,
      patchTab,
      reopenClosed,
      consolidate,
      readOuterRatio,
      writeOuterRatio,
    ],
  );
}
