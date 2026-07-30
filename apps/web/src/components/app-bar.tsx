"use client";

import { ChevronDown } from "lucide-react";
import { Eyebrow } from "@/components/eyebrow";
import { Logo } from "@/components/logo";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";
import type { Project } from "@/models/workbench";

/** The membership as the app bar needs it: one value per outcome, so none can be read as
 *  another and no two props can disagree about which holds. */
export type ProjectsState =
  | { readonly status: "pending" }
  | { readonly status: "error" }
  | { readonly status: "ready"; readonly projects: Project[] };

/** What the trigger calls the current Project: one string per outcome.
 *
 *  The ellipsis means "the membership has not answered" and nothing else. Reusing it for
 *  an answered "none" leaves a caller who belongs to no Project watching what reads as a
 *  spinner; reusing it for a failed query hides the failure behind one for good, since a
 *  query that has stopped retrying never resolves. */
export function projectName(
  state: ProjectsState,
  activeProject: Project | null,
): string {
  if (state.status === "pending") return "…";
  if (state.status === "error") return "Unavailable";
  return activeProject?.name ?? "No Project";
}

// The shared chrome (design-spec §2.0): logo + wordmark, a divider, the Project
// selector, and the verified caller.
export function AppBar({
  userEmail,
  projects,
  activeProject,
  onSelectProject,
}: {
  userEmail: string;
  projects: ProjectsState;
  activeProject: Project | null;
  onSelectProject: (id: string) => void;
}) {
  const listed = projects.status === "ready" ? projects.projects : [];
  const items: MenuItem[] = listed.map((project) => ({
    key: project.id,
    label: <ProjectLabel project={project} />,
    selected: project.id === activeProject?.id,
    onSelect: () => onSelectProject(project.id),
  }));
  const name = projectName(projects, activeProject);

  return (
    <header className="flex h-[54px] shrink-0 items-center justify-between border-b border-line-primary bg-white px-[22px]">
      <div className="flex items-center gap-[16px]">
        <div className="flex items-center gap-[10px]">
          <Logo className="h-[30px] w-auto" />
          <span className="text-[16px] font-semibold tracking-[-0.01em] text-ink-primary">
            Themis
          </span>
        </div>
        <span className="h-[24px] w-px bg-line-primary" aria-hidden />
        <DropdownMenu
          items={items}
          // Names the button by the state it carries; a static label would replace the
          // computed name and leave the active Project unannounced.
          ariaLabel={`Project: ${name}`}
          emptyLabel={
            <div className="px-[14px] py-[16px] text-[12.5px] text-ink-faintest">
              {emptyMessage(projects)}
            </div>
          }
          triggerClassName="flex h-[38px] items-center gap-[11px] rounded-field border border-line-primary bg-white px-[13px]"
          menuClassName="tscroll mt-[6px] max-h-[320px] w-[280px] overflow-auto rounded-card"
          itemClassName="px-[14px] py-[10px]"
        >
          <span
            className="size-[8px] rounded-[2px] bg-teal-project-dot"
            aria-hidden
          />
          <span className="flex flex-col items-start leading-[1.1]">
            <Eyebrow className="text-[9.5px] tracking-[0.14em]">
              PROJECT
            </Eyebrow>
            <span className="text-[13px] font-semibold text-ink-primary">
              {name}
            </span>
          </span>
          <ChevronDown
            className="ml-[2px] size-[11px] text-ink-faintest"
            aria-hidden
          />
        </DropdownMenu>
      </div>

      <div className="flex items-center gap-[9px] pl-[4px]">
        <span className="max-w-[280px] truncate font-mono text-[12px] text-ink-muted">
          {userEmail}
        </span>
        <ChevronDown className="size-[11px] text-ink-faintest" aria-hidden />
      </div>
    </header>
  );
}

/** What an open menu with no rows says. Never a membership claim the query has not answered. */
function emptyMessage(state: ProjectsState): string {
  if (state.status === "pending") return "Loading Projects…";
  if (state.status === "error") return "Could not load your Projects.";
  return "You are not a member of any Project.";
}

function ProjectLabel({ project }: { project: Project }) {
  return (
    <span className="flex flex-col gap-[4px]">
      <span className="truncate text-[13px] font-medium text-ink-body">
        {project.name}
      </span>
      <span className="truncate font-mono text-[10.5px] text-ink-faintest">
        {project.id}
      </span>
    </span>
  );
}
