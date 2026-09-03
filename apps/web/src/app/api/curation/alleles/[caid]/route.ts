import { NextResponse } from "next/server";
import { curationContext, runCuration } from "@/curation/http";

/** GET /api/curation/alleles/:caid — the identity the ClinGen Allele Registry holds for an allele id,
 *  for the registration form to fill itself from. Manager only. */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ caid: string }> },
): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const { caid } = await params;
    return NextResponse.json(await access.resolveAllele(caid));
  });
}
