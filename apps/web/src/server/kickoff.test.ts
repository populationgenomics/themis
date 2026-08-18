import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { type AnalysisInputs, AnalysisInputsSchema } from "@/models/workbench";
import { kickoffText } from "./kickoff";

function variant(
  transcript: string,
  hgvsC: string,
  clinicalContext: string,
): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: {
      case: "variantClassification",
      value: { transcript, hgvsC, clinicalContext },
    },
  });
}

function freeForm(prompt: string): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: { case: "freeForm", value: { prompt } },
  });
}

// One sample per scenario, keyed by its oneof case, so the exhaustiveness check below can demand one
// for every case the proto declares.
const SAMPLES: Record<string, AnalysisInputs | undefined> = {
  variantClassification: variant(
    "NM_001382309.1",
    "c.332del",
    "de novo, developmental delay",
  ),
  freeForm: freeForm("Re-review the MYH7 VUS calls."),
};

describe("the instruction a session opens with", () => {
  test("a classification carries every input the scenario collects", () => {
    // Each field is collected because the agent needs it; one dropped from the template reaches the
    // agent nowhere else, and the run proceeds without it.
    const text = kickoffText(
      variant(
        "NM_000059.4",
        "c.7007G>A",
        "predictive testing, no tumour tissue",
      ),
    );
    expect(text).toContain("NM_000059.4");
    expect(text).toContain("c.7007G>A");
    expect(text).toContain("predictive testing, no tumour tissue");
  });

  test("a free-form instruction reaches the agent as written", () => {
    const prompt = "Re-review the MYH7 VUS calls against the current criteria.";
    expect(kickoffText(freeForm(prompt))).toBe(prompt);
  });

  test("inputs carrying no scenario raise rather than opening an empty run", () => {
    // The boundary rejects these, so reaching here without a case is a fault. Unlike the display
    // path (`lib/scenario.ts`), which renders an unrecognised scenario: nothing is asked of the
    // agent on its behalf, so there is nothing to degrade to.
    expect(() => kickoffText(create(AnalysisInputsSchema, {}))).toThrow(
      /no scenario/,
    );
  });

  test("every scenario the proto declares has a kickoff", () => {
    // Read off the oneof descriptor, so a scenario added to the proto fails here until it has one —
    // otherwise the first Analysis created from it raises at create time instead.
    const cases = AnalysisInputsSchema.oneofs[0].fields.map((f) => f.localName);
    expect(cases.length).toBeGreaterThan(1);
    for (const name of cases) {
      const inputs = SAMPLES[name];
      if (!inputs) throw new Error(`no sample inputs for scenario ${name}`);
      expect(kickoffText(inputs).length).toBeGreaterThan(0);
    }
  });
});
