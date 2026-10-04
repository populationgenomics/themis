import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { Classification, Confidence } from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  Svcv4Classification_Line_Kind,
  Svcv4Classification_LineSchema,
  Svcv4ClassificationSchema,
} from "@/models/widgets";
import { fbn1Classification } from "@/widgets/svcv4-classification-fixture";
import {
  alternativeMark,
  codeGroups,
  counted,
  decisiveMarks,
  domain,
  lettered,
  needleLabels,
  needles,
  pointsOf,
  reviewReason,
  signedNumber,
  signedPoints,
  stacks,
} from "./model";

// What the widget reads off a payload: the stacks sum to the tally, the groups keep the payload's
// order, and a code needs review exactly when nobody ticked it and it is uncertain or decides the
// class.

describe("the ruler's stacks", () => {
  test("sum to the tally the codes and lines are counted towards", () => {
    const payload = fbn1Classification();
    const net = stacks(payload).reduce(
      (sum, segment) =>
        segment.kind === "code"
          ? sum + (segment.to - segment.from)
          : sum - (segment.to - segment.from),
      0,
    );
    expect(net).toBeCloseTo(pointsOf(payload.tally?.total ?? ""), 9);
  });

  test("leave out the codes on a path the tally does not count", () => {
    const payload = fbn1Classification();
    const onSplice = payload.codes.filter((code) => code.pathFamily === "SPL");
    expect(onSplice.some((code) => code.points !== "")).toBe(true);
    expect(onSplice.every((code) => !counted(payload, code))).toBe(true);
    const labels = stacks(payload).map((segment) => segment.label);
    expect(labels.some((label) => label.startsWith("SPL_"))).toBe(false);
  });

  test("draw a cap as a cut over the end of the stack it took points from", () => {
    const payload = fbn1Classification();
    payload.lines.push(
      create(Svcv4Classification_LineSchema, {
        kind: Svcv4Classification_Line_Kind.CAP,
        label: "LOC combined cap",
        points: "-2.0",
        rawPoints: "6.0",
        note: "LOC total capped at +4.0",
      }),
    );
    const [cut] = stacks(payload).filter((segment) => segment.kind === "cut");
    const end = Math.max(...stacks(payload).map((segment) => segment.to));
    expect(cut).toMatchObject({ label: "LOC combined cap", to: end });
    expect(cut.to - cut.from).toBeCloseTo(2, 9);
  });

  test("stack an award on its side like a code, never as a cut", () => {
    const payload = fbn1Classification();
    payload.lines.push(
      create(Svcv4Classification_LineSchema, {
        kind: Svcv4Classification_Line_Kind.AWARD,
        label: "critical residue",
        points: "1.0",
        rawPoints: "1",
        pathFamily: "MIS",
      }),
    );
    const segments = stacks(payload);
    expect(segments.some((segment) => segment.kind === "cut")).toBe(false);
    const [award] = segments.filter(
      (segment) => segment.label === "critical residue",
    );
    expect(award.to).toBeGreaterThan(award.from);
    expect(award.from).toBeCloseTo(7, 9);
  });

  test("stack benign codes leftwards, and cut a cap on the benign subtotal off that side", () => {
    const payload = fbn1Classification();
    const frequency = payload.codes.find((code) => code.code === "POP_FRQ");
    if (frequency === undefined) throw new Error("no POP_FRQ");
    frequency.points = "-6.0";
    payload.lines.push(
      create(Svcv4Classification_LineSchema, {
        kind: Svcv4Classification_Line_Kind.CAP,
        label: "a benign cap",
        points: "1.0",
        rawPoints: "-6.0",
      }),
    );
    const segments = stacks(payload);
    const [benign] = segments.filter((segment) => segment.label === "POP_FRQ");
    expect([benign.from, benign.to]).toEqual([0, -6]);
    const [cut] = segments.filter((segment) => segment.kind === "cut");
    expect([cut.from, cut.to]).toEqual([-5, -6]);
  });

  test("span every needle and band edge", () => {
    const payload = fbn1Classification();
    const [low, high] = domain(payload);
    for (const needle of needles(payload)) {
      expect(needle.total).toBeGreaterThan(low);
      expect(needle.total).toBeLessThan(high);
    }
    expect(low).toBeLessThan(-4);
    expect(high).toBeGreaterThan(10);
  });
});

