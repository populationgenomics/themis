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
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Predicted and functional effect. The pieces here are the ones the calculator instantiates for
// several consequence classes from one wording: the mechanism × exon-relevance matrix (eleven
// classes), and the splice chain — prediction, assay, functional, informative variants (seven).

const SPLICE_CLASSES = new Set<Consequence>([
  Consequence.MISSENSE,
  Consequence.NONSENSE,
  Consequence.FRAMESHIFT,
  Consequence.INFRAME_INDEL,
  Consequence.INTRONIC,
  Consequence.SYNONYMOUS,
  Consequence.EXON_DELETION,
  Consequence.EXON_DUPLICATION,
  Consequence.CANONICAL_SPLICE,
]);

// --- SM18 mechanism × exon relevance ------------------------------------------------------------
//
// The calculator draws this grid as its multipliers (100% / 50% / 25% / 0%). Those cells ARE the
// arithmetic, so the worksheet asks for the two axes instead and never shows a multiplier.

const MECHANISM: Cell[] = [
  {
    id: "matrix.mechanism.established",
    cell: "SM18.mechanism.established",
    label: "Established",
  },
  {
    id: "matrix.mechanism.likely",
    cell: "SM18.mechanism.likely",
    label: "Likely",
  },
  {
    id: "matrix.mechanism.suspected",
    cell: "SM18.mechanism.suspected",
    label: "Suspected",
  },
  {
    id: "matrix.mechanism.uncertain",
    cell: "SM18.mechanism.uncertain",
    label: "Uncertain or not LoF",
  },
];

const EXON_RELEVANCE: Cell[] = [
  {
    id: "matrix.exon.all",
    cell: "SM18.exon.all",
    label:
      "Residue is present in ALL clinically relevant transcripts (Full Score)",
  },
  {
    id: "matrix.exon.most",
    cell: "SM18.exon.most",
    label:
      "Residue is present in MOST clinically relevant transcripts (Half Score)",
  },
  {
    id: "matrix.exon.none",
    cell: "SM18.exon.none",
    label: "Residue NOT present in clinically relevant transcripts (No Score)",
  },
];

function MatrixBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <p className="framework-voice mt-1 mb-1 font-medium text-[13.5px] text-ink-label">
        Loss of Function Score (GenCC Framework)
      </p>
      <ChoiceRows
        name="matrix_mechanism"
        cells={MECHANISM}
        value={readField(assessment, "matrix.mechanism")}
        onChange={(cell) =>
          onChange(
            withField(assessment, { ...cell, id: "matrix.mechanism" }, cell.id),
          )
        }
        onBlur={onBlur}
      />
      <p className="framework-voice mt-4 mb-1 font-medium text-[13.5px] text-ink-label">
        Exon relevance
      </p>
      <ChoiceRows
        name="matrix_exon"
        cells={EXON_RELEVANCE}
        value={readField(assessment, "matrix.exon")}
        onChange={(cell) =>
          onChange(
            withField(assessment, { ...cell, id: "matrix.exon" }, cell.id),
          )
        }
        onBlur={onBlur}
      />
      <FrameworkNote>
        Note: If this score is changed then please recalculate all the points
        for this variant type. Molecular Mechanism &amp; Exon Relevance Matrix
        fraction is applied only to the positive scores.
      </FrameworkNote>
    </div>
  );
}

// --- MIS_PRD ------------------------------------------------------------------------------------
//
// The calculator labels each bin `<points> (<score range>)`. Points-free, the score range is the
// label and the ordering carries the rest; the predictor is a separate control because SM6 fixes it
// per gene rather than per variant.

const PREDICTORS: Cell[] = [
  {
    id: "mis_prd.predictor.bayesdel",
    cell: "MIS_PRD.predictor.bayesdel",
    label: "BayesDel",
  },
  {
    id: "mis_prd.predictor.mutpred2",
    cell: "MIS_PRD.predictor.mutpred2",
    label: "MutPred2",
  },
  {
    id: "mis_prd.predictor.revel",
    cell: "MIS_PRD.predictor.revel",
    label: "REVEL",
  },
  {
    id: "mis_prd.predictor.vest4",
    cell: "MIS_PRD.predictor.vest4",
    label: "VEST4",
  },
  {
    id: "mis_prd.predictor.alphamissense",
    cell: "MIS_PRD.predictor.alphamissense",
    label: "AlphaMissense",
  },
  {
    id: "mis_prd.predictor.esm1b",
    cell: "MIS_PRD.predictor.esm1b",
    label: "ESM1b",
  },
  {
    id: "mis_prd.predictor.varity_r",
    cell: "MIS_PRD.predictor.varity_r",
    label: "VARITY_R",
  },
];

