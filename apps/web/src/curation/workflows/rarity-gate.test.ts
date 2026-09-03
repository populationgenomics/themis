import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  AssessmentStatus,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import {
  Consequence,
  Inheritance,
} from "@/gen/themis/evidence/models/evidence_pb";
import { withField } from "../ui/primitives";
import { FRQ_DAFT, FRQ_FAF } from "./frequency";
import {
  barredWorkflowIds,
  barringBin,
  RARITY_GATED_CODES,
} from "./rarity-gate";
import { ALL_WORKFLOWS, groupsFor } from "./registry";

const AUTOSOMAL_DOMINANT = {
  inheritance: Inheritance.AUTOSOMAL_DOMINANT,
  consequenceClass: Consequence.MISSENSE,
};

function popFrq(daft: string, faf: string, status = AssessmentStatus.SCORED) {
  let assessment = create(WorkflowAssessmentSchema, { status });
  assessment = withField(assessment, FRQ_DAFT, daft);
  return withField(assessment, FRQ_FAF, faf);
}

function visibleWorkflows() {
  return groupsFor(AUTOSOMAL_DOMINANT).flatMap((group) => group.workflows);
}

describe("which frequencies bar the rarity-gated codes", () => {
  test.each([
    ["0.00001", "0", false],
    ["0.00001", "0.0000149", false],
    ["0.00001", "0.000015", false],
    ["0.00001", "0.00005", true],
    ["0.00001", "0.00015", true],
  ])("a threshold of %s against %s bars: %p", (daft, faf, expected) => {
    expect(barringBin(popFrq(daft, faf)) !== null).toBe(expected);
  });

  test("the barring row is the one the numbers selected", () => {
    expect(barringBin(popFrq("0.00001", "0.00015"))?.id).toBe(
      "pop_frq.bin.ge_15x",
    );
  });

  test.each([
    AssessmentStatus.UNSPECIFIED,
    AssessmentStatus.NO_DATA,
    AssessmentStatus.NOT_APPLICABLE,
  ])("a frequency that is not scored (status %p) bars nothing", (status) => {
    // `no data` and `not applicable` are findings about the frequency; neither establishes one.
    expect(barringBin(popFrq("0.00001", "0.00015", status))).toBeNull();
  });

  test("an unanswered frequency workflow bars nothing", () => {
    expect(barringBin(undefined)).toBeNull();
  });

  test("an incomplete frequency bars nothing", () => {
    expect(barringBin(popFrq("0.00001", ""))).toBeNull();
  });
});

describe("which workflows the gate reaches", () => {
  test("every calculator workflow carrying a gated code, and no other", () => {
    const barred = barredWorkflowIds(
      visibleWorkflows(),
      popFrq("0.00001", "0.00015"),
    );
    const expected = visibleWorkflows()
      .filter(
        (workflow) =>
          workflow.source !== "supplement" &&
          (RARITY_GATED_CODES as readonly string[]).includes(workflow.code),
      )
      .map((workflow) => workflow.id);
    expect([...barred].sort()).toEqual(expected.sort());
    // Every gated code is reached, which is the property; how many workflows carry each is a fact
    // about the routing on screen and would make this a change detector.
    const reached = new Set(
      visibleWorkflows()
        .filter((workflow) => barred.has(workflow.id))
        .map((workflow) => workflow.code),
    );
    expect([...reached].sort()).toEqual([...RARITY_GATED_CODES].sort());
  });

  test("the non-segregation branch stays answerable on a common variant", () => {
    // It carries a gated code, and the calculator prints no such workflow — so its note was written
    // over a set that could not have held one. Every row it offers awards benignity, which agrees
    // with the frequency that fired the gate rather than accumulating against it, and barring it
    // would re-open the gap the branch was transcribed to close.
    const nonSegregation = ALL_WORKFLOWS.find(
      (workflow) => workflow.id === "loc_seg_non_segregation",
    );
    expect(nonSegregation?.code).toBe("LOC_SEG");
    expect(nonSegregation?.source).toBe("supplement");
    const barred = barredWorkflowIds(
      ALL_WORKFLOWS,
      popFrq("0.00001", "0.00015"),
    );
    expect(barred.has("loc_seg_non_segregation")).toBe(false);
    // The calculator's own four segregation tables, sharing that code, are barred.
    expect(barred.has("loc_seg_ad")).toBe(true);
    expect(barred.has("loc_seg_xl")).toBe(true);
  });

  test("each gated code is carried by a workflow that exists", () => {
    // A code renamed in the transcription without being renamed here would silently gate nothing.
    for (const code of RARITY_GATED_CODES) {
      expect(ALL_WORKFLOWS.some((workflow) => workflow.code === code)).toBe(
        true,
      );
    }
  });

  test("nothing is barred while the frequency does not bar", () => {
    expect(
      barredWorkflowIds(visibleWorkflows(), popFrq("0.00001", "0.000015")).size,
    ).toBe(0);
  });
});