describe("a code's review", () => {
  test("is needed where the call is uncertain or an alternative moves the class, until it is ticked", () => {
    const payload = fbn1Classification();
    const decisive = decisiveMarks(payload);
    expect([...decisive.keys()]).toEqual(["CLN_DNV"]);
    const byCode = new Map(payload.codes.map((code) => [code.code, code]));
    const dnv = byCode.get("CLN_DNV");
    const frequency = byCode.get("POP_FRQ");
    if (dnv === undefined || frequency === undefined)
      throw new Error("no codes");
    const [mark] = decisive.get("CLN_DNV") ?? [];
    expect(reviewReason(dnv, decisive, false)).toContain(
      `${mark} moves the class`,
    );
    expect(reviewReason(dnv, decisive, true)).toBeUndefined();
    expect(frequency.confidence).toBe(Confidence.SETTLED);
    expect(reviewReason(frequency, decisive, false)).toBeUndefined();
  });

  test("names every reason at once, and every alternative that moves the class", () => {
    const payload = fbn1Classification();
    const dnv = payload.codes.find((code) => code.code === "CLN_DNV");
    if (dnv === undefined) throw new Error("no CLN_DNV");
    dnv.confidence = Confidence.LEANING;
    const reason = reviewReason(dnv, new Map([["CLN_DNV", ["B", "D"]]]), false);
    expect(reason).toBe("the call leans; B, D move the class");
  });

  test("an open value that lands in another class decides the class", () => {
    const payload = fbn1Classification();
    payload.sensitivity = [];
    payload.openValues = fbn1Classification().sensitivity.slice(0, 1);
    expect([...decisiveMarks(payload)]).toEqual([["CLN_DNV", ["A"]]]);
  });

  test("an alternative that keeps the class decides nothing", () => {
    const payload = fbn1Classification();
    for (const alternative of payload.sensitivity) {
      if (alternative.tally)
        alternative.tally.finalClass = Classification.LIKELY_PATHOGENIC;
    }
    expect(decisiveMarks(payload).size).toBe(0);
  });
});

describe("the alternatives' letters", () => {
  test("run open values first, then sensitivity rows, one letter each", () => {
    const payload = fbn1Classification();
    payload.openValues = fbn1Classification().sensitivity.slice(0, 1);
    const rows = lettered(payload);
    expect(rows.map((row) => row.kind)).toEqual([
      "open",
      ...payload.sensitivity.map(() => "sensitivity" as const),
    ]);
    expect(new Set(rows.map((row) => row.mark)).size).toBe(rows.length);
    expect(
      needles(payload)
        .slice(1)
        .map((needle) => needle.mark),
    ).toEqual(rows.map((row) => row.mark));
  });

  test("never repeat, past the alphabet too", () => {
    const marks = Array.from({ length: 800 }, (_, index) =>
      alternativeMark(index),
    );
    expect(new Set(marks).size).toBe(marks.length);
    expect(marks.every((mark) => /^[A-Z]+$/.test(mark))).toBe(true);
    expect([alternativeMark(0), alternativeMark(25)]).toEqual(["A", "Z"]);
    expect(() => alternativeMark(-1)).toThrow("no letter");
  });
});

