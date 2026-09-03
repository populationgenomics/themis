import type { WorkflowAssessment } from "@/gen/themis/curation/models/curation_pb";
import {
  type Cell,
  readField,
  withField,
  withoutField,
} from "../ui/primitives";

// POP_FRQ's frequency rows and the ratio that selects one.
//
// The labels and the multiples are two readings of the same framework rows, so they are declared
// together: `<(1.5x of DAFT)` and `minMultiple: 1.5` cannot drift apart while they sit on one line,
// and `fidelity.test.ts` checks that each multiple appears in its own verbatim label.
//
// The arithmetic mirrors `themis/svcv4/frequency.py:pop_frq` — the multiple is `faf / daft` and the
// last bin at or below it wins, the reference's open boundaries closed to the lower edge, which is
// the reading the calculator's own `>=` labels take. `test_cell_inventory.py` joins the exported
// multiples to `reference.frequency_bins`, so a framework revision breaks a test rather than the
// stored answers.
//
// It compares exactly where `pop_frq` divides at 28 significant digits, so the two part on a ratio
// that rounds to a boundary only in the 29th — unreachable from two frequencies anybody types, and
// the direction that refuses to round a near-boundary value up to one.

export const FRQ_DAFT: Cell = {
  id: "pop_frq.daft",
  cell: "POP_FRQ.daft",
  label: "Disease Allele Frequency Threshold",
};

export const FRQ_FAF: Cell = {
  id: "pop_frq.faf",
  cell: "POP_FRQ.faf",
  label: "Variant GrpMax Filtering Allele Frequency",
};

/** The field the derived row is stored under. One field carries the selection; the four rows are
 *  what the nearest-alternative picker enumerates. */
export const FRQ_BIN_FIELD_ID = "pop_frq.bin";

/** The four rows, in the framework's order. `barsRarityGatedCodes` is the gate the calculator prints
 *  under this workflow, restated as a property of the row that triggers it rather than as the points
 *  comparison it is worded in — the worksheet holds no points. */
export const FRQ_BINS: Cell[] = [
  {
    id: "pop_frq.bin.lt_1_5x",
    cell: "POP_FRQ.bin.lt_1_5x",
    label: "Frequency of VBC <(1.5x of DAFT)",
    ratio: { minMultiple: 0, barsRarityGatedCodes: false },
  },
  {
    id: "pop_frq.bin.1_5x_to_5x",
    cell: "POP_FRQ.bin.1_5x_to_5x",
    label: "Frequency of VBC >=( 1.5x of DAFT ) - <(5x of DAFT )",
    ratio: { minMultiple: 1.5, barsRarityGatedCodes: false },
  },
  {
    id: "pop_frq.bin.5x_to_15x",
    cell: "POP_FRQ.bin.5x_to_15x",
    label: "Frequency of VBC >=(5x of DAFT - <(15x of DAFT)",
    ratio: { minMultiple: 5, barsRarityGatedCodes: true },
  },
  {
    id: "pop_frq.bin.ge_15x",
    cell: "POP_FRQ.bin.ge_15x",
    label: "Frequency of VBC >=(15x of DAFT)",
    ratio: { minMultiple: 15, barsRarityGatedCodes: true },
  },
];

/** What the two typed numbers amount to. `unreadable` carries its own reason because the screen has
 *  to say which of the three it is: an unfinished entry and a rejected one are different states, and
 *  neither may read as "rarer than the threshold". */
export type FrequencyRatio =
  | { kind: "incomplete" }
  | { kind: "unreadable"; reason: string }
  | { kind: "binned"; multiple: number; bin: Cell };

/** A decimal exactly, as `num / 10 ** scale`.
 *
 *  Binary floating point is not usable for the comparison: `0.00015 / 0.00001` is
 *  `14.999999999999998`, so a plain 15× multiple of an ordinary threshold would bin one row below
 *  where `frequency.py` — which works in `decimal.Decimal` — puts it. Both inputs are decimal
 *  literals a curator typed, so they are held as written and compared by cross-multiplication. */
