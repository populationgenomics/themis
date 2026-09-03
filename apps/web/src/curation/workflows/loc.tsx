"use client";

import { Inheritance } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  FrameworkNote,
  readField,
  withField,
} from "../ui/primitives";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Locus specificity (LOC), transcribed from the ClinGen Pilot Calculator.

const RARITY_NOTE =
  "Note: For this workflow to the applicable, the variant must meet ‘rarity’ definition (POP_FRQ >= -1.0)";

const PHE_STEP1: Cell[] = [
  { id: "loc_phe.step1.no", cell: "LOC_PHE.step1.no", label: "No" },
  { id: "loc_phe.step1.yes", cell: "LOC_PHE.step1.yes", label: "Yes" },
];

const PHE_STEP2: Cell[] = [
  { id: "loc_phe.yield.ge_82", cell: "LOC_PHE.yield.ge_82", label: "≥82%" },
  { id: "loc_phe.yield.68_82", cell: "LOC_PHE.yield.68_82", label: "≥68-<82%" },
  { id: "loc_phe.yield.51_68", cell: "LOC_PHE.yield.51_68", label: "≥51-<68%" },
  { id: "loc_phe.yield.33_51", cell: "LOC_PHE.yield.33_51", label: "≥33-<51%" },
  { id: "loc_phe.yield.0_33", cell: "LOC_PHE.yield.0_33", label: "0-<33%" },
];

/** Two ordered steps rather than one table: the diagnostic yield is only asked once the case is
 *  observed with a phenotype specific for the gene, and the calculator prints them in that order. */
function LocPheBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  const step1 = readField(assessment, "loc_phe.step1");
  return (
    <div>
      <p className="framework-voice mt-1 mb-1 font-medium text-[13.5px] text-ink-label">
        Step 1: Case observed with phenotype specific for the gene?
      </p>
      <ChoiceRows
        name="loc_phe_step1"
        cells={PHE_STEP1}
        value={step1}
        onChange={(cell) =>
          onChange(
            withField(assessment, { ...cell, id: "loc_phe.step1" }, cell.id),
          )
        }
        onBlur={onBlur}
      />
      {step1 === "loc_phe.step1.yes" && (
        <>
          <p className="framework-voice mt-4 mb-1 font-medium text-[13.5px] text-ink-label">
            Step 2: Diagnostic yield of testing methodology for specific
            phenotype
          </p>
          <ChoiceRows
            name="loc_phe_step2"
            cells={PHE_STEP2}
            value={readField(assessment, "loc_phe.yield")}
            onChange={(cell) =>
              onChange(
                withField(
                  assessment,
                  { ...cell, id: "loc_phe.yield" },
                  cell.id,
                ),
              )
            }
            onBlur={onBlur}
          />
        </>
      )}
      <FrameworkNote>{RARITY_NOTE}</FrameworkNote>
    </div>
  );
}

// The four segregation tables print one Inheritance Pattern over their zygosity rows, and the
// phenotype of the informative relative in each row's own column.

const SEG_AD: Cell[] = [
  {
    id: "loc_seg_ad.het_affected",
    cell: "LOC_SEG.ad.het_affected",
    group: "Autosomal Dominant",
    label: "Heterozygous",
    detail: "Affected male or female",
  },
];

const SEG_AR: Cell[] = [
  {
    id: "loc_seg_ar.hom_or_chet_affected",
    cell: "LOC_SEG.ar.hom_or_chet_affected",
    group: "Autosomal Recessive",
    label: "Homozygous or Compound Heterozygous",
    detail: "Affected male or female",
  },
];

const SEG_SD: Cell[] = [
  {
    id: "loc_seg_sd.hom_or_chet_severe",
    cell: "LOC_SEG.sd.hom_or_chet_severe",
    group: "Autosomal Semi-Dominant",
    label: "Homozygous or Compound Heterozygous",
    detail: "Severely affected male or female",
  },
  {
    id: "loc_seg_sd.het_affected",
    cell: "LOC_SEG.sd.het_affected",
    group: "Autosomal Semi-Dominant",
    label: "Heterozygous",
    detail: "Affected male or female",
  },
];

const SEG_XL: Cell[] = [
  {
    id: "loc_seg_xl.hemi_severe_male",
    cell: "LOC_SEG.xl.hemi_severe_male",
    group: "X-Linked",
    label: "Hemizygous",
    detail: "Severely affected male",
  },
  {
    id: "loc_seg_xl.hom_or_chet_severe_female",
    cell: "LOC_SEG.xl.hom_or_chet_severe_female",
    group: "X-Linked",
    label: "Homozygous or Compound Heterozygous",
    detail: "Severely affected female",
  },
  {
    id: "loc_seg_xl.het_affected_female",
    cell: "LOC_SEG.xl.het_affected_female",
    group: "X-Linked",
    label: "Heterozygous",
    detail: "Affected female",
  },
];

export const LOC_WORKFLOWS: WorkflowDef[] = [
  {
    id: "loc_phe",
    code: "LOC_PHE",
    title: "Workflow for Specific Phenotype Observation",
    cells: [...PHE_STEP1, ...PHE_STEP2],
    applies: () => true,
    Body: LocPheBody,
  },
  {
    id: "loc_seg_ad",
    code: "LOC_SEG",
    title: "Workflow for Segregation with Disease - Autosomal Dominant",
    cells: SEG_AD,
    applies: ({ inheritance }) =>
      inheritance === Inheritance.AUTOSOMAL_DOMINANT,
    Body: countBody(SEG_AD, "Applicable individuals", [RARITY_NOTE]),
  },
  {
    id: "loc_seg_ar",
    code: "LOC_SEG",
    title: "Workflow for Segregation with Disease - Autosomal Recessive",
    cells: SEG_AR,
    applies: ({ inheritance }) =>
      inheritance === Inheritance.AUTOSOMAL_RECESSIVE,
    Body: countBody(SEG_AR, "Applicable individuals", [RARITY_NOTE]),
  },
  {
    id: "loc_seg_sd",
    code: "LOC_SEG",
    title: "Workflow for Segregation with Disease - Semi-Dominant",
    cells: SEG_SD,
    applies: ({ inheritance }) => inheritance === Inheritance.SEMIDOMINANT,
    Body: countBody(SEG_SD, "Applicable individuals", [RARITY_NOTE]),
  },
  {
    id: "loc_seg_xl",
    code: "LOC_SEG",
    title:
      "Workflow for Segregation with Disease - Co-segregation - X-linked inheritance",
    cells: SEG_XL,
    applies: ({ inheritance }) => inheritance === Inheritance.X_LINKED,
    Body: countBody(SEG_XL, "Applicable individuals", [RARITY_NOTE]),
  },
];
