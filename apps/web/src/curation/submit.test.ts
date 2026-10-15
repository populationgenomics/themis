import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  AssessmentSchema,
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  AssessmentStatus,
  ModeOfInheritance,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { DEV_USER_EMAIL } from "@/server/identity";
import { CurationAccess } from "./access";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import { ROUTING_ID } from "./sections";
import { hiddenAnswers, submittable } from "./ui/worksheet";
import { workflowsFor } from "./workflows/registry";

// A submission takes what the curator can see. A draft for a workflow the routing has taken off the
// screen (a consequence class changed, or a mode registered more narrowly than the curator routed
// on) stays a draft rather than going into the reference unseen.

const WORKSHEET = "ws_0002"; // the dev user's MYH7 worksheet in the fixture seed

function scored(rationale: string): WorkflowAssessment {
  return create(WorkflowAssessmentSchema, {
    status: AssessmentStatus.SCORED,
    rationale,
  });
}

async function seeded() {
  const store = new FixtureCurationStore();
  const access = new CurationAccess(
    store,
    DEV_USER_EMAIL,
    "manager",
    new FixtureVariantResolver(),
  );
  const put = (workflowId: string, workflow: WorkflowAssessment) =>
    access.saveDraft(WORKSHEET, {
      workflowId,
      assessment: create(AssessmentSchema, {
        kind: { case: "workflow", value: workflow },
      }),
    });
  await put("cln_aff_ad", scored("shown"));
  // Hidden under autosomal dominant, and missing the rationale submitting would demand of it.
  await put("cln_aff_arxl", scored(""));
  await access.saveDraft(WORKSHEET, {
    workflowId: ROUTING_ID,
    assessment: create(AssessmentSchema, {
      kind: { case: "routing", value: { rationale: "why" } },
    }),
  });
  return { store, access };
}

describe("a submission", () => {
  test("takes the shown workflows and the sections, and leaves a hidden draft a draft", async () => {
    const { store, access } = await seeded();
    const submission = await access.submit(WORKSHEET, "", ["cln_aff_ad"]);
    const committed = (await store.assessments(submission.id)).map(
      (e) => e.workflowId,
    );
    expect(committed).toEqual(["cln_aff_ad", ROUTING_ID]);
    expect((await store.drafts(WORKSHEET)).map((e) => e.workflowId)).toContain(
      "cln_aff_arxl",
    );
  });

  test("still refuses a shown scored workflow without a rationale", async () => {
    const { access } = await seeded();
    await expect(
      access.submit(WORKSHEET, "", ["cln_aff_ad", "cln_aff_arxl"]),
    ).rejects.toThrow(/cln_aff_arxl/);
  });
});

describe("the hidden answers the submit card names", () => {
  test("are the answered workflows the routing leaves off the screen", () => {
    const visible = workflowsFor({
      modeOfInheritance: ModeOfInheritance.X_LINKED_DOMINANT,
      consequenceClass: Consequence.MISSENSE,
    });
    expect(
      hiddenAnswers(
        { cln_aff_ad: scored("a"), cln_aff_arxl: scored("b") },
        visible,
      ),
    ).toEqual(["CLN_AFF cln_aff_arxl"]);
  });
});

describe("the submit button", () => {
  test("follows the server on a submission holding only a section", async () => {
    const store = new FixtureCurationStore();
    const access = new CurationAccess(
      store,
      DEV_USER_EMAIL,
      "manager",
      new FixtureVariantResolver(),
    );
    const visible = workflowsFor({
      modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
      consequenceClass: Consequence.UNSPECIFIED,
    });
    // Nothing held: the button is off, and the server refuses.
    expect(submittable({}, visible, new Set())).toBe(0);
    await expect(access.submit(WORKSHEET, "", [])).rejects.toThrow(
      /nothing to submit/,
    );
    // A routing draft alone: the server takes it, so the button has to offer it.
    await access.saveDraft(WORKSHEET, {
      workflowId: ROUTING_ID,
      assessment: create(AssessmentSchema, {
        kind: { case: "routing", value: { rationale: "why" } },
      }),
    });
    expect(submittable({}, visible, new Set([ROUTING_ID]))).toBe(1);
    await access.submit(WORKSHEET, "", []);
  });
});
