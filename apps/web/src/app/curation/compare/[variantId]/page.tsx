import { headers } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { isCurationAccessError } from "@/curation/access";
import { curationContext } from "@/curation/http";
import {
  AssessmentStatus,
  type WorkflowAssessment,
} from "@/gen/themis/curation/models/curation_pb";
import { isResourceNotFoundError } from "@/server/errors";

export const dynamic = "force-dynamic";

// Where two curators' submitted answers are read against each other.
//
// The comparison is per workflow rather than per total, because the measurement is which row each
// curator chose and why. Two people reaching the same code by different rows have not agreed, and
// only the cell shows it; two reaching different rows with the same reasoning is a framework
// finding rather than a mistake, and only the rationale shows that.
//
// `access.comparison` refuses this page to a manager assigned to the variant, and refuses it until
// two curators have submitted — one submitted answer is not a comparison, and serving it would let a
// manager read a colleague's reasoning under the name of one.
//
// Everything rendered comes from the stored assessment, never from today's transcription. A
// worksheet pins the `workflows_version` it was answered against, so labelling an old answer with
// current wording would report a question the curator was never asked.

export default async function ComparePage({
  params,
}: {
  params: Promise<{ variantId: string }>;
}) {
  const { variantId } = await params;
  const access = await curationContext(
    new Request("http://internal/curation", { headers: await headers() }),
  );

  let answers: Awaited<ReturnType<typeof access.comparison>>;
  try {
    answers = await access.comparison(variantId);
  } catch (error) {
    if (isResourceNotFoundError(error)) notFound();
    if (isCurationAccessError(error)) {
      return (
        <main className="mx-auto max-w-3xl px-6 py-12">
          <p className="framework-voice rounded-md border border-line-primary bg-white px-5 py-8 text-[13.5px] text-ink-muted">
            {error.message}
          </p>
          <Link
            href="/curation/manage"
            className="framework-voice mt-4 inline-block text-[13px] text-ink-faint hover:text-ink-body"
          >
            ← Variants under curation
          </Link>
        </main>
      );
    }
    throw error;
  }

  const variant = await access.listVariants();
  const subject = variant.find((v) => v.id === variantId);

  // Every workflow either curator answered, in registry order so the two columns line up.
  const workflowIds = [
    ...new Set(answers.flatMap((a) => a.entries.map((e) => e.workflowId))),
  ].sort();

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-8">
        <Link
          href="/curation/manage"
          className="framework-voice mb-2 inline-block text-[12.5px] text-ink-faint hover:text-ink-body"
        >
          ← Variants under curation
        </Link>
        <h1 className="framework-voice font-medium text-[22px] text-ink-primary">
          Where the curators diverge
        </h1>
        {subject ? (
          <p className="mt-1 font-mono text-[13px] text-ink-muted">
            {subject.transcript}:{subject.hgvsC} · {subject.gene} ·{" "}
            {subject.diseaseLabel}
          </p>
        ) : null}
        <p className="framework-voice mt-2 text-[13px] text-ink-muted">
          A disagreement inside the framework's own analyst spread is data about
          the framework, not a defect. Read the reasoning, not the count.
        </p>
      </header>

      <div className="space-y-4">
        {workflowIds.map((workflowId) => {
          const perCurator = answers.map((answer) => {
            const entry = answer.entries.find(
              (e) => e.workflowId === workflowId,
            );
            return {
              curator: answer.worksheet.curatorEmail,
              assessment:
                entry?.assessment.kind.case === "workflow"
                  ? entry.assessment.kind.value
                  : undefined,
            };
          });
          const cells = perCurator.map((p) =>
            (p.assessment?.fields ?? [])
              .map((f) => f.cellId)
              .sort()
              .join("|"),
          );
          const agreed = new Set(cells).size === 1;
          return (
            <section
              key={workflowId}
              className={`rounded-md border bg-white p-5 ${agreed ? "border-line-primary" : "border-amber-uncertainty-border"}`}
            >
              <div className="mb-3 flex items-baseline justify-between gap-3 border-line-row border-b pb-2">
                <span>
                  <span className="font-mono text-[12px] text-ink-faint">
                    {codeOf(perCurator) ?? workflowId}
                  </span>
                  <span className="framework-voice ml-2 text-[14px] text-ink-primary">
                    {workflowId}
                  </span>
                </span>
                <span
                  className={`framework-voice text-[12.5px] ${agreed ? "text-ink-faint" : "text-amber-uncertainty-heading"}`}
                >
                  {agreed ? "same cells" : "different cells"}
                </span>
              </div>
              <div className="grid gap-5 md:grid-cols-2">
                {perCurator.map(({ curator, assessment }) => (
                  <CuratorAnswer
                    key={curator}
                    curator={curator}
                    assessment={assessment}
                  />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </main>
  );
}

/** The evidence code, read off a stored cell id (`CLN_AFF.ad.specific_full`) rather than looked up:
 *  the answer names its own framework address, and that survives a change to the components. */
function codeOf(
  perCurator: { assessment?: WorkflowAssessment }[],
): string | undefined {
  for (const { assessment } of perCurator) {
    const cell = assessment?.fields.find((f) => f.cellId !== "")?.cellId;
    if (cell) return cell.split(".")[0];
  }
  return undefined;
}

function CuratorAnswer({
  curator,
  assessment,
}: {
  curator: string;
  assessment?: WorkflowAssessment;
}) {
  const status =
    assessment?.status === AssessmentStatus.SCORED
      ? "scored"
      : assessment?.status === AssessmentStatus.NOT_APPLICABLE
        ? "not applicable"
        : assessment?.status === AssessmentStatus.NO_DATA
          ? "no data"
          : "not answered";
  return (
    <div>
      <p className="framework-voice mb-1 text-[12.5px] text-ink-faint">
        {curator} · {status}
      </p>
      {assessment ? (
        <>
          <ul className="mb-2 space-y-0.5">
            {assessment.fields.map((field) => (
              <li
                key={field.fieldId}
                className="framework-voice text-[13px] text-ink-body"
              >
                {field.label}
                {field.value ? (
                  <span className="ml-1 font-mono text-ink-muted">
                    {field.value}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          {assessment.rationale ? (
            <p className="curator-voice border-line-row border-l-2 pl-3 text-[15px] text-ink-body">
              {assessment.rationale}
            </p>
          ) : null}
        </>
      ) : (
        <p className="framework-voice text-[13px] text-ink-faintest">
          nothing recorded
        </p>
      )}
    </div>
  );
}
