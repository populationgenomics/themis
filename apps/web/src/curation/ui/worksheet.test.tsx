import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import { VerdictAssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import {
  Consequence,
  ConsequenceSchema,
  Inheritance,
  InheritanceSchema,
} from "@/gen/themis/evidence/models/evidence_pb";
import {
  Classification,
  ClassificationSchema,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { workflowsFor } from "../workflows/registry";
import {
  CLASSIFICATIONS,
  CONSEQUENCES,
  INHERITANCES,
  RoutingCard,
  VerdictCard,
} from "./worksheet";

// That the curator can say what the contract can hold.
//
// The routing and the verdict are the three answers a round compares side by side, so a member the
// picker leaves out is not a missing option — it is a case a curator has to answer as something else,
// silently, and a divergence that reads as disagreement rather than as an unaskable question. Read
// off the generated descriptors, so a member added to the framework fails here rather than going
// unofferable.

/** The values of the options under one of the routing card's headings, in the order rendered. */
function optionsUnder(markup: string, heading: string): number[] {
  const after = markup.split(`>${heading}</span>`)[1];
  if (after === undefined) {
    throw new Error(`the routing card renders no "${heading}" control`);
  }
  const select = after.slice(0, after.indexOf("</select>"));
  return [...select.matchAll(/<option value="(\d+)"/g)].map((match) =>
    Number(match[1]),
  );
}

const UNANSWERED = {
  inheritance: Inheritance.UNSPECIFIED,
  consequenceClass: Consequence.UNSPECIFIED,
};

describe("the consequence class the worksheet routes on", () => {
  test("offers every member the contract names, non-coding included", () => {
    const offered = new Set(CONSEQUENCES.map(([value]) => value));
    expect(offered.size).toBe(CONSEQUENCES.length);
    expect(offered.has(Consequence.UNSPECIFIED)).toBe(false);
    expect(
      ConsequenceSchema.values
        .filter(
          (value) =>
            value.number !== Consequence.UNSPECIFIED &&
            !offered.has(value.number),
        )
        .map((value) => value.name),
    ).toEqual([]);
  });

  test("reaches the curator as a select that opens on a placeholder", () => {
    const markup = renderToStaticMarkup(
      <RoutingCard routing={UNANSWERED} onChange={() => {}} />,
    );
    // The placeholder first, then the list: a picker that opened on its first real option would
    // record a routing nobody stated, which is what the registration used to do.
    expect(optionsUnder(markup, "Consequence class")).toEqual([
      Consequence.UNSPECIFIED,
      ...CONSEQUENCES.map(([value]) => value),
    ]);
    expect(optionsUnder(markup, "Mode of inheritance")).toEqual([
      Inheritance.UNSPECIFIED,
      ...INHERITANCES.map(([value]) => value),
    ]);
  });
});

describe("the mode of inheritance the worksheet routes on", () => {
  test("offers every member the contract names, the unrouted modes included", () => {
    const offered = new Set(INHERITANCES.map(([value]) => value));
    expect(offered.size).toBe(INHERITANCES.length);
    expect(offered.has(Inheritance.UNSPECIFIED)).toBe(false);
    expect(
      InheritanceSchema.values
        .filter(
          (value) =>
            value.number !== Inheritance.UNSPECIFIED &&
            !offered.has(value.number),
        )
        .map((value) => value.name),
    ).toEqual([]);
  });
});

describe("a stored mode the workflows do not branch on", () => {
  // Offered like any other: that the framework routes nothing on a mode is no reason a curator
  // cannot state it. So the picker has to carry it as the answer it is — a select whose value
  // matches none of its options falls back to the first, which here reads "Select…": an unanswered
  // routing, over a stored answer, beside a note explaining what that answer routes.
  test("renders as itself and routes nothing", () => {
    const routing = { ...UNANSWERED, inheritance: Inheritance.MITOCHONDRIAL };
    expect(workflowsFor(routing).map((workflow) => workflow.id)).toEqual(
      workflowsFor(UNANSWERED).map((workflow) => workflow.id),
    );
    const markup = renderToStaticMarkup(
      <RoutingCard routing={routing} onChange={() => {}} />,
    );
    // React marks the option matching the select's value, so this is the mode reading back as
    // itself; a value matching no option leaves the placeholder marked instead.
    expect(markup).toContain(
      `<option value="${Inheritance.MITOCHONDRIAL}" selected="">`,
    );
  });
});

describe("the class the worksheet ends at", () => {
  test("offers every member the framework can reach, the gate's two terminal outcomes included", () => {
    const offered = new Set(CLASSIFICATIONS.map(([value]) => value));
    expect(offered.size).toBe(CLASSIFICATIONS.length);
    expect(offered.has(Classification.UNSPECIFIED)).toBe(false);
    expect(
      ClassificationSchema.values
        .filter(
          (value) =>
            value.number !== Classification.UNSPECIFIED &&
            !offered.has(value.number),
        )
        .map((value) => value.name),
    ).toEqual([]);
  });

  test("reaches the curator as one button per class", () => {
    const markup = renderToStaticMarkup(
      <VerdictCard
        verdict={create(VerdictAssessmentSchema, {})}
        scored={[]}
        onChange={() => {}}
        onBlur={() => {}}
      />,
    );
    for (const [, label] of CLASSIFICATIONS) {
      expect(markup).toContain(`>${label}</button>`);
    }
  });
});
