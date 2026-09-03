"use client";

import { useEffect } from "react";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  type Cell,
  ChoiceRows,
  CountRows,
  readField,
  withField,
  withoutField,
} from "../ui/primitives";
import { countBody } from "./shared";
import type { Routing, WorkflowBodyProps, WorkflowDef } from "./types";

// Splice-chain blocks a consequence class words for itself rather than taking the shared wording in
// predicted.tsx. A GT/AG splice variant keys its informative variants to the ±1,2 dinucleotide and
// to a matching predicted/observed event, where every other class keys them to the donor/acceptor
// region; the calculator prints that table twice, once for a likely or uncertain splicing change and
// once for an unlikely one, which differ in what a P/LP informative variant is worth.

const isCanonicalSplice = ({ consequenceClass }: Routing) =>
  consequenceClass === Consequence.CANONICAL_SPLICE;

// The two descriptions the calculator spans over the rows beneath them, and the row both tables
// share.
const P_LP_SAME_EVENT =
  "P/LP variant with the same predicted/observed event. The VBC must precisely match the predicted/observed event of the INF variant AND the strength of the prediction for the VBC event must be of similar or higher score than the INF variant.";
const B_LB_SAME_DINUCLEOTIDE =
  "B/LB variant in the same ±1,2 dinucleotide with same predicted splicing impact AND the strength of the prediction for the VBC event must be of similar or lower score than the INF variant";
const VUS_SAME_MOTIF =
  "VUS informative variants in the same ±1,2 dinucleotide or donor/acceptor motif";

const SPL_INF_GT_AG_DEFAULT: Cell[] = [
  {
    id: "spl_inf_gt_ag_default.p_first",
    cell: "SPL_INF.gt_ag_default.p_first",
    group: P_LP_SAME_EVENT,
    label: "First P Variant",
  },
  {
    id: "spl_inf_gt_ag_default.lp_first",
    cell: "SPL_INF.gt_ag_default.lp_first",
    group: P_LP_SAME_EVENT,
    label: "First LP Variant",
  },
  {
    id: "spl_inf_gt_ag_default.plp_additional",
    cell: "SPL_INF.gt_ag_default.plp_additional",
    group: P_LP_SAME_EVENT,
    label: "Additional P/LP variants",
  },
  {
    id: "spl_inf_gt_ag_default.vus",
    cell: "SPL_INF.gt_ag_default.vus",
    label: VUS_SAME_MOTIF,
  },
  {
    id: "spl_inf_gt_ag_default.b_first",
    cell: "SPL_INF.gt_ag_default.b_first",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "First B Variant",
  },
  {
    id: "spl_inf_gt_ag_default.lb_first",
    cell: "SPL_INF.gt_ag_default.lb_first",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "First LB Variant",
  },
  {
    id: "spl_inf_gt_ag_default.blb_additional",
    cell: "SPL_INF.gt_ag_default.blb_additional",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "Additional B/LB variants",
  },
];

const P_LP_RECONSIDER: Cell = {
  id: "spl_inf_gt_ag_unlikely.p_reconsider",
  cell: "SPL_INF.gt_ag_unlikely.p_reconsider",
  group: P_LP_SAME_EVENT,
  label: "Reconsider Evidence",
};

const SPL_INF_GT_AG_UNLIKELY_PATH: Cell[] = [P_LP_RECONSIDER];

const SPL_INF_GT_AG_UNLIKELY_COUNTS: Cell[] = [
  {
    id: "spl_inf_gt_ag_unlikely.vus",
    cell: "SPL_INF.gt_ag_unlikely.vus",
    label: VUS_SAME_MOTIF,
  },
  {
    id: "spl_inf_gt_ag_unlikely.b_first",
    cell: "SPL_INF.gt_ag_unlikely.b_first",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "First B Variant",
  },
  {
    id: "spl_inf_gt_ag_unlikely.lb_first",
    cell: "SPL_INF.gt_ag_unlikely.lb_first",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "First LB Variant",
  },
  {
    id: "spl_inf_gt_ag_unlikely.blb_additional",
    cell: "SPL_INF.gt_ag_unlikely.blb_additional",
    group: B_LB_SAME_DINUCLEOTIDE,
    label: "Additional B/LB variants",
  },
];

/** The P/LP row takes no count: where the other table counts three variants, this one prints a lone
 *  cell that sends the curator back to the evidence, with the counted rows beneath it. */
function SplInfGtAgUnlikelyBody({
  assessment,
  onChange,
  onBlur,
}: WorkflowBodyProps) {
  // Drops the count a draft filled against an earlier transcription holds under the P/LP row's own
  // id, from when that row took one. No control reads it now, so it would sit unseen in the draft
  // and still reach the submission.
  useEffect(() => {
    const stale = assessment.fields.some(
      (field) => field.fieldId === P_LP_RECONSIDER.id,
    );
    if (stale) onChange(withoutField(assessment, P_LP_RECONSIDER.id));
  }, [assessment, onChange]);
  return (
    <div>
      <ChoiceRows
        name="spl_inf_gt_ag_unlikely_path"
        cells={SPL_INF_GT_AG_UNLIKELY_PATH}
        value={readField(assessment, "spl_inf_gt_ag_unlikely.path")}
        onChange={(cell) =>
          onChange(
            withField(
              assessment,
              { ...cell, id: "spl_inf_gt_ag_unlikely.path" },
              cell.id,
            ),
          )
        }
        onBlur={onBlur}
      />
      <CountRows
        cells={SPL_INF_GT_AG_UNLIKELY_COUNTS}
        assessment={assessment}
        onChange={onChange}
        countLabel="Applicable Variants"
        onBlur={onBlur}
      />
    </div>
  );
}

export const SPLICE_VARIANT_WORKFLOWS: WorkflowDef[] = [
  {
    id: "spl_inf_gt_ag_default",
    code: "SPL_INF",
    title: "Workflow for Informative Variants (splicing likely or uncertain)",
    cells: SPL_INF_GT_AG_DEFAULT,
    applies: isCanonicalSplice,
    Body: countBody(SPL_INF_GT_AG_DEFAULT, "Applicable Variants"),
  },
  {
    id: "spl_inf_gt_ag_unlikely",
    code: "SPL_INF",
    title: "Workflow for Informative Variants (splicing change unlikely)",
    cells: [...SPL_INF_GT_AG_UNLIKELY_PATH, ...SPL_INF_GT_AG_UNLIKELY_COUNTS],
    applies: isCanonicalSplice,
    Body: SplInfGtAgUnlikelyBody,
  },
];
