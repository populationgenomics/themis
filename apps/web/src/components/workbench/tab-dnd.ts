// The within-window tab-drag payload and the pure drop resolution (unit-tested without a DOM). A drag
// carries its tab id and source pane id on a custom `DataTransfer` type; that type is a carrier only —
// its value is unreadable during `dragover`, so a strip accepts any drag (`preventDefault` at
// `dragover`) and validates the payload's SHAPE at `drop` (§Alternatives: type-gated drop rejected).
// A drop whose payload is not a recognized tab move parses to `null` and is ignored — the seam
// external-file ingestion later reuses.

export const TAB_DND_TYPE = "application/x-themis-tab";

export interface TabDragPayload {
  tabId: string;
  paneId: string;
}

export function encodeTabDrag(payload: TabDragPayload): string {
  return JSON.stringify(payload);
}

/** Parse a drop payload, returning it only when it is a well-formed tab move; any other shape (a
 *  foreign drag, malformed JSON, a missing type) yields null so the drop is ignored, not a crash. */
export function parseTabDrag(raw: string | null): TabDragPayload | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const { tabId, paneId } = value as Record<string, unknown>;
  if (typeof tabId !== "string" || typeof paneId !== "string") return null;
  return { tabId, paneId };
}

/** The insertion slot for a pointer dropped over a vertical tab list: the count of tab midpoints above
 *  the pointer, i.e. an index in [0, midpoints.length]. */
export function insertionIndex(midpoints: number[], pointerY: number): number {
  let i = 0;
  while (i < midpoints.length && midpoints[i] < pointerY) i++;
  return i;
}

export type DropIntent =
  | { type: "none" }
  | { type: "reorder"; tabId: string; toIndex: number }
  | { type: "move"; tabId: string; toPaneId: string; toIndex: number };

/** Resolve a validated drop into a reducer intent. `current` is the tab's live location in this window
 *  (null ⇒ not a tab of this window ⇒ ignored). A different target pane is a move to the pointer's slot;
 *  the same pane is a reorder, unless the insertion slot is the tab's own place (before or right after
 *  it), a no-op. */
export function dropIntent(
  tabId: string,
  targetPaneId: string,
  insertAt: number,
  current: { paneId: string; index: number } | null,
): DropIntent {
  if (!current) return { type: "none" };
  if (targetPaneId !== current.paneId)
    return { type: "move", tabId, toPaneId: targetPaneId, toIndex: insertAt };
  if (insertAt === current.index || insertAt === current.index + 1)
    return { type: "none" };
  const toIndex = insertAt > current.index ? insertAt - 1 : insertAt;
  return { type: "reorder", tabId, toIndex };
}

// Cross-window drag. A cross-window OS drag can strip the custom `TAB_DND_TYPE`, so its payload is not
// read at the destination. Instead the source mints an opaque session id, places it in the standard
// `text/plain` type (carrying no structured data), and broadcasts the tab+source window over the
// workspace channel; every other window keeps a live-session map from those broadcasts and, at drop,
// correlates the id back to the tab to move. It carries no slot: a cross-window drop appends to the
// target pane, since the OS drag surfaces no in-strip pointer position to resolve. See
// docs/design/document-pane.md §Tabs.

/** A live cross-window drag: the tab being dragged and the window it started in, keyed by an opaque
 *  session id. A window learns of a remote drag from a `drag-session` broadcast and drops it on
 *  `drag-end`; BroadcastChannel never echoes to the sender, so a window never holds its own session. */
export interface DragSession {
  tabId: string;
  sourceWinId: string;
}

export type LiveSessions = Record<string, DragSession>;

export function addDragSession(
  sessions: LiveSessions,
  sessionId: string,
  tabId: string,
  sourceWinId: string,
): LiveSessions {
  return { ...sessions, [sessionId]: { tabId, sourceWinId } };
}

export function removeDragSession(
  sessions: LiveSessions,
  sessionId: string,
): LiveSessions {
  const { [sessionId]: _gone, ...rest } = sessions;
  return rest;
}

/** The live session for `sessionId`, or null for an unknown/stale/absent id (a foreign drop). */
export function resolveSession(
  sessions: LiveSessions,
  sessionId: string | null,
): DragSession | null {
  if (!sessionId) return null;
  return sessions[sessionId] ?? null;
}

export type DropResolution =
  | DropIntent
  | { type: "cross-window-move"; tabId: string; toPaneId: string };

/** Resolve a drop, preferring the within-window path. `within` is `dropIntent`'s result from the
 *  custom-type payload — `none` when the drag is not a tab of this window (e.g. a cross-window OS drag
 *  that stripped the custom type). Only then does a live remote session count, and only when it started
 *  in another window: a same-window session is never a cross-window move, and an absent session (a
 *  foreign drag) is ignored. */
export function resolveDrop(args: {
  within: DropIntent;
  session: DragSession | null;
  destWinId: string;
  destPaneId: string;
}): DropResolution {
  if (args.within.type !== "none") return args.within;
  if (args.session && args.session.sourceWinId !== args.destWinId)
    return {
      type: "cross-window-move",
      tabId: args.session.tabId,
      toPaneId: args.destPaneId,
    };
  return { type: "none" };
}
