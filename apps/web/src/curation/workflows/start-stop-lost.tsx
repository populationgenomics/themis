"use client";

import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import { type Cell, ChoiceRows, readField, withField } from "../ui/primitives";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Start-lost and stop-lost predicted and functional effect, transcribed from the ClinGen Pilot
// Calculator.
//
// One predicted-effect table spans two evidence families: the branch a row sits under selects `NUL_`
// (no rescue start codon; non-stop decay) or `CDS_` (the alternative start is used; the protein
// extends), which is why the calculator leaves the evidence code of these tables blank and each cell
// carries the family of its own branch.

const ALT_MET =
  "Potential inframe alternative MET start codon and there are no known P/LP variants between VBC and alt MET";

const FUNCTIONAL_ALT_START =
  "There is evidence that an alternative inframe start produces functional protein";

const START_LOST_PRD: Cell[] = [
  {
    id: "start_lost_prd.no_alt_met",
    cell: "NUL_PRD.no_alt_met.absent_protein",
    group:
      "No alternate inframe MET start codon(s) OR there is an alt MET start codon but there are known P/LP LoF variants between VBC and alt MET",
    label: "Predicted to result in non funtcion or absent protein",
  },
  {
    id: "start_lost_prd.alt_met.gt50",
    cell: "CDS_PRD.alt_met.gt50",
    group: ALT_MET,
    label:
      "Removes/alters >50% of protein OR Use of alternative start removes/alters an entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "start_lost_prd.alt_met.gt25",
    cell: "CDS_PRD.alt_met.gt25",
    group: ALT_MET,
    label:
      "Removes/alters >25% of protein OR Use of alternative start removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "start_lost_prd.alt_met.gt10",
    cell: "CDS_PRD.alt_met.gt10",
    group: ALT_MET,
    label:
      "Removes/alters >10% of protein OR Use of alternative start removes/alters a functional domain with some evidence in the Molecular Mechanism",
  },
  {
    id: "start_lost_prd.alt_met.lt10",
    cell: "CDS_PRD.alt_met.lt10",
    group: ALT_MET,
    label:
      "Removes/alters <10% of protein OR Use of alternative start impacts region with unknown or no known function in Molecular Mechanism",
  },
  {
    id: "start_lost_prd.functional_alt_start.no_data",
    cell: "CDS_PRD.functional_alt_start.no_data",
    group: FUNCTIONAL_ALT_START,
    label:
      "No functional data available for alternative start codon protein product",
  },
  {
    id: "start_lost_prd.functional_alt_start.retains_function",
    cell: "CDS_PRD.functional_alt_start.retains_function",
    group: FUNCTIONAL_ALT_START,
    label:
      "Functional data shows that a shorter protein using the alternative inframe start downstream of VBC retains function as compared to the full length transcript",
  },
];

const NSD_NOT_PREDICTED = "Non–stop Decay is NOT Predicted or is Unknown";

const STOP_LOST_PRD: Cell[] = [
  {
    id: "stop_lost_prd.nsd_predicted",
    cell: "NUL_PRD.nsd_predicted.removes_100",
    group:
      "Non–stop Decay is Predicted (PolyA site is encountered before an in-frame stop is encountered)",
    label: "Non–stop decay predicted, removes 100% of protein",
  },
  {
    id: "stop_lost_prd.nsd_not_predicted.experimentally_implicated",
    cell: "CDS_PRD.nsd_not_predicted.experimentally_implicated",
    group: NSD_NOT_PREDICTED,
    label:
      "Interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus has been experimentally implicated in the molecular mechanism",
  },
  {
    id: "stop_lost_prd.nsd_not_predicted.some_evidence_and_extension",
    cell: "CDS_PRD.nsd_not_predicted.some_evidence_and_extension",
    group: NSD_NOT_PREDICTED,
    label:
      "There is some evidence supporting interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus as a Molecular Mechanism AND Predicted amino acid extension is >30 codons",
  },
  {
    id: "stop_lost_prd.nsd_not_predicted.some_evidence_or_extension",
    cell: "CDS_PRD.nsd_not_predicted.some_evidence_or_extension",
    group: NSD_NOT_PREDICTED,
    label:
      "There is some evidence supporting interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus as a Molecular Mechanism OR Predicted amino acid extension is >30 codons",
  },
  {
    id: "stop_lost_prd.nsd_not_predicted.unknown_function",
    cell: "CDS_PRD.nsd_not_predicted.unknown_function",
    group: NSD_NOT_PREDICTED,
    label:
      "Addition of non native amino acids to the carboxy terminus has unknown or no known function in the Molecular Mechanism",
  },
];

// The functional table is one wording per class, and the calculator prints its evidence code as the
// `__` placeholder: which family scores it follows the branch taken above.

function functionalCells(
  workflowId: string,
  code: string,
  path: string,
): Cell[] {
  return [
    {
      id: `${workflowId}.yes`,
      cell: `${code}.${path}.assay_consistent_with_controls`,
      label:
        "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
    },
    { id: `${workflowId}.no`, cell: `${code}.${path}.no`, label: "No" },
  ];
}

const START_LOST_FXN = functionalCells(
  "start_lost_fxn",
  "CDS_FXN",
  "start_loss",
);
const STOP_LOST_FXN = functionalCells("stop_lost_fxn", "CDS_FXN", "stop_loss");

const NSD_PLP = "P/LP stop loss variant also predicted to result in NSD";
const NSD_BLB = "B/LB stop loss variant also predicted to result in NSD";

