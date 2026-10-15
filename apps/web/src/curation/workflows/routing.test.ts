import { describe, expect, test } from "bun:test";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  ModeOfInheritance,
  ModeOfInheritanceSchema,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { ALL_WORKFLOWS } from "./registry";
import type { WorkflowDef } from "./types";

// What the registered mode of inheritance puts on screen.
//
// Every `applies` names the modes it covers, so a mode is routed only where a workflow says so. The
// calculator's headings group X-linked with dominant in one branch and with recessive in another,
// and SM4 sends an affected XX proband to the biallelic table only for a recessive entity
// (svcv4-interpretations.md §X-linked entities), so the two X-linked modes route apart.

/** `DescEnumValue.number` is a plain number; `applies` takes the member type. The consequence class
 *  is held fixed throughout, so only the mode varies. */
function applies(workflow: WorkflowDef, mode: number): boolean {
  return workflow.applies({
    modeOfInheritance: mode as ModeOfInheritance,
    consequenceClass: Consequence.MISSENSE,
  });
}

const MODES = ModeOfInheritanceSchema.values.map((value) => value.number);

/** The workflows whose presence depends on the mode at all. Everything else is on screen whatever
 *  the mode, so comparing whole screens would count it as routed. */
const MODE_ROUTED = ALL_WORKFLOWS.filter(
  (workflow) => new Set(MODES.map((mode) => applies(workflow, mode))).size > 1,
);

function routedBy(mode: number): string[] {
  return MODE_ROUTED.filter((workflow) => applies(workflow, mode)).map(
    (workflow) => workflow.id,
  );
}

describe("the mode a registration states", () => {
  test("an unstated mode routes no workflow, so every predicate names its modes", () => {
    expect(routedBy(ModeOfInheritance.UNSPECIFIED)).toEqual([]);
  });

  test.each(MODES.filter((mode) => mode !== ModeOfInheritance.UNSPECIFIED))(
    "%p routes the observation workflows",
    (mode) => {
      // An affected-observation table, an unaffected one and a segregation one for every mode.
      const codes = new Set(
        ALL_WORKFLOWS.filter((w) => routedBy(mode).includes(w.id)).map(
          (w) => w.code,
        ),
      );
      expect([...codes].sort()).toEqual(
        expect.arrayContaining(["CLN_AFF", "CLN_UAF", "LOC_SEG"]),
      );
    },
  );

  test("semidominant takes the supplements' one table in POP_HMZ and CLN_UAF", () => {
    // svcv4-interpretations.md §Semidominant entities in `POP_HMZ` and `CLN_UAF`.
    const routed = routedBy(ModeOfInheritance.SEMIDOMINANT);
    expect(routed.filter((id) => /^(pop_hmz|cln_uaf)_/.test(id))).toEqual([
      "pop_hmz_arxl",
      "cln_uaf_ad",
    ]);
  });

  test("X-linked dominant and recessive differ in the biallelic affected table alone", () => {
    const dominant = routedBy(ModeOfInheritance.X_LINKED_DOMINANT);
    const recessive = routedBy(ModeOfInheritance.X_LINKED_RECESSIVE);
    expect(dominant.filter((id) => !recessive.includes(id))).toEqual([]);
    expect(recessive.filter((id) => !dominant.includes(id))).toEqual([
      "cln_aff_arxl",
    ]);
    // Both still count an affected XY proband from the monoallelic table.
    expect(dominant).toContain("cln_aff_ad");
  });
});
