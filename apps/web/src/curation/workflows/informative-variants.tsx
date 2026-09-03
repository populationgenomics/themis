"use client";

import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  CountRows,
  FrameworkNote,
  readField,
  withField,
} from "../ui/primitives";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Informative-variant tables for missense and start-loss variants, transcribed from the ClinGen
// Pilot Calculator.
//
// The missense table names MIS_INF outright. The two start-loss tables leave the evidence code
// blank, because which family scores them follows the branch taken in the predicted-effect table
// above; their cells carry both.

// --- MIS_INF -----------------------------------------------------------------------------------
//
// The three row labels repeat verbatim under each of the four criteria, which the calculator
// separates by point value alone.

const MIS_SAME_AA_PLP =
  "Distinct Nucleotide, Same Amino Acid Pathogenic/Likely Pathogenic";
const MIS_DISTINCT_AA_PLP =
  "Distinct Amino Acid Pathogenic/Likely Pathogenic & Grantham Difference of Informative Variant ≤ VBC";
const MIS_DISTINCT_AA_BLB =
  "Distinct Amino Acid Benign/Likely Benign & Grantham Difference of Informative Variant ≥ VBC";
const MIS_SAME_AA_BLB =
  "Distinct Nucleotide, Same Amino Acid Benign/Likely Benign";

const MIS_INF: Cell[] = [
  {
    id: "mis_inf.same_aa_plp.p_first",
    cell: "MIS_INF.same_aa_plp.p_first",
    group: MIS_SAME_AA_PLP,
    label: "First P Variant",
  },
  {
    id: "mis_inf.same_aa_plp.lp_first",
    cell: "MIS_INF.same_aa_plp.lp_first",
    group: MIS_SAME_AA_PLP,
    label: "First LP Variant",
  },
  {
    id: "mis_inf.same_aa_plp.plp_additional",
    cell: "MIS_INF.same_aa_plp.plp_additional",
    group: MIS_SAME_AA_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "mis_inf.distinct_aa_plp.p_first",
    cell: "MIS_INF.distinct_aa_plp.p_first",
    group: MIS_DISTINCT_AA_PLP,
    label: "First P Variant",
  },
  {
    id: "mis_inf.distinct_aa_plp.lp_first",
    cell: "MIS_INF.distinct_aa_plp.lp_first",
    group: MIS_DISTINCT_AA_PLP,
    label: "First LP Variant",
  },
  {
    id: "mis_inf.distinct_aa_plp.plp_additional",
    cell: "MIS_INF.distinct_aa_plp.plp_additional",
    group: MIS_DISTINCT_AA_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "mis_inf.distinct_aa_blb.b_first",
    cell: "MIS_INF.distinct_aa_blb.b_first",
    group: MIS_DISTINCT_AA_BLB,
    label: "First B Variant",
  },
  {
    id: "mis_inf.distinct_aa_blb.lb_first",
    cell: "MIS_INF.distinct_aa_blb.lb_first",
    group: MIS_DISTINCT_AA_BLB,
    label: "First LB Variant",
  },
  {
    id: "mis_inf.distinct_aa_blb.blb_additional",
    cell: "MIS_INF.distinct_aa_blb.blb_additional",
    group: MIS_DISTINCT_AA_BLB,
    label: "Additional B/LB variants",
  },
  {
    id: "mis_inf.same_aa_blb.b_first",
    cell: "MIS_INF.same_aa_blb.b_first",
    group: MIS_SAME_AA_BLB,
    label: "First B Variant",
  },
  {
    id: "mis_inf.same_aa_blb.lb_first",
    cell: "MIS_INF.same_aa_blb.lb_first",
    group: MIS_SAME_AA_BLB,
    label: "First LB Variant",
  },
  {
    id: "mis_inf.same_aa_blb.blb_additional",
    cell: "MIS_INF.same_aa_blb.blb_additional",
    group: MIS_SAME_AA_BLB,
    label: "Additional B/LB variants",
  },
  {
    id: "mis_inf.none_met",
    cell: "MIS_INF.none_met",
    label: "Informative Variants for Which None of Above Four Criteria Met",
  },
];

// --- Start loss: two tables, one per path of the predicted-effect tree --------------------------

const START_LOSS_PLP =
  "Pathogenic/Likely Pathogenic variants at position +1, +2, +3";

const START_LOSS_BLB =
  "B/LB variants at position +1, +2, +3 OR PTC introduced by B/LB variant occurs upstream of alternate start codon";

const START_LOSS_NOTE =
  "Note: If the VBC is a c.1A>C variant, then prior observations of P variants c.1A>T or c.1A>G would earn full points and prior observations of second or third position P variants would earn half the points specified in the informative variants table. If the VBC is in the second or third position, and prior P observations are in any position, use full points.";