interface Exact {
  num: bigint;
  scale: number;
}

/** Empty, unreadable, or a value — three outcomes and not two: a field not yet filled and a field
 *  filled with something that is not a frequency are different states, and the screen says which.
 *  `Number("")` is 0, so the empty case is taken first — a blank threshold must never arrive as a
 *  threshold of zero. */
type ParsedFrequency =
  | { kind: "empty" }
  | { kind: "unreadable" }
  | { kind: "value"; value: Exact };

const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/;

/** How many decimal places either side of the point a frequency may carry. Bounded because the scale
 *  drives `10 ** scale` as a bigint: an unbounded one turns `1e-1000000` into a `RangeError` thrown
 *  during render, and `1e-400` into a ratio that displays as `NaN`. No allele frequency reaches this
 *  far — the smallest gnomAD can express is around `1e-7` — so beyond it is a typo, and a typo is
 *  refused rather than computed. */
const SCALE_LIMIT = 400;

function parseFrequency(raw: string): ParsedFrequency {
  const text = raw.trim();
  if (text === "") return { kind: "empty" };
  const match = DECIMAL.exec(text);
  if (!match) return { kind: "unreadable" };
  const [, sign, whole = "", fraction = "", exponent] = match;
  if (whole === "" && fraction === "") return { kind: "unreadable" };
  const scale = fraction.length - Number(exponent ?? 0);
  if (!Number.isSafeInteger(scale) || Math.abs(scale) > SCALE_LIMIT) {
    return { kind: "unreadable" };
  }
  const digits = BigInt(`${whole}${fraction}` || "0");
  const num = sign === "-" ? -digits : digits;
  return { kind: "value", value: normalise({ num, scale }) };
}

/** `10 ** n` as a bigint. Written through the constructor rather than as a `10n` literal: the app's
 *  tsconfig targets ES2017, which has no bigint literal syntax. */
function pow10(n: number): bigint {
  return BigInt(10) ** BigInt(n);
}

/** Scale down to a whole-number exponent, so every `Exact` has `scale >= 0`. */
function normalise({ num, scale }: Exact): Exact {
  return scale >= 0 ? { num, scale } : { num: num * pow10(-scale), scale: 0 };
}

function multiply(a: Exact, b: Exact): Exact {
  return { num: a.num * b.num, scale: a.scale + b.scale };
}

/** Whether `a >= b`, exactly. */
function atLeast(a: Exact, b: Exact): boolean {
  const scale = Math.max(a.scale, b.scale);
  return a.num * pow10(scale - a.scale) >= b.num * pow10(scale - b.scale);
}

/** A declared multiple as an exact decimal. `String(1.5)` is `"1.5"`: the multiples the framework
 *  states are all exactly representable, so the declaration stays a readable number. */
function exactMultiple(value: number): Exact {
  const parsed = parseFrequency(String(value));
  if (parsed.kind !== "value") {
    throw new Error(`a frequency row states an unreadable multiple: ${value}`);
  }
  return parsed.value;
}

const ZERO: Exact = { num: BigInt(0), scale: 0 };
const ONE: Exact = { num: BigInt(1), scale: 0 };

function ratioOf(cell: Cell): {
  minMultiple: number;
  barsRarityGatedCodes: boolean;
} {
  if (!cell.ratio) {
    throw new Error(
      `${cell.cell} is a frequency row with no ratio: the derivation has no threshold to compare against`,
    );
  }
  return cell.ratio;
}

/** Bin the variant's filtering allele frequency against the disease threshold.
 *
 * @param daftText The Disease Allele Frequency Threshold, as the curator typed it.
 * @param fafText The variant's GrpMax filtering allele frequency, as the curator typed it. Zero is
 *   a value and not an absence of one: the framework asks for 0 where the variant is absent from
 *   gnomAD, and an absent variant is no benignity evidence.
 * @param bins The framework's rows, lowest multiple first.
 */