const PREDICTOR_BINS: Record<string, string[]> = {
  "mis_prd.predictor.bayesdel": [
    "",
    "≤ -0.52",
    "-0.051 - -0.34",
    "-0.35 - -0.16",
    "-0.17 - 0.12",
    "0.13 - 0.26",
    "0.27 - 0.40",
    "0.41 - 0.49",
    "≥ 0.50",
  ],
  "mis_prd.predictor.mutpred2": [
    "≤0.010",
    "0.011 - 0.0318",
    "0.0319 - 0.197",
    "0.198 - 0.391",
    "0.392 - 0.736",
    "0.737 - 0.828",
    "0.829 - 0.894",
    "0.895 - 0.931",
    "≥ 0.932",
  ],
  "mis_prd.predictor.revel": [
    "≤0.016",
    "0.017 - 0.052",
    "0.053 - 0.184",
    "0.185 - 0.290",
    "0.291 - 0.643",
    "0.644 - 0.772",
    "0.773 - 0.878",
    "0.879 - 0.931",
    "≥ 0.932",
  ],
  "mis_prd.predictor.vest4": [
    "",
    "≤0.077",
    "0.078 - 0.302",
    "0.303 - 0.449",
    "0.450 - 0.763",
    "0.764 - 0.860",
    "0.861 - 0.908",
    "0.909 - 0.964",
    "≥ 0.965",
  ],
  "mis_prd.predictor.alphamissense": [
    "",
    "≤0.070",
    "0.071 - 0.099",
    "0.100 - 0.169",
    "0.170 - 0.791",
    "0.792 - 0.905",
    "0.906 - 0.971",
    "0.972 - 0.989",
    ">0.990",
  ],
  "mis_prd.predictor.esm1b": [
    "",
    ">=8.8",
    "8.9 - -3.1",
    "-3.2 - -6.3",
    "-6.4 - -10.6",
    "-10.7 - -12.1",
    "-12.2 - -13.9",
    "-14.0 - -23.9",
    "<=-24.0",
  ],
  "mis_prd.predictor.varity_r": [
    "≤0.036",
    "0.037 - 0.063",
    "0.063 - 0.116",
    "0.117 - 0.251",
    "0.252 - 0.674",
    "0.675 - 0.841",
    "0.842 - 0.914",
    "0.915 - 0.965",
    "≥0.966",
  ],
};

// The nine bins the calculator prints per predictor, ordered from most benign to most damaging.
const BIN_TIERS = ["t-4a", "t-4b", "t-3", "t-2", "t-1", "t0", "t1", "t2", "t3"];

function misPrdCells(predictorId: string): Cell[] {
  const bins = PREDICTOR_BINS[predictorId] ?? [];
  return bins
    .map((range, i) => ({ range, tier: BIN_TIERS[i] ?? `t${i}` }))
    .filter(({ range }) => range !== "")
    .map(({ range, tier }) => ({
      id: `mis_prd.bin.${tier}`,
      cell: `MIS_PRD.${predictorId.split(".").pop()}.${tier}`,
      label: range,
    }));
}

function MisPrdBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  const predictor = readField(assessment, "mis_prd.predictor");
  return (
    <div>
      <p className="framework-voice mt-1 mb-1 font-medium text-[13.5px] text-ink-label">
        Predictor use
      </p>
      <ChoiceRows
        name="mis_prd_predictor"
        cells={PREDICTORS}
        value={predictor}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "mis_prd.predictor" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      {predictor !== "" && (
        <>
          <p className="framework-voice mt-4 mb-1 font-medium text-[13.5px] text-ink-label">
            Assess Amino Acid Change Prediction — score
          </p>
          <ChoiceRows
            name="mis_prd_bin"
            cells={misPrdCells(predictor)}
            value={readField(assessment, "mis_prd.bin")}
            onChange={(cell) =>
              onChange(
                withField(assessment, { ...cell, id: "mis_prd.bin" }, cell.id),
              )
            }
            onBlur={onBlur}
          />
        </>
      )}
      <FrameworkNote>
        NOTE: Molecular Mechanism score is not applied for this workflow. ONLY
        the Exon Relevance Matrix Fraction (full, half or no score) is Applied.
        Exon Relevance Matrix Fraction (Full, half or no score) is applied to
        the predictor score (positive).
      </FrameworkNote>
    </div>
  );
}

