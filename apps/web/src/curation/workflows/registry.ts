import { CASE_CONTROL_WORKFLOWS } from "./case-control";
import { CLN_WORKFLOWS } from "./cln";
import { EXON_CNV_WORKFLOWS } from "./exon-cnv";
import { FRAMESHIFT_WORKFLOWS } from "./frameshift";
import { MISSED_INF_WORKFLOWS } from "./informative-variants";
import { INFRAME_INDEL_WORKFLOWS } from "./inframe-indel";
import { LOC_WORKFLOWS } from "./loc";
import { NON_SEGREGATION_WORKFLOWS } from "./non-segregation";
import { NONSENSE_WORKFLOWS } from "./nonsense";
import { POP_WORKFLOWS } from "./pop";
import { PREDICTED_WORKFLOWS } from "./predicted";
import { SPLICE_VARIANT_WORKFLOWS } from "./splice-variants";
import { START_STOP_LOST_WORKFLOWS } from "./start-stop-lost";
import type { Routing, WorkflowDef, WorkflowGroup } from "./types";

// Every transcribed workflow, in the calculator's own order, and which of them the curator's stated
// routing puts on screen.

/** The worksheet-level sections. They are stored exactly as workflows are — same table, same
 *  auto-save, same submission snapshot — so nothing downstream needs a second read path for them. */
export const CASE_ID = "case";
export const ROUTING_ID = "routing";
export const VERDICT_ID = "verdict";

const GROUPS: WorkflowGroup[] = [
  {
    key: "pop",
    title: "Population Observations (POP)",
    workflows: POP_WORKFLOWS,
  },
  {
    key: "cln",
    title: "Clinical Observations (CLN)",
    workflows: [...CLN_WORKFLOWS, ...CASE_CONTROL_WORKFLOWS],
  },
  {
    key: "loc",
    title: "Locus Specificity (LOC)",
    workflows: [...LOC_WORKFLOWS, ...NON_SEGREGATION_WORKFLOWS],
  },
  {
    key: "prd",
    title: "Predicted and Functional Effect",
    workflows: [
      ...PREDICTED_WORKFLOWS,
      ...NONSENSE_WORKFLOWS,
      ...FRAMESHIFT_WORKFLOWS,
      ...INFRAME_INDEL_WORKFLOWS,
      ...EXON_CNV_WORKFLOWS,
      ...START_STOP_LOST_WORKFLOWS,
      ...SPLICE_VARIANT_WORKFLOWS,
      ...MISSED_INF_WORKFLOWS,
    ],
  },
];

export const ALL_WORKFLOWS: WorkflowDef[] = GROUPS.flatMap((g) => g.workflows);

/** Refuses a duplicate id at module load: two workflows sharing one would silently overwrite each
 *  other's stored answer, and the collision is invisible in the rendered page. */
const seen = new Set<string>();
for (const workflow of ALL_WORKFLOWS) {
  if (seen.has(workflow.id)) {
    throw new Error(`two curation workflows share the id ${workflow.id}`);
  }
  seen.add(workflow.id);
}

/** Refuses a section description no curator can read: a blank one, and one on an `inputs` cell,
 *  which `ValueField` prints without a heading. Either way the stored label carries wording the
 *  screen never showed.
 *
 *  Whether the sections of a table can be drawn at all is `sections()`' business: `cells` is the
 *  nearest-alternative union of every table a workflow renders, not a table. */
export function refuseUnreadableDescriptions(workflows: WorkflowDef[]): void {
  for (const workflow of workflows) {
    for (const cell of workflow.cells) {
      if (cell.group !== undefined && cell.group.trim() === "") {
        throw new Error(
          `the curation cell ${cell.cell} carries a blank section description`,
        );
      }
    }
    for (const input of workflow.inputs ?? []) {
      if (input.group !== undefined) {
        throw new Error(
          `the curation input ${input.cell} carries a section description, which no table prints`,
        );
      }
    }
  }
}

refuseUnreadableDescriptions(ALL_WORKFLOWS);

export function workflowById(id: string): WorkflowDef | undefined {
  return ALL_WORKFLOWS.find((w) => w.id === id);
}

/** The groups a worksheet shows, with the workflows the routing excludes dropped. A group whose
 *  workflows all drop out is omitted rather than rendered empty. */
export function groupsFor(routing: Routing): WorkflowGroup[] {
  return GROUPS.map((group) => ({
    ...group,
    workflows: group.workflows.filter((w) => w.applies(routing)),
  })).filter((group) => group.workflows.length > 0);
}

export function workflowsFor(routing: Routing): WorkflowDef[] {
  return groupsFor(routing).flatMap((g) => g.workflows);
}
