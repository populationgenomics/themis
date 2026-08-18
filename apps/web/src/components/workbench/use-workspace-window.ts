"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { openViaRegistry, REGISTRY } from "./content-kinds";
import {
  addDragSession,
  type LiveSessions,
  removeDragSession,
} from "./tab-dnd";
import type { WorkspaceModelController } from "./use-workspace-model";
import { computeTarget } from "./workspace-model";
import {
  applyWorkspaceCommand,
  buildSnapshot,
  CHANNEL_PARAM,
  type CrossWindowDrag,
  makeCrossWindowDrag,
  WINDOW_PARAM,
  type WindowActions,
  type WorkingDocumentSignal,
  type WorkspaceCommand,
  type WorkspaceMessage,
  windowDestinations,
} from "./workspace-sync";

// Main-window orchestration of the N-window mirror. Main is authoritative: it broadcasts a whole-
// workspace snapshot on every change (and on a child's request) and applies commands children send
// back. Runtime-only bookkeeping — a per-child window-handle map and one BroadcastChannel — lives in
// refs, never in the snapshot or persistence. Children open via `window.open` with the opener retained
// (no `noopener`: that would destroy the focus/close handles), so they share main's process and die
// with it. See docs/design/document-pane.md §"Windows: one source of truth, N thin mirrors".

const CHILD_FEATURES = "popup,width=860,height=960";
const CLOSED_POLL_MS = 500;

function childUrl(channelId: string, winId: string): string {
  const params = new URLSearchParams({
    [CHANNEL_PARAM]: channelId,
    [WINDOW_PARAM]: winId,
  });
  return `/pane?${params.toString()}`;
}

export interface WorkspaceWindows {
  /** Move-to-window menu actions (open a new child, move to an existing one, list destinations). */
  windowActions: WindowActions;
  /** Cross-window drag over the workspace channel (announce main's drags, resolve a remote one). */
  crossWindowDrag: CrossWindowDrag;
  /** Raise the window holding an already-open paper (reveal's surface op). Returns false when no live
   *  window answers (closed/never opened), so the caller can fall back to a local move. */
  focusWindow(winId: string): boolean;
}

