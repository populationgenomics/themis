import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { AnalysisInputsSchema, AnalysisSchema } from "@/models/workbench";
import {
  freeForm,
  SAMPLES,
  scenarioCases,
  variant,
} from "@/models/workbench.test-support";
import {
  analysisTitle,
  cardContent,
  requireInputs,
  scenarioLabel,
  splitVariant,
} from "./scenario";

describe("how a scenario names its Analysis", () => {
  test("a classification is named by the variant it is about", () => {
    expect(analysisTitle(variant("NM_001382309.1", "c.332del"))).toBe(
      "NM_001382309.1:c.332del",
    );
  });

  test("a free-form Analysis is named by the opening of its instruction", () => {
    expect(analysisTitle(freeForm("Re-review the MYH7 VUS calls."))).toBe(
      "Re-review the MYH7 VUS calls.",
    );
  });

  test("a long free-form instruction is cut at a word boundary, never mid-word", () => {
    const prompt =
      "Summarise gnomAD v4 constraint for SCN2A and flag which missense regions are depleted relative to expectation";
    const title = analysisTitle(freeForm(prompt));
    expect(title.endsWith("…")).toBe(true);
    const kept = title.slice(0, -1);
    // Every word the title shows appears whole in the instruction: it is a prefix, and what follows
    // it there is a space rather than the rest of a word it cut through.
    expect(prompt.startsWith(kept)).toBe(true);
    expect(prompt[kept.length]).toBe(" ");
  });

  test("every scenario the proto declares is named, labelled, and rendered", () => {
    // Read off the oneof descriptor, so a scenario added to the proto fails here until it has a
    // rendering — a hand-written list would only fail when someone remembered to extend it.
    const cases = scenarioCases();
    expect(cases.length).toBeGreaterThan(1);
    // What an unset oneof renders — a scenario this build predates. Compared against rather than
    // checked for length: every fallback is a non-empty string, so a length assertion passes on a
    // scenario that has a sample and no rendering, which is the case this test exists to catch.
    const unrecognised = create(AnalysisInputsSchema, {});
    for (const name of cases) {
      const inputs = SAMPLES[name];
      if (!inputs) throw new Error(`no sample inputs for scenario ${name}`);
      expect(analysisTitle(inputs)).not.toBe(analysisTitle(unrecognised));
      expect(scenarioLabel(inputs)).not.toBe(scenarioLabel(unrecognised));
      expect(cardContent(inputs).body).not.toBe(cardContent(unrecognised).body);
    }
  });
});

describe("what a card shows", () => {
  test("a classification leads with the variant and explains it with the clinical context", () => {
    expect(
      cardContent(variant("NM_1.1", "c.1A>T", "hypotonia, de novo")),
    ).toEqual({
      identifier: "NM_1.1:c.1A>T",
      body: "hypotonia, de novo",
    });
  });

  test("a free-form instruction fills the card rather than being split against itself", () => {
    // Splitting it into a heading and the remainder leaves the body starting mid-sentence.
    const prompt = "Re-review the MYH7 VUS calls against the current criteria.";
    expect(cardContent(freeForm(prompt))).toEqual({
      identifier: null,
      body: prompt,
    });
  });
});

describe("inputs are required to render an Analysis at all", () => {
  test("an Analysis with no inputs raises rather than rendering blank", () => {
    const analysis = create(AnalysisSchema, { id: "an_1" });
    expect(() => requireInputs(analysis)).toThrow("an_1");
  });

  test("a scenario this build predates renders as one, rather than failing the page", () => {
    // An older build reads a newer build's row as an unset oneof (the member it does not know stays
    // an unknown field). Raising here would cost the whole Project listing, which parses every row
    // server-side, for one Analysis it cannot name.
    const unknown = create(AnalysisInputsSchema, {});
    expect(analysisTitle(unknown)).toBe("Unrecognised scenario");
    expect(scenarioLabel(unknown)).toBe("Unrecognised scenario");
    expect(cardContent(unknown).body.length).toBeGreaterThan(0);
  });
});

describe("splitting a pasted variant", () => {
  test("a full HGVS string splits into transcript and coding change", () => {
    expect(splitVariant("NM_001382309.1:c.332del")).toEqual({
      transcript: "NM_001382309.1",
      hgvsC: "c.332del",
    });
  });

  test("surrounding whitespace from a paste is dropped", () => {
    expect(splitVariant("  NM_000059.4 : c.7007G>A  ")).toEqual({
      transcript: "NM_000059.4",
      hgvsC: "c.7007G>A",
    });
  });

  test("without a colon neither half is known, so neither is guessed", () => {
    // Create stays disabled on this rather than storing the whole string as a transcript.
    expect(splitVariant("c.332del")).toEqual({ transcript: "", hgvsC: "" });
  });

  test("a protein-level colon in the change stays with the change", () => {
    expect(splitVariant("NM_1.1:c.332del:p.(Asn111ThrfsTer16)")).toEqual({
      transcript: "NM_1.1",
      hgvsC: "c.332del:p.(Asn111ThrfsTer16)",
    });
  });
});
