"use client";

import {
  keepPreviousData,
  type QueryClient,
  type UseQueryResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { workbench } from "@/lib/rpc";
import {
  type AnalysisInputs,
  type PollResponse,
  SubAgentStatus,
  type ThreadResponse,
} from "@/models/workbench";
import { workspaceCopy } from "@/workspace-copy/client";
import type { FileAtCommit, RecordedTip } from "@/workspace-copy/copy";

// TanStack Query wiring for what the browser must keep asking for: the liveness poll over the
// generated Workbench client (`@/lib/rpc`), and the working document it signals, read from the
// browser's copy of the workspace repository (`@/workspace-copy/client`). Stored state that is fixed
// for the life of a page (Projects, a Project's Analyses, an Analysis's identity) is read by that
// page's server component instead — see docs/design/workbench-navigation.md.
//
// The poll drives the workbench: one ~2.5s tick returns the FULL projected event list each time
// (replace-by-id, never append), plus the workspace branch's tip. The copy is brought up to date,
// and the document read again, only when that tip moves.

const POLL_INTERVAL_MS = 2500;

export function useCreateAnalysis() {
  return useMutation({
    mutationFn: (input: { inputs: AnalysisInputs; projectId: string }) =>
      workbench.createAnalysis(input),
  });
}

/** The liveness tick. Disabled until an analysis exists, and otherwise runs for as
 *  long as one is open: an Analysis is Project-scoped and resumable, so a finished
 *  turn is a pause another curator can steer out of, not a state to stop on. A
 *  hidden tab pauses and catches up on focus rather than polling unseen. */
export function usePoll(id: string | null): UseQueryResult<PollResponse> {
  return useQuery({
    queryKey: ["poll", id],
    queryFn: async () => {
      if (id === null) {
        throw new Error("usePoll query ran with a null analysis id");
      }
      return workbench.poll({ analysisId: id });
    },
    enabled: id !== null,
    refetchInterval: POLL_INTERVAL_MS,
  });
}

/** One spawned thread's own stream — fetched only while its card is expanded, and
 *  re-read on the poll's interval while the thread is running. `status` sits in the
 *  query key because a `refetchInterval` flipping to false fires no final fetch
 *  (docs/design/conversation-view.md). */
export function useThread(
  analysisId: string,
  threadId: string,
  status: SubAgentStatus,
  expanded: boolean,
): UseQueryResult<ThreadResponse> {
  return useQuery({
    queryKey: ["thread", analysisId, threadId, status],
    queryFn: () => workbench.getThread({ analysisId, threadId }),
    enabled: expanded,
    refetchInterval:
      status === SubAgentStatus.RUNNING ? POLL_INTERVAL_MS : false,
    placeholderData: keepPreviousData,
  });
}

/** The curator's turn. The RPC accepts it; the poll is what surfaces it and whatever the
 *  agent does with it, so a success invalidates the tick rather than writing to the cache —
 *  the poll stays the single authority on the conversation, and the invalidation only
 *  shortens the window the locally-echoed turn is shown for. */
export function useSteer(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (text: string) => workbench.steer({ analysisId, text }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["poll", analysisId] }),
  });
}

/** The curator halting the run's current step. A success invalidates the tick for
 *  `useSteer`'s reason: the poll surfaces the halted step (the in-flight call closed
 *  with an error result), and the invalidation shortens the window it shows stale. */
export function useInterrupt(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => workbench.interrupt({ analysisId }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["poll", analysisId] }),
  });
}

/** The failures of the browser's copy that trying again can clear: a relay call that failed, a
 *  download, a hydration the document kept moving under, a worker that stopped answering and is
 *  started again, a copy another build's worker evicted. Every other — a workspace over the copy's
 *  ceiling, a damaged repository, a document that is not UTF-8 — is definitive. */
const RETRYABLE_COPY_FAILURES = new Set([
  "ConnectError",
  "DownloadError",
  "HydrationError",
  "CopyWorkerLostError",
  "CommitNotInCopyError",
]);

/** Whether a read of the copy that failed `failureCount` times with `error` is tried again. */
export function retryCopy(failureCount: number, error: Error): boolean {
  return RETRYABLE_COPY_FAILURES.has(error.name) && failureCount < 3;
}

