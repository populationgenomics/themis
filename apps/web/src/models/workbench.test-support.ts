import { create } from "@bufbuild/protobuf";
import { type AnalysisInputs, AnalysisInputsSchema } from "@/models/workbench";

// Shared by the tests that walk the scenarios: the cases the proto declares, and one sample of each.

/** The scenario cases the proto declares, read off the `scenario` oneof by name. Raises if the
 *  message no longer carries a oneof of that name, rather than reading a different one. */
export function scenarioCases(): string[] {
  const oneof = AnalysisInputsSchema.oneofs.find((o) => o.name === "scenario");
  if (!oneof) throw new Error("AnalysisInputs declares no `scenario` oneof");
  return oneof.fields.map((f) => f.localName);
}

export function variant(
  transcript: string,
  hgvsC: string,
  clinicalContext = "de novo, developmental delay",
): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: {
      case: "variantClassification",
      value: { transcript, hgvsC, clinicalContext },
    },
  });
}

export function freeForm(prompt: string): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: { case: "freeForm", value: { prompt } },
  });
}

/** One sample per scenario, keyed by its oneof case, so an exhaustiveness check can demand one for
 *  every case the proto declares. */
export const SAMPLES: Record<string, AnalysisInputs | undefined> = {
  variantClassification: variant("NM_001382309.1", "c.332del"),
  freeForm: freeForm("Re-review the MYH7 VUS calls."),
};
