"use client";

import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  FrameworkNote,
  readField,
  withField,
} from "../ui/primitives";
import { countBody } from "./shared";
import type { Routing, WorkflowBodyProps, WorkflowDef } from "./types";

// In-frame Indel (smaller than 1 exon): the predicted-effect workflows the calculator prints for
// this consequence class alone. The mechanism matrix and the splice chain it also prints here are
// wordings it shares with other classes, and are transcribed once in predicted.tsx.

const REPETITIVE =
  "InDel is located in a repetitive region (>5 repetitive units) AND There is NO known Association of Disease and Repeat Length *";

const NOT_REPETITIVE =
  "InDel is NOT located in a repetitive region (>5 repetitive units)";

const REPEAT_FOOTNOTE =
  "* InDel is located in a repetitive region (>5 repetitive units) AND there is known Association of Disease and Repeat Length : follow ACMG Guideline For Classification of Repeat Pathogenicity";

const CDS_PRD: Cell[] = [
  {
    id: "cds_prd.repetitive.not_polymorphic",
    cell: "CDS_PRD.repetitive.not_polymorphic",
    group: REPETITIVE,
    label: "Region is not polymorphic",
  },
  {
    id: "cds_prd.repetitive.polymorphic",
    cell: "CDS_PRD.repetitive.polymorphic",
    group: REPETITIVE,
    label:
      "Repetitve region is polymorphic (mulitple InDels of various repeat lengths in gnomAD)",
  },
  {
    id: "cds_prd.not_repetitive.gt50",
    cell: "CDS_PRD.not_repetitive.gt50",
    group: NOT_REPETITIVE,
    label:
      "Removes/alters >50% of protein OR Removes entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "cds_prd.not_repetitive.gt25",
    cell: "CDS_PRD.not_repetitive.gt25",
    group: NOT_REPETITIVE,
    label:
      "Removes/alters >25% of protein OR Removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "cds_prd.not_repetitive.gt10",
    cell: "CDS_PRD.not_repetitive.gt10",
    group: NOT_REPETITIVE,
    label:
      "Removes/alters >10% of protein OR Removes/alters a functional domain with some evidence in the Molecular Mechanism OR Calibrated in silico inframe predictive tool suggests a damaging impact in the +2 score range",
  },
  {
    id: "cds_prd.not_repetitive.damaging",
    cell: "CDS_PRD.not_repetitive.damaging",
    group: NOT_REPETITIVE,
    label: "In silico inframe predictive tool suggests a damaging impact",
  },
  {
    id: "cds_prd.not_repetitive.indeterminate",
    cell: "CDS_PRD.not_repetitive.indeterminate",
    group: NOT_REPETITIVE,
    label:
      "In silico inframe predictive tool suggests indeterminate effect OR no in silico tool result is available",
  },
  {
    id: "cds_prd.not_repetitive.benign",
    cell: "CDS_PRD.not_repetitive.benign",
    group: NOT_REPETITIVE,
    label: "In silico inframe predictive tool suggests a benign impact",
  },
];

function CdsPrdBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="cds_prd"
        cells={CDS_PRD}
        value={readField(assessment, "cds_prd")}
        onChange={(cell) =>
          onChange(withField(assessment, { ...cell, id: "cds_prd" }, cell.id))
        }
        onBlur={onBlur}
      />
      <FrameworkNote>{REPEAT_FOOTNOTE}</FrameworkNote>
    </div>
  );
}

const CDS_FXN: Cell[] = [
  {
    id: "cds_fxn.yes",
    cell: "CDS_FXN.assay_consistent_with_controls",
    label:
      "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
  },
  { id: "cds_fxn.no", cell: "CDS_FXN.no", label: "No" },
];

function CdsFxnBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <ChoiceRows
      name="cds_fxn"
      cells={CDS_FXN}
      value={readField(assessment, "cds_fxn")}
      onChange={(cell) =>
        onChange(withField(assessment, { ...cell, id: "cds_fxn" }, cell.id))
      }
      onBlur={onBlur}
    />
  );
}

// The calculator prints two informative-variant tables under one heading and one evidence code: the
// first compares the length of the informative variant to the VBC, the second the region it alters.

