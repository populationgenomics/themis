import { describe, expect, test } from "bun:test";
import { createTimeline, keyframes, pulse } from "./not-found-scene";

// The timeline is the only part of the scene testable without a GL context: the tracks are
// pure functions of time. A NaN out of one of them is silent and non-local — it reaches a
// three.js matrix and the character vanishes with a clean console — so the guard against
// it, not the authored numbers, is what these pin down.
describe("keyframes", () => {
  test("rejects non-increasing times", () => {
    expect(() =>
      keyframes([
        [0, 0],
        [1, 1],
        [1, 2],
      ]),
    ).toThrow(/strictly increase/);
    expect(() =>
      keyframes([
        [0, 0],
        [2, 1],
        [1, 2],
      ]),
    ).toThrow(/strictly increase/);
  });

  test("holds the end values outside the authored range", () => {
    const track = keyframes([
      [1, 10],
      [2, 20],
    ]);
    expect(track(0)).toBe(10);
    expect(track(1)).toBe(10);
    expect(track(2)).toBe(20);
    expect(track(99)).toBe(20);
  });

  test("eases between keys without leaving the value range", () => {
    const track = keyframes([
      [0, 0],
      [1, 10, "inOutCubic"],
    ]);
    expect(track(0.5)).toBeCloseTo(5, 5);
    for (let t = 0; t <= 1; t += 0.05) {
      expect(track(t)).toBeGreaterThanOrEqual(0);
      expect(track(t)).toBeLessThanOrEqual(10);
    }
  });
});

describe("pulse", () => {
  test("is zero outside the window and peaks at its centre", () => {
    expect(pulse(0, 1, 0.2)).toBe(0);
    expect(pulse(1, 1, 0.2)).toBe(0);
    expect(pulse(1.2, 1, 0.2)).toBe(0);
    expect(pulse(5, 1, 0.2)).toBe(0);
    expect(pulse(1.1, 1, 0.2)).toBeCloseTo(1, 5);
  });
});

describe("createTimeline", () => {
  // Building the tracks runs the strictly-increasing check over every authored timestamp,
  // so a duplicated or transposed number fails here rather than at mount.
  test("every authored track builds", () => {
    const tracks = Object.values(createTimeline());
    expect(tracks.length).toBeGreaterThan(0);
    expect(tracks.every((t) => typeof t === "function")).toBe(true);
  });

  test("no track yields a non-finite value across the timeline", () => {
    const timeline = createTimeline();
    for (const [name, track] of Object.entries(timeline)) {
      // past END too: the hold evaluates tracks beyond their last key
      for (let t = -1; t <= 15; t += 0.05) {
        const v = track(t);
        if (!Number.isFinite(v)) {
          throw new Error(`${name}(${t.toFixed(2)}) = ${v}`);
        }
      }
    }
  });
});
