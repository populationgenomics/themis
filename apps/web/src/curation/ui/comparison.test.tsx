import { describe, expect, test } from "bun:test";
import {
  create,
  type JsonValue,
  type MessageInitShape,
  toJson,
} from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  AssessmentSchema,
  type FieldValue,
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  AssessmentStatus,
  Classification,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { ROUTING_ID, VERDICT_ID } from "../sections";
import { WORKFLOWS_VERSION } from "../version";
import { FRQ_DAFT, FRQ_FAF, withDerivedBin } from "../workflows/frequency";
import { ALL_WORKFLOWS, workflowById } from "../workflows/registry";
import type { WorkflowDef } from "../workflows/types";
import {
  agreed,
  Comparison,
  type CuratorAnswers,
  decodeSubmitted,
  keyedByOccurrence,
  printedValue,
  workflowAgreement,
  workflowOrder,
} from "./comparison";
import { cellLabel, fieldValue, withField } from "./primitives";

// The comparison names each section the way the worksheet named it to the curator, and prints a
// stored value only where the label beside it does not already say it.

function registered(id: string): WorkflowDef {
  const workflow = workflowById(id);
  if (workflow === undefined) throw new Error(`no workflow ${id}`);
  return workflow;
}

function json(kind: MessageInitShape<typeof AssessmentSchema>["kind"]) {
  return toJson(AssessmentSchema, create(AssessmentSchema, { kind }));
}

function answers(
  curator: string,
  entries: Record<string, JsonValue>,
  workflowsVersion = WORKFLOWS_VERSION,
): CuratorAnswers {
  return { curator, workflowsVersion, entries };
}

function scored(workflow: WorkflowAssessment): JsonValue {
  return json({
    case: "workflow",
    value: { ...workflow, status: AssessmentStatus.SCORED },
  });
}

const EMPTY = create(WorkflowAssessmentSchema, {});

const CLINICAL_CONTEXT = "Infant with meconium ileus at birth.";

/** A selection, stored the way every `ChoiceRows` body stores one: under the table's field id, with
 *  the chosen cell's id as the value. */
function selected(fieldId: string, workflowId: string, cellId: string) {
  const cell = registered(workflowId).cells.find((c) => c.id === cellId);
  if (cell === undefined) throw new Error(`no cell ${cellId}`);
  return withField(EMPTY, { ...cell, id: fieldId }, cell.id);
}

