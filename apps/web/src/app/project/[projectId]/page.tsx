import { timestampDate } from "@bufbuild/protobuf/wkt";
import { headers } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AppBar } from "@/components/app-bar";
import { BackLink } from "@/components/back-link";
import { Eyebrow } from "@/components/eyebrow";
import { ReaderTime } from "@/components/reader-time";
import { absoluteTime, timeAgo } from "@/lib/format";
import { cardContent, requireInputs } from "@/lib/scenario";
import type { Analysis } from "@/models/workbench";
import { userContext } from "@/server/context";
import { Composer } from "./composer";

// One Project: start an Analysis, or open one already run. The scenario an Analysis was created from
// renders its card (docs/design/analysis-scenarios.md); this page places them.

export const dynamic = "force-dynamic";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const { userEmail, backend } = await userContext(await headers());
  // Resolved through the membership rather than by catching the list's not-found, so the page has the
  // Project's name for its heading either way.
  const project = (await backend.listProjects()).find(
    (p) => p.id === projectId,
  );
  if (!project) notFound();
  const analyses = await backend.listAnalyses(projectId);

  return (
    <div className="flex h-svh flex-col overflow-hidden bg-surface-warm-panel">
      <AppBar
        userEmail={userEmail}
        left={
          <>
            <BackLink href="/">Projects</BackLink>
            <span className="flex min-w-0 items-center gap-[9px]">
              <span
                className="size-[8px] shrink-0 rounded-[2px] bg-teal-project-dot"
                aria-hidden
              />
              <span className="truncate text-[13px] font-semibold text-ink-primary">
                {project.name}
              </span>
            </span>
          </>
        }
      />
      <main className="tscroll flex-1 overflow-auto px-[56px] py-[40px]">
        <div className="mx-auto flex max-w-[1100px] flex-col gap-[32px]">
          <Composer projectId={projectId} />

          <section className="flex flex-col gap-[14px]">
            <div className="flex items-baseline gap-[8px]">
              <Eyebrow className="text-[10px]">Recent analyses</Eyebrow>
              <span className="font-mono text-[11px] text-ink-faintest">
                {analyses.length}
              </span>
            </div>
            {analyses.length === 0 ? (
              <p className="rounded-card border border-dashed border-line-dashed bg-white px-[20px] py-[28px] text-[13px] text-ink-faintest">
                No analysis has been run in this Project yet.
              </p>
            ) : (
              <ul className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-[14px]">
                {analyses.map((analysis) => (
                  <li key={analysis.id}>
                    <AnalysisCard analysis={analysis} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

function AnalysisCard({ analysis }: { analysis: Analysis }) {
  // A row without a created_at is malformed, not a card to render blank: the list is ordered by it.
  if (!analysis.createdAt) {
    throw new Error(`analysis has no created_at: ${analysis.id}`);
  }
  const iso = timestampDate(analysis.createdAt).toISOString();
  const { identifier, body } = cardContent(requireInputs(analysis));
  return (
    <Link
      href={`/analysis/${analysis.id}`}
      className="flex h-full flex-col gap-[10px] rounded-card border border-line-primary bg-white px-[18px] py-[16px] hover:border-line-input hover:shadow-[0_1px_3px_rgba(0,0,0,0.05)]"
    >
      {identifier !== null && (
        <span className="truncate font-mono text-[13px] font-medium text-ink-primary">
          {identifier}
        </span>
      )}
      <span
        className={
          identifier === null
            ? "line-clamp-4 text-[13px] leading-[1.55] text-ink-body"
            : "line-clamp-3 text-[12.5px] leading-[1.55] text-ink-muted"
        }
      >
        {body}
      </span>
      <ReaderTime
        className="mt-auto pt-[2px] font-mono text-[10.5px] text-ink-faintest"
        iso={iso}
        pinnedLabel={timeAgo(iso)}
        pinnedTitle={absoluteTime(iso)}
      />
    </Link>
  );
}