/** Drop every read of `analysisId`'s copy this window holds, so each shown one reads the copy
 *  again: after the copy was cleared, which hydrates it from nothing on that read. */
export function resetWorkspaceReads(
  queryClient: QueryClient,
  analysisId: string,
): Promise<void> {
  return Promise.all([
    queryClient.resetQueries({ queryKey: ["workspace-document", analysisId] }),
    queryClient.resetQueries({ queryKey: ["workspace-history", analysisId] }),
    queryClient.resetQueries({ queryKey: ["workspace-file", analysisId] }),
  ]).then(() => undefined);
}

/** What was read at a commit, beside the commit: a query keeps the previous commit's read on screen
 *  while the next loads, and what is drawn from it has to name the commit it came from. */
export interface ReadAt<T> {
  commit: string;
  value: T;
}

/** A working document read at a commit, and whether it was read as the commit the working-doc tab
 *  pins: a body kept on screen while the next loads says what it was read as. */
export interface DocumentRead extends ReadAt<string | null> {
  pinned: boolean;
}

/** The working document at `commit`, read from the browser's copy of the Analysis's workspace
 *  repository once the copy holds `tip`: the Poll's tip when following the branch, an earlier
 *  commit when the working-doc tab pins one. A null value is a commit with no working document. */
export function useWorkspaceDocument(
  key: {
    analysisId: string;
    tip: string;
    commit: string;
    pinned: boolean;
  } | null,
): UseQueryResult<DocumentRead> {
  return useQuery({
    queryKey: ["workspace-document", key?.analysisId, key?.commit, key?.pinned],
    queryFn: async () => {
      if (key === null) {
        throw new Error(
          "useWorkspaceDocument query ran with no commit to read",
        );
      }
      await workspaceCopy.sync(key.analysisId, key.tip);
      return {
        commit: key.commit,
        value: await workspaceCopy.readDocument(key.analysisId, key.commit),
        pinned: key.pinned,
      };
    },
    enabled: key !== null,
    // A commit never changes, so a document read at one never goes stale.
    staleTime: Number.POSITIVE_INFINITY,
    retry: retryCopy,
    // Keep the previous version's body on screen across a same-analysis version switch; never
    // carry a body across an analysis switch.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === key?.analysisId ? previous : undefined,
  });
}

/** Each tip the reflog recorded for the collaborative branch, newest first, once the copy holds
 *  `tip`: what the version picker lists. */
export function useWorkspaceHistory(
  key: { analysisId: string; tip: string } | null,
): UseQueryResult<RecordedTip[]> {
  return useQuery({
    queryKey: ["workspace-history", key?.analysisId, key?.tip],
    queryFn: async () => {
      if (key === null) {
        throw new Error("useWorkspaceHistory query ran with no tip");
      }
      await workspaceCopy.sync(key.analysisId, key.tip);
      return workspaceCopy.history(key.analysisId, key.tip);
    },
    enabled: key !== null,
    // The history at a tip is fixed: the reflog only grows, and a new tip re-keys the query.
    staleTime: Number.POSITIVE_INFINITY,
    retry: retryCopy,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === key?.analysisId ? previous : undefined,
  });
}

/** The file at `path` in `commit`, its bytes and tree mode, read from the browser's copy once it holds
 *  `tip`: an asset a working-document widget draws. A null value is a commit with no such file. */
export function useWorkspaceFile(
  key: { analysisId: string; tip: string; commit: string; path: string } | null,
): UseQueryResult<ReadAt<FileAtCommit | null>> {
  return useQuery({
    queryKey: ["workspace-file", key?.analysisId, key?.commit, key?.path],
    queryFn: async () => {
      if (key === null) {
        throw new Error("useWorkspaceFile query ran with no file to read");
      }
      await workspaceCopy.sync(key.analysisId, key.tip);
      return {
        commit: key.commit,
        value: await workspaceCopy.readFile(
          key.analysisId,
          key.commit,
          key.path,
        ),
      };
    },
    enabled: key !== null,
    // A commit never changes, so a file read at one never goes stale.
    staleTime: Number.POSITIVE_INFINITY,
    retry: retryCopy,
    // Keep the previous commit's asset drawn while the next commit's loads, never another file's.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === key?.analysisId &&
      previousQuery?.queryKey[3] === key?.path
        ? previous
        : undefined,
  });
}