// --- Functional assay (one wording, per evidence code) ------------------------------------------

export function functionalCells(code: string): Cell[] {
  return [
    {
      id: `fxn_${code}.yes`,
      cell: `${code}.assay_consistent_with_controls`,
      label:
        "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
    },
    { id: `fxn_${code}.no`, cell: `${code}.no`, label: "No" },
  ];
}

export function functionalBody(code: string) {
  return function Body({ assessment, onChange, onBlur }: WorkflowBodyProps) {
    const cells = functionalCells(code);
    return (
      <div>
        <ChoiceRows
          name={`fxn_${code}`}
          cells={cells}
          value={readField(assessment, `fxn_${code}`)}
          onChange={(cell) =>
            onChange(
              withField(assessment, { ...cell, id: `fxn_${code}` }, cell.id),
            )
          }
          onBlur={onBlur}
        />
      </div>
    );
  };
}

// --- SPL_PRD: assess splice change prediction ---------------------------------------------------

const SPLICE_LIKELY_NO_NMD =
  "Splicing change likely — Predicted exon skipping or use of cryptic splice site disrupts reading frame. (Introduced PTC not located 50 nt upstream of the last exon–exon boundary, NMD not predicted)";

const SPLICE_LIKELY_INFRAME =
  "Splicing change likely — Predicted exon skipping or use of cryptic splice site has no impact on reading frame (NMD not predicted)";

const SPLICE_PRD: Cell[] = [
  {
    id: "spl_prd.likely_nmd",
    cell: "SPL_PRD.likely.nmd",
    group:
      "Splicing change likely — Predicted exon skipping or use of cryptic splice site disrupts reading frame (Introduced PTC upstream of 50 nt upstream of the last exon–exon boundary, NMD predicted)",
    label: "Removes 100% of protein",
  },
  {
    id: "spl_prd.likely_no_nmd.alt_start",
    cell: "SPL_PRD.likely.no_nmd.alt_start",
    group: SPLICE_LIKELY_NO_NMD,
    label:
      "Alternate inframe start downstream of VBC is used in functional transcripts",
  },
  {
    id: "spl_prd.likely_no_nmd.gt50",
    cell: "SPL_PRD.likely.no_nmd.gt50",
    group: SPLICE_LIKELY_NO_NMD,
    label:
      "Removes/alters >50% of protein OR Removes/alters entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_no_nmd.gt25",
    cell: "SPL_PRD.likely.no_nmd.gt25",
    group: SPLICE_LIKELY_NO_NMD,
    label:
      "Removes/alters >25% of protein OR Removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_no_nmd.gt10",
    cell: "SPL_PRD.likely.no_nmd.gt10",
    group: SPLICE_LIKELY_NO_NMD,
    label:
      "Removes/alters >10% of protein OR Removes/alters a functional domain with some evidence in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_no_nmd.lt10",
    cell: "SPL_PRD.likely.no_nmd.lt10",
    group: SPLICE_LIKELY_NO_NMD,
    label:
      "Removes/alters <10% of protein OR Role of region in protein function is unknown",
  },
  {
    id: "spl_prd.likely_inframe.alt_start",
    cell: "SPL_PRD.likely.inframe.alt_start",
    group: SPLICE_LIKELY_INFRAME,
    label:
      "Alternate inframe start downstream of VBC has demonstrated protein function",
  },
  {
    id: "spl_prd.likely_inframe.gt50",
    cell: "SPL_PRD.likely.inframe.gt50",
    group: SPLICE_LIKELY_INFRAME,
    label:
      "Removes/alters >50% of protein OR Removes/alters entire critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_inframe.gt25",
    cell: "SPL_PRD.likely.inframe.gt25",
    group: SPLICE_LIKELY_INFRAME,
    label:
      "Removes/alters >25% of protein OR Removes/alters a portion of a critical functional domain that has been experimentally implicated in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_inframe.gt10",
    cell: "SPL_PRD.likely.inframe.gt10",
    group: SPLICE_LIKELY_INFRAME,
    label:
      "Removes/alters >10% of protein OR Removes/alters a functional domain with some evidence in the Molecular Mechanism",
  },
  {
    id: "spl_prd.likely_inframe.insilico_damaging",
    cell: "SPL_PRD.likely.inframe.insilico_damaging",
    group: SPLICE_LIKELY_INFRAME,
    label: "In silico inframe predictive tools suggest a damaging impact",
  },
  {
    id: "spl_prd.likely_inframe.lt10",
    cell: "SPL_PRD.likely.inframe.lt10",
    group: SPLICE_LIKELY_INFRAME,
    label:
      "Removes/alters <10% of protein OR Role of region in protein function is unknown",
  },
  {
    id: "spl_prd.likely_inframe.insilico_benign",
    cell: "SPL_PRD.likely.inframe.insilico_benign",
    group: SPLICE_LIKELY_INFRAME,
    label: "In silico inframe predictive tools suggest a benign impact",
  },
  {
    id: "spl_prd.uncertain",
    cell: "SPL_PRD.uncertain",
    label: "Splicing change uncertain",
  },
  {
    id: "spl_prd.unlikely",
    cell: "SPL_PRD.unlikely",
    label: "Splicing change unlikely",
  },
];

function SplPrdBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  return (
    <div>
      <ChoiceRows
        name="spl_prd"
        cells={SPLICE_PRD}
        value={readField(assessment, "spl_prd")}
        onChange={(cell) =>
          onChange(withField(assessment, { ...cell, id: "spl_prd" }, cell.id))
        }
        onBlur={onBlur}
      />
      <FrameworkNote>
        Abbreviations: NMD: Nonsense-mediated decay, PTC: Premature termination
        codon, VBC: Variant being classified
      </FrameworkNote>
    </div>
  );
}

// --- SPL_SPA: the RNA assay, whose rows depend on the prediction branch -------------------------

const SPA_LIKELY_AVAILABLE =
  "Splicing data is available for VBC showing an inferred variant-specific impact (compared to controls)";

const SPA_LIKELY_CONCORDANT = `${SPA_LIKELY_AVAILABLE} — Splicing data and PRD are concordant with regards to impact`;

const SPA_LIKELY_DISCORDANT = `${SPA_LIKELY_AVAILABLE} — Splicing data and PRD are NOT concordant with regards to impact`;

const SPA_LIKELY: Cell[] = [
  {
    id: "spl_spa.likely.none",
    cell: "SPL_SPA.likely.none",
    label:
      "Splicing data is NOT available for VBC showing an inferred variant-specific impact (compared to controls)",
  },
  {
    id: "spl_spa.likely.concordant.near_complete",
    cell: "SPL_SPA.likely.concordant.near_complete",
    group: SPA_LIKELY_CONCORDANT,
    label:
      "Proportion of Alternative Transcripts (Inferred to Be) Produced by VBC is near to complete",
  },
  {
    id: "spl_spa.likely.concordant.substantial",
    cell: "SPL_SPA.likely.concordant.substantial",
    group: SPA_LIKELY_CONCORDANT,
    label:
      "Proportion of Alternative Transcripts (Inferred to Be) Produced by VBC is near complete to sunstantial",
  },
  {
    id: "spl_spa.likely.concordant.incomplete",
    cell: "SPL_SPA.likely.concordant.incomplete",
    group: SPA_LIKELY_CONCORDANT,
    label:
      "Proportion of Alternative Transcripts (Inferred to Be) Produced by VBC is incomplete",
  },
  {
    id: "spl_spa.likely.discordant",
    cell: "SPL_SPA.likely.discordant",
    group: SPA_LIKELY_DISCORDANT,
    label: "Reconsider SPL_PRD",
  },
];

const SPA_UNCERTAIN_AVAILABLE =
  "Splicing data available for VBC showing an inferred variant-specific impact (compared to controls)";

