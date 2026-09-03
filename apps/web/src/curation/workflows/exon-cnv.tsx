"use client";

import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  CountRows,
  readField,
  withField,
} from "../ui/primitives";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Deletion and duplication of one or more exons, transcribed from the ClinGen Pilot Calculator.
//
// Each predicted-effect table spans two code families — a path ending in loss of the transcript is
// scored NUL_, one leaving a coding product CDS_ — which is why the calculator leaves the evidence
// code of these blocks blank and why a cell name carries the family of its own row.

/** Rows the calculator spans one criterion over, each with its own Region Information. */
function regionRows(
  fieldPrefix: string,
  cellPrefix: string,
  group: string,
  rows: { id: string; label: string }[],
): Cell[] {
  return rows.map((row) => ({
    id: `${fieldPrefix}.${row.id}`,
    cell: `${cellPrefix}.${row.id}`,
    group,
    label: row.label,
  }));
}

/** The informative-variant table: a pathogenic criterion spanning its three counted variants, a row
 *  that scores nothing, and a benign criterion spanning its three. */
function informativeRows(
  fieldPrefix: string,
  cellPrefix: string,
  headings: {
    pathogenic: string;
    middle: { id: string; label: string };
    benign: string;
  },
): Cell[] {
  return [
    {
      id: `${fieldPrefix}.p_first`,
      cell: `${cellPrefix}.p_first`,
      group: headings.pathogenic,
      label: "First P Variant",
    },
    {
      id: `${fieldPrefix}.lp_first`,
      cell: `${cellPrefix}.lp_first`,
      group: headings.pathogenic,
      label: "First LP Variant",
    },
    {
      id: `${fieldPrefix}.plp_additional`,
      cell: `${cellPrefix}.plp_additional`,
      group: headings.pathogenic,
      label: "Additional P/LP variants",
    },
    {
      id: `${fieldPrefix}.${headings.middle.id}`,
      cell: `${cellPrefix}.${headings.middle.id}`,
      label: headings.middle.label,
    },
    {
      id: `${fieldPrefix}.b_first`,
      cell: `${cellPrefix}.b_first`,
      group: headings.benign,
      label: "First B Variant",
    },
    {
      id: `${fieldPrefix}.lb_first`,
      cell: `${cellPrefix}.lb_first`,
      group: headings.benign,
      label: "First LB Variant test",
    },
    {
      id: `${fieldPrefix}.blb_additional`,
      cell: `${cellPrefix}.blb_additional`,
      group: headings.benign,
      label: "Additional B/LB variants",
    },
  ];
}

/** A single-select table, the selected row stored under the workflow's own field. */
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

// --- Functional assessment (one wording, both classes) ------------------------------------------

function functionalCells(fieldPrefix: string, code: string): Cell[] {
  return [
    {
      id: `${fieldPrefix}.yes`,
      cell: `${code}.assay_consistent_with_controls`,
      label:
        "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
    },
    { id: `${fieldPrefix}.no`, cell: `${code}.no`, label: "No" },
  ];
}

const DEL_FXN = functionalCells("del_fxn", "NUL_FXN/CDS_FXN.del");
const DUP_FXN = functionalCells("dup_fxn", "NUL_FXN/CDS_FXN.dup");

// --- Deletion of one or more exon(s) ------------------------------------------------------------

const DEL_NO_NMD =
  "Single or multi exon deletion disrupts reading frame but NMD is NOT predicted OR Single or multi exon deletion has no impact on reading frame";

const DEL_ALTERNATE_START =
  "Upstream or downstream inframe start predicted and is not in deleted region";

