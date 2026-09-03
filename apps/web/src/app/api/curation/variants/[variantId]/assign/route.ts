import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  requiredString,
  runCuration,
} from "@/curation/http";
import { WORKFLOWS_VERSION } from "@/curation/version";

/** POST /api/curation/variants/[variantId]/assign — assign a curator, minting their worksheet.
 *
 *  The worksheet pins the transcription version current at assignment; a later correction does not
 *  move a worksheet already in flight. */
export async function POST(
  request: Request,
  ctx: { params: Promise<{ variantId: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const { variantId } = await ctx.params;
    const body = await jsonBody(request);
    const worksheet = await access.assign(
      variantId,
      requiredString(body, "curatorEmail"),
      WORKFLOWS_VERSION,
    );
    return NextResponse.json({ id: worksheet.id });
  });
}
