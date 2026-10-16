import { ClientInputError } from "@/server/errors";
import { optionalString, requiredString } from "./http";
import { MODES, modeFromName, modeName } from "./mode";
import {
  CLINICAL_CONTEXT_MAX_LENGTH,
  clinicalContextLength,
  MONDO_ID,
  type NewVariant,
} from "./model";
import { CLINGEN_ALLELE_ID } from "./resolver";

/** A register request's body as the variant it registers. Throws `ClientInputError`, naming the
 *  field, for a body that does not state a whole registration: both curators of the variant answer
 *  whatever gets registered, so nothing here is defaulted. */
export function parseRegistration(body: Record<string, unknown>): NewVariant {
  // Checked where it is persisted, not only where it is looked up: a retrieval that failed leaves the
  // id it failed on in the form.
  const clingenAlleleId = optionalString(body, "clingenAlleleId").trim();
  if (clingenAlleleId !== "" && !CLINGEN_ALLELE_ID.test(clingenAlleleId)) {
    throw new ClientInputError(
      `${clingenAlleleId} is not a ClinGen allele id: expected the form CA123456, or leave it blank`,
    );
  }
  const mondoId = requiredString(body, "mondoId").trim();
  if (!MONDO_ID.test(mondoId)) {
    throw new ClientInputError(
      `${mondoId} is not a MONDO id: expected the form MONDO:0005045`,
    );
  }
  const named = requiredString(body, "modeOfInheritance");
  const modeOfInheritance = modeFromName(named);
  if (modeOfInheritance === undefined) {
    throw new ClientInputError(
      `${named} is not a mode of inheritance: expected one of ${MODES.map((m) => modeName(m.mode)).join(", ")}`,
    );
  }
  // Trimmed as a run's own is: the whitespace around the prose states nothing.
  const clinicalContext = requiredString(body, "clinicalContext").trim();
  const length = clinicalContextLength(clinicalContext);
  if (length > CLINICAL_CONTEXT_MAX_LENGTH) {
    throw new ClientInputError(
      `clinicalContext is ${length} characters; a registration states at most ${CLINICAL_CONTEXT_MAX_LENGTH}`,
    );
  }
  return {
    gene: requiredString(body, "gene").trim(),
    // Trimmed because both are part of the uniqueness key: a trailing space would register the same
    // entity twice.
    transcript: requiredString(body, "transcript").trim(),
    hgvsC: requiredString(body, "hgvsC").trim(),
    clingenAlleleId,
    diseaseLabel: requiredString(body, "diseaseLabel"),
    mondoId,
    modeOfInheritance,
    clinicalContext,
  };
}
