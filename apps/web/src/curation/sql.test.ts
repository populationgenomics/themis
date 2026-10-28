import { describe, expect, test } from "bun:test";
import { parseVariant, type VariantRow } from "./sql";

const ROW: VariantRow = {
  id: "var-1",
  project_id: "validation",
  gene: "FBN1",
  transcript: "NM_000138.5",
  hgvs_c: "c.7003C>T",
  clingen_allele_id: "",
  disease_label: "Marfan syndrome",
  mondo_id: "MONDO:0007947",
  mode_of_inheritance: "AUTOSOMAL_DOMINANT",
  clinical_context: "Tall stature and ectopia lentis.",
  created_by: "manager@example.org",
  created_at: new Date("2026-08-01T09:00:00Z"),
};

describe("a registration read from curation.variants", () => {
  test("carries its clinical context", () => {
    expect(parseVariant(ROW).clinicalContext).toBe(
      "Tall stature and ectopia lentis.",
    );
  });

  test("carries its Project", () => {
    expect(parseVariant(ROW).projectId).toBe("validation");
  });

  test("without a clinical context is refused, naming the row", () => {
    expect(() => parseVariant({ ...ROW, clinical_context: null })).toThrow(
      /curation\.variants row var-1 holds no clinical context/,
    );
  });
});
