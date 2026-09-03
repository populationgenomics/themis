"use client";

import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import { type Cell, ChoiceRows, readField, withField } from "../ui/primitives";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Nonsense variants: the predicted effect, the functional assessment, and the three
// informative-variant tables the calculator prints for the class.

// The first column of the predicted-effect table, which the calculator spans over the rows beneath
// it. A row's own text does not say which NMD branch it sits under.
const NMD_NO_ALT_START =
  "VBC is more than 50 nt upstream of the last exon–exon boundary ( NMD predicted ) No known evidence for alternate functional start codon 3' of VBC";
const NMD_ALT_START =
  "VBC is more than 50 nt upstream of the last exon–exon boundary ( NMD predicted ) Putative alternative start codon 3' of VBC with functional or genetic evidence";
const NMD_NOT_PREDICTED =
  "VBC located within the last or only exon, gene documented to not undergo NMD, or VBC within 50 nt upstream of the last exon–exon boundary ( NMD not predicted )";

const NUL_PRD: Cell[] = [
  {
    id: "nul_prd.nmd.no_alt_start",
    cell: "NUL_PRD.nmd.no_alt_start",
    group: NMD_NO_ALT_START,
    label: "Removes 100% of protein",
  },
  {
    id: "nul_prd.nmd.alt_start.retains_function",
    cell: "NUL_PRD.nmd.alt_start.retains_function",
    group: NMD_ALT_START,
    label:
      "Functional data shows that a shorter protein using the alternative inframe start downstream of VBC retains function as compared to the full length transcript",
  },
  {
    id: "nul_prd.nmd.alt_start.gt50",
    cell: "NUL_PRD.nmd.alt_start.gt50",
    group: NMD_ALT_START,
    label:
      "Removes/alters >50% of protein OR Use of alternative start removes/alters an entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "nul_prd.nmd.alt_start.gt25",
    cell: "NUL_PRD.nmd.alt_start.gt25",
    group: NMD_ALT_START,
    label:
      "Removes/alters >25% of protein OR Use of alternative start removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "nul_prd.nmd.alt_start.gt10",
    cell: "NUL_PRD.nmd.alt_start.gt10",
    group: NMD_ALT_START,
    label:
      "Removes/alters >10% of protein OR Use of alternative start removes/alters a region with some evidence in the Molecular Mechanism",
  },
  {
    id: "nul_prd.nmd.alt_start.lt10",
    cell: "NUL_PRD.nmd.alt_start.lt10",
    group: NMD_ALT_START,
    label:
      "Removes/alters <10% of protein OR Use of alternative start impacts region with unknown or no known function in Molecular Mechanism",
  },
  {
    id: "nul_prd.no_nmd.gt50",
    cell: "NUL_PRD.no_nmd.gt50",
    group: NMD_NOT_PREDICTED,
    label:
      "Removes/alters >50% of protein OR Removes/alters entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "nul_prd.no_nmd.gt25",
    cell: "NUL_PRD.no_nmd.gt25",
    group: NMD_NOT_PREDICTED,
    label:
      "Removes/alters >25% of protein OR Removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "nul_prd.no_nmd.gt10",
    cell: "NUL_PRD.no_nmd.gt10",
    group: NMD_NOT_PREDICTED,
    label:
      "Removes/alters >10% of protein OR Removes/alters a region with some evidence in the Molecular Mechanism",
  },
  {
    id: "nul_prd.no_nmd.lt10",
    cell: "NUL_PRD.no_nmd.lt10",
    group: NMD_NOT_PREDICTED,
    label:
      "Removes/alters <10% of protein OR Role of region in protein function is unknown",
  },
];

function NulPrdBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <ChoiceRows
      name="nul_prd"
      cells={NUL_PRD}
      value={readField(assessment, "nul_prd")}
      onChange={(cell) =>
        onChange(withField(assessment, { ...cell, id: "nul_prd" }, cell.id))
      }
      onBlur={onBlur}
    />
  );
}

const NUL_FXN: Cell[] = [
  {
    id: "nul_fxn.yes",
    cell: "NUL_FXN.assay_consistent_with_controls",
    label:
      "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
  },
  { id: "nul_fxn.no", cell: "NUL_FXN.no", label: "No" },
];

function NulFxnBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <ChoiceRows
      name="nul_fxn"
      cells={NUL_FXN}
      value={readField(assessment, "nul_fxn")}
      onChange={(cell) =>
        onChange(withField(assessment, { ...cell, id: "nul_fxn" }, cell.id))
      }
      onBlur={onBlur}
    />
  );
}

// --- NUL_INF: three informative-variant tables, one per branch of the predicted-effect tree -------
//
// The calculator titles all three "Workflow for Informative Variants"; what separates them is the
// description each spans over its counted rows, which names the variants the table counts.

const EXON_NMD_PLP =
  "P/LP variant in this exon for the same MDE predicted to lead transcript to NMD";
const EXON_NMD_BLB =
  "B/LB variant in this exon predicted to lead transcript to NMD";

