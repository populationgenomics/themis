"use client";

import {
  type UseQueryResult,
  useMutation,
  useQuery,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Analysis,
  DocumentResponse,
  PollResponse,
  Project,
} from "@/models/workbench";

// TanStack Query wiring over the typed client (`@/lib/api`). The poll drives the
// workbench: one ~2.5s tick returns the FULL projected event list each time
// (replace-by-id, never append), plus the working-document version signal. The
// document refetches only when that version changes.

const POLL_INTERVAL_MS = 2500;

/** The Projects the user belongs to — the app-bar's Project selector and the scope
 *  for create/list. */
export function useProjects(): UseQueryResult<Project[]> {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const { projects } = await api.listProjects();
      return projects;
    },
  });
}

export function useCreateAnalysis() {
  return useMutation({
    mutationFn: (input: { prompt: string; projectId: string }) =>
      api.createAnalysis(input),
  });
}

// The invalidation prefix; the per-Project key is `["analyses", projectId]`, which
// this matches so a create refreshes the active Project's list.
export const ANALYSES_QUERY_KEY = ["analyses"] as const;

/** The active Project's analyses for the session switcher, newest first. Disabled
 *  until a Project is selected. */
export function useAnalyses(
  projectId: string | null,
): UseQueryResult<Analysis[]> {
  return useQuery({
    queryKey: ["analyses", projectId],
    queryFn: async () => {
      if (projectId === null) {
        throw new Error("useAnalyses query ran with a null project id");
      }
      const { analyses } = await api.listAnalyses(projectId);
      return analyses;
    },
    enabled: projectId !== null,
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
      return api.pollAnalysis(id);
    },
    enabled: id !== null,
    refetchInterval: POLL_INTERVAL_MS,
  });
}

/** The working-document body authority. Keyed on the poll's version signal so it
 *  refetches only when a new version is produced; disabled until then. */
export function useDocument(
  id: string | null,
  version: number | null,
): UseQueryResult<DocumentResponse> {
  return useQuery({
    queryKey: ["document", id, version],
    queryFn: () => {
      if (id === null) {
        throw new Error("useDocument query ran with a null analysis id");
      }
      return api.getDocument(id);
    },
    enabled: id !== null && version !== null,
  });
}
