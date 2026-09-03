import type { AnalysisInputs } from "@/models/workbench";

// The kickoff text an Analysis's agent session opens with, rendered from its scenario inputs. This
// text is what "variant classification" MEANS to the agent, so it is server-side and never a
// client input. It is not persisted: the conversation carries what was sent
// (docs/design/analysis-scenarios.md).

interface OutlineSection {
  heading: string;
  holds: string;
}

/** The sections a classification's working document holds, in order. */
export const VARIANT_CLASSIFICATION_OUTLINE: ReadonlyArray<OutlineSection> = [
  {
    heading: "Title",
    holds:
      'the variant (canonical and HGVS), gene, and the class verdict — or "class not established" with the classes in contention — then a notice that the framework applied is a draft standard under evaluation and the document is not a clinical classification.',
  },
  {
    heading: "Variant & MDE",
    holds:
      "resolved identifiers, gene, consequence, the chosen transcript, the disease entity with its MONDO id, and the entity's gene–disease validity gate level.",
  },
  {
    heading: "ClinVar-first check",
    holds:
      "the records surfaced (stars, review status, submitters), any VCEP consensus, and the adopt-or-proceed decision with reasoning.",
  },
  {
    heading: "Evidence assessment",
    holds:
      "per applicable code: the evidence, its provenance, the decision-tree cell scored, and for a judgement call the reasoning and an explicit uncertainty; the predictor the policy selected and the entry that decided it; a one-line derivation for each supplied code; codes not assessable and why; the deposit-request list of papers the store could not serve.",
  },
  {
    heading: "Point tally",
    holds:
      "the audit trail, gate effect, total, band and VUS sub-band, and the final class — with a judgement input open, per-value totals replace the total, band and sub-band, the audit trail and gate effect stay, and the class appears only where the surviving values agree; the matrix multiplier and both missense paths where relevant; closing with the sensitivity table — each judgement input varied across its plausible range, the total and class each yields, and which calls are class-determinative.",
  },
  {
    heading: "Verdict",
    holds:
      "the holistic reading of the claims and their provenance, where the classification lands and why, and what holds it at this class rather than the one above — or, with no class established, what is open and what would settle it.",
  },
  {
    heading: "Feedback / reflection",
    holds:
      "the six fixed questions: what in the workflow was unclear, which services were hard to use, which tools are missing, what information could not be gathered, what would reach a more conclusive class, and what code a ready helper should have written.",
  },
];

/** The instruction to open the session with. Raises on inputs carrying no scenario — the boundary
 *  rejects those, so reaching here without one is a fault, not an empty run to start. */
export function kickoffText(inputs: AnalysisInputs): string {
  switch (inputs.scenario.case) {
    case "variantClassification": {
      const { transcript, hgvsC, clinicalContext } = inputs.scenario.value;
      return [
        `Classify ${transcript}:${hgvsC}.`,
        `Clinical context: ${clinicalContext}`,
        "Establish the disease entity or entities from the variant and the clinical context, then classify against what you establish. List any literature you needed and could not retrieve.",
        "Write the working document in these sections, in this order:",
        outline(VARIANT_CLASSIFICATION_OUTLINE),
      ].join("\n\n");
    }
    case "freeForm":
      return inputs.scenario.value.prompt;
    default:
      throw new Error("analysis inputs carry no scenario");
  }
}

/** The outline as the agent reads it: one numbered line per section. */
function outline(sections: ReadonlyArray<OutlineSection>): string {
  return sections
    .map((s, i) => `${i + 1}. **${s.heading}** — ${s.holds}`)
    .join("\n");
}
