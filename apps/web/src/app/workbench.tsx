"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { AnalysisBrowser } from "@/components/analysis-browser";
import { AppBar } from "@/components/app-bar";
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

const ANALYSIS_PARAM = "analysis";

export function Workbench({ userEmail }: { userEmail: string }) {
  const searchParams = useSearchParams();
  const analysisId = searchParams.get(ANALYSIS_PARAM);
  const queryClient = useQueryClient();

  const [prompt, setPrompt] = useState("");
  const projects = useProjects();
  const activeProject = projects.data?.[0] ?? null;
  const create = useCreateAnalysis();
  const analyses = useAnalyses(activeProject?.id ?? null);
  const poll = usePoll(analysisId);
  const workingDocumentVersion = poll.data?.workingDocumentVersion ?? null;
  const doc = useDocument(analysisId, workingDocumentVersion);

  function selectAnalysis(id: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set(ANALYSIS_PARAM, id);
    window.history.pushState(null, "", `?${params.toString()}`);
  }

  function createAnalysis() {
    const text = prompt.trim();
    if (!text || !activeProject) return;
    create.mutate(
      { prompt: text, projectId: activeProject.id },
      {
        onSuccess: (res) => {
          queryClient.invalidateQueries({ queryKey: ANALYSES_QUERY_KEY });
          setPrompt("");
          selectAnalysis(res.id);
        },
      },
    );
  }

  return (
    <div className="flex h-svh flex-col overflow-hidden">
      <AppBar userEmail={userEmail} projectName={activeProject?.name ?? "…"} />
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
                Could not create the analysis: {create.error.message}
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