const NUL_INF_EXON_NMD: Cell[] = [
  {
    id: "nul_inf_exon_nmd.p_first",
    cell: "NUL_INF.exon_nmd.p_first",
    group: EXON_NMD_PLP,
    label: "First P Variant",
  },
  {
    id: "nul_inf_exon_nmd.lp_first",
    cell: "NUL_INF.exon_nmd.lp_first",
    group: EXON_NMD_PLP,
    label: "First LP Variant",
  },
  {
    id: "nul_inf_exon_nmd.plp_additional",
    cell: "NUL_INF.exon_nmd.plp_additional",
    group: EXON_NMD_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "nul_inf_exon_nmd.none",
    cell: "NUL_INF.exon_nmd.none",
    label: "No informative variants in this exon",
  },
  {
    id: "nul_inf_exon_nmd.b_first",
    cell: "NUL_INF.exon_nmd.b_first",
    group: EXON_NMD_BLB,
    label: "First B Variant",
  },
  {
    id: "nul_inf_exon_nmd.lb_first",
    cell: "NUL_INF.exon_nmd.lb_first",
    group: EXON_NMD_BLB,
    label: "First LB Variant test",
  },
  {
    id: "nul_inf_exon_nmd.blb_additional",
    cell: "NUL_INF.exon_nmd.blb_additional",
    group: EXON_NMD_BLB,
    label: "Additional B/LB variants",
  },
];

const ALT_MET_START_PLP =
  "P/LP variant for the same MDE between VBC and alternative Met start codon";
const ALT_MET_START_BLB =
  "Bengin / Likely Benign variant for the same MDE between VBC and alternative Met start codon";

const NUL_INF_ALT_MET_START: Cell[] = [
  {
    id: "nul_inf_alt_met_start.p_first",
    cell: "NUL_INF.alt_met_start.p_first",
    group: ALT_MET_START_PLP,
    label: "First P Variant",
  },
  {
    id: "nul_inf_alt_met_start.lp_first",
    cell: "NUL_INF.alt_met_start.lp_first",
    group: ALT_MET_START_PLP,
    label: "First LP Variant",
  },
  {
    id: "nul_inf_alt_met_start.plp_additional",
    cell: "NUL_INF.alt_met_start.plp_additional",
    group: ALT_MET_START_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "nul_inf_alt_met_start.vus",
    cell: "NUL_INF.alt_met_start.vus",
    label: "VUS variants between VBC and alternative Met for the same MDE",
  },
  {
    id: "nul_inf_alt_met_start.b_first",
    cell: "NUL_INF.alt_met_start.b_first",
    group: ALT_MET_START_BLB,
    label: "First B Variant",
  },
  {
    id: "nul_inf_alt_met_start.lb_first",
    cell: "NUL_INF.alt_met_start.lb_first",
    group: ALT_MET_START_BLB,
    label: "First LB Variant test",
  },
  {
    id: "nul_inf_alt_met_start.blb_additional",
    cell: "NUL_INF.alt_met_start.blb_additional",
    group: ALT_MET_START_BLB,
    label: "Additional B/LB variants",
  },
];

const EXON_PTC_PLP =
  "P/LP variant in this exon resulting in PTC, downstream of VBC";
const EXON_PTC_BLB =
  "Bengin / Likely Benign variant in this exon resulting in PTC, upstream of VBC";

const NUL_INF_EXON_PTC: Cell[] = [
  {
    id: "nul_inf_exon_ptc.p_first",
    cell: "NUL_INF.exon_ptc.p_first",
    group: EXON_PTC_PLP,
    label: "First P Variant",
  },
  {
    id: "nul_inf_exon_ptc.lp_first",
    cell: "NUL_INF.exon_ptc.lp_first",
    group: EXON_PTC_PLP,
    label: "First LP Variant",
  },
  {
    id: "nul_inf_exon_ptc.plp_additional",
    cell: "NUL_INF.exon_ptc.plp_additional",
    group: EXON_PTC_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "nul_inf_exon_ptc.vus",
    cell: "NUL_INF.exon_ptc.vus",
    label: "VUS informative variants in this exon",
  },
  {
    id: "nul_inf_exon_ptc.b_first",
    cell: "NUL_INF.exon_ptc.b_first",
    group: EXON_PTC_BLB,
    label: "First B Variant",
  },
  {
    id: "nul_inf_exon_ptc.lb_first",
    cell: "NUL_INF.exon_ptc.lb_first",
    group: EXON_PTC_BLB,
    label: "First LB Variant test",
  },
  {
    id: "nul_inf_exon_ptc.blb_additional",
    cell: "NUL_INF.exon_ptc.blb_additional",
    group: EXON_PTC_BLB,
    label: "Additional B/LB variants",
  },
];

export const NONSENSE_WORKFLOWS: WorkflowDef[] = [
  {
    id: "nul_prd",
    code: "NUL_PRD",
    title: "Predicted Effect Workflow for Nonsense variant with LoF mechanism",
    cells: NUL_PRD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.NONSENSE,
    Body: NulPrdBody,
  },
  {
    id: "nul_fxn",
    code: "NUL_FXN",
    title: "Functional Assessment for Nonsense variant with LoF mechanism",
    cells: NUL_FXN,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.NONSENSE,
    Body: NulFxnBody,
  },
  {
    id: "nul_inf_exon_nmd",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells: NUL_INF_EXON_NMD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.NONSENSE,
    Body: countBody(NUL_INF_EXON_NMD, "Applicable variants"),
  },
  {
    id: "nul_inf_alt_met_start",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells: NUL_INF_ALT_MET_START,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.NONSENSE,
    Body: countBody(NUL_INF_ALT_MET_START, "Applicable variants"),
  },
  {
    id: "nul_inf_exon_ptc",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells: NUL_INF_EXON_PTC,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.NONSENSE,
    Body: countBody(NUL_INF_EXON_PTC, "Applicable variants"),
  },
];
