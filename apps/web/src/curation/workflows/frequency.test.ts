import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  AssessmentStatus,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { readField, withField } from "../ui/primitives";
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

// The frequency row follows from the two numbers, so the boundaries between rows are the part a
// framework revision or a careless edit can move without anything else noticing.

function binIdFor(daft: string, faf: string): string | null {
  const ratio = frequencyRatio(daft, faf);
  return ratio.kind === "binned" ? ratio.bin.id : null;
}

/** A threshold and the frequency that is `multiple` times it, both as a curator would type them.
 *  `0.00001` and `0.00015` are the pair whose binary-float quotient is 14.999999999999998. */
const THRESHOLD = "0.00001";
const TIMES: Record<string, string> = {
  "0": "0",
  "1.49": "0.0000149",
  "1.5": "0.000015",
  "4.999": "0.00004999",
  "5": "0.00005",
  "14.999": "0.00014999",
  "15": "0.00015",
  "1500": "0.015",
};

function assessment(fields: [typeof FRQ_DAFT, string][]) {
  let next = create(WorkflowAssessmentSchema, {
    status: AssessmentStatus.SCORED,
  });
  for (const [cell, value] of fields) next = withField(next, cell, value);
  return next;
}

describe("the frequency row a ratio selects", () => {
  test.each([
    // Each row's own span, and both sides of every boundary. `>=` takes the higher row, which is the
    // reading the calculator's labels state and `frequency.py` implements.
    ["0", "pop_frq.bin.lt_1_5x"],
    ["1.49", "pop_frq.bin.lt_1_5x"],
    ["1.5", "pop_frq.bin.1_5x_to_5x"],
    ["4.999", "pop_frq.bin.1_5x_to_5x"],
    ["5", "pop_frq.bin.5x_to_15x"],
    ["14.999", "pop_frq.bin.5x_to_15x"],
    ["15", "pop_frq.bin.ge_15x"],
    ["1500", "pop_frq.bin.ge_15x"],
  ])("a multiple of %s falls in %s", (multiple, expected) => {
    expect(binIdFor(THRESHOLD, TIMES[multiple])).toBe(expected);
  });

  test("a boundary multiple binds exactly, not to a float's near miss", () => {
    // 0.00015 / 0.00001 is 14.999999999999998 as a double and exactly 15 in decimal. The row has to
    // follow the decimal, or the worksheet and `frequency.py` disagree on an ordinary pair of
    // frequencies.
    expect(Number("0.00015") / Number(THRESHOLD)).toBeLessThan(15);
    expect(binIdFor(THRESHOLD, "0.00015")).toBe("pop_frq.bin.ge_15x");
  });

  test("the curator's own example bins as the calculator does", () => {
    const ratio = frequencyRatio("1.18e-05", "2.800e-7");
    expect(ratio.kind).toBe("binned");
    if (ratio.kind !== "binned") return;
    expect(ratio.bin.id).toBe("pop_frq.bin.lt_1_5x");
    expect(formatMultiple(ratio.multiple)).toBe("0.0237");
  });

  test("scientific notation on either side is read as a number", () => {
    expect(binIdFor("1e-5", "2e-4")).toBe("pop_frq.bin.ge_15x");
    expect(binIdFor("0.00001", "0.0002")).toBe("pop_frq.bin.ge_15x");
  });

  test("an absent variant is the lowest row, not an absence of one", () => {
    // The framework asks for 0 where the variant is absent from gnomAD, and absence is no benignity
    // evidence — the same reading `frequency.py` takes.
    expect(binIdFor("1.18e-05", "0")).toBe("pop_frq.bin.lt_1_5x");
  });
});