export function useWorkspaceWindow(
  controller: WorkspaceModelController,
  workingDocument: WorkingDocumentSignal | null,
): WorkspaceWindows {
  // One channel id for the whole session, stable from first render so a child URL can carry it.
  const [channelId] = useState(() =>
    typeof crypto !== "undefined" ? crypto.randomUUID() : "channel",
  );
  const channelRef = useRef<BroadcastChannel | null>(null);
  const handlesRef = useRef<Map<string, Window>>(new Map());
  // Live cross-window drags this window learns of from other windows' `drag-session`/`drag-end`
  // broadcasts (never its own — BroadcastChannel does not echo to the sender).
  const sessionsRef = useRef<LiveSessions>({});

  // Latest values for the channel handlers, which are installed once at mount.
  const controllerRef = useRef(controller);
  controllerRef.current = controller;
  const workingDocRef = useRef(workingDocument);
  workingDocRef.current = workingDocument;

  // A child window closed: reparent its pinned tabs into main's active pane, drop its closable tabs,
  // and forget its handle. Triggered by the `handle.closed` poll — never a child message, since a
  // child's `beforeunload` fires on reload too and must not reparent a merely-refreshing window — and
  // by `focusWindow` when it finds a handle already closed, so a closed child is always pruned.
  const handleChildClosing = useCallback((winId: string) => {
    handlesRef.current.get(winId)?.close();
    handlesRef.current.delete(winId);
    controllerRef.current.closeWindow(winId);
  }, []);

  const focusWindow = useCallback(
    (winId: string): boolean => {
      if (winId === controllerRef.current.state.mainId) {
        window.focus();
        return true;
      }
      const handle = handlesRef.current.get(winId);
      if (!handle || handle.closed) {
        // The window is gone. Reparent + prune it here rather than only dropping the handle —
        // otherwise the close poll (which iterates the handles) never sees it, and it lingers as a
        // phantom window in state with its tabs stranded until reload.
        handleChildClosing(winId);
        return false;
      }
      handle.focus();
      return true;
    },
    [handleChildClosing],
  );

  const moveToNewWindow = useCallback(
    (tabId: string) => {
      const winId = `win-${crypto.randomUUID()}`;
      const paneId = `pane-${crypto.randomUUID()}`;
      const child = window.open(
        childUrl(channelId, winId),
        `themis-pane-${winId}`,
        CHILD_FEATURES,
      );
      // Popup blocked: don't mint a window with no browser window to live in. Log rather than drop in
      // silence. Child-originated new-window requests can't reach here — the menu item is hidden in a
      // mirror (its window.open would run in this handler with no user activation and always block).
      if (!child) {
        console.error(
          "new-window open was blocked by the browser (allow popups)",
        );
        return;
      }
      handlesRef.current.set(winId, child);
      controllerRef.current.moveTabToNewWindow(tabId, winId, paneId);
      child.focus();
    },
    [channelId],
  );

  // Move a whole pane's tabs into one new window: the first tab mints the window's pane, the rest append.
  const moveTabsToNewWindow = useCallback(
    (tabIds: string[]) => {
      if (tabIds.length === 0) return;
      const winId = `win-${crypto.randomUUID()}`;
      const paneId = `pane-${crypto.randomUUID()}`;
      const child = window.open(
        childUrl(channelId, winId),
        `themis-pane-${winId}`,
        CHILD_FEATURES,
      );
      if (!child) {
        console.error(
          "new-window open was blocked by the browser (allow popups)",
        );
        return;
      }
      handlesRef.current.set(winId, child);
      const controller = controllerRef.current;
      controller.moveTabToNewWindow(tabIds[0], winId, paneId);
      for (const id of tabIds.slice(1)) controller.moveTabToWindow(id, winId);
      child.focus();
    },
    [channelId],
  );

  const handleCommand = useCallback(
    (command: WorkspaceCommand) => {
      if (command.type === "moveTabToWindow" && command.toWinId === null) {
        moveToNewWindow(command.tabId);
        return;
      }
      if (command.type === "moveTabsToNewWindow") {
        moveTabsToNewWindow(command.tabIds);
        return;
      }
      if (command.type === "openTab") {
        const state = controllerRef.current.state;
        const src = command.src;
        // The requesting window may have closed between posting and now; its reveal is moot (and
        // computing a placement into it would throw on the missing window).
        if (
          src.kind === "document" &&
          !state.windows.some((w) => w.id === src.winId)
        )
          return;
        const kind = REGISTRY[command.kind];
        // A mirror on a bundle that predates a registry change can post a since-removed kind; drop it
        // rather than throw `undefined.id` out of this channel handler with no diagnostic.
        if (!kind) return;
        const paperId = kind.id(command.args);
        const target = computeTarget(state, src, paperId);
        // A child's reveal of a paper already open in another window surfaces it — main raises that
        // window (the child cannot reach a sibling). If that window's handle is dead, place the paper
        // locally in the requesting window instead of losing the reveal. (A raise the browser *refuses*
        // is not detectable here — `focusWindow` reports success either way — so a surface into a
        // backgrounded window can still land there unseen: raising from a channel callback has no user
        // activation to spend.)
        if (target.op === "surface" && !focusWindow(target.winId)) {
          openViaRegistry(
            controllerRef.current,
            { kind: command.kind, args: command.args, src: command.src },
            { forceLocal: true },
          ).catch(() => {});
          return;
        }
      }
      try {
        applyWorkspaceCommand(controllerRef.current, command);
      } catch (error) {
        // A mirror renders from a snapshot that lags main by a broadcast round trip, so it can post a
        // command naming a tab/window main has already removed (e.g. a double-click on a tab's close
        // button). Those reducer paths throw on an id that no longer resolves; the corrective snapshot
        // is already on its way, so drop the stale command rather than let it escape this handler.
        console.warn("dropped a stale mirror command", command.type, error);
      }
    },
    [moveToNewWindow, moveTabsToNewWindow, focusWindow],
  );

  // Latest command handler for the channel, which is installed once at mount: this effect must not
  // re-run, or its cleanup would tear the curator's windows down mid-session.
  const handleCommandRef = useRef(handleCommand);
  handleCommandRef.current = handleCommand;

  // Install the channel and the close-poll backstop once. A child re-mounting posts `request-state`;
  // the same handler replies. Main going away — the tab unloading, or this workbench unmounting as the
  // route changes — posts `main-closing` so children self-close rather than mirroring a channel nobody
  // publishes to, and closes the handles it holds.
  useEffect(() => {
    const channel = new BroadcastChannel(channelId);
    channelRef.current = channel;
    const handles = handlesRef.current;
    channel.onmessage = (event: MessageEvent<WorkspaceMessage>) => {
      const message = event.data;
      if (message.kind === "request-state")
        channel.postMessage({
          kind: "state",
          snapshot: buildSnapshot(
            controllerRef.current.state,
            workingDocRef.current,
          ),
        } satisfies WorkspaceMessage);
      else if (message.kind === "command")
        handleCommandRef.current(message.command);
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
    const poll = window.setInterval(() => {
      for (const [winId, handle] of handlesRef.current)
        if (handle.closed) handleChildClosing(winId);
    }, CLOSED_POLL_MS);
    const closeChildren = () =>
      channel.postMessage({ kind: "main-closing" } satisfies WorkspaceMessage);
    window.addEventListener("beforeunload", closeChildren);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener("beforeunload", closeChildren);
      closeChildren();
      // Belt and braces for a blocked or already-detached child: `main-closing` asks a child to close
      // itself, and the handle closes the ones that do not answer.
      for (const handle of handles.values()) handle.close();
      handles.clear();
      channel.close();
      channelRef.current = null;
    };
  }, [channelId, handleChildClosing]);

  // Broadcast the snapshot on every workspace or working-doc-version change.
  useEffect(() => {
    channelRef.current?.postMessage({
      kind: "state",
      snapshot: buildSnapshot(controller.state, workingDocument),
    } satisfies WorkspaceMessage);
  }, [controller.state, workingDocument]);

  // Reconcile handles: a window that left the state (a child that moved its last tab out, so the
  // reducer pruned it) has its browser window closed and its handle forgotten.
  useEffect(() => {
    const live = new Set(controller.state.windows.map((w) => w.id));
    for (const [winId, handle] of handlesRef.current)
      if (!live.has(winId)) {
        handle.close();
        handlesRef.current.delete(winId);
      }
  }, [controller.state.windows]);

  const windowActions: WindowActions = useMemo(
    () => ({
      // Only the main window can open a new browser window: window.open here carries the user's
      // activation. A mirror's request would run in main's channel handler with none and be blocked.
      canOpenWindow: true,
      destinations: (currentWinId) =>
        windowDestinations(controller.state.windows, currentWinId),
      moveToWindow: (tabId, winId) => {
        // Raise first: `focusWindow` prunes a dead destination (closing its tabs), so moving before
        // the liveness check would hand the tab to a window that is about to drop it — destroying it
        // rather than moving it. A destination can still be in `state.windows` (offered) but already
        // closed, until the close poll catches up.
        if (!focusWindow(winId)) return;
        controller.moveTabToWindow(tabId, winId);
      },
      moveToNewWindow: (tabId) => moveToNewWindow(tabId),
      moveTabsToNewWindow: (tabIds) => moveTabsToNewWindow(tabIds),
    }),
    [
      controller.state.windows,
      controller,
      focusWindow,
      moveToNewWindow,
      moveTabsToNewWindow,
    ],
  );

  const crossWindowDrag = useMemo(
    () =>
      makeCrossWindowDrag(
        (message) => channelRef.current?.postMessage(message),
        sessionsRef,
      ),
    [],
  );

  return { windowActions, crossWindowDrag, focusWindow };
}
