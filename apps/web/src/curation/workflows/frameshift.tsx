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

// Frameshifting Indel (smaller than 1 exon), transcribed from the ClinGen Pilot Calculator: the
// absent-protein chain the class prints in its own wording — the prediction table (NUL_PRD), the
// functional assessment (NUL_FXN), and the five informative-variant tables (NUL_INF) it prints
// under one heading.

const isFrameshift = ({ consequenceClass }: Routing) =>
  consequenceClass === Consequence.FRAMESHIFT;

// --- NUL_PRD: predicted effect ------------------------------------------------------------------

const NMD_NO_ALTERNATE_START =
  "Introduced PTC is >50 nt upstream of the last exon-exon boundary AND ( NMD predicted ) No known evidence for alternate functional start codon 3' of VBC";
const NMD_ALTERNATE_START =
  "Introduced PTC is >50 nt upstream of the last exon-exon boundary AND ( NMD predicted ) Putative alternative start codon 3' of VBC with functional or genetic evidence";
const NO_NMD =
  "Introduced PTC located within the last or only exon, gene documented to not undergo NMD, or PTC within 50 nt upstream of the last exon–exon boundary (even if non coding). In all cases, introduced PTC occurs upstream of normal stop codon AND ( NMD is not predicted )";
const NSD =
  "Introduced PTC is located 3' of the native stop codon (i.e., frameshift variant predicts elongation) Non–stop Decay is Predicted (PolyA site is encountered before an in-frame stop is encountered)";
const NO_NSD =
  "Introduced PTC is located 3' of the native stop codon (i.e., frameshift variant predicts elongation) Non–stop Decay is not Predicted OR is unknown";

function prdCells(
  branch: string,
  group: string,
  rows: [string, string][],
): Cell[] {
  return rows.map(([row, label]) => ({
    id: `frame_nul_prd.${branch}.${row}`,
    cell: `NUL_PRD.frame_${branch}.${row}`,
    group,
    label,
  }));
}

const NUL_PRD: Cell[] = [
  ...prdCells("nmd", NMD_NO_ALTERNATE_START, [
    ["removes_100", "NMD–predicted, removes 100% of protein"],
  ]),
  ...prdCells("alternate_start", NMD_ALTERNATE_START, [
    [
      "retains_function",
      "Functional data shows that a shorter protein using the alternative inframe start downstream of VBC retains function as compared to the full length transcript",
    ],
    [
      "gt50",
      "Removes/alters >50% of protein OR Use of alternative start removes/alters an entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
    ],
    [
      "gt25",
      "Removes/alters >25% of protein OR Use of alternative start removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
    ],
    [
      "gt10",
      "Removes/alters >10% of protein OR Use of alternative start removes/alters a region with some evidence in the Molecular Mechanism",
    ],
    [
      "lt10",
      "Removes/alters <10% of protein OR Use of alternative start impacts region with unknown or no known function in Molecular Mechanism",
    ],
  ]),
  ...prdCells("no_nmd", NO_NMD, [
    [
      "gt50",
      "Removes/alters >50% of protein OR Removes/alters entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
    ],
    [
      "gt25",
      "Removes/alters >25% of protein OR Removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
    ],
    [
      "gt10",
      "Removes/alters >10% of protein OR Removes/alters a region with some evidence in the Molecular Mechanism",
    ],
    [
      "lt10",
      "Removes/alters <10% of protein OR Role of region in protein function is unknown",
    ],
  ]),
  ...prdCells("nsd", NSD, [["removes_100", "Removes 100% of protein"]]),
  ...prdCells("no_nsd", NO_NSD, [
    [
      "experimentally_implicated",
      "Interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus has been experimentally implicated in the molecular mechanism",
    ],
    [
      "some_evidence_and_extension",
      "There is some evidence supporting interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus as a Molecular Mechanism AND Predicted amino acid extension is ≥30 codons past native stop codon",
    ],
    [
      "some_evidence_or_extension",
      "There is some evidence supporting interference of gene or protein function by the addition of non-native amino acids to the carboxy terminus as a Molecular Mechanism OR Predicted amino acid extension is ≥30 codons past native stop codon",
    ],
    [
      "unknown_function",
      "Addition of non native amino acids to the carboxy terminus has unknown or no known function in the Molecular Mechanism",
    ],
  ]),
];

function NulPrdBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="frame_nul_prd"
        cells={NUL_PRD}
        value={readField(assessment, "frame_nul_prd")}
        onChange={(cell) =>
          onChange(
            withField(assessment, { ...cell, id: "frame_nul_prd" }, cell.id),
          )
        }
        onBlur={onBlur}
      />
      <FrameworkNote>
        Abbreviations: PTC:Premature termination codon
      </FrameworkNote>
    </div>
  );
}

// --- NUL_FXN: functional assessment -------------------------------------------------------------

const NUL_FXN: Cell[] = [
  {
    id: "frame_nul_fxn.yes",
    cell: "NUL_FXN.frame.assay_consistent_with_controls",
    label:
      "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
  },
  { id: "frame_nul_fxn.no", cell: "NUL_FXN.frame.no", label: "No" },
];

function NulFxnBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <ChoiceRows
      name="frame_nul_fxn"
      cells={NUL_FXN}
      value={readField(assessment, "frame_nul_fxn")}
      onChange={(cell) =>
        onChange(
          withField(assessment, { ...cell, id: "frame_nul_fxn" }, cell.id),
        )
      }
      onBlur={onBlur}
    />
  );
}

// --- NUL_INF: informative variants ---------------------------------------------------------------
//
// Five tables under one heading, sharing their sub-rows and differing only in the two group cells
// that say which variants are informative.

function informativeCells(
  branch: string,
  pathogenic: string,
  benign: string,
): Cell[] {
  const address = (row: string) => ({
    id: `frame_nul_inf_${branch}.${row}`,
    cell: `NUL_INF.frame_${branch}.${row}`,
  });
  return [
    { ...address("p_first"), group: pathogenic, label: "First P Variant" },
    { ...address("lp_first"), group: pathogenic, label: "First LP Variant" },
    {
      ...address("plp_additional"),
      group: pathogenic,
      label: "Additional P/LP variants",
    },
    { ...address("none"), label: "No informative variants in this exon" },
    { ...address("b_first"), group: benign, label: "First B Variant" },
    {
      ...address("lb_first"),
      group: benign,
      label: "First LB Variant test",
    },
    {
      ...address("blb_additional"),
      group: benign,
      label: "Additional B/LB variants",
    },
  ];
}

function informativeWorkflow({
  branch,
  pathogenic,
  benign,
}: {
  branch: string;
  pathogenic: string;
  benign: string;
}): WorkflowDef {
  const cells = informativeCells(branch, pathogenic, benign);
  return {
    id: `frame_nul_inf_${branch}`,
    code: "NUL_INF",
    title: "Workflow for Informative Variants",
    cells,
    applies: isFrameshift,
    Body: countBody(cells, "Applicable Variants"),
  };
}

export const FRAMESHIFT_WORKFLOWS: WorkflowDef[] = [
  {
    id: "frame_nul_prd",
    code: "NUL_PRD",
    title: "Predicted Effect Workflow for Frameshift",
    cells: NUL_PRD,
    applies: isFrameshift,
    Body: NulPrdBody,
  },
  {
    id: "frame_nul_fxn",
    code: "NUL_FXN",
    title: "Functional Assessment for Frameshift",
    cells: NUL_FXN,
    applies: isFrameshift,
    Body: NulFxnBody,
  },
  informativeWorkflow({
    branch: "one",
    pathogenic:
      "P/LP variant in this exon (nt change) for the same MDE predicted to lead transcript to NMD",
    benign: "B/LB variant in this exon predicted to lead transcript to NMD",
  }),
  informativeWorkflow({
    branch: "two",
    pathogenic:
      "P/LP PTC variant for the same MDE occurs between the introduced PTC and the alternate start codon",
    benign:
      "PTC introducted by B/LB variant occurs upstream of alternate start codon",
  }),
  informativeWorkflow({
    branch: "three",
    pathogenic:
      "P/LP variant in this exon resulting in PTC that is between the frameshift and the normal stop codon. Similar impact",
    benign: "B/LB variant in this exon resulting in PTC, upstream of VBC.",
  }),
  informativeWorkflow({
    branch: "four",
    pathogenic:
      "P/LP stop loss or frameshift extension variant also predicted to result in NSD",
    benign: "B/LB variant also predicted to result in NSD",
  }),
  informativeWorkflow({
    branch: "five",
    pathogenic:
      "P/LP frameshift variant not predicted to result in NSD but predicted to result in an elongation with similar impact as VBC",
    benign:
      "B/LB frameshift variant not predicted to result in NSD but predicted to result in an elongation with similar impact as VBC",
  }),
];