describe("a submission", () => {
  test("splits into the worksheet's sections and its workflows", () => {
    const decoded = decodeSubmitted(
      answers("a@x", {
        [ROUTING_ID]: json({ case: "routing", value: { rationale: "why" } }),
        [VERDICT_ID]: json({
          case: "verdict",
          value: { classification: Classification.BENIGN },
        }),
        cds_fxn: scored(EMPTY),
      }),
    );
    expect(decoded.routing?.rationale).toBe("why");
    expect(decoded.verdict?.classification).toBe(Classification.BENIGN);
    expect([...decoded.workflows.keys()]).toEqual(["cds_fxn"]);
  });

  test("is refused where a section's id holds another kind", () => {
    expect(() =>
      decodeSubmitted(answers("a@x", { [VERDICT_ID]: scored(EMPTY) })),
    ).toThrow(/a@x's submission holds workflow under verdict/);
  });

  test("is refused where a workflow's id holds a section", () => {
    expect(() =>
      decodeSubmitted(
        answers("a@x", { cds_fxn: json({ case: "routing", value: {} }) }),
      ),
    ).toThrow(/holds routing under cds_fxn/);
  });
});

describe("the workflows compared", () => {
  test("follow the registry, with ids it no longer holds after them in id order", () => {
    const order = workflowOrder(
      [
        { cds_fxn: EMPTY, zz_retired: EMPTY, pop_frq: EMPTY },
        { cds_prd: EMPTY, aa_retired: EMPTY, cds_fxn: EMPTY },
      ].map((workflows) => ({
        curator: "a@x",
        workflowsVersion: WORKFLOWS_VERSION,
        workflows: new Map(Object.entries(workflows)),
      })),
    );
    const known = order.filter((id) => workflowById(id) !== undefined);
    const position = (id: string) =>
      ALL_WORKFLOWS.findIndex((w) => w.id === id);
    expect(known.map(position)).toEqual(
      known.map(position).sort((a, b) => a - b),
    );
    expect(new Set(known)).toEqual(new Set(["cds_fxn", "cds_prd", "pop_frq"]));
    expect(order.slice(known.length)).toEqual(["aa_retired", "zz_retired"]);
  });
});

describe("agreement", () => {
  test("needs every curator to have stated the same answer", () => {
    const unstated = Classification.UNSPECIFIED;
    expect(agreed([Classification.VUS, Classification.VUS], unstated)).toBe(
      true,
    );
    expect(agreed([Classification.VUS, Classification.BENIGN], unstated)).toBe(
      false,
    );
    expect(agreed([unstated, unstated], unstated)).toBe(false);
    expect(agreed([undefined, undefined], unstated)).toBe(false);
  });
});

describe("a stored value", () => {
  const field = (assessment: WorkflowAssessment, fieldId: string) => {
    const found = assessment.fields.find((f) => f.fieldId === fieldId);
    if (found === undefined) throw new Error(`no field ${fieldId}`);
    return found;
  };

  test("is not printed for a selection, whose label already names the row", () => {
    const answer = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
    expect(
      printedValue(
        field(answer, "cds_fxn"),
        registered("cds_fxn"),
        WORKFLOWS_VERSION,
      ),
    ).toBeUndefined();
  });

  test("is not printed for a selection from a table the workflow's cells do not list", () => {
    // MIS_PRD's score bins depend on the predictor and are not among its `cells`; the bin is still
    // stored under a table's field id, never a row's.
    const bin: FieldValue = fieldValue(
      { id: "mis_prd.bin", cell: "MIS_PRD.revel.t2", label: "0.773 - 0.878" },
      "mis_prd.bin.t2",
    );
    expect(
      printedValue(bin, registered("mis_prd"), WORKFLOWS_VERSION),
    ).toBeUndefined();
  });

  test("is printed for the two frequencies typed, and not for the row they derive", () => {
    let answer = withField(EMPTY, FRQ_DAFT, "0.0153");
    answer = withDerivedBin(withField(answer, FRQ_FAF, "0.00984"));
    const popFrq = registered("pop_frq");
    expect(
      answer.fields.map((f) => printedValue(f, popFrq, WORKFLOWS_VERSION)),
    ).toEqual([undefined, "0.0153", "0.00984"]);
  });

  test("is printed for a count", () => {
    const workflow = registered("cln_aff_arxl");
    const answer = withField(EMPTY, workflow.cells[0], "3");
    expect(printedValue(answer.fields[0], workflow, WORKFLOWS_VERSION)).toBe(
      "3",
    );
  });

  test("is printed whole where the registry cannot say what it is", () => {
    const selection = field(
      selected("cds_fxn", "cds_fxn", "cds_fxn.no"),
      "cds_fxn",
    );
    expect(
      printedValue(selection, registered("cds_fxn"), "an-earlier-version"),
    ).toBe("cds_fxn.no");
    expect(printedValue(selection, undefined, WORKFLOWS_VERSION)).toBe(
      "cds_fxn.no",
    );
  });
});

describe("the comparison", () => {
  const markup = renderToStaticMarkup(
    <Comparison
      clinicalContext={CLINICAL_CONTEXT}
      answers={[
        answers("a@x", {
          [ROUTING_ID]: json({
            case: "routing",
            value: { consequenceClass: Consequence.INFRAME_INDEL },
          }),
          [VERDICT_ID]: json({
            case: "verdict",
            value: {
              classification: Classification.PATHOGENIC,
              classDeterminative: ["cds_fxn", "a_retired_workflow"],
            },
          }),
          cds_fxn: scored(selected("cds_fxn", "cds_fxn", "cds_fxn.no")),
        }),
        answers("b@x", {
          [VERDICT_ID]: json({
            case: "verdict",
            value: { classification: Classification.LIKELY_PATHOGENIC },
          }),
          cds_fxn: scored(selected("cds_fxn", "cds_fxn", "cds_fxn.yes")),
        }),
      ]}
    />,
  );

  test("draws the clinical context, the routing, the workflows and the verdict, in that order", () => {
    const at = (text: string) => {
      const index = markup.indexOf(text);
      expect(index).toBeGreaterThanOrEqual(0);
      return index;
    };
    const titles = [
      ">Clinical context<",
      ">Routing<",
      registered("cds_fxn").title,
      ">Verdict<",
    ];
    const positions = titles.map(at);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  test("states the registration's clinical context once, not per curator", () => {
    expect(markup.split(CLINICAL_CONTEXT).length - 1).toBe(1);
  });

  test("names a workflow by its code and title, never its stored id", () => {
    const workflow = registered("cds_fxn");
    expect(markup).toContain(`>${workflow.code}<`);
    expect(markup).toContain(`>${workflow.title}<`);
    expect(markup).not.toContain(">cds_fxn<");
  });

  test("reads the classification as the worksheet names it, and whether the curators share it", () => {
    expect(markup).toContain("Classification: Pathogenic");
    expect(markup).toContain("Classification: Likely pathogenic");
    expect(markup).toContain(">different classification<");
  });

  test("names the class-determinative workflows, a retired one by its stored id", () => {
    expect(markup).toContain(">Class-determinative<");
    expect(markup).toContain(">a_retired_workflow<");
  });
});

describe("a workflow's agreement", () => {
  const no = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
  const yes = selected("cds_fxn", "cds_fxn", "cds_fxn.yes");
  const as = (
    status: AssessmentStatus,
    workflow: WorkflowAssessment = EMPTY,
  ): WorkflowAssessment => ({ ...workflow, status });
  const { SCORED, NOT_APPLICABLE, NO_DATA, UNSPECIFIED } = AssessmentStatus;
  const counted = (n: string) =>
    withField(EMPTY, registered("cln_aff_arxl").cells[0], n);
  const frequency = (faf: string) =>
    withDerivedBin(
      withField(withField(EMPTY, FRQ_DAFT, "0.0153"), FRQ_FAF, faf),
    );

  test.each([
    ["the same row", [as(SCORED, no), as(SCORED, no)], true, "same answer"],
    [
      "different rows",
      [as(SCORED, no), as(SCORED, yes)],
      false,
      "different cells",
    ],
    [
      "one status kept the fields the other scored",
      [as(SCORED, no), as(NOT_APPLICABLE, no)],
      false,
      "different status",
    ],
    [
      "the same unscored status",
      [as(NO_DATA), as(NO_DATA)],
      true,
      "same answer",
    ],
    [
      "one curator answered nothing",
      [as(NOT_APPLICABLE), undefined],
      false,
      "not answered by every curator",
    ],
    [
      "no curator chose a status",
      [as(UNSPECIFIED), as(UNSPECIFIED)],
      false,
      "not answered by every curator",
    ],
    [
      "one row holding different counts",
      [as(SCORED, counted("3")), as(SCORED, counted("2"))],
      false,
      "different values",
    ],
    [
      "one row holding the same count",
      [as(SCORED, counted("3")), as(SCORED, counted("3"))],
      true,
      "same answer",
    ],
    [
      "the same derived row from different frequencies",
      [as(SCORED, frequency("0.00984")), as(SCORED, frequency("0.0104"))],
      false,
      "different values",
    ],
  ] as const)("reads %s", (_, answers, isAgreed, label) => {
    expect(workflowAgreement([...answers])).toEqual({
      agreed: isAgreed,
      label,
    });
  });

  test.each([
    ["the same row", "cds_fxn", [no, no], "same answer"],
    ["different rows", "cds_fxn", [no, yes], "different cells"],
    [
      "one row holding different counts",
      "cln_aff_arxl",
      [counted("3"), counted("2")],
      "different values",
    ],
  ] as const)(
    "reads %s the same across transcriptions",
    (_, workflowId, [first, second], label) => {
      // One worksheet pins an earlier version, where every value prints; agreement must not follow.
      const markup = renderToStaticMarkup(
        <Comparison
          clinicalContext={CLINICAL_CONTEXT}
          answers={[
            answers("a@x", { [workflowId]: scored(first) }, "svcv4-earlier"),
            answers("b@x", { [workflowId]: scored(second) }),
          ]}
        />,
      );
      expect(markup).toContain(`>${label}<`);
    },
  );
});

describe("an unscored answer", () => {
  const markup = renderToStaticMarkup(
    <Comparison
      clinicalContext={CLINICAL_CONTEXT}
      answers={["a@x", "b@x"].map((curator) =>
        answers(curator, {
          cds_fxn: json({
            case: "workflow",
            value: {
              ...selected("cds_fxn", "cds_fxn", "cds_fxn.no"),
              status: AssessmentStatus.NOT_APPLICABLE,
              statusReason: `${curator} found the precondition unmet.`,
            },
          }),
        }),
      )}
    />,
  );

  test("draws why it was not scored", () => {
    expect(markup).toContain(">Why not<");
    expect(markup).toContain("a@x found the precondition unmet.");
    expect(markup).toContain("a@x · not applicable");
  });

  test("leaves out the fields a status change left stored", () => {
    const cell = registered("cds_fxn").cells.find((c) => c.id === "cds_fxn.no");
    if (cell === undefined) throw new Error("no cell cds_fxn.no");
    expect(markup).not.toContain(`>${cellLabel(cell)}<`);
  });
});

describe("the transcription a worksheet pins", () => {
  const render = (versions: [string, string]) =>
    renderToStaticMarkup(
      <Comparison
        clinicalContext={CLINICAL_CONTEXT}
        answers={[
          answers("a@x", { cds_fxn: scored(EMPTY) }, versions[0]),
          answers("b@x", { cds_fxn: scored(EMPTY) }, versions[1]),
        ]}
      />,
    );

  test("is named beside each curator where the curators' differ", () => {
    const markup = render(["svcv4-old", WORKFLOWS_VERSION]);
    expect(markup).toContain("a@x · svcv4-old · scored");
    expect(markup).toContain(`b@x · ${WORKFLOWS_VERSION} · scored`);
    expect(markup).toContain(`>a@x · svcv4-old<`);
  });

  test("is not named where they share it", () => {
    expect(render([WORKFLOWS_VERSION, WORKFLOWS_VERSION])).not.toContain(
      WORKFLOWS_VERSION,
    );
  });
});

describe("a class-determinative list naming one workflow twice", () => {
  test("keys each occurrence apart", () => {
    const keys = keyedByOccurrence(["cds_fxn", "pop_frq", "cds_fxn"]).map(
      ({ key }) => key,
    );
    expect(new Set(keys).size).toBe(keys.length);
  });

  test("draws every occurrence", () => {
    // Server rendering does not report a key collision, so the keys themselves are checked above.
    const markup = renderToStaticMarkup(
      <Comparison
        clinicalContext={CLINICAL_CONTEXT}
        answers={["a@x", "b@x"].map((curator) =>
          answers(curator, {
            [VERDICT_ID]: json({
              case: "verdict",
              value: { classDeterminative: ["cds_fxn", "cds_fxn"] },
            }),
          }),
        )}
      />,
    );
    expect(markup.split(`>${registered("cds_fxn").title}<`).length - 1).toBe(4);
  });
});
