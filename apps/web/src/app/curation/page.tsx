import { headers } from "next/headers";
import Link from "next/link";
import { curationContext } from "@/curation/http";
import { WorksheetList } from "@/curation/ui/worksheet-list";

export const dynamic = "force-dynamic";

/** The curator's own worksheets. Nobody else's appear here, whatever the caller's role. */
export default async function CurationHome() {
  const access = await curationContext(
    new Request("http://internal/curation", { headers: await headers() }),
  );
  const worksheets = await access.myWorksheets();
  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <header className="mb-8 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="framework-voice font-medium text-[22px] text-ink-primary">
            Curation
          </h1>
          <p className="framework-voice mt-1 text-[13.5px] text-ink-muted">
            SVCv4 worksheets assigned to you.
          </p>
        </div>
        {access.isManager ? (
          <Link
            href="/curation/manage"
            className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body hover:border-ink-ghost"
          >
            Manage variants
          </Link>
        ) : null}
      </header>
      <WorksheetList
        rows={worksheets.map(
          ({ worksheet, variant, draftCount, latestSubmission }) => ({
            worksheetId: worksheet.id,
            gene: variant.gene,
            transcript: variant.transcript,
            hgvsC: variant.hgvsC,
            diseaseLabel: variant.diseaseLabel,
            draftCount,
            submittedAt: latestSubmission?.submittedAt.toISOString() ?? null,
          }),
        )}
      />
    </main>
  );
}