const CDS_INF_ONE_PLP = "P/LP variant that is shorter than VBC";
const CDS_INF_ONE_BLB =
  "Benign / Likely Benign variant in this exon predicted to lead transcript to NMD";

const CDS_INF_ONE: Cell[] = [
  {
    id: "cds_inf_one.p_first",
    cell: "CDS_INF.one.p_first",
    group: CDS_INF_ONE_PLP,
    label: "First P Variant",
  },
  {
    id: "cds_inf_one.lp_first",
    cell: "CDS_INF.one.lp_first",
    group: CDS_INF_ONE_PLP,
    label: "First LP Variant",
  },
  {
    id: "cds_inf_one.plp_additional",
    cell: "CDS_INF.one.plp_additional",
    group: CDS_INF_ONE_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "cds_inf_one.vus",
    cell: "CDS_INF.one.vus",
    label:
      "VUS informative variants OR P/LP variants that are longer than the VBC OR B/LB variants that are shorter than the VBC",
  },
  {
    id: "cds_inf_one.b_first",
    cell: "CDS_INF.one.b_first",
    group: CDS_INF_ONE_BLB,
    label: "First B Variant",
  },
  {
    id: "cds_inf_one.lb_first",
    cell: "CDS_INF.one.lb_first",
    group: CDS_INF_ONE_BLB,
    label: "First LB Variant test",
  },
  {
    id: "cds_inf_one.blb_additional",
    cell: "CDS_INF.one.blb_additional",
    group: CDS_INF_ONE_BLB,
    label: "Additional B/LB variants",
  },
];

const CDS_INF_TWO_PLP =
  "P/LP variant resulting in similarly altered/removed region";
const CDS_INF_TWO_BLB =
  "B/LB variant resulting in similarly altered/removed region";

const CDS_INF_TWO: Cell[] = [
  {
    id: "cds_inf_two.p_first",
    cell: "CDS_INF.two.p_first",
    group: CDS_INF_TWO_PLP,
    label: "First P Variant",
  },
  {
    id: "cds_inf_two.lp_first",
    cell: "CDS_INF.two.lp_first",
    group: CDS_INF_TWO_PLP,
    label: "First LP Variant",
  },
  {
    id: "cds_inf_two.plp_additional",
    cell: "CDS_INF.two.plp_additional",
    group: CDS_INF_TWO_PLP,
    label: "Additional P/LP variants",
  },
  {
    id: "cds_inf_two.vus",
    cell: "CDS_INF.two.vus",
    label: "VUS informative variants",
  },
  {
    id: "cds_inf_two.b_first",
    cell: "CDS_INF.two.b_first",
    group: CDS_INF_TWO_BLB,
    label: "First B Variant",
  },
  {
    id: "cds_inf_two.lb_first",
    cell: "CDS_INF.two.lb_first",
    group: CDS_INF_TWO_BLB,
    label: "First LB Variant test",
  },
  {
    id: "cds_inf_two.blb_additional",
    cell: "CDS_INF.two.blb_additional",
    group: CDS_INF_TWO_BLB,
    label: "Additional B/LB variants",
  },
];

const isInframeIndel = ({ consequenceClass }: Routing) =>
  consequenceClass === Consequence.INFRAME_INDEL;

export const INFRAME_INDEL_WORKFLOWS: WorkflowDef[] = [
  {
    id: "cds_prd",
    code: "CDS_PRD",
    title: "Assess Protein Effect / Gene Product Prediction",
    cells: CDS_PRD,
    applies: isInframeIndel,
    Body: CdsPrdBody,
  },
  {
    id: "cds_fxn",
    code: "CDS_FXN",
    title: "Functional Assessment for Frameshift In-frame Indel",
    cells: CDS_FXN,
    applies: isInframeIndel,
    Body: CdsFxnBody,
  },
  {
    id: "cds_inf_one",
    code: "CDS_INF",
    title: "Workflow for Informative Variants",
    cells: CDS_INF_ONE,
    applies: isInframeIndel,
    Body: countBody(CDS_INF_ONE, "Applicable Variants"),
  },
  {
    id: "cds_inf_two",
    code: "CDS_INF",
    title: "Workflow for Informative Variants",
    cells: CDS_INF_TWO,
    applies: isInframeIndel,
    Body: countBody(CDS_INF_TWO, "Applicable Variants"),
  },
];
