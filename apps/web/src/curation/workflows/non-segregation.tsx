"use client";

import { Inheritance } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  FrameworkNote,
  readField,
  withField,
} from "../ui/primitives";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// The non-segregation branch of locus segregation (LOC_SEG), transcribed from Supplementary
// Material 5 "Specific Phenotype and Segregation" rather than from the ClinGen Pilot Calculator:
// the calculator's four segregation tables print positive co-segregation rows only, so a
// non-segregation has nowhere to be recorded there and SM5's prose is its only statement.
//
// SM5 does not agree with itself here, and the rows carry both readings rather than settling one:
// the co-segregation section awards benignity in "autosomal recessive inheritance with
// homozygosity", and the supplement's closing note withholds benignity from autosomal recessive
// inheritance outright. SM5's Figure 2 is an image, so the per-observation points it tabulates and
// the two-armed non-segregant test it draws are absent here.

const BENIGNITY = "non-segregations provide robust evidence of benignity";

const BENIGNITY_RULE =
  "In autosomal dominant inheritance, autosomal recessive inheritance with homozygosity, and X-linked inheritance, non-segregations provide robust evidence of benignity, which was conceptually captured by the BS4 criterion in SVC V3.";

const DETERMINATION: Cell[] = [
  {
    id: "loc_seg_non_segregation.autosomal_dominant",
    cell: "LOC_SEG.non_segregation.autosomal_dominant",
    label: "autosomal dominant inheritance",
    detail: BENIGNITY,
  },
  {
    id: "loc_seg_non_segregation.autosomal_recessive_homozygous",
    cell: "LOC_SEG.non_segregation.autosomal_recessive_homozygous",
    label: "autosomal recessive inheritance with homozygosity",
    detail: BENIGNITY,
  },
  {
    id: "loc_seg_non_segregation.x_linked",
    cell: "LOC_SEG.non_segregation.x_linked",
    label: "X-linked inheritance",
    detail: BENIGNITY,
  },
  {
    id: "loc_seg_non_segregation.autosomal_recessive_no_benignity",
    cell: "LOC_SEG.non_segregation.autosomal_recessive_no_benignity",
    label: "MDEs with autosomal recessive inheritance",
    detail:
      "We do not award evidence for benignity for VBCs that show non-segregations",
  },
];

const QUALIFYING_OBSERVATION =
  "If the non-segregation event is a VBC-positive, phenotype-negative individual, the non-segregation only qualifies if the penetrance of the phenotype is very close to 100%.";

const DILIGENT_SEARCH =
  "It is important to perform a diligent search for non-segregations as they serve a critical role of negating diagnostic yield / phenotype specificity points. While it is not necessary to identify multiple non-segregations, not recognizing one could lead to an overestimation of the LOC_PHE evidence of pathogenicity.";

const ZEROES_CO_SEGREGATION =
  "If non-segregations are observed, the co-segregation points that were awarded in the prior steps would be negated or zeroed out.";

const LOG_ODDS =
  "A non-segregation observation negates segregation observations because, as noted above, the former garners a likelihood of co-segregation (colocalization of the responsible trait with that location in the genome) of -∞.";

const ZEROES_PHENOTYPE_SPECIFICITY =
  "Here, non-segregation observations are used to negate or zero out the phenotype specificity points.";

const RE_ANALYSE_OTHER_LOCI =
  "the analyst should repeat the analysis for other VBCs in this patient in other genes that have been associated with that heritable phenotype, excluding the locus where the recombination was observed.";

const OBSERVATION_MUST_BE_ROBUST =
  "As can be appreciated from these considerations, the observation of a non-segregation can markedly affect the Bayes points that are awarded to a given VBC. For that reason, analysts need to be confident that the observation is robust.";

const AUTOSOMAL_RECESSIVE_REASONS =
  "If the inherited phenotype has locus homogeneity and a non-segregation is observed, there is a serious issue with the case data that has to be re-evaluated. If the inherited disorder has locus heterogeneity, some non-segregations may not be evidence of benignity of a non-segregating VBC but instead are an indication that some other locus/gene associated with that phenotype is causing the phenotype in that particular family and the testee is simply a carrier for the non-segregating variant.";

/** The rows enumerate the inheritance contexts of the benignity sentence that heads them, plus the
 *  closing note's blanket recessive exclusion — which contradicts the second row. */
function NonSegregationBody({
  assessment,
  onChange,
  onBlur,
}: WorkflowBodyProps) {
  return (
    <div>
      <p className="framework-voice mt-1 mb-1 font-medium text-[13.5px] text-ink-label">
        {BENIGNITY_RULE}
      </p>
      <ChoiceRows
        name="loc_seg_non_segregation_determination"
        cells={DETERMINATION}
        value={readField(assessment, "loc_seg_non_segregation.determination")}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "loc_seg_non_segregation.determination" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      <FrameworkNote>{QUALIFYING_OBSERVATION}</FrameworkNote>
      <FrameworkNote>{DILIGENT_SEARCH}</FrameworkNote>
      <FrameworkNote>{ZEROES_CO_SEGREGATION}</FrameworkNote>
      <FrameworkNote>{LOG_ODDS}</FrameworkNote>
      <FrameworkNote>{ZEROES_PHENOTYPE_SPECIFICITY}</FrameworkNote>
      <FrameworkNote>{RE_ANALYSE_OTHER_LOCI}</FrameworkNote>
      <FrameworkNote>{OBSERVATION_MUST_BE_ROBUST}</FrameworkNote>
      <FrameworkNote>{AUTOSOMAL_RECESSIVE_REASONS}</FrameworkNote>
    </div>
  );
}

export const NON_SEGREGATION_WORKFLOWS: WorkflowDef[] = [
  {
    id: "loc_seg_non_segregation",
    code: "LOC_SEG",
    title: "Co-segregation (LOC_SEG) — Non-segregation",
    applicability:
      "Next, non-segregations are considered in a similar way that they were considered for LOC_PHE, above (see that section for caveats and criteria for evaluating non-segregations).",
    cells: DETERMINATION,
    source: "supplement",
    applies: ({ inheritance }) =>
      inheritance === Inheritance.AUTOSOMAL_DOMINANT ||
      inheritance === Inheritance.AUTOSOMAL_RECESSIVE ||
      inheritance === Inheritance.SEMIDOMINANT ||
      inheritance === Inheritance.X_LINKED,
    Body: NonSegregationBody,
  },
];
