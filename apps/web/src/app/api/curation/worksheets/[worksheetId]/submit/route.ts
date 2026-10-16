import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  optionalString,
  runCuration,
} from "@/curation/http";
import { ClientInputError } from "@/server/errors";

/** POST /api/curation/worksheets/[worksheetId]/submit — commit the drafts of the workflows the
 *  worksheet shows, named in `workflowIds`, as one submission; the worksheet's two sections, the
 *  routing and the verdict, always go with them.
 *
 *  Transactional in the store: either every one of those drafts lands under the new submission or
 *  none does. Refused where none of them holds an answer. */
export async function POST(
  request: Request,
  ctx: { params: Promise<{ worksheetId: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const { worksheetId } = await ctx.params;
    const body = await jsonBody(request);
    const workflowIds = body.workflowIds;
    if (
      !Array.isArray(workflowIds) ||
      !workflowIds.every((id) => typeof id === "string")
    ) {
      throw new ClientInputError(
        "workflowIds, the workflows the worksheet shows, is required as a list of ids",
      );
    }
    const submission = await access.submit(
      worksheetId,
      optionalString(body, "note"),
      workflowIds,
    );
    return NextResponse.json({
      id: submission.id,
      submittedAt: submission.submittedAt.toISOString(),
    });
  });
}
