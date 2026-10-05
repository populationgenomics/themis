import { describe, expect, test } from "bun:test";
import { type Anchor, place } from "./tooltip";

// Where a tooltip's panel goes: on the side it prefers where that has room, on the other side where
// only that has room, and never past the viewport's margin.

const VIEWPORT = { width: 800, height: 600 };
const SIZE = { width: 200, height: 60 };

function anchorAt(x: number, top: number, bottom = top + 20): Anchor {
  return { left: x, right: x, top, bottom };
}

describe("a tooltip's panel", () => {
  test("sits centred on the side it prefers, a gap from its anchor", () => {
    expect(place(anchorAt(400, 300), SIZE, VIEWPORT, "above")).toEqual({
      left: 300,
      top: 234,
    });
    expect(place(anchorAt(400, 300), SIZE, VIEWPORT, "below")).toEqual({
      left: 300,
      top: 326,
    });
  });

  test("takes the other side where the preferred one has no room", () => {
    expect(place(anchorAt(400, 20), SIZE, VIEWPORT, "above").top).toBe(46);
    expect(place(anchorAt(400, 560), SIZE, VIEWPORT, "below").top).toBe(494);
  });

  test("stays inside the viewport's margin where neither side has room", () => {
    const tall = { width: 200, height: 580 };
    for (const placement of ["above", "below"] as const) {
      const { top } = place(anchorAt(400, 300), tall, VIEWPORT, placement);
      expect(top).toBeGreaterThanOrEqual(8);
      expect(top + tall.height).toBeLessThanOrEqual(VIEWPORT.height - 8);
    }
  });

  test("a panel as tall as its cap allows sits at the top margin and ends at the bottom one", () => {
    // The panel's height is capped at the viewport less both margins, and scrolls within that.
    const capped = { width: 200, height: VIEWPORT.height - 16 };
    for (const placement of ["above", "below"] as const) {
      const { top } = place(anchorAt(400, 300), capped, VIEWPORT, placement);
      expect(top).toBe(8);
      expect(top + capped.height).toBe(VIEWPORT.height - 8);
    }
  });

  test("moves in from a side edge rather than leaving the viewport", () => {
    expect(place(anchorAt(10, 300), SIZE, VIEWPORT, "above").left).toBe(8);
    expect(place(anchorAt(795, 300), SIZE, VIEWPORT, "above").left).toBe(592);
  });
});
