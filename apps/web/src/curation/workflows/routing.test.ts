import { describe, expect, test } from "bun:test";
import {
  Consequence,
  Inheritance,
  InheritanceSchema,
} from "@/gen/themis/evidence/models/evidence_pb";
import { ALL_WORKFLOWS } from "./registry";
import type { WorkflowDef } from "./types";

// What a mode of inheritance the calculator does not split on puts on screen.
//
// The shared vocabulary is the one the curated sources harmonise onto, so it carries modes — Y-linked,
// mitochondrial, undetermined — that the AD versus AR/X-linked splits have no branch for. The right
// answer for one of them is none of the inheritance-routed workflows, and every `applies` says
// positively which modes it covers so that stays true. A predicate written as "not AD-like, therefore
// AR/X-linked" would instead route a mitochondrial worksheet into the recessive branch, which is a
// wrong question asked in the framework's own wording.

/** `DescEnumValue.number` is a plain number; `applies` takes the member type. The consequence class
 *  is held fixed throughout, so only the mode varies. */
function applies(workflow: WorkflowDef, mode: number): boolean {
  return workflow.applies({
    inheritance: mode as Inheritance,
    consequenceClass: Consequence.MISSENSE,
  });
}

const MODES = InheritanceSchema.values.map((value) => value.number);

/** The workflows whose presence depends on the mode at all — those `applies` answers differently for
 *  two modes. Everything else is on screen whatever the routing says about inheritance, so it is not
 *  what a mode routing something in means, and comparing whole screens would count it as one. */
const MODE_ROUTED = ALL_WORKFLOWS.filter(
  (workflow) => new Set(MODES.map((mode) => applies(workflow, mode))).size > 1,
);

function routedBy(mode: number): string[] {
  return MODE_ROUTED.filter((workflow) => applies(workflow, mode)).map(
    (workflow) => workflow.id,
  );
}

describe("a mode of inheritance the transcribed workflows do not branch on", () => {
  test.each([
    Inheritance.Y_LINKED,
    Inheritance.MITOCHONDRIAL,
    Inheritance.UNDETERMINED,
  ])("%p puts no mode-routed workflow on screen", (mode) => {
    expect(routedBy(mode)).toEqual([]);
  });

  test("is exactly the set the framework has no branch for", () => {
    // Read off the generated descriptor: a mode added to the contract lands here until a workflow
    // covers it, or until this list says none ever will. The four names are also what makes the
    // check above non-vacuous — a covered mode leaking into a branch would join them.
    const unrouted = InheritanceSchema.values
      .filter((value) => routedBy(value.number).length === 0)
      .map((value) => value.name);
    expect(unrouted).toEqual([
      "INHERITANCE_UNSPECIFIED",
      "INHERITANCE_Y_LINKED",
      "INHERITANCE_MITOCHONDRIAL",
      "INHERITANCE_UNDETERMINED",
    ]);
  });
});
