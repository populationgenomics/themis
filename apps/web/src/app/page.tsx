import { timestampDate } from "@bufbuild/protobuf/wkt";
import { headers } from "next/headers";
import Link from "next/link";
import { AppBar } from "@/components/app-bar";
import { ReaderTime } from "@/components/reader-time";
import { absoluteTime, timeAgo } from "@/lib/format";
import type { Analysis, Project } from "@/models/workbench";
import { userContext } from "@/server/context";

// The landing page: the Projects the caller belongs to. A Project is the access boundary an Analysis
// is bound to, so it is the level a curator navigates first. Counts and the latest Analysis date come
// from one read over the whole membership, not one per Project.

export const dynamic = "force-dynamic";

export interface ProjectRow {
  project: Project;
  analysisCount: number;
  latestIso: string | null;
}

/** Each Project with what the card shows: how many Analyses it holds and when the most recent
 *  arrived. Analyses outside the listed Projects cannot occur — both come from the same membership. */
export function projectRows(
  projects: Project[],
  analyses: Analysis[],
): ProjectRow[] {
  return projects.map((project) => {
    const own = analyses.filter((a) => a.projectId === project.id);
    const latest = own
      .map((a) => {
        // Folding an absent timestamp into the max would date the Project by epoch 0 and read as
        // "no analyses yet" — a wrong card rather than a fault. The list is ordered by this column.
        if (!a.createdAt)
          throw new Error(`analysis has no created_at: ${a.id}`);
        return timestampDate(a.createdAt).getTime();
      })
      .reduce((a, b) => Math.max(a, b), 0);
    return {
      project,
      analysisCount: own.length,
      latestIso: latest === 0 ? null : new Date(latest).toISOString(),
    };
  });
}

export default async function ProjectsPage() {
  const { userEmail, backend } = await userContext(await headers());
  const [projects, analyses] = await Promise.all([
    backend.listProjects(),
    backend.listAllAnalyses(),
  ]);
  const rows = projectRows(projects, analyses);

  return (
    <div className="flex h-svh flex-col overflow-hidden bg-surface-warm-panel">
      <AppBar userEmail={userEmail} />
      <main className="tscroll flex-1 overflow-auto px-[56px] py-[40px]">
        <div className="mx-auto flex max-w-[1100px] flex-col gap-[24px]">
          <div className="flex flex-col gap-[6px]">
            <h1 className="text-[22px] font-semibold tracking-[-0.01em] text-ink-primary">
              Your Projects
            </h1>
            <p className="text-[13px] text-ink-muted">
              A Project holds the datasets and people an Analysis is scoped to.
              Open one to see its analyses, or start a new one.
            </p>
          </div>

          {rows.length === 0 ? (
            <p className="rounded-card border border-dashed border-line-dashed bg-white px-[20px] py-[28px] text-[13px] text-ink-faintest">
              You are not a member of any Project.
            </p>
          ) : (
            <ul className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-[14px]">
              {rows.map((row) => (
                <li key={row.project.id}>
                  <ProjectCard row={row} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}

function ProjectCard({ row }: { row: ProjectRow }) {
  const { project, analysisCount, latestIso } = row;
  return (
    <Link
      href={`/project/${project.id}`}
      className="flex h-full flex-col gap-[12px] rounded-card border border-line-primary bg-white px-[18px] py-[16px] hover:border-line-input hover:shadow-[0_1px_3px_rgba(0,0,0,0.05)]"
    >
      <span className="flex items-center gap-[9px]">
        <span
          className="size-[8px] shrink-0 rounded-[2px] bg-teal-project-dot"
          aria-hidden
        />
        <span className="truncate text-[15px] font-semibold text-ink-primary">
          {project.name}
        </span>
      </span>
      <span className="truncate font-mono text-[10.5px] text-ink-faintest">
        {project.id}
      </span>
      <span className="mt-auto flex items-center gap-[8px] text-[12px] text-ink-muted">
        <span>
          {analysisCount} {analysisCount === 1 ? "analysis" : "analyses"}
        </span>
        {latestIso !== null && (
          <>
            <span
              className="size-[3px] rounded-full bg-separator-dot"
              aria-hidden
            />
            <span>
              latest{" "}
              <ReaderTime
                iso={latestIso}
                pinnedLabel={timeAgo(latestIso)}
                pinnedTitle={absoluteTime(latestIso)}
              />
            </span>
          </>
        )}
      </span>
    </Link>
  );
}
