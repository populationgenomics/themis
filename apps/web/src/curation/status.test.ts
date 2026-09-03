import { describe, expect, test } from "bun:test";
import {
  VARIANT_STATUS_ORDER,
  type VariantStatus,
  variantProgress,
  type WorksheetFacts,
  type WorksheetStatus,
  worksheetStatus,
} from "./status";

const pending: WorksheetFacts = { draftCount: 0, submittedAt: null };
const started: WorksheetFacts = { draftCount: 3, submittedAt: null };
const submitted: WorksheetFacts = {
  draftCount: 12,
  submittedAt: "2026-08-13T00:00:00.000Z",
};

describe("one curator's worksheet", () => {
  test.each<[WorksheetFacts, WorksheetStatus]>([
    [pending, "pending"],
    [started, "in_progress"],
    [submitted, "submitted"],
  ])("%p reads as %s", (facts, expected) => {
    expect(worksheetStatus(facts)).toBe(expected);
  });

  test("a submitted worksheet stays submitted while its drafts are edited again", () => {
    // Reopening is the curator editing drafts, and the submission is what a round reads. A worksheet
    // that fell back to `in_progress` would hide a reference that exists.
    expect(worksheetStatus({ draftCount: 20, submittedAt: "2026-08-13" })).toBe(
      "submitted",
    );
  });
});

describe("a variant, over its curators", () => {
  test("no curators assigned is its own state, not pending", () => {
    // Registered-but-unassigned is work for a manager; assigned-but-unstarted is work for a curator.
    const progress = variantProgress([]);
    expect(progress.status).toBe("unassigned");
    expect(progress.assigned).toBe(0);
  });

  test.each<[WorksheetFacts[], VariantStatus]>([
    [[pending, pending], "pending"],
    [[started, pending], "in_progress"],
    [[pending, started], "in_progress"],
    [[submitted, pending], "part_submitted"],
    [[submitted, started], "part_submitted"],
    [[submitted, submitted], "complete"],
    [[submitted], "complete"],
  ])("%p reads as %s", (rows, expected) => {
    expect(variantProgress(rows).status).toBe(expected);
  });

  test("complete means every assigned curator, never merely one", () => {
    // The state the concordance measurement cannot use is a variant reading complete while a second
    // curator has not started, so this is the property rather than a case.
    for (const assigned of [2, 3, 4]) {
      for (let done = 0; done < assigned; done += 1) {
        const rows = [
          ...Array(done).fill(submitted),
          ...Array(assigned - done).fill(started),
        ];
        expect(variantProgress(rows).status).not.toBe("complete");
      }
      expect(variantProgress(Array(assigned).fill(submitted)).status).toBe(
        "complete",
      );
    }
  });

  test("part submitted says which fraction", () => {
    const progress = variantProgress([submitted, started, pending]);
    expect(progress.status).toBe("part_submitted");
    expect(progress.submitted).toBe(1);
    expect(progress.assigned).toBe(3);
  });

  test("every state is orderable, so sorting cannot silently drop one", () => {
    const reached = new Set([
      variantProgress([]).status,
      variantProgress([pending]).status,
      variantProgress([started]).status,
      variantProgress([submitted, pending]).status,
      variantProgress([submitted]).status,
    ]);
    expect([...reached].sort()).toEqual([...VARIANT_STATUS_ORDER].sort());
    for (const status of reached) {
      expect(VARIANT_STATUS_ORDER).toContain(status);
    }
  });
});
