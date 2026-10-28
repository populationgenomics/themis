import Link from "next/link";
import { curationPageAccess } from "@/curation/page-access";
import { WorksheetList } from "@/curation/ui/worksheet-list";
import { curationPath } from "@/lib/routes";

export const dynamic = "force-dynamic";

/** The caller's own worksheets in the Project. Nobody else's appear here, whatever the caller's
 *  roles. */
export default async function CurationHome({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const access = await curationPageAccess(projectId);
  const worksheets = await access.myWorksheets();
  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <header className="mb-8 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="framework-voice font-medium text-[22px] text-ink-primary">
            Curation in {projectId}
          </h1>
          <p className="framework-voice mt-1 text-[13.5px] text-ink-muted">
            {access.isCuratorHere
              ? "SVCv4 worksheets assigned to you."
              : "You manage this Project's curation; worksheets go to its curators."}
          </p>
        </div>
        {access.isManager ? (
          <Link
            href={curationPath(projectId, "manage")}
            className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body hover:border-ink-ghost"
          >
            Manage variants
          </Link>
        ) : null}
      </header>
      <WorksheetList
        projectId={projectId}
        rows={worksheets.map(
          ({ worksheet, variant, draftCount, latestSubmission }) => ({
            worksheetId: worksheet.id,
            gene: variant.gene,
            transcript: variant.transcript,
            hgvsC: variant.hgvsC,
            diseaseLabel: variant.diseaseLabel,
            modeOfInheritance: variant.modeOfInheritance,
            draftCount,
            submittedAt: latestSubmission?.submittedAt.toISOString() ?? null,
          }),
        )}
      />
    </main>
  );
}
