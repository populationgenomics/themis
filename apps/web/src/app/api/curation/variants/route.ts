import { NextResponse } from "next/server";
import { curationContext, jsonBody, runCuration } from "@/curation/http";
import { parseRegistration } from "@/curation/registration";

/** POST /api/curation/variants — register a variant to be curated. Manager only. A second
 *  registration of a variant against the same entity is a 409. */
export async function POST(request: Request): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const variant = await access.createVariant(
      parseRegistration(await jsonBody(request)),
    );
    return NextResponse.json({ id: variant.id });
  });
}
