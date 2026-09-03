import { headers } from "next/headers";
import Link from "next/link";
import { curationContext } from "@/curation/http";
import { AssignPanel, NewVariantForm } from "@/curation/ui/manage";

export const dynamic = "force-dynamic";

/** The manager's view: the variants under curation, who is on each, and how far they have got.
 *  Counts and submission times only — never another curator's answers. */
export default async function ManagePage() {
  const access = await curationContext(
    new Request("http://internal/curation", { headers: await headers() }),
  );
  const variants = await access.listVariants();
  const people = await access.listPeople();
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
            Variants under curation
          </h1>
          <p className="framework-voice mt-1 text-[13.5px] text-ink-muted">
            Assign curators. A variant curated by two people gives a reading of
            how much analyst-to-analyst spread is normal.
          </p>
        </div>
        <Link
          href="/curation"
          className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body hover:border-ink-ghost"
        >
          My worksheets
        </Link>
      </header>
      <div className="mb-5">
        <NewVariantForm />
      </div>
      <AssignPanel
        curators={people.map((p) => p.email)}
        variants={progress.map(({ variant, rows }) => ({
          id: variant.id,
          gene: variant.gene,
          transcript: variant.transcript,
          hgvsC: variant.hgvsC,
          diseaseLabel: variant.diseaseLabel,
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