const DEL_PRD: Cell[] = [
  {
    id: "del_prd.whole_gene",
    cell: "NUL_PRD.whole_gene.removes_100",
    group: "Whole gene deletion",
    label: "Removes 100% of protein Only Molecular Mechanism Score is applied",
  },
  {
    id: "del_prd.nmd_predicted",
    cell: "NUL_PRD.nmd_predicted.removes_100",
    group:
      "Single or multi exon deletion disrupts reading frame AND Introduced PTC is more than 50nt upstream of the last exon-exon boundary (NMD-predicted)",
    label: "Removes 100% of protein",
  },
  ...regionRows("del_prd.no_nmd", "CDS_PRD.nmd_not_predicted", DEL_NO_NMD, [
    { id: "noncoding", label: "Deleted exon is noncoding" },
    {
      id: "gt50",
      label:
        "Removes/alters >50% of protein OR removes/alters an entire critical functional domain that has been experimentally implicated in the disease mechanism",
    },
    {
      id: "gt25",
      label:
        "Removes/alters >25% of protein OR removes/alters a portion of a critical functional domain that has been experimentally implicated in the disease mechanism",
    },
    {
      id: "gt10",
      label:
        "Removes/alters >10% of protein OR removes/alters a functional domain with some evidence in the disease mechanism",
    },
    {
      id: "lt10",
      label:
        "Removes/alters <10% of protein OR impacts region with unknown or no known function in disease mechanism",
    },
  ]),
  {
    id: "del_prd.no_alternate_start",
    cell: "NUL_PRD.no_alternate_start.no_protein",
    group:
      "No alternate inframe start OR Out of frame alternate start predicted",
    label: "No translated protein predicted",
  },
  ...regionRows(
    "del_prd.alternate_start",
    "CDS_PRD.alternate_start",
    DEL_ALTERNATE_START,
    [
      {
        id: "gt50",
        label:
          "Removes/alters >50% of protein OR Use of alternative start removes/alters an entire critical functional domain that has been experimentally implicated in the disease mechanism",
      },
      {
        id: "gt25",
        label:
          "Removes/alters >25% of protein OR Use of alternative start removes/alters a portion of a critical functional domain that has been experimentally implicated in the disease mechanism",
      },
      {
        id: "gt10",
        label:
          "Removes/alters >10% of protein OR Use of alternative start removes/alters a functional domain with some evidence in the disease mechanism",
      },
      {
        id: "lt10",
        label:
          "Removes/alters <10% of protein OR Use of alternative start impacts region with unknown or no known function in disease mechanism",
      },
    ],
  ),
  {
    id: "del_prd.functional_alternate_start",
    cell: "CDS_PRD.functional_alternate_start.rescue",
    group:
      "Upstream or downstream inframe start used in alternate functional transcripts and not in deleted region",
    label: "Rescue predicted, functional protein translated",
  },
];

const DEL_INF = informativeRows("del_inf", "NUL_INF", {
  pathogenic:
    "Pathogenic / Likely Pathogenic variant in this exon predicted to lead transcript to NMD",
  middle: { id: "none", label: "No informative variants in this exon" },
  benign:
    "Benign / Likely Benign variant in this exon predicted to lead transcript to NMD",
});

// --- Duplication or gain of one or more exon(s) -------------------------------------------------

const DUP_PARTIAL_GENE = "Affects Partial Gene";

const DUP_TANDEM_IN_CDS = `${DUP_PARTIAL_GENE} — VBC proved tandem AND Both breakpoints of the VBC are inside the start and end points of the CDS`;

const DUP_NOT_TANDEM_IN_CDS = `${DUP_PARTIAL_GENE} — VBC is NOT a proved tandem AND Both breakpoints of the VBC are inside the start and end points of the CDS`;

const DUP_NO_NMD =
  "Single or Multi exon duplication not predicted to disrupt reading frame OR Single or Multi exon duplication predicted to disrupt reading frame but introduced PTC is not >50 nt Upstream of last exon–exon boundary (NMD not predicted)";

const DUP_FRACTIONS = [
  {
    id: "gt50",
    label:
      "Duplicates >50% of protein OR Disrupts critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "gt25",
    label:
      "Duplicates >25% of protein OR Alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "gt10",
    label:
      "Duplicates >10% of protein OR Alters a region with some evidence in the Molecular Mechanism",
  },
  {
    id: "lt10",
    label:
      "Duplicates <10% of protein OR Role of region in protein function is unknown",
  },
];

