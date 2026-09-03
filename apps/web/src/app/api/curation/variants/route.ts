import { NextResponse } from "next/server";
import {
  curationContext,
  jsonBody,
  optionalString,
  requiredString,
  runCuration,
} from "@/curation/http";
import { CLINGEN_ALLELE_ID } from "@/curation/resolver";
import { ClientInputError } from "@/server/errors";

/** POST /api/curation/variants — register a variant to be curated. Manager only. */
export async function POST(request: Request): Promise<Response> {
  return runCuration(async () => {
    const access = await curationContext(request);
    const body = await jsonBody(request);
    // Checked where it is persisted, not only where it is looked up: a retrieval that failed leaves the
    // id it failed on in the form, and both curators of the variant answer whatever gets registered.
    const clingenAlleleId = optionalString(body, "clingenAlleleId").trim();
    if (clingenAlleleId !== "" && !CLINGEN_ALLELE_ID.test(clingenAlleleId)) {
      throw new ClientInputError(
        `${clingenAlleleId} is not a ClinGen allele id: expected the form CA123456, or leave it blank`,
      );
    }
    const variant = await access.createVariant({
      gene: requiredString(body, "gene"),
      transcript: requiredString(body, "transcript"),
      hgvsC: requiredString(body, "hgvsC"),
      clingenAlleleId,
      diseaseLabel: requiredString(body, "diseaseLabel"),
      mondoId: optionalString(body, "mondoId"),
    });
    return NextResponse.json({ id: variant.id });
  });
}
