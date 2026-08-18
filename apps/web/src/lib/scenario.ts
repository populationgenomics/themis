import type { Analysis, AnalysisInputs } from "@/models/workbench";

// How each scenario names and presents itself. Every surface that labels an Analysis — a card, the
// app bar, a page title — renders through here, so adding a scenario is adding its case, never
// special-casing a card. The oneof case is the scenario (docs/design/analysis-scenarios.md).

/** The scenario inputs an Analysis was created from. Raises when unset: every surface renders from
 *  them, and an Analysis without them is a corrupt row rather than one to display blank. */
export function requireInputs(analysis: Analysis): AnalysisInputs {
  if (!analysis.inputs) {
    throw new Error(`analysis has no inputs: ${analysis.id}`);
  }
  return analysis.inputs;
}

// An unset oneof is a scenario written by a build newer than this one: proto keeps the member it does
// not know as an unknown field, so the case reads as absent. That is a state to render, not to raise
// on — a rollback or an overlapping deploy would otherwise make one row take down a whole listing.
const UNRECOGNISED = "Unrecognised scenario";

/** The Analysis's identity in one line: the variant it is about, the opening of the instruction that
 *  has no other name, or — for a scenario this build predates — a label saying so. */
export function analysisTitle(inputs: AnalysisInputs): string {
  switch (inputs.scenario.case) {
    case "variantClassification": {
      const { transcript, hgvsC } = inputs.scenario.value;
      return `${transcript}:${hgvsC}`;
    }
    case "freeForm":
      return firstLine(inputs.scenario.value.prompt);
    default:
      return UNRECOGNISED;
  }
}

/** What the scenario is called where a curator picks or reads it. */
export function scenarioLabel(inputs: AnalysisInputs): string {
  switch (inputs.scenario.case) {
    case "variantClassification":
      return "Variant classification";
    case "freeForm":
      return "Free-form";
    default:
      return UNRECOGNISED;
  }
}

/** What a card shows for this Analysis: the identifier the scenario is known by, when it has one,
 *  and the prose beneath it. Free-form has no identifier — its instruction IS the content, so it
 *  fills the card rather than being split into a heading and the remainder of its own sentence. */
export function cardContent(inputs: AnalysisInputs): {
  identifier: string | null;
  body: string;
} {
  switch (inputs.scenario.case) {
    case "variantClassification":
      return {
        identifier: analysisTitle(inputs),
        body: inputs.scenario.value.clinicalContext,
      };
    case "freeForm":
      return { identifier: null, body: inputs.scenario.value.prompt };
    default:
      return {
        identifier: null,
        body: "This analysis was created by a newer version of Themis and cannot be shown here.",
      };
  }
}

/** The line the Analysis page's chrome hovers to expand its one-line title: the clinical picture a
 *  classification runs against, and the whole instruction a free-form title was cut from. */
export function analysisDetail(inputs: AnalysisInputs): string {
  return cardContent(inputs).body;
}

/** The opening of a free-form instruction, cut at a word boundary so a title never ends mid-word. */
function firstLine(prompt: string, limit = 72): string {
  const line = prompt.trim().split("\n", 1)[0].trim();
  if (line.length <= limit) return line;
  const cut = line.slice(0, limit);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > limit / 2 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

/** Split a pasted `NM_001382309.1:c.332del` into its transcript and coding change. Curators copy the
 *  identifier as one unit, so one field takes it; the model stores the pair, because what resolves
 *  them takes them apart. Everything before the first colon is the transcript; without a colon
 *  neither half is known, and both come back empty rather than guessed at. */
export function splitVariant(value: string): {
  transcript: string;
  hgvsC: string;
} {
  const trimmed = value.trim();
  const colon = trimmed.indexOf(":");
  if (colon === -1) return { transcript: "", hgvsC: "" };
  return {
    transcript: trimmed.slice(0, colon).trim(),
    hgvsC: trimmed.slice(colon + 1).trim(),
  };
}
