"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { AnalysisBrowser } from "@/components/analysis-browser";
import { AppBar, type ProjectsState } from "@/components/app-bar";
import { Eyebrow } from "@/components/eyebrow";
import { ConversationPane } from "@/components/workbench/conversation-pane";
import { WorkingDocumentPane } from "@/components/workbench/working-document-pane";
import {
  ANALYSES_QUERY_KEY,
  useAnalyses,
  useCreateAnalysis,
  useDocument,
  usePoll,
  useProjects,
} from "@/lib/queries";
import { errorMessage } from "@/lib/rpc";
import type { Project } from "@/models/workbench";

const ANALYSIS_PARAM = "analysis";
const PROJECT_PARAM = "project";

/** The Project the URL names, or the first the caller belongs to.
 *
 *  A `project` the membership does not carry resolves to the first rather than to
 *  nothing: the id reaches here from a hand-edited or shared URL, and every request
 *  it would scope is authorized server-side regardless. The selector then shows which
 *  Project answered, and a URL that named the unreachable one is rewritten to it.
 *  A URL naming no Project is left as it is. */
export function resolveProject(
  projects: Project[] | undefined,
  requestedId: string | null,
): Project | null {
  if (!projects || projects.length === 0) return null;
  return projects.find((p) => p.id === requestedId) ?? projects[0];
}

/** The search params for switching to `id`, or `null` when the selection changes nothing.
 *
 *  Switching drops the open Analysis: one belongs to a single Project, so carrying its id
 *  across would poll an id the new Project does not contain. Re-selecting the Project
 *  already active is not a switch — the menu fires on the ticked row too, and treating that
 *  as one would close the open Analysis and add a history entry to undo. */
export function projectParams(
  current: URLSearchParams,
  id: string,
  activeId: string | null,
): URLSearchParams | null {
  if (id === activeId) return null;
  const params = new URLSearchParams(current);
  params.set(PROJECT_PARAM, id);
  params.delete(ANALYSIS_PARAM);
  return params;
}

/** The membership query as one value per outcome.
 *
 *  A failed query leaves `data` undefined for good once react-query stops retrying, so reading
 *  absence as "still loading" would show a caller a spinner that never resolves. */
function membershipState(
  data: Project[] | undefined,
  isError: boolean,
): ProjectsState {
  if (data !== undefined) return { status: "ready", projects: data };
  return isError ? { status: "error" } : { status: "pending" };
}

export function Workbench({ userEmail }: { userEmail: string }) {
  const searchParams = useSearchParams();
  const analysisId = searchParams.get(ANALYSIS_PARAM);
  const queryClient = useQueryClient();

  const [prompt, setPrompt] = useState("");
  const projects = useProjects();
  const projectsState = membershipState(projects.data, projects.isError);
  const requestedProjectId = searchParams.get(PROJECT_PARAM);
  const activeProject = resolveProject(projects.data, requestedProjectId);

  const create = useCreateAnalysis();
  const analyses = useAnalyses(activeProject?.id ?? null);
  const poll = usePoll(analysisId);
  const workingDocumentVersion = poll.data?.workingDocumentVersion ?? null;
  const doc = useDocument(analysisId, workingDocumentVersion);

  // The Project as of the latest commit, for a callback that outlives the render it closed over.
  const liveProjectId = useRef<string | null>(null);
  useEffect(() => {
    liveProjectId.current = activeProject?.id ?? null;
  }, [activeProject]);

  // A named `project` the membership does not carry is rewritten to the one that answered,
  // so the URL a user shares names the Project they are looking at and `selectAnalysis` stops
  // copying a dead id forward. Corrected through `projectParams`, so this writer drops the open
  // Analysis on the same terms a switch does: the requested Project did not resolve, so an
  // Analysis alongside it cannot be assumed to belong to the one that did.
  //
  // An absent `project` is left alone. It is not a wrong answer to correct, and writing the
  // resolved id in would pair it with whatever `analysis` the link carried — the cross-Project
  // pairing this rule exists to prevent. Replaces rather than pushes: not a navigation to undo.
  useEffect(() => {
    if (requestedProjectId === null || !activeProject) return;
    const params = projectParams(
      searchParams,
      activeProject.id,
      requestedProjectId,
    );
    if (params === null) return;
    window.history.replaceState(null, "", `?${params.toString()}`);
  }, [activeProject, requestedProjectId, searchParams]);

  function selectAnalysis(id: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set(ANALYSIS_PARAM, id);
    window.history.pushState(null, "", `?${params.toString()}`);
  }

  function selectProject(id: string) {
    const params = projectParams(searchParams, id, activeProject?.id ?? null);
    if (params === null) return;
    window.history.pushState(null, "", `?${params.toString()}`);
  }

  function createAnalysis() {
    const text = prompt.trim();
    if (!text || !activeProject) return;
    const createdIn = activeProject.id;
    create.mutate(
      { prompt: text, projectId: createdIn },
      {
        onSuccess: (res) => {
          queryClient.invalidateQueries({ queryKey: ANALYSES_QUERY_KEY });
          setPrompt("");
          // A create is a round trip, and the selector stays live across it. Opening the new
          // Analysis after a switch would drag the caller back to the Project it belongs to.
          if (liveProjectId.current !== createdIn) return;
          selectAnalysis(res.id);
        },
      },
    );
  }

  return (
    <div className="flex h-svh flex-col overflow-hidden">
      <AppBar
        userEmail={userEmail}
        projects={projectsState}
        activeProject={activeProject}
        onSelectProject={selectProject}
      />
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-line-primary bg-white px-[56px] py-[16px]">
          <div className="mx-auto flex max-w-[1330px] flex-col gap-[9px]">
            <div className="flex items-center justify-between">
              <Eyebrow className="text-[10px]">New analysis</Eyebrow>
              <AnalysisBrowser
                analyses={analyses.data ?? []}
                currentId={analysisId}
                onSelect={selectAnalysis}
              />
            </div>
            <div className="flex items-end gap-[12px]">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={2}
                placeholder="Describe the task to run…"
                aria-label="Analysis prompt"
                className="tscroll min-h-0 flex-1 resize-none rounded-card border border-line-input bg-white px-[14px] py-[10px] text-[13.5px] leading-[1.55] text-ink-body outline-none placeholder:text-ink-faintest focus:shadow-focus-ring"
              />
              <button
                type="button"
                onClick={createAnalysis}
                disabled={
                  prompt.trim().length === 0 ||
                  create.isPending ||
                  activeProject === null
                }
                className="flex h-[40px] shrink-0 items-center gap-[7px] rounded-field bg-primary px-[18px] text-[13.5px] font-semibold text-primary-foreground shadow-[0_1px_2px_rgba(0,0,0,0.06)] disabled:opacity-50"
              >
                <ArrowUp
                  className="size-[16px]"
                  strokeWidth={2.4}
                  aria-hidden
                />
                Create
              </button>
            </div>
            {create.isError && (
              <p role="alert" className="text-[12.5px] text-error-text">
                Could not create the analysis: {errorMessage(create.error)}
              </p>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-1">
          <ConversationPane events={poll.data?.events ?? []} />
          <WorkingDocumentPane document={doc.data?.document ?? null} />
        </div>
      </div>
    </div>
  );
}
