import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  RoutingAssessmentSchema,
  VerdictAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import {
  Consequence,
  ConsequenceSchema,
} from "@/gen/themis/evidence/models/evidence_pb";
import {
  Classification,
  ClassificationSchema,
  ModeOfInheritance,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { CONSEQUENCES } from "../consequence";
import { CLASSIFICATIONS, Header, RoutingCard, VerdictCard } from "./worksheet";

// That the curator can say what the contract can hold, and is asked nothing the registration fixes.
//
// The routing and the verdict are what a round compares side by side, so a member the picker leaves
// out is not a missing option: it is a case a curator has to answer as something else, silently, and
// a divergence that reads as disagreement rather than as an unaskable question. Read off the
// generated descriptors, so a member added to the framework fails here rather than going
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

function routingCard(): string {
  return renderToStaticMarkup(
    <RoutingCard
      value={create(RoutingAssessmentSchema, {})}
      onChange={() => {}}
      onBlur={() => {}}
    />,
  );
}

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
    // The placeholder first, then the list: a picker that opened on its first real option would
    // record a routing nobody stated.
    expect(optionsUnder(routingCard(), "Consequence class")).toEqual([
      Consequence.UNSPECIFIED,
      ...CONSEQUENCES.map(([value]) => value),
    ]);
  });
});

describe("the registered entity", () => {
  test("is not asked of the curator: the routing card's one picker is the consequence class", () => {
    const markup = routingCard();
    expect(markup.match(/<select/g)?.length).toBe(1);
    expect(markup).toContain('aria-label="Routing rationale"');
  });

  test("is shown in the header, its mode included", () => {
    const markup = renderToStaticMarkup(
      <Header
        saveState="idle"
        init={{
          worksheetId: "ws",
          workflowsVersion: "v",
          submissionCount: 0,
          drafts: {},
          variant: {
            gene: "F8",
            transcript: "NM_000132.4",
            hgvsC: "c.1A>G",
            diseaseLabel: "hemophilia A",
            mondoId: "MONDO:0010602",
            modeOfInheritance: ModeOfInheritance.X_LINKED_RECESSIVE,
          },
        }}
      />,
    );
    expect(markup).toContain("MONDO:0010602");
    expect(markup).toContain("X-linked recessive");
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
