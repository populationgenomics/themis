import { fromJson } from "@bufbuild/protobuf";
import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  requiredString,
  runCuration,
} from "@/curation/http";
import {
  type Assessment,
  AssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { ClientInputError } from "@/server/errors";

/** PUT /api/curation/worksheets/[worksheetId]/draft — upsert one workflow's draft.
 *
 *  The auto-save write. Scoped to the caller's own worksheet by `saveDraft`; another curator's is
 *  not-found, not forbidden. */
export async function PUT(
  request: Request,
  ctx: { params: Promise<{ worksheetId: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const { worksheetId } = await ctx.params;
    const body = await jsonBody(request);
    const workflowId = requiredString(body, "workflowId");
    let assessment: Assessment;
    try {
      assessment = fromJson(AssessmentSchema, body.assessment as never);
    } catch (error) {
      throw new ClientInputError(
        `assessment is not a valid Assessment: ${(error as Error).message}`,
      );
    }
    await access.saveDraft(worksheetId, { workflowId, assessment });
    return NextResponse.json({ saved: true });
  });
}