const SPA_UNCERTAIN: Cell[] = [
  {
    id: "spl_spa.uncertain.none",
    cell: "SPL_SPA.uncertain.none",
    label:
      "Splicing data NOT available for VBC showing an inferred variant-specific impact (compared to controls)",
  },
  {
    id: "spl_spa.uncertain.clear",
    cell: "SPL_SPA.uncertain.clear",
    group: SPA_UNCERTAIN_AVAILABLE,
    label: "Clear evidence of disruptive splice effect",
  },
  {
    id: "spl_spa.uncertain.some",
    cell: "SPL_SPA.uncertain.some",
    group: SPA_UNCERTAIN_AVAILABLE,
    label: "Some evidence of disruptive splice effect",
  },
  {
    id: "spl_spa.uncertain.unconvincing",
    cell: "SPL_SPA.uncertain.unconvincing",
    group: SPA_UNCERTAIN_AVAILABLE,
    label: "Unconvincing evidence of splice effect",
  },
  {
    id: "spl_spa.uncertain.some_no_effect",
    cell: "SPL_SPA.uncertain.some_no_effect",
    group: SPA_UNCERTAIN_AVAILABLE,
    label: "Some evidence of no splice effect",
  },
  {
    id: "spl_spa.uncertain.convincing_no_effect",
    cell: "SPL_SPA.uncertain.convincing_no_effect",
    group: SPA_UNCERTAIN_AVAILABLE,
    label: "Convincing evidence of no splice effect",
  },
];

const SPA_UNLIKELY_AVAILABLE = "Splicing data available for VBC";

const SPA_UNLIKELY: Cell[] = [
  {
    id: "spl_spa.unlikely.none",
    cell: "SPL_SPA.unlikely.none",
    label: "Splicing data NOT available for VBC",
  },
  {
    id: "spl_spa.unlikely.incomplete",
    cell: "SPL_SPA.unlikely.incomplete",
    group: SPA_UNLIKELY_AVAILABLE,
    label:
      "Splice products from the assay represent incomplete or no presence of an aberrant product",
  },
  {
    id: "spl_spa.unlikely.low_level",
    cell: "SPL_SPA.unlikely.low_level",
    group: SPA_UNLIKELY_AVAILABLE,
    label:
      "Splice products show only low-level presence of an aberrant product",
  },
  {
    id: "spl_spa.unlikely.near_complete",
    cell: "SPL_SPA.unlikely.near_complete",
    group: SPA_UNLIKELY_AVAILABLE,
    label:
      "Splice products show near to complete or complete presence of an aberrant productt — Reconsider SPL_PRD points",
  },
];

/** Which assay table applies follows the branch chosen under SPL_PRD, as in the calculator. Until
 *  that is answered there is nothing to show, and saying so beats showing the wrong table. */
export function spaCellsFor(splPrd: string): Cell[] {
  if (splPrd.startsWith("spl_prd.likely")) return SPA_LIKELY;
  if (splPrd === "spl_prd.uncertain") return SPA_UNCERTAIN;
  if (splPrd === "spl_prd.unlikely") return SPA_UNLIKELY;
  return [];
}

// --- SPL_INF: informative variants --------------------------------------------------------------

const SPL_INF_PLP_SAME_REGION =
  "P/LP variant in the same donor/acceptor region. The predicted/observed event of the VBC must precisely match the predicted/observed event of the INF variant AND the strength of the prediction for the VBC event must be of similar or higher score than the INF variant.";

const SPL_INF_BLB_SAME_REGION =
  "B/LB variant in the same donor/acceptor region with same predicted splicing impact AND the strength of the prediction for the VBC event must be of similar or lower score than the INF variant.";

const SPL_INF_DEFAULT: Cell[] = [
  {
    id: "spl_inf.p_first",
    cell: "SPL_INF.p_first",
    group: SPL_INF_PLP_SAME_REGION,
    label: "First P Variant",
  },
  {
    id: "spl_inf.lp_first",
    cell: "SPL_INF.lp_first",
    group: SPL_INF_PLP_SAME_REGION,
    label: "First LP Variant",
  },
  {
    id: "spl_inf.plp_additional",
    cell: "SPL_INF.plp_additional",
    group: SPL_INF_PLP_SAME_REGION,
    label: "Additional P/LP variants",
  },
  {
    id: "spl_inf.vus",
    cell: "SPL_INF.vus",
    label: "VUS informative variants in the same donor/acceptor motif",
  },
  {
    id: "spl_inf.b_first",
    cell: "SPL_INF.b_first",
    group: SPL_INF_BLB_SAME_REGION,
    label: "First B Variant",
  },
  {
    id: "spl_inf.lb_first",
    cell: "SPL_INF.lb_first",
    group: SPL_INF_BLB_SAME_REGION,
    label: "First LB Variant",
  },
  {
    id: "spl_inf.blb_additional",
    cell: "SPL_INF.blb_additional",
    group: SPL_INF_BLB_SAME_REGION,
    label: "Additional B/LB variants",
  },
];

