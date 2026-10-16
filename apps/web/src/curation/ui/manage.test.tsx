import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import { AssignPanel, type VariantRow } from "./manage";

// What the manager's list says about each registration's clinical context.

function row(clinicalContext: string): VariantRow {
  return {
    id: "var",
    gene: "FBN1",
    transcript: "NM_000138.5",
    hgvsC: "c.7003C>T",
    diseaseLabel: "Marfan syndrome",
    mondoId: "MONDO:0007947",
    modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
    clinicalContext,
    rows: [],
  };
}

function panel(clinicalContext: string): string {
  return renderToStaticMarkup(
    <AssignPanel variants={[row(clinicalContext)]} curators={[]} />,
  );
}

describe("a registration's clinical context on the manager's list", () => {
  test("is there to read", () => {
    expect(panel("Tall stature and ectopia lentis.")).toContain(
      "Tall stature and ectopia lentis.",
    );
  });
});
