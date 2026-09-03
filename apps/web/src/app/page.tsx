import { headers } from "next/headers";
import { AppBar } from "@/components/app-bar";
import { CurationPanel } from "@/curation/ui/panel";
import { userContext } from "@/server/context";
import { ProjectsPanel } from "@/workbench/projects-panel";

// The landing page composes the surfaces a caller has, and owns none of them. Each panel resolves
// its own caller and its own data, and each decides for itself whether it has anything to show — so
// neither module imports the other, and adding or removing one is a line here.

export const dynamic = "force-dynamic";

export default async function LandingPage() {
  const { userEmail } = await userContext(await headers());
  return (
    <div className="flex h-svh flex-col overflow-hidden bg-surface-warm-panel">
      <AppBar userEmail={userEmail} />
      <main className="tscroll flex-1 overflow-auto px-[56px] py-[40px]">
        <div className="mx-auto flex max-w-[1100px] flex-col gap-[24px]">
          <ProjectsPanel />
          <CurationPanel />
        </div>
      </main>
    </div>
  );
}