const START_LOST_INF_DEFAULT: Cell[] = [
  {
    id: "start_lost_inf_default.p_first",
    cell: "NUL_INF/CDS_INF.start_loss_default.p_first",
    group: START_LOSS_PLP,
    label: "First P Variant",
  },
  {
    id: "start_lost_inf_default.lp_first",
    cell: "NUL_INF/CDS_INF.start_loss_default.lp_first",
    group: START_LOSS_PLP,
    label: "First LP Variant",
  },
  {
    id: "start_lost_inf_default.plp_additional",
    cell: "NUL_INF/CDS_INF.start_loss_default.plp_additional",
    group: START_LOSS_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "start_lost_inf_default.vus",
    cell: "NUL_INF/CDS_INF.start_loss_default.vus",
    label: "VUS informative variants in this exon",
  },
  {
    id: "start_lost_inf_default.b_first",
    cell: "NUL_INF/CDS_INF.start_loss_default.b_first",
    group: START_LOSS_BLB,
    label: "First B Variant",
  },
  {
    id: "start_lost_inf_default.lb_first",
    cell: "NUL_INF/CDS_INF.start_loss_default.lb_first",
    group: START_LOSS_BLB,
    label: "First LB Variant",
  },
  {
    id: "start_lost_inf_default.blb_additional",
    cell: "NUL_INF/CDS_INF.start_loss_default.blb_additional",
    group: START_LOSS_BLB,
    label: "Additional B/LB variants",
  },
];

const START_LOST_INF_ALT_START_PATH: Cell[] = [
  {
    id: "start_lost_inf_alt_start.reconsider",
    cell: "CDS_INF.start_loss_functional_alt_start.reconsider",
    group: "P/LP variants exist resulting in similarly altered/removed region",
    label: "Reconsider the use of this path of the flow diagram",
  },
];

const START_LOST_INF_ALT_START_COUNTS: Cell[] = [
  {
    id: "start_lost_inf_alt_start.none",
    cell: "CDS_INF.start_loss_functional_alt_start.none",
    label: "No informative variants in this exon",
  },
  {
    id: "start_lost_inf_alt_start.b_first",
    cell: "CDS_INF.start_loss_functional_alt_start.b_first",
    group: START_LOSS_BLB,
    label: "First B Variant",
  },
  {
    id: "start_lost_inf_alt_start.lb_first",
    cell: "CDS_INF.start_loss_functional_alt_start.lb_first",
    group: START_LOSS_BLB,
    label: "First LB Variant",
  },
  {
    id: "start_lost_inf_alt_start.blb_additional",
    cell: "CDS_INF.start_loss_functional_alt_start.blb_additional",
    group: START_LOSS_BLB,
    label: "Additional B/LB variants",
  },
];

/** The first row takes no count: the calculator prints it as a lone cell that sends the curator back
 *  to the flow diagram, with the counted rows beneath it. */
function StartLostInfAltStartBody({
  assessment,
  onChange,
  onBlur,
}: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="start_lost_inf_alt_start_path"
        cells={START_LOST_INF_ALT_START_PATH}
        value={readField(assessment, "start_lost_inf_alt_start.path")}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "start_lost_inf_alt_start.path" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      <CountRows
        cells={START_LOST_INF_ALT_START_COUNTS}
        assessment={assessment}
        onChange={onChange}
        countLabel="Applicable Variants"
        onBlur={onBlur}
      />
      <FrameworkNote>{START_LOSS_NOTE}</FrameworkNote>
    </div>
  );
}

export const MISSED_INF_WORKFLOWS: WorkflowDef[] = [
  {
    id: "mis_inf",
    code: "MIS_INF",
    title: "Workflow for Informative Variants",
    applicability: "Informative Variants",
    cells: MIS_INF,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.MISSENSE,
    Body: countBody(MIS_INF, "Applicable Variants"),
  },
  {
    id: "start_lost_inf_default",
    code: "NUL_INF/CDS_INF",
    title: "Workflow for Informative Variants",
    applicability: "Informative Variants",
    cells: START_LOST_INF_DEFAULT,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.START_LOST,
    Body: countBody(START_LOST_INF_DEFAULT, "Applicable Variants", [
      START_LOSS_NOTE,
    ]),
  },
  {
    id: "start_lost_inf_alt_start",
    // CDS only: this table is reached solely from the functional-alternate-start branch, so naming
    // the null family too would claim a path the flow cannot take.
    code: "CDS_INF",
    title:
      "Workflow for Informative Variants (Alternate Inframe Start Produces Functional Protein)",
    applicability: "Informative Variants",
    cells: [
      ...START_LOST_INF_ALT_START_PATH,
      ...START_LOST_INF_ALT_START_COUNTS,
    ],
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.START_LOST,
    Body: StartLostInfAltStartBody,
  },
];
