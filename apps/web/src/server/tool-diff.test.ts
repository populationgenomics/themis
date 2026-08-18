import { describe, expect, test } from "bun:test";
import { DiffLineKind } from "@/models/workbench";
import { replacementDiff } from "./tool-diff";

// The differ's contract: every line of both sides reaches the output exactly once,
// marked with the side it fell on, in reading order.

const kinds = (before: string, after: string) =>
  replacementDiff(before, after).map((line) => line.kind);
const texts = (before: string, after: string) =>
  replacementDiff(before, after).map((line) => line.text);

const { CONTEXT, REMOVED, ADDED } = DiffLineKind;

describe("the replacement diff", () => {
  test("a pure insertion is all added, a pure deletion all removed", () => {
    expect(kinds("", "one\ntwo")).toEqual([ADDED, ADDED]);
    expect(kinds("one\ntwo", "")).toEqual([REMOVED, REMOVED]);
  });

  test("an unchanged line between two changes stays context", () => {
    expect(kinds("a\nkeep\nb", "c\nkeep\nd")).toEqual([
      REMOVED,
      ADDED,
      CONTEXT,
      REMOVED,
      ADDED,
    ]);
  });

  test("a coalesced run of changed lines is one entry per line", () => {
    // The library reports a run of lines as one change; a row is drawn per line, so a
    // run that stayed joined would render as a single row carrying newlines.
    const lines = replacementDiff("a\nb\nc", "x\ny\nz");
    expect(lines).toHaveLength(6);
    expect(lines.every((line) => !line.text.includes("\n"))).toBe(true);
  });

  test("each side reads back off the output exactly as it went in", () => {
    // The contract in full: dropping a line, repeating one, or reordering the output all
    // survive a check that every line merely appears somewhere.
    const before = "alpha\nbeta\ngamma\nalpha";
    const after = "alpha\ndelta\ngamma\nepsilon\nalpha";
    const out = replacementDiff(before, after);
    const side = (kind: DiffLineKind) =>
      out
        .filter((line) => line.kind === kind || line.kind === CONTEXT)
        .map((line) => line.text);
    expect(side(REMOVED)).toEqual(before.split("\n"));
    expect(side(ADDED)).toEqual(after.split("\n"));
  });

  test("a trailing newline ends the last line rather than adding an empty one", () => {
    expect(texts("one\ntwo\n", "one\ntwo\n")).toEqual(["one", "two"]);
    // ...so ending the last line changes no line. Reported, it would draw as a line
    // removed and re-added carrying identical text.
    expect(kinds("one\ntwo\n", "one\ntwo")).toEqual([CONTEXT, CONTEXT]);
    expect(kinds("one\ntwo", "one\ntwo\n")).toEqual([CONTEXT, CONTEXT]);
  });

  test("two identical sides are all context", () => {
    expect(kinds("same\nlines", "same\nlines")).toEqual([CONTEXT, CONTEXT]);
  });

  test("a replacement past the alignment cap keeps every line", () => {
    // The fallback stops aligning, not reporting: an edit too large to align must still
    // show both sides whole.
    const before = Array.from({ length: 2500 }, (_, i) => `old ${i}`).join(
      "\n",
    );
    const after = Array.from({ length: 3 }, (_, i) => `new ${i}`).join("\n");
    const lines = replacementDiff(before, after);
    expect(lines).toHaveLength(2503);
    expect(lines.filter((l) => l.kind === REMOVED)).toHaveLength(2500);
    expect(lines.filter((l) => l.kind === ADDED)).toHaveLength(3);
    expect(lines.some((l) => l.kind === CONTEXT)).toBe(false);
  });

  test("the same replacement diffs the same way twice", () => {
    const once = replacementDiff("a\nb", "a\nc");
    expect(replacementDiff("a\nb", "a\nc")).toEqual(once);
  });

  test("every line names a side — zero never leaves the differ", () => {
    // The client degrades a zero kind rather than throwing (a stale tab decodes an
    // unknown kind name to zero), so this is where a projection that stopped setting the
    // field fails.
    const replacements = [
      ["", "one\ntwo"],
      ["one\ntwo", ""],
      ["a\nkeep\nb", "c\nkeep\nd"],
      ["same\nlines", "same\nlines"],
    ] as const;
    for (const [before, after] of replacements) {
      const drawn = kinds(before, after);
      expect(drawn.length).toBeGreaterThan(0);
      expect(drawn).not.toContain(DiffLineKind.UNSPECIFIED);
    }
  });
});
