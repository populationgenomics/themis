import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { AnalysisInputsSchema } from "@/models/workbench";
import {
  freeForm,
  SAMPLES,
  scenarioCases,
  variant,
} from "@/models/workbench.test-support";
import { kickoffText, VARIANT_CLASSIFICATION_OUTLINE } from "./kickoff";

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

  test("a classification ends with the working document's outline", () => {
    // Asserted as the tail, one section per line in the outline's order: a section dropped, reordered,
    // or followed by anything else fails here.
    const text = kickoffText(
      variant("NM_000059.4", "c.7007G>A", "predictive testing"),
    );
    const sections = VARIANT_CLASSIFICATION_OUTLINE;
    expect(sections.length).toBeGreaterThan(1);
    const tail = text.split("\n").slice(-sections.length);
    expect(tail).toHaveLength(sections.length);
    sections.forEach((section, i) => {
      expect(tail[i]).toContain(`**${section.heading}**`);
    });
  });

  test("no other scenario carries the classification's outline", () => {
    // On the rendered heading, not the bare word: a free-form instruction may well say "verdict".
    // Vacuous while free-form echoes its sample prompt; it bites for a structured scenario added later.
    const others = scenarioCases().filter(
      (name) => name !== "variantClassification",
    );
    expect(others.length).toBeGreaterThan(0);
    for (const name of others) {
      const inputs = SAMPLES[name];
      if (!inputs) throw new Error(`no sample inputs for scenario ${name}`);
      const text = kickoffText(inputs);
      for (const section of VARIANT_CLASSIFICATION_OUTLINE) {
        expect(text).not.toContain(`**${section.heading}**`);
      }
    }
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
    const cases = scenarioCases();
    expect(cases.length).toBeGreaterThan(1);
    for (const name of cases) {
      const inputs = SAMPLES[name];
      if (!inputs) throw new Error(`no sample inputs for scenario ${name}`);
      expect(kickoffText(inputs).length).toBeGreaterThan(0);
    }
  });
});