const STOP_LOST_INF_NSD: Cell[] = [
  {
    id: "stop_lost_inf_nsd.p_first",
    cell: "NUL_INF.stop_loss_nsd.p_first",
    group: NSD_PLP,
    label: "First P Variant",
  },
  {
    id: "stop_lost_inf_nsd.lp_first",
    cell: "NUL_INF.stop_loss_nsd.lp_first",
    group: NSD_PLP,
    label: "First LP Variant",
  },
  {
    id: "stop_lost_inf_nsd.plp_additional",
    cell: "NUL_INF.stop_loss_nsd.plp_additional",
    group: NSD_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "stop_lost_inf_nsd.vus",
    cell: "NUL_INF.stop_loss_nsd.vus",
    label: "VUS informative variants in this exon",
  },
  {
    id: "stop_lost_inf_nsd.b_first",
    cell: "NUL_INF.stop_loss_nsd.b_first",
    group: NSD_BLB,
    label: "First B Variant",
  },
  {
    id: "stop_lost_inf_nsd.lb_first",
    cell: "NUL_INF.stop_loss_nsd.lb_first",
    group: NSD_BLB,
    label: "First LB Variant test",
  },
  {
    id: "stop_lost_inf_nsd.blb_additional",
    cell: "NUL_INF.stop_loss_nsd.blb_additional",
    group: NSD_BLB,
    label: "Additional B/LB variants",
  },
];

const EXTENSION_PLP =
  "P/LP stop loss variant not predicted to result in NSD but predicted to result in an elongation with similar impact as VBC";
const EXTENSION_BLB =
  "B/LB stop loss variant not predicted to result in NSD but predicted to result in an elongation with similar impact as VBC";

const STOP_LOST_INF_EXTENSION: Cell[] = [
  {
    id: "stop_lost_inf_extension.p_first",
    cell: "CDS_INF.stop_loss_extension.p_first",
    group: EXTENSION_PLP,
    label: "First P Variant",
  },
  {
    id: "stop_lost_inf_extension.lp_first",
    cell: "CDS_INF.stop_loss_extension.lp_first",
    group: EXTENSION_PLP,
    label: "First LP Variant",
  },
  {
    id: "stop_lost_inf_extension.plp_additional",
    cell: "CDS_INF.stop_loss_extension.plp_additional",
    group: EXTENSION_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "stop_lost_inf_extension.vus",
    cell: "CDS_INF.stop_loss_extension.vus",
    label: "VUS informative variants in this exon",
  },
  {
    id: "stop_lost_inf_extension.b_first",
    cell: "CDS_INF.stop_loss_extension.b_first",
    group: EXTENSION_BLB,
    label: "First B Variant",
  },
  {
    id: "stop_lost_inf_extension.lb_first",
    cell: "CDS_INF.stop_loss_extension.lb_first",
    group: EXTENSION_BLB,
    label: "First LB Variant test",
  },
  {
    id: "stop_lost_inf_extension.blb_additional",
    cell: "CDS_INF.stop_loss_extension.blb_additional",
    group: EXTENSION_BLB,
    label: "Additional B/LB variants",
  },
];

/** One table, one selection: the calculator's rows are a single radio group, branch rows included. */
function choiceBody(fieldId: string, cells: Cell[]) {
  return function Body({ assessment, onChange, onBlur }: WorkflowBodyProps) {
    return (
      <ChoiceRows
        name={fieldId}
        cells={cells}
        value={readField(assessment, fieldId)}
        onChange={(cell) =>
          onChange(withField(assessment, { ...cell, id: fieldId }, cell.id))
        }
        onBlur={onBlur}
      />
    );
  };
}

export const START_STOP_LOST_WORKFLOWS: WorkflowDef[] = [
  {
    id: "start_lost_prd",
    code: "CDS_PRD",
    title: "Predicted Effect Workflow for Start Loss Variant",
    cells: START_LOST_PRD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.START_LOST,
    Body: choiceBody("start_lost_prd", START_LOST_PRD),
  },
  {
    id: "start_lost_fxn",
    code: "CDS_FXN",
    title: "Functional Assessment for Start Loss Variant",
    cells: START_LOST_FXN,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.START_LOST,
    Body: choiceBody("start_lost_fxn", START_LOST_FXN),
  },
  {
    id: "stop_lost_prd",
    code: "CDS_PRD",
    title: "Predicted Effect Workflow for Stop Loss Variant",
    cells: STOP_LOST_PRD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.STOP_LOST,
    Body: choiceBody("stop_lost_prd", STOP_LOST_PRD),
  },
  {
    id: "stop_lost_fxn",
    code: "CDS_FXN",
    title: "Functional Assessment for Stop Loss Variant",
    cells: STOP_LOST_FXN,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.STOP_LOST,
    Body: choiceBody("stop_lost_fxn", STOP_LOST_FXN),
  },
  {
    id: "stop_lost_inf_nsd",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    applicability: "Informative Variants",
    cells: STOP_LOST_INF_NSD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.STOP_LOST,
    Body: countBody(STOP_LOST_INF_NSD, "Applicable variants"),
  },
  {
    id: "stop_lost_inf_extension",
    code: "CDS_INF",
    title: "Workflow for Informative Variants",
    applicability: "Informative Variants",
    cells: STOP_LOST_INF_EXTENSION,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.STOP_LOST,
    Body: countBody(STOP_LOST_INF_EXTENSION, "Applicable variants"),
  },
];