function SplSpaBody({
  assessment,
  siblings,
  onChange,
  onBlur,
}: WorkflowBodyProps) {
  const branch = readField(siblings.spl_prd ?? assessment, "spl_prd");
  const cells = spaCellsFor(branch);
  if (cells.length === 0) {
    return (
      <p className="framework-voice py-2 text-[13px] text-ink-muted">
        Answer the splice change prediction first — which assay table applies
        follows the branch taken there.
      </p>
    );
  }
  const chosen = readField(assessment, "spl_spa");
  // Revising the prediction branch changes which table applies. A selection from the old table is
  // not in the new one, so it would sit unseen in the draft and still reach the submission.
  if (chosen !== "" && !cells.some((c) => c.id === chosen)) {
    onChange(withField(assessment, { ...cells[0], id: "spl_spa" }, ""));
  }
  return (
    <div>
      <ChoiceRows
        name="spl_spa"
        cells={cells}
        value={chosen}
        onChange={(cell) =>
          onChange(withField(assessment, { ...cell, id: "spl_spa" }, cell.id))
        }
        onBlur={onBlur}
      />
    </div>
  );
}

export const PREDICTED_WORKFLOWS: WorkflowDef[] = [
  {
    id: "matrix",
    code: "SM18",
    title: "Molecular Mechanism & Exon Relevance",
    applicability:
      "Applies to the predicted-effect codes the framework scales by the matrix.",
    cells: [...MECHANISM, ...EXON_RELEVANCE],
    applies: () => true,
    Body: MatrixBody,
  },
  {
    id: "mis_prd",
    code: "MIS_PRD",
    title: "Misense Variant Assess Amino Acid Change Prediction",
    cells: PREDICTORS,
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.MISSENSE,
    Body: MisPrdBody,
  },
  {
    id: "mis_fxn",
    code: "MIS_FXN",
    title: "Functional Assessment of Single Amino acid Change",
    cells: functionalCells("MIS_FXN"),
    applies: ({ consequenceClass }) =>
      consequenceClass === Consequence.MISSENSE,
    Body: functionalBody("MIS_FXN"),
  },
  {
    id: "spl_prd",
    code: "SPL_PRD",
    title: "Assess Splice Change Prediction",
    cells: SPLICE_PRD,
    applies: ({ consequenceClass }) => SPLICE_CLASSES.has(consequenceClass),
    Body: SplPrdBody,
  },
  {
    id: "spl_spa",
    code: "SPL_SPA",
    title: "Functional Assessment of alteration to splicing — RNA assay",
    cells: [...SPA_LIKELY, ...SPA_UNCERTAIN, ...SPA_UNLIKELY],
    applies: ({ consequenceClass }) => SPLICE_CLASSES.has(consequenceClass),
    Body: SplSpaBody,
  },
  {
    id: "spl_fxn",
    code: "SPL_FXN",
    title: "Functional Assessment of alteration to splicing",
    cells: functionalCells("SPL_FXN"),
    applies: ({ consequenceClass }) => SPLICE_CLASSES.has(consequenceClass),
    Body: functionalBody("SPL_FXN"),
  },
  {
    id: "spl_inf",
    code: "SPL_INF",
    title: "Workflow for Informative Variants",
    applicability: "Informative Variants for MDE",
    cells: SPL_INF_DEFAULT,
    applies: ({ consequenceClass }) => SPLICE_CLASSES.has(consequenceClass),
    Body: countBody(SPL_INF_DEFAULT, "Applicable variants"),
  },
];

export { SPA_LIKELY, SPA_UNCERTAIN, SPA_UNLIKELY };