const DUP_PRD: Cell[] = [
  {
    id: "dup_prd.tandem_cds.nmd",
    cell: "NUL_PRD.tandem_cds.nmd.removes_100",
    group: `${DUP_TANDEM_IN_CDS} — Single or Multi Exon Duplication Predicted to Disrupt Reading Frame AND Introduced PTC >50 nt Upstream of Last Exon–Exon Boundary (NMD Predicted)`,
    label: "Removes 100% of protein",
  },
  ...regionRows(
    "dup_prd.tandem_cds.no_nmd",
    "CDS_PRD.tandem_cds.no_nmd",
    `${DUP_TANDEM_IN_CDS} — ${DUP_NO_NMD}`,
    DUP_FRACTIONS,
  ),
  {
    id: "dup_prd.tandem_outside_cds",
    cell: "CDS_PRD.tandem_outside_cds.unlikely_lof",
    group: `${DUP_PARTIAL_GENE} — VBC proved tandem AND Both breakpoints of the VBC are not inside the start or end points of the CDS`,
    label: "Unlikely to lead to LoF",
  },
  {
    id: "dup_prd.not_tandem_cds.nmd",
    cell: "NUL_PRD.not_tandem_cds.nmd.removes_100",
    group: `${DUP_NOT_TANDEM_IN_CDS} — Single or multi exon duplication predicted to disrupt reading frame AND Introduced PTC >50 nt upstream of last exon–exon boundary (NMD predicted)`,
    label: "Removes 100% of protein",
  },
  ...regionRows(
    "dup_prd.not_tandem_cds.no_nmd",
    "CDS_PRD.not_tandem_cds.no_nmd",
    `${DUP_NOT_TANDEM_IN_CDS} — ${DUP_NO_NMD}`,
    DUP_FRACTIONS,
  ),
  {
    id: "dup_prd.not_tandem_outside_cds",
    cell: "CDS_PRD.not_tandem_outside_cds.unlikely_lof",
    group: `${DUP_PARTIAL_GENE} — VBC is NOT a proved tandem AND Both breakpoints of the VBC are not inside the start or end points of the CDS`,
    label: "Unlikely to lead to LoF",
  },
  {
    id: "dup_prd.whole_gene",
    cell: "CDS_PRD.whole_gene.unlikely_lof",
    group: "Whole Gene",
    label: "Unlikely to lead to LoF Consult dosage sensitivity map",
  },
];

const DUP_VUS = { id: "vus", label: "VUS informative variants" };

const DUP_INF_NMD_OVERLAP = informativeRows(
  "dup_inf_nmd_overlap",
  "NUL_INF.nmd_overlap",
  {
    pathogenic: "P/LP variant resulting in NMD overlap some exons",
    middle: DUP_VUS,
    benign: "B/LB variant resulting in a similarly altered/duplicated region",
  },
);

const DUP_INF_SIMILAR_REGION = informativeRows(
  "dup_inf_similar_region",
  "CDS_INF.similar_region",
  {
    pathogenic:
      "P/LP variant resulting in a similarly altered/duplicated region",
    middle: DUP_VUS,
    benign: "B/LB variant resulting in a similarly altered/duplicated region",
  },
);

const DUP_INF_TANDEM = informativeRows(
  "dup_inf_tandem",
  "NUL_INF/CDS_INF.tandem_confirmed",
  {
    pathogenic:
      "P/LP duplication of the same exons as VBC has been confirmed to be in tandem in prior cases",
    middle: DUP_VUS,
    benign:
      "B/LB duplication of the same exons as VBC has been confirmed to be in tandem in prior cases",
  },
);

const DUP_INF_BENIGN_RECONSIDER: Cell[] = [
  {
    id: "dup_inf_benign.reconsider",
    cell: "CDS_INF.benign_only.reconsider",
    group: "P/LP Variants Exist Resulting in Similarly Altered/Removed Region",
    label: "Reconsider Evidence",
  },
];

