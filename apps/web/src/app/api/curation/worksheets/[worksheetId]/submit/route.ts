import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  optionalString,
  runCuration,
} from "@/curation/http";

/** POST /api/curation/worksheets/[worksheetId]/submit — commit every draft as one submission.
 *
 *  Transactional in the store: either every workflow's draft lands under the new submission or none
 *  does. Refused where the worksheet holds no answers. */
export async function POST(
  request: Request,
  ctx: { params: Promise<{ worksheetId: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const { worksheetId } = await ctx.params;
    const body = await jsonBody(request);
    const submission = await access.submit(
      worksheetId,
      optionalString(body, "note"),
    );
    return NextResponse.json({
      id: submission.id,
      submittedAt: submission.submittedAt.toISOString(),
    });
  });
}
