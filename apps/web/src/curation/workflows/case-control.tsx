"use client";

import {
  type Cell,
  ChoiceRows,
  FrameworkNote,
  readField,
  withField,
} from "../ui/primitives";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Case-control observations (CLN_CCS), transcribed from Supplementary Material 4 "Clinical
// Observations" rather than from the ClinGen Pilot Calculator: the calculator scores CLN_CCS but
// prints no workflow for it, so SM4 is the only statement of the criteria. SM4 in turn states them
// in prose — the other CLN codes each get a table — and its flow diagram is an image, so the
// benign branch is carried by the figure's caption alone.

const OR_ABOVE_5 = "If the calculated OR for the case-control analysis is >5.0";
const CI_INCLUDES_1 = "If the CI includes 1.0 (e.g., OR = 5.5, CI = 0.9–7.4)";
const OR_NEAR_OR_BELOW_1 =
  "OR near to, or less than 1.0 should be evidence of benignity";

const DETERMINATION: Cell[] = [
  {
    id: "cln_ccs.or_above_5",
    cell: "CLN_CCS.or_above_5",
    label: OR_ABOVE_5,
  },
  {
    id: "cln_ccs.ci_includes_1",
    cell: "CLN_CCS.ci_includes_1",
    label: CI_INCLUDES_1,
  },
  {
    id: "cln_ccs.or_near_or_below_1",
    cell: "CLN_CCS.or_near_or_below_1",
    label: OR_NEAR_OR_BELOW_1,
  },
];

const CI_NOTE =
  "However, analysts should also consider the confidence interval (CI) around the OR as it is as important as the measure of association itself.";

const CONSIDERATIONS_HEADING =
  "There are several important considerations to note when using case-control testing:";

const CONSIDERATIONS = [
  "Cases and controls must be adequately matched to ensure there are no systematic differences between the two that could lead to false positive or false negative associations. This includes matching on genetically determined ancestry, sequencing technology/platform, and on quality control performed on both samples and variants.",
  "Care should be taken to ensure accuracy of phenotyping to ensure a consistent definition of cases and controls. A control set may either be pre-determined to be free of disease, or derived from a population cohort within which the prevalence of disease is not elevated.",
  "Ascertainment bias must be considered. For example, if cases are recruited from a single centre or geographic region, or with elevated rates of consanguinity then this may increase the frequency of benign as well as pathogenic variants. Similarly, care should be taken when cases have been ascertained due to family history.",
  "If the numbers of cases and controls used in the case-control test are imbalanced, for example when a relatively small case cohort (e.g., n=100) is compared to a population dataset (e.g., UK Biobank), then it is very easy to achieve statistical significance in a case-control test with only a small number of cases with the variant. To ensure robustness, we restrict use of this approach to moderate frequency variants (e.g., a minimum of five cases with the variant). The allele frequency of the variant should also be deemed to be compatible with the disorder before performing a case-control analysis. In addition to these points, professional judgement should be used in scenarios where case and control sets are imbalanced to ‘sense check’ the level of evidence being given to a variant.",
];

const OTHER_CLN_CODES_NOTE =
  "When the CLN_CCS evidence code is applied, regardless of the point value assigned, all other Clinical Observation (CLN) codes should be marked as “NA” (Not Applicable), with the sole exception of CLN_DNV, which may be awarded if the VBC is confirmed to be a de novo occurrence in an affected proband.";

const NO_ROBUST_STUDY_NOTE =
  "If the variant is rare (e.g., POP_FRQ being assigned 0.0 or -1.0) and robust case-control studies are unavailable or fail to meet statistical criteria, individual proband occurrences should be evaluated using the Affected Probands (CLN_AFF) guidelines and the case-control criterion should be coded as CLN_CCS_ND.";

function CaseControlBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="cln_ccs_determination"
        cells={DETERMINATION}
        value={readField(assessment, "cln_ccs.determination")}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "cln_ccs.determination" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      <FrameworkNote>{CI_NOTE}</FrameworkNote>
      <p className="framework-voice mt-4 mb-1 font-medium text-[13.5px] text-ink-label">
        {CONSIDERATIONS_HEADING}
      </p>
      {CONSIDERATIONS.map((consideration) => (
        <FrameworkNote key={consideration}>{consideration}</FrameworkNote>
      ))}
      <FrameworkNote>{OTHER_CLN_CODES_NOTE}</FrameworkNote>
      <FrameworkNote>{NO_ROBUST_STUDY_NOTE}</FrameworkNote>
    </div>
  );
}

export const CASE_CONTROL_WORKFLOWS: WorkflowDef[] = [
  {
    id: "cln_ccs",
    code: "CLN_CCS",
    title: "Case-Control Studies",
    applicability:
      "The use of case-control analyses should be restricted to variants of moderate frequency, where a true enrichment in cases over controls can be assessed (i.e., ≥5 observations in a case cohort). Further, there must be a large enough cohort of unrelated cases to enable an accurate and unbiased estimate of case variant frequency (i.e., ≥100).",
    cells: DETERMINATION,
    source: "supplement",
    applies: () => true,
    Body: CaseControlBody,
  },
];
