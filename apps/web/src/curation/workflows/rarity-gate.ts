import {
  AssessmentStatus,
  type WorkflowAssessment,
} from "@/gen/themis/curation/models/curation_pb";
import type { Cell } from "../ui/primitives";
import { derivedBin } from "./frequency";
import type { WorkflowDef } from "./types";

// SVCv4's rarity gate: a variant common enough against its disease threshold bars the clinical and
// locus codes outright, whatever observations exist for them.
//
// The calculator words the gate in points ("applicable if Frequency >= -1.0"); this surface holds no
// points, so the gate rides on the frequency rows themselves — `Cell.ratio.barsRarityGatedCodes`.
// `test_cell_inventory.py` asserts the flagged rows are exactly those the library's reference prices
// below -1.0, so the two statements of one rule cannot drift.
//
// The gate is read from POP_FRQ's own two numbers rather than from its stored row, so it does not
// depend on that workflow's body having rendered.

/** The codes SVCv4 bars on frequency. Several workflows carry each — the AD and AR/X-linked variants
 *  of an observation code, and segregation's five tables — so the gate keys on the code. */
export const RARITY_GATED_CODES = [
  "CLN_AFF",
  "CLN_DNV",
  "LOC_PHE",
  "LOC_SEG",
] as const;

/** The frequency row barring the rarity-gated codes, or null where none does.
 *
 * A POP_FRQ that is not `scored` bars nothing: `no data` and `not applicable` are findings about the
 * frequency, and neither establishes one to gate on.
 */
export function barringBin(
  popFrq: WorkflowAssessment | undefined,
): Cell | null {
  if (!popFrq || popFrq.status !== AssessmentStatus.SCORED) return null;
  const bin = derivedBin(popFrq);
  return bin?.ratio?.barsRarityGatedCodes ? bin : null;
}

/** Whether the gate reaches this workflow at all.
 *
 *  A gated code, and one the calculator prints. The note is the calculator's, and the set it
 *  quantified over held only the workflows the calculator prints — so it bars `LOC_SEG`'s four
 *  co-segregation tables, every row of which awards points toward pathogenic, and not the
 *  non-segregation branch, which the calculator prints nowhere and which can only award benignity.
 *  Barring that one would suppress an observation agreeing with the very frequency that triggered
 *  the gate, and re-open the gap the branch was transcribed to close.
 *
 *  Keyed on `source` rather than on the workflow's name, so the rule is one the framework states
 *  rather than a list anybody can grow. */
function gateReaches(workflow: WorkflowDef): boolean {
  return (
    workflow.source !== "supplement" &&
    (RARITY_GATED_CODES as readonly string[]).includes(workflow.code)
  );
}

/** The ids of the workflows the frequency bars, empty where it bars none. */
export function barredWorkflowIds(
  workflows: WorkflowDef[],
  popFrq: WorkflowAssessment | undefined,
): Set<string> {
  if (!barringBin(popFrq)) return new Set();
  return new Set(workflows.filter(gateReaches).map((workflow) => workflow.id));
}
