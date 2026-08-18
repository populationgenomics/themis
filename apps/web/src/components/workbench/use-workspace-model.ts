"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import type { Orientation } from "@/components/ui/resizable";
import { readStore, writeStore } from "@/lib/browser-store";
import { openViaRegistry, REGISTRY } from "./content-kinds";
import {
  type Edge,
  findTab,
  type OpenTabOpts,
  type PaneSide,
  type Source,
  type Tab,
  useWorkspaceModel,
  type WorkspaceState,
} from "./workspace-model";

// The controller around the pure windowed reducer (workspace-model.ts). It exposes typed action
// methods, keeps the reducer pure, and owns all persistence — which covers the arrangement only: the
// conversation edge, the orientation-specific outer ratios (a width % docked left/right, a height %
// docked top/bottom, kept under separate keys so an edge flip never carries one into the other), the
// per-pane-side strip label mode, and the inner two-pane divider. Which papers are open belongs to
// the Analysis that cited them and persists nowhere (docs/design/workbench-navigation.md). Persisted
// values load post-mount so SSR and the first client render match the reducer defaults. Papers open
// through a create-thunk: the id is resolved up front, the async fetch runs only for a genuinely new
// tab, so nothing un-serialisable reaches the reducer.

const EDGE_KEY = "workbench:conversationEdge";
const RATIO_H_KEY = "workbench:ratioH";
const RATIO_V_KEY = "workbench:ratioV";
const LABEL_KEY_PREFIX = "workbench:tabLabels:";
const INNER_RATIO_KEY = "workbench:innerRatio";

/** The persisted-value validators reject anything the reducer's types forbid, so a stale or
 *  hand-edited record falls back to the default rather than corrupting state. */
export function parseEdge(raw: unknown): Edge | null {
  return raw === "left" || raw === "right" || raw === "top" || raw === "bottom"
    ? raw
    : null;
}

export function parseRatio(raw: unknown): number | null {
  return typeof raw === "number" && Number.isFinite(raw) && raw > 0 && raw < 1
    ? raw
    : null;
}

export function parseLabelMode(raw: unknown): boolean | null {
  return typeof raw === "boolean" ? raw : null;
}

function ratioKey(orientation: Orientation): string {
  return orientation === "horizontal" ? RATIO_H_KEY : RATIO_V_KEY;
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
  readOuterRatio(orientation: Orientation): Promise<number | null>;
  writeOuterRatio(orientation: Orientation, ratio: number): void;
}

export function useWorkspaceModelController(): WorkspaceModelController {
  const [state, dispatch] = useWorkspaceModel();

  // Latest state for the async open, which reads placement before deciding to run the create-thunk.
  const stateRef = useRef(state);
  stateRef.current = state;

  // A curator who arranges the workbench before the restore resolves has already written their
  // choice, so restoring over it would leave the store and the screen disagreeing until the next
  // load. The restore yields to them from the first such action onward.
  const arrangedRef = useRef(false);

  // Load the persisted arrangement post-mount, so SSR and the first client render both show the
  // reducer default. A malformed entry parses to null and leaves that default standing.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // One pass, not four sequential round trips: the window this races is the reads' duration.
      const [edgeRaw, labelA, labelB, ratioRaw] = await Promise.all([
        readStore(EDGE_KEY),
        readStore(`${LABEL_KEY_PREFIX}a`),
        readStore(`${LABEL_KEY_PREFIX}b`),
        readStore(INNER_RATIO_KEY),
      ]);
      if (cancelled || arrangedRef.current) return;
      const edge = parseEdge(edgeRaw);
      if (edge) dispatch({ type: "setConversationEdge", edge });
      for (const [side, raw] of [
        ["a", labelA],
        ["b", labelB],
      ] as const) {
        const mode = parseLabelMode(raw);
        if (mode !== null)
          dispatch({
            type: "setLabel",
            winId: stateRef.current.mainId,
            side,
            value: mode,
          });
      }
      const ratio = parseRatio(ratioRaw);
      if (ratio !== null)
        dispatch({
          type: "setSplitRatio",
          winId: stateRef.current.mainId,
          ratio,
        });
    })();
    return () => {
      cancelled = true;
    };
  }, [dispatch]);

  const setConversationEdge = useCallback(
    (edge: Edge) => {
      arrangedRef.current = true;
      dispatch({ type: "setConversationEdge", edge });
      void writeStore(EDGE_KEY, edge);
    },
    [dispatch],
  );

  const setLabel = useCallback(
    (winId: string, side: PaneSide, value: boolean) => {
      arrangedRef.current = true;
      dispatch({ type: "setLabel", winId, side, value });
      if (winId === stateRef.current.mainId)
        void writeStore(LABEL_KEY_PREFIX + side, value);
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
    (winId: string, ratio: number) => {
      arrangedRef.current = true;
      dispatch({ type: "setSplitRatio", winId, ratio });
      // Main's divider only: a child window's split is that window's, and it dies with it.
      if (winId === stateRef.current.mainId)
        void writeStore(INNER_RATIO_KEY, ratio);
    },
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

  // What this session has written, per orientation. The stored read is asynchronous, so a curator who
  // drags the outer divider while it is in flight would otherwise have their ratio applied and then
  // overwritten by the one the read started before.
  const outerRatioRef = useRef(new Map<Orientation, number>());
  const readOuterRatio = useCallback(async (orientation: Orientation) => {
    const stored = parseRatio(await readStore(ratioKey(orientation)));
    return outerRatioRef.current.get(orientation) ?? stored;
  }, []);
  const writeOuterRatio = useCallback(
    (orientation: Orientation, ratio: number) => {
      arrangedRef.current = true;
      outerRatioRef.current.set(orientation, ratio);
      void writeStore(ratioKey(orientation), ratio);
    },
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
