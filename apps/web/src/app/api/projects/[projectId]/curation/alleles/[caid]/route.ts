import { NextResponse } from "next/server";
import { curationContext, runCuration } from "@/curation/http";

/** GET /api/projects/[projectId]/curation/alleles/:caid — the identity the ClinGen Allele Registry
 *  holds for an allele id, for the registration form to fill itself from. Manager only. */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ projectId: string; caid: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const { projectId, caid } = await params;
    const access = await curationContext(request, projectId);
    return NextResponse.json(await access.resolveAllele(caid));
  });
}
