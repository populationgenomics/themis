"use client";

import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { resetWorkspaceReads } from "@/lib/queries";
import { workspaceCopy } from "@/workspace-copy/client";

// Clearing an Analysis's copy at a curator's request (docs/design/workbench-workspace.md, "The copy
// is a cache"): the SharedWorker deletes the copy once nothing holds its lock, and every window of
// the browser showing the Analysis reads it again, which hydrates it from nothing.

/** Asks the curator before the copy is cleared, and resolves whether to go ahead: an edit not yet
 *  published is discarded with the copy. The widget layer provides one; with none, nothing held
 *  only in the copy is lost. */
export type ClearCopyGuard = (analysisId: string) => Promise<boolean>;

export const ClearCopyGuardContext = createContext<ClearCopyGuard | null>(null);

/** Where a clear stands: not asked for, running, or failed with why. */
export type ClearCopyState =
  | { kind: "idle" }
  | { kind: "clearing" }
  | { kind: "failed"; message: string };

export interface CopyClearing {
  state: ClearCopyState;
  /** Clear the copy and read it again; asks the guard first, if there is one. */
  clear(): void;
}

const CHANNEL = "themis-workspace-copy-cleared";

/** Names this window in its announcements: a channel delivers to every other channel object of its
 *  name, this window's own listener included, and the clearing window re-reads for itself. */
const THIS_WINDOW = crypto.randomUUID();

interface CopyCleared {
  analysisId: string;
  window: string;
}

/** The clear control for `analysisId`'s copy, or null with no Analysis. */
export function useCopyClearing(
  analysisId: string | null,
): CopyClearing | null {
  const queryClient = useQueryClient();
  const guard = useContext(ClearCopyGuardContext);
  const [state, setState] = useState<ClearCopyState>({ kind: "idle" });
  const clear = useCallback(() => {
    if (analysisId === null) {
      throw new Error("clearing a copy needs an Analysis");
    }
    void clearCopy(
      analysisId,
      {
        guard,
        reset: workspaceCopy.reset,
        announce: announceCopyCleared,
        queryClient,
      },
      setState,
    );
  }, [analysisId, guard, queryClient]);
  return analysisId === null ? null : { state, clear };
}

/** What a clear runs against: the guard to ask, the copy to reset, the other windows to tell, and
 *  this window's reads. */
export interface ClearCopyDeps {
  guard: ClearCopyGuard | null;
  reset(analysisId: string): Promise<unknown>;
  announce(analysisId: string): void;
  queryClient: QueryClient;
}

/** Clear `analysisId`'s copy, reporting each step to `report`: ask the guard, if any, then reset
 *  the copy, tell the other windows, and read it again here. A failure is reported, and logged, as
 *  the clear's outcome. */
export async function clearCopy(
  analysisId: string,
  deps: ClearCopyDeps,
  report: (state: ClearCopyState) => void,
): Promise<void> {
  try {
    if (deps.guard !== null && !(await deps.guard(analysisId))) return;
    report({ kind: "clearing" });
    await deps.reset(analysisId);
    deps.announce(analysisId);
    await resetWorkspaceReads(deps.queryClient, analysisId);
    report({ kind: "idle" });
  } catch (error) {
    console.error(`clearing the copy of ${analysisId} failed`, error);
    report({
      kind: "failed",
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

/** Tell every other window of this browser that `analysisId`'s copy was cleared. */
function announceCopyCleared(analysisId: string): void {
  const channel = new BroadcastChannel(CHANNEL);
  channel.postMessage({
    analysisId,
    window: THIS_WINDOW,
  } satisfies CopyCleared);
  channel.close();
}

/** In a window showing `analysisId`, read its copy again whenever another window clears it. */
export function useCopyClearedAnnouncements(analysisId: string | null): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (analysisId === null) return;
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const data = event.data as Partial<CopyCleared> | null;
      if (data?.analysisId === analysisId && data.window !== THIS_WINDOW)
        void resetWorkspaceReads(queryClient, analysisId);
    };
    return () => channel.close();
  }, [analysisId, queryClient]);
}
