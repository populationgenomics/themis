import { timestampDate } from "@bufbuild/protobuf/wkt";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { absoluteTime, timeAgo } from "@/lib/format";
import {
  analysisDetail,
  analysisTitle,
  requireInputs,
  scenarioLabel,
} from "@/lib/scenario";
import { userContext } from "@/server/context";
import { isResourceNotFoundError } from "@/server/errors";
import { Workbench } from "./workbench";

// One Analysis, and nothing that switches away from it. The Analysis resolves here rather than in the
// browser: its scenario inputs and owning Project are fixed for the life of the page, and an id the
// caller cannot reach is a 404 before render instead of a workbench polling a dead id.

export const dynamic = "force-dynamic";

export default async function AnalysisPage({
  params,
}: {
  params: Promise<{ analysisId: string }>;
}) {
  const { analysisId } = await params;
  const { userEmail, backend } = await userContext(await headers());

  const analysis = await backend.getAnalysis(analysisId).catch((e: unknown) => {
    if (isResourceNotFoundError(e)) notFound();
    throw e;
  });
  if (!analysis.createdAt) {
    throw new Error(`analysis has no created_at: ${analysis.id}`);
  }
  const inputs = requireInputs(analysis);
  const project = (await backend.listProjects()).find(
    (p) => p.id === analysis.projectId,
  );
  // The read above authorized against this membership, so a Project missing from it is inconsistent
  // state, not a caller error.
  if (!project) {
    throw new Error(
      `analysis ${analysis.id} authorized against unlisted project ${analysis.projectId}`,
    );
  }

  return (
    // Keyed on the Analysis: the App Router reconciles this subtree by position across a param
    // change, so without it a switch would carry one Analysis's panes, tabs and channel into the
    // next, and the unmount teardown would never run.
    <Workbench
      key={analysis.id}
      userEmail={userEmail}
      analysis={{
        id: analysis.id,
        title: analysisTitle(inputs),
        detail: analysisDetail(inputs),
        scenario: scenarioLabel(inputs),
        created: created(timestampDate(analysis.createdAt).toISOString()),
        projectId: project.id,
        projectName: project.name,
      }}
    />
  );
}

/** The created time the chrome shows: the instant, plus this render of it. The chrome reformats it on
 *  the reader's clock once mounted (`components/reader-time.tsx`); these are what the markup carries
 *  until then. */
function created(iso: string): {
  iso: string;
  pinnedLabel: string;
  pinnedTitle: string;
} {
  return { iso, pinnedLabel: timeAgo(iso), pinnedTitle: absoluteTime(iso) };
}