describe("the needles' labels", () => {
  // One unit per letter, and the space between letters of a merged label.
  const width = (marks: readonly string[]) => marks.join(" ").length;

  test("merge needles that would overlap, and keep apart the ones that would not", () => {
    const labels = needleLabels(
      [
        { mark: "C", at: 50 },
        { mark: "A", at: 10 },
        { mark: "B", at: 50.5 },
        { mark: "D", at: 80 },
      ],
      width,
      1,
      [0, 100],
    );
    expect(labels).toEqual([
      { at: 10, marks: ["A"] },
      { at: 50.25, marks: ["C", "B"] },
      { at: 80, marks: ["D"] },
    ]);
  });

  test("merge a label into the one before it once merging has widened it", () => {
    // B alone clears A; B and C merge into a label three wide, which then reaches A.
    const labels = needleLabels(
      [
        { mark: "A", at: 10 },
        { mark: "B", at: 12.5 },
        { mark: "C", at: 12.5 },
      ],
      width,
      1,
      [0, 100],
    );
    expect(labels.map((label) => label.marks)).toEqual([["A", "B", "C"]]);
  });

  test("keep a label inside the ruler, moving it in from an edge", () => {
    const [left, right] = needleLabels(
      [
        { mark: "A", at: 0 },
        { mark: "B", at: 99 },
        { mark: "C", at: 100 },
      ],
      width,
      1,
      [0, 100],
    );
    expect(left).toEqual({ at: 0.5, marks: ["A"] });
    expect(right).toEqual({ at: 98.5, marks: ["B", "C"] });
  });

  test("centre a label wider than the ruler", () => {
    const labels = needleLabels(
      [
        { mark: "A", at: 1 },
        { mark: "B", at: 2 },
      ],
      width,
      1,
      [0, 2],
    );
    expect(labels).toEqual([{ at: 1, marks: ["A", "B"] }]);
  });

  test("leave no two labels overlapping, and name every needle exactly once", () => {
    const placed = Array.from({ length: 30 }, (_, index) => ({
      mark: alternativeMark(index),
      at: (index * 37) % 101,
    }));
    const labels = needleLabels(placed, width, 1, [0, 100]);
    for (const label of labels) {
      expect(label.at - width(label.marks) / 2).toBeGreaterThanOrEqual(0);
      expect(label.at + width(label.marks) / 2).toBeLessThanOrEqual(100);
    }
    for (const [left, right] of labels
      .slice(1)
      .map((label, index) => [labels[index], label])) {
      expect(right.at - left.at).toBeGreaterThanOrEqual(
        (width(left.marks) + width(right.marks)) / 2 + 1,
      );
    }
    const named = labels.flatMap((label) => label.marks);
    expect(named.sort()).toEqual(placed.map((needle) => needle.mark).sort());
  });
});

describe("the codes' groups", () => {
  test("follow the payload's order, each path under its route", () => {
    const groups = codeGroups(fbn1Classification());
    expect(groups.map((group) => group.heading)).toEqual([
      "Population",
      "Clinical",
      "Locus",
      "amino-acid (MIS_)",
      "splice blue (SPL_)",
    ]);
    expect(
      groups.flatMap((group) => group.codes.map((code) => code.code)),
    ).toEqual(fbn1Classification().codes.map((code) => code.code));
  });

  test("name a family this build does not know by its code", () => {
    const payload = create(Svcv4ClassificationSchema, {
      codes: [{ code: "XYZ_ABC", family: "XYZ" }],
    });
    expect(codeGroups(payload).map((group) => group.heading)).toEqual(["XYZ"]);
  });
});

describe("points", () => {
  test("print signed, with a true minus and no trailing zero", () => {
    expect(signedPoints("7.0")).toBe("+7");
    expect(signedPoints("-1.5")).toBe("−1.5");
    expect(signedPoints("0.0")).toBe("0");
    expect(signedPoints("-0")).toBe("0");
    expect(signedNumber(0.1 + 0.2)).toBe("+0.3");
  });

  test("refuse text the payload's rules admit none of", () => {
    expect(() => pointsOf("1E+1")).toThrow("not decimal text");
    expect(() => pointsOf("")).toThrow("not decimal text");
  });
});
