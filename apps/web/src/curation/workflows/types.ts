import type { ComponentType } from "react";
import type { WorkflowAssessment } from "@/gen/themis/curation/models/curation_pb";
import type {
  Consequence,
  Inheritance,
} from "@/gen/themis/evidence/models/evidence_pb";
import type { Cell } from "../ui/primitives";

// What a transcribed workflow is, as the worksheet holds it.
//
// A component rather than a row in a definitions table: several workflows are not the regular shape
// (population frequency takes two typed numbers and a bin; specific phenotype is two ordered steps),
// and a schema general enough for those is a schema nobody can read.

/** The routing the curator states, which decides the workflows on screen. Both vocabularies are the
 *  shared ones, so a curator's routing and a run's are the same members and can be compared. Each
 *  admits members the calculator's splits have no branch for; a workflow says which ones it covers
 *  and is absent for the rest. */
export interface Routing {
  inheritance: Inheritance;
  consequenceClass: Consequence;
}

export interface WorkflowBodyProps {
  assessment: WorkflowAssessment;
  /** The other workflows' current answers, by workflow id. A few workflows are conditioned on a
   *  sibling: which RNA-assay table applies follows the branch chosen under the splice prediction,
   *  as in the calculator. Read-only — a workflow never writes another's answer. */
  siblings: Readonly<Record<string, WorkflowAssessment>>;
  onChange: (next: WorkflowAssessment) => void;
  /** Commit the current draft now — bound to a control losing focus. */
  onBlur: () => void;
}

export interface WorkflowDef {
  /** The stable id this workflow's answer is stored under. Never reused for another workflow: a
   *  stored assessment is addressed by it, and a round joins on it. */
  id: string;
  /** The SVCv4 code component the workflow scores. Several workflows share one (the AD and AR/XL
   *  variants of an observation code), which is why it is not the id. */
  code: string;
  /** The calculator's own title, verbatim. */
  title: string;
  /** The calculator's applicability line, verbatim, where it prints one. */
  applicability?: string;
  /** Every decision-tree cell the workflow offers, for the nearest-alternative picker. */
  cells: Cell[];
  /** Controls the workflow asks for that answer no decision-tree row — the two frequencies POP_FRQ
   *  bins. Their wording is the framework's and is checked as such, but no round joins on them and
   *  the library prices none of them, so they are not `cells`. */
  inputs?: Cell[];
  /** Set where the workflow is transcribed from a supplement because the calculator prints none
   *  (§Which source a workflow comes from). Absent means the calculator prints it.
   *
   *  Load-bearing, not documentation: the rarity gate applies only to workflows the calculator both
   *  prints and gates, so a supplement-sourced one is outside a note that could not have been written
   *  about it. `fidelity.test.ts` checks the declaration against the sources both ways, so one cannot
   *  be forgotten or claimed falsely. */
  source?: "supplement";
  /** Whether the curator's stated routing puts this workflow on screen. */
  applies: (routing: Routing) => boolean;
  Body: ComponentType<WorkflowBodyProps>;
}

export type WorkflowGroup = {
  key: string;
  title: string;
  workflows: WorkflowDef[];
};
