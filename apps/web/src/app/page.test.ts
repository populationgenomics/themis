import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { AnalysisSchema, ProjectSchema } from "@/models/workbench";
import { projectRows } from "./page";

// What each Project card is built from. The failure these guard against is silent: a Project that
// holds Analyses rendering as though it held none.

const projects = [
  create(ProjectSchema, { id: "proj_a", name: "A" }),
  create(ProjectSchema, { id: "proj_b", name: "B" }),
];

function analysis(id: string, projectId: string, iso?: string) {
  return create(AnalysisSchema, {
    id,
    projectId,
    ...(iso ? { createdAt: timestampFromDate(new Date(iso)) } : {}),
  });
}

describe("the Projects page's rows", () => {
  test("counts and dates each Project from its own Analyses", () => {
    const rows = projectRows(projects, [
      analysis("an_1", "proj_a", "2026-08-01T00:00:00Z"),
      analysis("an_2", "proj_a", "2026-08-03T00:00:00Z"),
      analysis("an_3", "proj_b", "2026-08-09T00:00:00Z"),
    ]);
    expect(rows.map((r) => r.analysisCount)).toEqual([2, 1]);
    expect(rows[0].latestIso).toBe("2026-08-03T00:00:00.000Z");
    expect(rows[1].latestIso).toBe("2026-08-09T00:00:00.000Z");
  });

  test("a Project with no Analyses has no date, rather than epoch 0", () => {
    const rows = projectRows(projects, [
      analysis("an_1", "proj_a", "2026-08-01T00:00:00Z"),
    ]);
    expect(rows[1].analysisCount).toBe(0);
    expect(rows[1].latestIso).toBeNull();
  });

  test("an Analysis with no timestamp raises rather than dating its Project by epoch 0", () => {
    // Folding an absent timestamp into the max would put the Project at 0, which renders as "no
    // analyses yet" — a card that is wrong rather than a fault anyone sees.
    expect(() =>
      projectRows(projects, [analysis("an_broken", "proj_a")]),
    ).toThrow("analysis has no created_at");
  });
});
