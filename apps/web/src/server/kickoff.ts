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
      "the calls that span codes, each with its reasoning and an explicit uncertainty: the mechanism level with the rubric criteria met and their sum, and the exon relevance; the predictor the policy selected and the entry that decided it; then the SVCv4 classification widget, which carries every code the paths taken admit, with its status, evidence, provenance, decision-tree cell or priced cells, reasoning and uncertainty; the deposit-request list of papers the store could not serve.",
  },
  {
    heading: "Point tally",
    holds:
      "a reading of the tally the widget draws: the gate effect, what holds the class where it is, and, with a judgement input open, the classes in contention; the widget carries the audit trail, the totals, the bands, the open values and the sensitivity rows, so this section restates none of their numbers; closing with which calls the sensitivity rows show to be class-determinative.",
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
