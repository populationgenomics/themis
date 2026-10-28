import Link from "next/link";
import { curationPageAccess } from "@/curation/page-access";
import { AssignPanel, NewVariantForm } from "@/curation/ui/manage";
import { curationPath } from "@/lib/routes";

export const dynamic = "force-dynamic";

/** The manager's view of the Project: the variants under curation, who is on each, and how far they
 *  have got. Counts and submission times only — never another curator's answers. */
export default async function ManagePage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const access = await curationPageAccess(projectId);
  if (!access.isManager) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-12">
        <p className="framework-voice rounded-md border border-line-primary bg-white px-5 py-8 text-[13.5px] text-ink-muted">
          You curate in {projectId}; managing its variants needs the manager
          role, which the site's access list grants.
        </p>
        <Link
          href={curationPath(projectId)}
          className="framework-voice mt-4 inline-block text-[13px] text-ink-faint hover:text-ink-body"
        >
          ← My worksheets
        </Link>
      </main>
    );
  }
  const variants = await access.listVariants();
  const progress = await Promise.all(
    variants.map(async (variant) => ({
      variant,
      rows: await access.progress(variant.id),
    })),
  );
  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-8 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="framework-voice font-medium text-[22px] text-ink-primary">
            Variants under curation in {projectId}
          </h1>
          <p className="framework-voice mt-1 text-[13.5px] text-ink-muted">
            Assign curators. A variant curated by two people gives a reading of
            how much analyst-to-analyst spread is normal.
          </p>
        </div>
        <Link
          href={curationPath(projectId)}
          className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body hover:border-ink-ghost"
        >
          My worksheets
        </Link>
      </header>
      <div className="mb-5">
        <NewVariantForm projectId={projectId} />
      </div>
      <AssignPanel
        projectId={projectId}
        variants={progress.map(({ variant, rows }) => ({
          id: variant.id,
          gene: variant.gene,
          transcript: variant.transcript,
          hgvsC: variant.hgvsC,
          diseaseLabel: variant.diseaseLabel,
          mondoId: variant.mondoId,
          modeOfInheritance: variant.modeOfInheritance,
          clinicalContext: variant.clinicalContext,
          rows: rows.map((row) => ({
            curatorEmail: row.worksheet.curatorEmail,
            draftCount: row.draftCount,
            submittedAt:
              row.latestSubmission?.submittedAt.toISOString() ?? null,
          })),
        }))}
      />
    </main>
  );
}