describe("what selects no row at all", () => {
  test.each([
    ["", "2.8e-7"],
    ["1.18e-05", ""],
    ["", ""],
    ["   ", "1"],
  ])("an unfinished entry (%s, %s) is incomplete", (daft, faf) => {
    expect(frequencyRatio(daft, faf).kind).toBe("incomplete");
  });

  test.each([
    ["abc", "1"],
    ["1", "abc"],
    ["1.2.3", "1"],
    ["Infinity", "1"],
  ])("an unreadable entry (%s, %s) is refused", (daft, faf) => {
    expect(frequencyRatio(daft, faf).kind).toBe("unreadable");
  });

  test("a blank threshold is never read as a threshold of zero", () => {
    // `Number("")` is 0, which would divide to Infinity and bin the variant at the top.
    expect(frequencyRatio("", "0.1").kind).toBe("incomplete");
  });

  test("a threshold of zero is refused rather than divided by", () => {
    const ratio = frequencyRatio("0", "0.1");
    expect(ratio.kind).toBe("unreadable");
    if (ratio.kind === "unreadable") expect(ratio.reason).toMatch(/above zero/);
  });

  test.each([
    ["-1", "0.1"],
    ["0.1", "-1"],
  ])("a negative frequency (%s, %s) is refused", (daft, faf) => {
    expect(frequencyRatio(daft, faf).kind).toBe("unreadable");
  });

  test.each([
    ["2", "0.1"],
    ["0.1", "2"],
  ])("a frequency above 1 (%s, %s) is refused", (daft, faf) => {
    // The likeliest transposition on this form is an allele count typed where a frequency belongs.
    expect(frequencyRatio(daft, faf).kind).toBe("unreadable");
  });

  test.each(["1e-1000000", "1e-401", "1e400", "1e999999999999999999999"])(
    "an exponent past what any frequency needs (%s) is refused, not computed",
    (value) => {
      // Unbounded, the scale drives `10 ** scale` as a bigint: `1e-1000000` threw a RangeError from
      // inside a render, and `1e-400` displayed its ratio as NaN. Both are typos, and a typo is
      // refused.
      expect(frequencyRatio("1.18e-05", value).kind).toBe("unreadable");
      expect(frequencyRatio(value, "1.18e-05").kind).toBe("unreadable");
    },
  );

  test("a frequency at the bound still bins, and prints a real multiple", () => {
    const ratio = frequencyRatio("1e-400", "1e-400");
    expect(ratio.kind).toBe("binned");
    if (ratio.kind !== "binned") return;
    expect(ratio.bin.id).toBe("pop_frq.bin.lt_1_5x");
    expect(formatMultiple(ratio.multiple)).toBe("1");
  });

  test("a row declared without its ratio raises rather than binning silently", () => {
    const untethered = FRQ_BINS.map(({ ratio, ...cell }) => cell);
    expect(() => frequencyRatio("1", "1", untethered)).toThrow(/no ratio/);
  });
});

describe("the row stored beside the two numbers", () => {
  test("is the row the numbers select, under one field id", () => {
    const stored = withDerivedBin(
      assessment([
        [FRQ_DAFT, "1.18e-05"],
        [FRQ_FAF, "2.800e-7"],
      ]),
    );
    expect(readField(stored, FRQ_BIN_FIELD_ID)).toBe("pop_frq.bin.lt_1_5x");
    const field = stored.fields.find((f) => f.fieldId === FRQ_BIN_FIELD_ID);
    // The cell id and the verbatim label ride along, so a stored answer records the question.
    expect(field?.cellId).toBe("POP_FRQ.bin.lt_1_5x");
    expect(field?.label).toBe("Frequency of VBC <(1.5x of DAFT)");
  });

  test("moves when a number moves", () => {
    const first = withDerivedBin(
      assessment([
        [FRQ_DAFT, "0.001"],
        [FRQ_FAF, "0.0001"],
      ]),
    );
    expect(readField(first, FRQ_BIN_FIELD_ID)).toBe("pop_frq.bin.lt_1_5x");
    const second = withDerivedBin(withField(first, FRQ_FAF, "0.02"));
    expect(readField(second, FRQ_BIN_FIELD_ID)).toBe("pop_frq.bin.ge_15x");
  });

  test("is cleared, not stale, when the numbers stop selecting one", () => {
    const binned = withDerivedBin(
      assessment([
        [FRQ_DAFT, "0.001"],
        [FRQ_FAF, "0.02"],
      ]),
    );
    const cleared = withDerivedBin(withField(binned, FRQ_DAFT, ""));
    expect(readField(cleared, FRQ_BIN_FIELD_ID)).toBe("");
    expect(derivedBin(cleared)).toBeNull();
  });

  test("is idempotent, so reconciling a hydrated draft settles in one pass", () => {
    const once = withDerivedBin(
      assessment([
        [FRQ_DAFT, "0.001"],
        [FRQ_FAF, "0.02"],
      ]),
    );
    expect(withDerivedBin(once)).toEqual(once);
  });

  test("overrides a row the numbers do not select", () => {
    // What a draft stored before the row was derived looks like.
    const handPicked = withField(
      assessment([
        [FRQ_DAFT, "0.001"],
        [FRQ_FAF, "0.0001"],
      ]),
      { ...FRQ_BINS[3], id: FRQ_BIN_FIELD_ID },
      FRQ_BINS[3].id,
    );
    expect(readField(withDerivedBin(handPicked), FRQ_BIN_FIELD_ID)).toBe(
      "pop_frq.bin.lt_1_5x",
    );
  });

  test("leaves the other captured fields alone", () => {
    const withProse = {
      ...assessment([[FRQ_DAFT, "0.001"]]),
      rationale: "why",
    };
    expect(withDerivedBin(withProse).rationale).toBe("why");
  });
});

describe("the multiple as the worksheet prints it", () => {
  test.each([
    [0, "0"],
    [0.023728813559322035, "0.0237"],
    [1.5, "1.5"],
    [15, "15"],
    [1234, "1.23e+3"],
  ])("%p reads as %s", (multiple, expected) => {
    expect(formatMultiple(multiple)).toBe(expected);
  });
});