const DUP_INF_BENIGN_TANDEM =
  "B/LB duplication of the same exons as VBC has been confirmed to be tandem in prior cases";

const DUP_INF_BENIGN_COUNTS: Cell[] = [
  {
    id: "dup_inf_benign.none",
    cell: "CDS_INF.benign_only.none",
    label: "No informative variants in this exon",
  },
  {
    id: "dup_inf_benign.b_first",
    cell: "CDS_INF.benign_only.b_first",
    group: DUP_INF_BENIGN_TANDEM,
    label: "First B Variant",
  },
  {
    id: "dup_inf_benign.lb_first",
    cell: "CDS_INF.benign_only.lb_first",
    group: DUP_INF_BENIGN_TANDEM,
    label: "First LB Variant test",
  },
  {
    id: "dup_inf_benign.blb_additional",
    cell: "CDS_INF.benign_only.blb_additional",
    group: DUP_INF_BENIGN_TANDEM,
    label: "Additional B/LB variants",
  },
];

/** The benign-only informative table: the calculator heads it with a lone radio that sends the
 *  curator back to the predicted-effect path, and the rows under it take counts. */
function DupInfBenignBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="dup_inf_benign_path"
        cells={DUP_INF_BENIGN_RECONSIDER}
        value={readField(assessment, "dup_inf_benign.path")}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "dup_inf_benign.path" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      <CountRows
        cells={DUP_INF_BENIGN_COUNTS}
        assessment={assessment}
        onChange={onChange}
        countLabel="Applicable variants"
        onBlur={onBlur}
      />
    </div>
  );
}

export const EXON_CNV_WORKFLOWS: WorkflowDef[] = [
  {
    id: "del_prd",
    code: "NUL_PRD/CDS_PRD",
    title: "Predicted Effect Workflow for Deletion of one or more exon(s)",
    cells: DEL_PRD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DELETION,
    Body: choiceBody("del_prd", DEL_PRD),
  },
  {
    id: "del_fxn",
    code: "NUL_FXN/CDS_FXN",
    title: "Functional Assessment for Gross Duplication/Insertion",
    cells: DEL_FXN,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DELETION,
    Body: choiceBody("del_fxn", DEL_FXN),
  },
  {
    id: "del_inf",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells: DEL_INF,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DELETION,
    Body: countBody(DEL_INF, "Applicable variants"),
  },
  {
    id: "dup_prd",
    code: "NUL_PRD/CDS_PRD",
    title: "Duplication or Gain Known to Affect One Gene",
    cells: DUP_PRD,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: choiceBody("dup_prd", DUP_PRD),
  },
  {
    id: "dup_fxn",
    code: "NUL_FXN/CDS_FXN",
    title: "Functional Assessment for Gross Duplication/Insertion",
    cells: DUP_FXN,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: choiceBody("dup_fxn", DUP_FXN),
  },
  {
    id: "dup_inf_nmd_overlap",
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells: DUP_INF_NMD_OVERLAP,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: countBody(DUP_INF_NMD_OVERLAP, "Applicable variants"),
  },
  {
    id: "dup_inf_similar_region",
    code: "CDS_INF",
    title: "Workflow for Informative Variants",
    cells: DUP_INF_SIMILAR_REGION,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: countBody(DUP_INF_SIMILAR_REGION, "Applicable variants"),
  },
  {
    id: "dup_inf_tandem",
    code: "NUL_INF/CDS_INF",
    title: "Workflow for Informative Variants",
    cells: DUP_INF_TANDEM,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: countBody(DUP_INF_TANDEM, "Applicable variants"),
  },
  {
    id: "dup_inf_benign",
    code: "CDS_INF",
    title: "Workflow for Informative Variants",
    cells: [...DUP_INF_BENIGN_RECONSIDER, ...DUP_INF_BENIGN_COUNTS],
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.EXON_DUPLICATION,
    Body: DupInfBenignBody,
  },
];
