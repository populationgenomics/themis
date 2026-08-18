import { describe, expect, test } from "bun:test";
import { parseEdge, parseLabelMode, parseRatio } from "./use-workspace-model";

// The controller persists the arrangement only; these are the validators guarding what comes back
// out of the store. A record is structured-cloned, so it returns with its type intact but no
// guarantee it is one the reducer accepts — each validator has to reject the wrong type as firmly as
// it rejects an out-of-domain value of the right one. The store wiring around them is screenshot-
// and manually covered.

// Values a store can hand back that no validator should accept.
const FOREIGN = [null, undefined, {}, [], "", 0, false] as const;

describe("conversation-edge persistence", () => {
  test("every edge is accepted", () => {
    for (const edge of ["left", "right", "top", "bottom"] as const) {
      expect(parseEdge(edge)).toBe(edge);
    }
  });

  test("anything that is not an edge is rejected", () => {
    for (const raw of [...FOREIGN, "sideways", 1]) {
      expect(parseEdge(raw)).toBeNull();
    }
  });
});

describe("orientation-specific ratio persistence", () => {
  test("an in-range fraction is accepted", () => {
    for (const r of [0.2, 0.33, 0.5, 0.75]) {
      expect(parseRatio(r)).toBe(r);
    }
  });

  test("a fraction at or past the bounds is rejected", () => {
    for (const raw of [0, 1, -0.2, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(parseRatio(raw)).toBeNull();
    }
  });

  test("a ratio that survived as a string is rejected, not coerced", () => {
    for (const raw of [...FOREIGN, "0.5"]) {
      expect(parseRatio(raw)).toBeNull();
    }
  });
});

describe("strip label-mode persistence", () => {
  test("both modes are accepted", () => {
    expect(parseLabelMode(true)).toBe(true);
    expect(parseLabelMode(false)).toBe(false);
  });

  test("a non-boolean is rejected", () => {
    for (const raw of [null, undefined, {}, [], "", "titles", 1]) {
      expect(parseLabelMode(raw)).toBeNull();
    }
  });
});
