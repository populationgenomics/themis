import { NextResponse } from "next/server";
import { curationContext, jsonBody, runCuration } from "@/curation/http";
import { parseRegistration } from "@/curation/registration";

/** POST /api/projects/[projectId]/curation/variants — register a variant to be curated in the
 *  Project. Manager only. A second registration of a variant against the same entity in the Project
 *  is a 409. */
export async function POST(
  request: Request,
  ctx: { params: Promise<{ projectId: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const { projectId } = await ctx.params;
    const access = await curationContext(request, projectId);
    const variant = await access.createVariant(
      parseRegistration(await jsonBody(request)),
    );
    return NextResponse.json({ id: variant.id });
  });
}