export function frequencyRatio(
  daftText: string,
  fafText: string,
  bins: Cell[] = FRQ_BINS,
): FrequencyRatio {
  const parsedDaft = parseFrequency(daftText);
  const parsedFaf = parseFrequency(fafText);
  if (parsedDaft.kind === "unreadable") {
    return { kind: "unreadable", reason: "The threshold is not a number." };
  }
  if (parsedFaf.kind === "unreadable") {
    return {
      kind: "unreadable",
      reason: "The variant's frequency is not a number.",
    };
  }
  if (parsedDaft.kind === "empty" || parsedFaf.kind === "empty") {
    return { kind: "incomplete" };
  }
  const daft = parsedDaft.value;
  const faf = parsedFaf.value;
  if (daft.num < BigInt(0) || faf.num < BigInt(0)) {
    return {
      kind: "unreadable",
      reason: "An allele frequency cannot be negative.",
    };
  }
  if (!atLeast(ONE, daft) || !atLeast(ONE, faf)) {
    return {
      kind: "unreadable",
      reason:
        "An allele frequency cannot exceed 1 — check this is a frequency and not a count.",
    };
  }
  if (atLeast(ZERO, daft)) {
    return {
      kind: "unreadable",
      reason:
        "The threshold has to be above zero for the variant's frequency to be a multiple of it.",
    };
  }
  // `faf >= daft * minMultiple` rather than `faf / daft >= minMultiple`: the same comparison without
  // a division, so it stays exact. The last row at or below the frequency wins, the reference's open
  // boundaries closed to the lower edge.
  let bin = bins[0];
  for (const candidate of bins) {
    const threshold = multiply(
      daft,
      exactMultiple(ratioOf(candidate).minMultiple),
    );
    if (atLeast(faf, threshold)) bin = candidate;
  }
  return { kind: "binned", multiple: multipleOf(faf, daft), bin };
}

/** How many places the printed multiple is divided to. Well past the three significant figures the
 *  worksheet shows, so rounding happens in the formatting and not here. */
const DISPLAY_PLACES = 9;

/** `faf / daft` as a double, for display only — the row above was chosen without it.
 *
 *  Divided as bigints and scaled down afterwards. Converting each side to a double first cannot work:
 *  `10 ** 400` is `Infinity`, so two perfectly good frequencies at that scale each became `0` and the
 *  ratio printed as `NaN`. */
function multipleOf(faf: Exact, daft: Exact): number {
  const scale = Math.max(faf.scale, daft.scale);
  const numerator = faf.num * pow10(scale - faf.scale) * pow10(DISPLAY_PLACES);
  const denominator = daft.num * pow10(scale - daft.scale);
  return Number(numerator / denominator) / 10 ** DISPLAY_PLACES;
}

/** The row the assessment's own two numbers select, or null where they select none. */
export function derivedBin(assessment: WorkflowAssessment): Cell | null {
  const ratio = frequencyRatio(
    readField(assessment, FRQ_DAFT.id),
    readField(assessment, FRQ_FAF.id),
  );
  return ratio.kind === "binned" ? ratio.bin : null;
}

/** The assessment with its frequency row brought into line with its two numbers. Idempotent, so it
 *  is safe both on the keystroke that changed a number and on hydrating a draft that stored a row
 *  the numbers do not select. */
export function withDerivedBin(
  assessment: WorkflowAssessment,
): WorkflowAssessment {
  const bin = derivedBin(assessment);
  if (!bin) return withoutField(assessment, FRQ_BIN_FIELD_ID);
  // Stored under one fixed field id whatever it selects, so an answer is one field and not four; the
  // selected row's own id is the value, and its `cell` and label ride along.
  return withField(assessment, { ...bin, id: FRQ_BIN_FIELD_ID }, bin.id);
}

/** The multiple as the worksheet prints it: three significant figures, without a trailing run of
 *  zeros that suggests a precision the division does not have. */
export function formatMultiple(multiple: number): string {
  if (multiple === 0) return "0";
  const fixed = multiple.toPrecision(3);
  return fixed.includes("e") ? fixed : String(Number(fixed));
}
