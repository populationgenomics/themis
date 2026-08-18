"use client";

import { Code, ConnectError } from "@connectrpc/connect";
import {
  keepPreviousData,
  type UseQueryResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { workbench } from "@/lib/rpc";
import {
  type AnalysisInputs,
  type DocumentResponse,
  type PollResponse,
  SubAgentStatus,
  type ThreadResponse,
} from "@/models/workbench";

// TanStack Query wiring over the generated Workbench client (`@/lib/rpc`) for what the browser
// must keep asking for: the liveness poll and the working document it signals. Stored state that
// is fixed for the life of a page (Projects, a Project's Analyses, an Analysis's identity) is read
// by that page's server component instead — see docs/design/workbench-navigation.md.
//
// The poll drives the workbench: one ~2.5s tick returns the FULL projected event list each time
// (replace-by-id, never append), plus the working-document version signal. The document refetches
// only when that version changes.

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

/** The working-document body authority, fetching exactly `version` — the poll's latest
 *  when following the current document, an older one when the working-doc tab pins it.
 *  Disabled until a version exists. */
export function useDocument(
  id: string | null,
  version: number | null,
): UseQueryResult<DocumentResponse> {
  return useQuery({
    queryKey: ["document", id, version],
    queryFn: () => {
      if (id === null || version === null) {
        throw new Error(
          "useDocument query ran with a null analysis id or version",
        );
      }
      return workbench.getDocument({ analysisId: id, version });
    },
    enabled: id !== null && version !== null,
    // A version's body is immutable (the store is append-only), so a cached entry never goes stale.
    staleTime: Number.POSITIVE_INFINITY,
    // A not-found version is definitive, not transient — retrying only prolongs the placeholder
    // body under the new version's label before the error surfaces.
    retry: (failureCount, error) =>
      ConnectError.from(error).code !== Code.NotFound && failureCount < 3,
    // Keep the previous version's body on screen across a same-analysis version switch; never
    // carry a body across an analysis switch.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === id ? previous : undefined,
  });
}
