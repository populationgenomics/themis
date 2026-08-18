import type { AnalysisInputs } from "@/models/workbench";

// The kickoff text an Analysis's agent session opens with, rendered from its scenario inputs. This
// text is what "variant classification" MEANS to the agent, so it is server-side and never a
// client input. It is not persisted: the conversation carries what was sent
// (docs/design/analysis-scenarios.md).

/** The instruction to open the session with. Raises on inputs carrying no scenario — the boundary
 *  rejects those, so reaching here without one is a fault, not an empty run to start. */
export function kickoffText(inputs: AnalysisInputs): string {
  switch (inputs.scenario.case) {
    case "variantClassification": {
      const { transcript, hgvsC, clinicalContext } = inputs.scenario.value;
      return [
        `Classify ${transcript}:${hgvsC}.`,
        `Clinical context: ${clinicalContext}`,
        "Establish the disease entity from the variant and the clinical context, then classify against what you establish. List any literature you needed and could not retrieve.",
      ].join("\n\n");
    }
    case "freeForm":
      return inputs.scenario.value.prompt;
    default:
      throw new Error("analysis inputs carry no scenario");
  }
}
