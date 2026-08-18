import { describe, expect, test } from "bun:test";
import { create, toBinary } from "@bufbuild/protobuf";
import { AnalysisInputsSchema } from "@/models/workbench";
import { type AnalysisRow, analysisForListing } from "./sql";

// `/` reads every Analysis the caller can reach, so the listing's per-row policy decides whether one
// unreadable row costs its own card or the whole entry route.

function row(inputs: Buffer): AnalysisRow {
  return {
    id: "an_1",
    session_id: "sess_1",
    project_id: "proj_a",
    inputs,
    created_at: new Date("2026-08-01T00:00:00Z"),
  } as AnalysisRow;
}

describe("a listing row whose payload will not decode", () => {
  test("renders as an unreadable scenario rather than failing the listing", () => {
    const analysis = analysisForListing(row(Buffer.from([0xff, 0xff, 0xff])));
    expect(analysis.id).toBe("an_1");
    expect(analysis.projectId).toBe("proj_a");
    // The same unset oneof a scenario this build predates decodes to, which the card layer names.
    expect(analysis.inputs?.scenario.case).toBeUndefined();
  });

  test("a decodable row is unaffected", () => {
    const inputs = create(AnalysisInputsSchema, {
      scenario: {
        case: "variantClassification",
        value: { transcript: "NM_1.1", hgvsC: "c.1A>T", clinicalContext: "x" },
      },
    });
    const analysis = analysisForListing(
      row(Buffer.from(toBinary(AnalysisInputsSchema, inputs))),
    );
    expect(analysis.inputs?.scenario.case).toBe("variantClassification");
  });
});
