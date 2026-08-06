import { describe, expect, test } from "bun:test";
import { locateQuoteInRuns } from "./highlight";

/** Absolute char index of a (run, offset) position within the concatenated runs. */
function absolute(
  runTexts: string[],
  p: { run: number; offset: number },
): number {
  return runTexts.slice(0, p.run).reduce((n, t) => n + t.length, 0) + p.offset;
}

// The property that must hold however the renderer splits text into nodes: the resolved start→end
// span, read back out of the concatenated runs, is exactly the quote. Asserting that rather than
// pinned (run, offset) pairs keeps the test valid across equivalent boundary placements.
function span(runTexts: string[], quote: string): string | null {
  const s = locateQuoteInRuns(runTexts, quote);
  if (!s) return null;
  const text = runTexts.join("");
  return text.slice(absolute(runTexts, s.start), absolute(runTexts, s.end));
}

describe("locateQuoteInRuns", () => {
  test("a quote within a single run resolves to that run", () => {
    expect(span(["the quick brown fox"], "quick brown")).toBe("quick brown");
  });

  test("a quote spanning two runs (emphasis mid-quote) resolves across the boundary", () => {
    // "quick brown" split as "quick " + "brown" (a <em> wraps "brown").
    expect(span(["the quick ", "brown", " fox"], "quick brown")).toBe(
      "quick brown",
    );
  });

  test("a quote ending exactly on a run boundary", () => {
    expect(span(["foo ", "bar", " baz"], "foo bar")).toBe("foo bar");
  });

  test("a quote starting exactly on a run boundary anchors to the run holding the quote", () => {
    // The scroll target is `startContainer.parentElement`, so start must resolve to the run that
    // holds the quote's first character (run 1), not the end of the preceding run.
    const runs = ["foo ", "bar baz"];
    const s = locateQuoteInRuns(runs, "bar baz");
    expect(span(runs, "bar baz")).toBe("bar baz");
    expect(s?.start).toEqual({ run: 1, offset: 0 });
  });

  test("empty runs between text (renderer artefacts) don't shift the result", () => {
    expect(span(["foo ", "", "bar", ""], "foo bar")).toBe("foo bar");
  });

  test("a leading empty run does not anchor the start to the empty node", () => {
    const runs = ["", "foo"];
    const s = locateQuoteInRuns(runs, "foo");
    expect(s?.start).toEqual({ run: 1, offset: 0 });
  });

  test("a quote absent from the runs is null", () => {
    expect(locateQuoteInRuns(["the quick brown fox"], "lazy dog")).toBeNull();
  });

  test("an empty quote is null (nothing to locate)", () => {
    expect(locateQuoteInRuns(["anything"], "")).toBeNull();
  });

  test("the first occurrence is chosen when the quote repeats", () => {
    const runs = ["ab", "cab", "c"]; // "abcabc"
    const s = locateQuoteInRuns(runs, "abc");
    expect(s && absolute(runs, s.start)).toBe(0);
  });
});
