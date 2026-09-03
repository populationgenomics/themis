"use client";

import { useEffect, useState } from "react";
import { Inheritance } from "@/gen/themis/evidence/models/evidence_pb";
import {
  DAFT_BINNING_TITLE,
  DaftBinningReference,
} from "../reference/daft-binning";
import {
  DAFT_CALCULATOR_TITLE,
  DaftCalculatorReference,
} from "../reference/daft-calculator";
import {
  type Cell,
  DerivedRows,
  FrameworkNote,
  readField,
  ValueField,
} from "../ui/primitives";
import { ReferenceButton, ReferenceDialog } from "../ui/reference-dialog";
import {
  derivedBin,
  FRQ_BIN_FIELD_ID,
  FRQ_BINS,
  FRQ_DAFT,
  FRQ_FAF,
  formatMultiple,
  frequencyRatio,
  withDerivedBin,
} from "./frequency";
import { countBody } from "./shared";
import type { WorkflowBodyProps, WorkflowDef } from "./types";

// Population observations (POP), transcribed from the ClinGen Pilot Calculator.

/** The frequency workflow's id. The rarity gate reads its answer, so the id is named rather than
 *  spelled at each reader. */
export const POP_FRQ_ID = "pop_frq";

/** Which reference the curator opened, of the two the calculator prints behind this workflow. */
type OpenReference = "calculator" | "binning" | null;

function PopFrqBody({ assessment, onChange, onBlur }: WorkflowBodyProps) {
  const [open, setOpen] = useState<OpenReference>(null);
  const daft = readField(assessment, FRQ_DAFT.id);
  const faf = readField(assessment, FRQ_FAF.id);
  const ratio = frequencyRatio(daft, faf);

  // Brings a stored draft's row into line with its own two numbers, for a draft filled against an
  // earlier transcription where the row was a click. It writes only where the numbers select a row
  // and the stored one differs, so opening an untouched worksheet writes nothing — and a draft that
  // recorded a row without both numbers, which the earlier transcription allowed, keeps it rather
  // than having it silently deleted and auto-saved away.
  //
  // Clearing is the editing path's business, not this one's: the two value fields write through
  // `withDerivedBin`, which drops the row when its numbers stop selecting one.
  useEffect(() => {
    const derived = derivedBin(assessment);
    if (!derived) return;
    if (readField(assessment, FRQ_BIN_FIELD_ID) === derived.id) return;
    onChange(withDerivedBin(assessment));
  }, [assessment, onChange]);

  return (
    <div>
      <div className="divide-y divide-line-row border-line-row border-t">
        {/* The two references belong to the threshold, as they do in the calculator, so they sit
            under its field rather than between the two. */}
        <div>
          <ValueField
            cell={FRQ_DAFT}
            assessment={assessment}
            onChange={(next) => onChange(withDerivedBin(next))}
            placeholder="Type Disease Allele Frequency Threshold"
            onBlur={onBlur}
          />
          <div className="flex flex-wrap justify-end gap-2 pb-2">
            <ReferenceButton
              label="Use Calculator Approach"
              onClick={() => setOpen("calculator")}
            />
            <ReferenceButton
              label="Use Binning Approach"
              onClick={() => setOpen("binning")}
            />
          </div>
        </div>
        <ValueField
          cell={FRQ_FAF}
          assessment={assessment}
          onChange={(next) => onChange(withDerivedBin(next))}
          placeholder="Type Variant GrpMax Filtering Allele Frequency"
          onBlur={onBlur}
        />
      </div>
      <p className="framework-voice mt-2 mb-1 text-[12.5px] text-ink-faint">
        If variant is absent from gnomAD, enter 0. If no GrpMax AF calculated,
        enter Total AF
      </p>
      <DerivedRows
        cells={FRQ_BINS}
        selected={ratio.kind === "binned" ? ratio.bin.id : null}
        note={<RatioNote daft={daft} faf={faf} ratio={ratio} />}
      />
      <FrameworkNote>
        CLN_AFF, CLN_DNV, LOC_PHE and LOC_SEG workflows are applicable if
        Frequency &gt;= -1.0. CLN_AFF, CLN_DNV, LOC_PHE and LOC_SEG workflows
        are NOT applicable if Frequency &lt; -1.0
      </FrameworkNote>
      <ReferenceDialog
        title={DAFT_CALCULATOR_TITLE}
        open={open === "calculator"}
        onClose={() => setOpen(null)}
      >
        <DaftCalculatorReference />
      </ReferenceDialog>
      <ReferenceDialog
        title={DAFT_BINNING_TITLE}
        open={open === "binning"}
        onClose={() => setOpen(null)}
      >
        <DaftBinningReference />
      </ReferenceDialog>
    </div>
  );
}

/** The division, stated. The row follows from it, so the curator checks the arithmetic rather than
 *  taking the selected row on trust. */
function RatioNote({
  daft,
  faf,
  ratio,
}: {
  daft: string;
  faf: string;
  ratio: ReturnType<typeof frequencyRatio>;
}) {
  if (ratio.kind === "incomplete") {
    return "Enter both numbers and the frequency row follows from them.";
  }
  if (ratio.kind === "unreadable") return ratio.reason;
  return (
    <>
      <span className="font-mono">{faf}</span> /{" "}
      <span className="font-mono">{daft}</span> ={" "}
      <span className="font-mono">{formatMultiple(ratio.multiple)}</span>× DAFT
    </>
  );
}

const HMZ_AD: Cell[] = [
  {
    id: "pop_hmz_ad.homozygous",
    cell: "POP_HMZ.ad.homozygous",
    label: "Homozygous individual",
    detail: "Autosomal Dominant",
  },
];

const HMZ_ARXL: Cell[] = [
  {
    id: "pop_hmz_arxl.homozygous_or_hemizygous",
    cell: "POP_HMZ.arxl.homozygous_or_hemizygous",
    label: "Homozygous or hemizygous individual",
    detail: "Autosomal Recessive or X-linked",
  },
];

export const POP_WORKFLOWS: WorkflowDef[] = [
  {
    id: POP_FRQ_ID,
    code: "POP_FRQ",
    title: "Workflow for Population Frequency",
    cells: FRQ_BINS,
    inputs: [FRQ_DAFT, FRQ_FAF],
    applies: () => true,
    Body: PopFrqBody,
  },
  {
    id: "pop_hmz_ad",
    code: "POP_HMZ",
    title:
      "Workflow for Population Homozygous Observations - Autosomal Dominant inheritance",
    cells: HMZ_AD,
    applies: ({ inheritance }) =>
      inheritance === Inheritance.AUTOSOMAL_DOMINANT ||
      inheritance === Inheritance.SEMIDOMINANT,
    Body: countBody(HMZ_AD, "Applicable observations", [
      "Note: Minimum 2 observations required to score, subsequent individuals scored per observation. * : Total is calculated after deducting 1 Applicable Observation.",
    ]),
  },
  {
    id: "pop_hmz_arxl",
    code: "POP_HMZ",
    title:
      "Workflow for Population Homozygous/Hemizygous Observations - Autosomal Recessive/X-linked inheritance",
    cells: HMZ_ARXL,
    applies: ({ inheritance }) =>
      inheritance === Inheritance.AUTOSOMAL_RECESSIVE ||
      inheritance === Inheritance.X_LINKED ||
      inheritance === Inheritance.SEMIDOMINANT,
    Body: countBody(HMZ_ARXL, "Applicable observations", [
      "Minimum 2 observations required to score, subsequent individuals scored per observation. Applicable for semi-dominant inheritance. * : Total is calculated after deducting 1 Applicable Observation",
    ]),
  },
];
