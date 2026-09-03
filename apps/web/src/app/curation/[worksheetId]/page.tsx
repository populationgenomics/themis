import { toJson } from "@bufbuild/protobuf";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { curationContext } from "@/curation/http";
import { Worksheet } from "@/curation/ui/worksheet";
import { AssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import { isResourceNotFoundError } from "@/server/errors";

export const dynamic = "force-dynamic";

/** One worksheet — the caller's own. Another curator's is a 404, never a distinguishable refusal. */
export default async function WorksheetPage({
  params,
}: {
  params: Promise<{ worksheetId: string }>;
}) {
  const { worksheetId } = await params;
  const access = await curationContext(
    new Request("http://internal/curation", { headers: await headers() }),
  );
  let detail: Awaited<ReturnType<typeof access.myWorksheet>>;
  try {
    detail = await access.myWorksheet(worksheetId);
  } catch (error) {
    if (isResourceNotFoundError(error)) notFound();
    throw error;
  }
  const drafts: Record<string, unknown> = {};
  for (const entry of detail.drafts) {
    if (entry.assessment.kind.case === undefined) {
      throw new Error(
        `worksheet ${worksheetId} holds a draft for ${entry.workflowId} with no assessment`,
      );
    }
    drafts[entry.workflowId] = toJson(AssessmentSchema, entry.assessment);
  }
  return (
    <Worksheet
      init={{
        worksheetId: detail.worksheet.id,
        workflowsVersion: detail.worksheet.workflowsVersion,
        submissionCount: detail.submissions.length,
        drafts,
        variant: {
          gene: detail.variant.gene,
          transcript: detail.variant.transcript,
          hgvsC: detail.variant.hgvsC,
          diseaseLabel: detail.variant.diseaseLabel,
          mondoId: detail.variant.mondoId,
        },
      }}
    />
  );
}
