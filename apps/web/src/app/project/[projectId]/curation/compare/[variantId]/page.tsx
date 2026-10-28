import { toJson } from "@bufbuild/protobuf";
import Link from "next/link";
import { notFound } from "next/navigation";
import { isCurationAccessError } from "@/curation/access";
import { inlineMode } from "@/curation/mode";
import { curationPageAccess } from "@/curation/page-access";
import { Comparison, type CuratorAnswers } from "@/curation/ui/comparison";
import { AssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import { curationPath } from "@/lib/routes";
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
// The wording of what was asked comes only from the stored labels, never from today's transcription.
// A worksheet pins the `workflows_version` it was answered against, so labelling an old answer with
// current wording would report a question the curator was never asked. A workflow's name comes from
// the registry: a workflow id is never reused for another workflow, so the registry's code and title
// name the same workflow whatever version a worksheet pinned.

export default async function ComparePage({
  params,
}: {
  params: Promise<{ projectId: string; variantId: string }>;
}) {
  const { projectId, variantId } = await params;
  const access = await curationPageAccess(projectId);

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
            href={curationPath(projectId, "manage")}
            className="framework-voice mt-4 inline-block text-[13px] text-ink-faint hover:text-ink-body"
          >
            ← Variants under curation
          </Link>
        </main>
      );
    }
    throw error;
  }

  // `comparison` has found the variant in this Project, so this read finds it too.
  const subject = await access.getVariant(variantId);

  const submitted: CuratorAnswers[] = answers.map((answer) => ({
    curator: answer.worksheet.curatorEmail,
    workflowsVersion: answer.worksheet.workflowsVersion,
    entries: Object.fromEntries(
      answer.entries.map((e) => [
        e.workflowId,
        toJson(AssessmentSchema, e.assessment),
      ]),
    ),
  }));

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-8">
        <Link
          href={curationPath(projectId, "manage")}
          className="framework-voice mb-2 inline-block text-[12.5px] text-ink-faint hover:text-ink-body"
        >
          ← Variants under curation
        </Link>
        <h1 className="framework-voice font-medium text-[22px] text-ink-primary">
          Where the curators diverge
        </h1>
        <p className="mt-1 font-mono text-[13px] text-ink-muted">
          {subject.transcript}:{subject.hgvsC} · {subject.gene} ·{" "}
          {subject.diseaseLabel} · {inlineMode(subject.modeOfInheritance)}
        </p>
        <p className="framework-voice mt-2 text-[13px] text-ink-muted">
          A disagreement inside the framework's own analyst spread is data about
          the framework, not a defect. Read the reasoning, not the count.
        </p>
      </header>

      <Comparison
        clinicalContext={subject.clinicalContext}
        answers={submitted}
      />
    </main>
  );
}
