import { describe, expect, test } from "bun:test";
import { absoluteTime, timeAgo } from "./format";

// `timeAgo` takes `now` so it is a pure function of its inputs; these pin the boundaries a card
// crosses as an Analysis ages, and that a bad instant fails rather than rendering "NaN min ago".

const NOW = Date.parse("2026-08-06T12:00:00Z");

function ago(ms: number): string {
  return timeAgo(new Date(NOW - ms).toISOString(), NOW);
}

describe("how a card dates an Analysis", () => {
  test.each([
    ["under a minute", 30 * 1000, "just now"],
    ["minutes", 5 * 60 * 1000, "5 min ago"],
    ["hours", 3 * 60 * 60 * 1000, "3 h ago"],
    ["days", 2 * 24 * 60 * 60 * 1000, "2 d ago"],
  ])("%s", (_label, elapsed, expected) => {
    expect(ago(elapsed as number)).toBe(expected as string);
  });

  test("past a week the elapsed form stops carrying the distinction, so the date is shown", () => {
    expect(ago(30 * 24 * 60 * 60 * 1000)).not.toMatch(/ago$/);
  });

  test("an unparseable instant raises rather than rendering nonsense", () => {
    expect(() => timeAgo("not-a-date", NOW)).toThrow("not an instant");
  });
});

describe("the absolute form a curator hovers", () => {
  test("names the zone it is in, so a server-rendered time cannot read as a local one", () => {
    // Rendered on the server: unmarked, it would read as the curator's own clock while being the
    // container's. The label is what makes the deferral of curator-local rendering safe.
    const shown = absoluteTime("2026-08-06T04:49:00Z");
    expect(shown).toContain("UTC");
    expect(shown).toContain("04:49");
  });

  test("is the same string wherever it is formatted", () => {
    // Pinned locale and zone: the value cannot depend on the host, which is what a server render
    // and a client hydration disagreeing would otherwise produce.
    // Asserted against an explicit UTC rendering rather than by moving the host between zones: the
    // runtime caches its resolved zone, so that comparison is two identical calls and proves nothing.
    const iso = "2026-08-06T04:49:00Z";
    expect(absoluteTime(iso)).toBe(
      new Date(iso).toLocaleString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "UTC",
        timeZoneName: "short",
      }),
    );
  });

  test("on the reader's clock it drops the label; pinned carries one", () => {
    // Asserted through the label rather than by moving the host between two zones: the runtime
    // resolves its default zone once and caches it, so reassigning `process.env.TZ` mid-process does
    // not reliably move it — a test written that way passes on what the reassignment left behind
    // rather than on the formatting.
    const iso = "2026-08-06T04:49:00Z";
    expect(absoluteTime(iso, "reader")).not.toContain("UTC");
    expect(absoluteTime(iso, "pinned")).toContain("UTC");
  });

  test("a bad instant raises rather than rendering", () => {
    // It reaches a `title` attribute, so "Invalid Date" would be shown to a curator as if it were a
    // time. `timeAgo` already raises; this is the same contract.
    expect(() => absoluteTime("not-a-date")).toThrow("not an instant");
    expect(() => absoluteTime("not-a-date", "reader")).toThrow(
      "not an instant",
    );
  });

  test("past a week the date follows the reader's zone too", () => {
    // The elapsed form is zone-free, but the date it falls back to is not — a instant late in the
    // UTC day is the next day for a reader east of it.
    // The pinned fallback names the UTC date whatever zone the host is in. Comparing it against a
    // reader-clock render instead would pass only on a non-UTC host — a flake waiting on a UTC runner.
    const iso = "2026-08-05T23:30:00Z";
    const now = Date.parse("2026-09-05T00:00:00Z");
    expect(timeAgo(iso, now)).toBe(
      new Date(iso).toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      }),
    );
  });
});
