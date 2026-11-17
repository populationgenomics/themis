import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  mock,
  spyOn,
  test,
} from "bun:test";
import {
  create,
  type JsonValue,
  type MessageInitShape,
  toJson,
} from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  findAll,
  install,
  MiniElement,
} from "@/components/minidom.test-support";
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
import { workflowById } from "../workflows/registry";
import type { WorkflowDef } from "../workflows/types";
import { Comparison, type CuratorAnswers } from "./comparison";
import { cellLabel, fieldValue, withField } from "./primitives";

// The comparison names each section the way the worksheet named it to the curator, marks whether
// the curators agreed, and prints a stored value only where the label beside it does not already
// say it.

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
});

afterEach(() => {
  mock.restore();
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  dom.uninstall();
});

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

function renderComparison(submitted: CuratorAnswers[]): string {
  return renderToStaticMarkup(
    <Comparison clinicalContext={CLINICAL_CONTEXT} answers={submitted} />,
  );
}

async function mountComparison(
  submitted: CuratorAnswers[],
): Promise<MiniElement> {
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });
  await React.act(async () => {
    root.render(
      <Comparison clinicalContext={CLINICAL_CONTEXT} answers={submitted} />,
    );
  });
  return container;
}

/** The printed `<li>` rows (label and optional monospaced value span) of the comparison. */
function fieldRows(
  container: MiniElement,
): { text: string; printedValue: string | undefined }[] {
  return (
    findAll(
      container,
      (node) => node instanceof MiniElement && node.tagName === "LI",
    ) as MiniElement[]
  ).map((li) => {
    const [span] = findAll(
      li,
      (node) => node instanceof MiniElement && node.tagName === "SPAN",
    ) as MiniElement[];
    return { text: li.textContent, printedValue: span?.textContent };
  });
}

/** The `<section>` whose heading contains `heading`. */
function sectionOf(markup: string, heading: string): string {
  const found = markup
    .split("<section ")
    .slice(1)
    .map((part) => `<section ${part}`)
    .find((part) => part.includes(heading));
  if (found === undefined) throw new Error(`no section for ${heading}`);
  return found.slice(0, found.indexOf("</section>") + "</section>".length);
}

describe("corrupt submissions", () => {
  test("refuses a submission where a section's id holds another kind", () => {
    expect(() =>
      renderComparison([answers("a@x", { [VERDICT_ID]: scored(EMPTY) })]),
    ).toThrow(/a@x's submission holds workflow under verdict/);
  });

  test("refuses a submission where a workflow's id holds a section", () => {
    expect(() =>
      renderComparison([
        answers("a@x", { cds_fxn: json({ case: "routing", value: {} }) }),
      ]),
    ).toThrow(/holds routing under cds_fxn/);
  });
});

describe("the comparison", () => {
  const markup = renderComparison([
    answers("a@x", {
      [ROUTING_ID]: json({
        case: "routing",
        value: {
          consequenceClass: Consequence.INFRAME_INDEL,
          rationale: "Three-base deletion in exon 10.",
        },
      }),
      [VERDICT_ID]: json({
        case: "verdict",
        value: {
          classification: Classification.PATHOGENIC,
          classDeterminative: ["cds_fxn", "a_retired_workflow"],
        },
      }),
      cds_fxn: scored(selected("cds_fxn", "cds_fxn", "cds_fxn.no")),
      zz_retired: scored(EMPTY),
      pop_frq: scored(EMPTY),
    }),
    answers("b@x", {
      [VERDICT_ID]: json({
        case: "verdict",
        value: { classification: Classification.LIKELY_PATHOGENIC },
      }),
      cds_prd: scored(EMPTY),
      aa_retired: scored(EMPTY),
      cds_fxn: scored(selected("cds_fxn", "cds_fxn", "cds_fxn.yes")),
    }),
  ]);

  test("draws clinical context, routing, registered workflows in registry order, retired workflows in id order, and verdict", () => {
    const at = (text: string) => {
      const index = markup.indexOf(text);
      expect(index).toBeGreaterThanOrEqual(0);
      return index;
    };
    const sequence = [
      ">Clinical context<",
      ">Routing<",
      `>${registered("pop_frq").title}<`,
      `>${registered("cds_prd").title}<`,
      `>${registered("cds_fxn").title}<`,
      ">aa_retired<",
      ">zz_retired<",
      ">Verdict<",
    ];
    const positions = sequence.map(at);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    expect(markup).toContain("Three-base deletion in exon 10.");
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
    expect(sectionOf(markup, ">Verdict<")).toContain(">Class-determinative<");
    expect(sectionOf(markup, ">Verdict<")).toContain(">a_retired_workflow<");
  });
});

describe("routing and verdict agreement", () => {
  test.each([
    [
      "both curators state the same consequence class",
      [Consequence.MISSENSE, Consequence.MISSENSE],
      true,
      "same consequence class",
    ],
    [
      "curators state different consequence classes",
      [Consequence.MISSENSE, Consequence.INFRAME_INDEL],
      false,
      "not the same consequence class",
    ],
    [
      "both curators leave the consequence class unstated",
      [Consequence.UNSPECIFIED, Consequence.UNSPECIFIED],
      false,
      "not the same consequence class",
    ],
    [
      "neither curator submitted a routing section",
      [undefined, undefined],
      false,
      "not the same consequence class",
    ],
  ] as const)("routing when %s", (_, [first, second], isAgreed, label) => {
    const entry = (
      consequenceClass: Consequence | undefined,
    ): Record<string, JsonValue> =>
      consequenceClass === undefined
        ? {}
        : {
            [ROUTING_ID]: json({
              case: "routing",
              value: { consequenceClass },
            }),
          };
    const section = sectionOf(
      renderComparison([
        answers("a@x", entry(first)),
        answers("b@x", entry(second)),
      ]),
      ">Routing<",
    );
    expect(section).toContain(`>${label}<`);
    expect(section.includes("border-amber-uncertainty-border")).toBe(!isAgreed);
  });

  test.each([
    [
      "both curators state the same classification",
      [Classification.VUS, Classification.VUS],
      true,
      "same classification",
    ],
    [
      "curators state different classifications",
      [Classification.VUS, Classification.BENIGN],
      false,
      "different classification",
    ],
    [
      "both curators leave the classification unstated",
      [Classification.UNSPECIFIED, Classification.UNSPECIFIED],
      false,
      "different classification",
    ],
    [
      "neither curator submitted a verdict",
      [undefined, undefined],
      false,
      "different classification",
    ],
  ] as const)("verdict when %s", (_, [first, second], isAgreed, label) => {
    const entry = (
      classification: Classification | undefined,
    ): Record<string, JsonValue> =>
      classification === undefined
        ? {}
        : {
            [VERDICT_ID]: json({
              case: "verdict",
              value: { classification },
            }),
          };
    const section = sectionOf(
      renderComparison([
        answers("a@x", entry(first)),
        answers("b@x", entry(second)),
      ]),
      ">Verdict<",
    );
    expect(section).toContain(`>${label}<`);
    expect(section.includes("border-amber-uncertainty-border")).toBe(!isAgreed);
  });
});

describe("a stored value", () => {
  test("is not printed for a selection, whose label already names the row", async () => {
    const answer = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
    const container = await mountComparison([
      answers("a@x", { cds_fxn: scored(answer) }),
      answers("b@x", {}),
    ]);
    expect(fieldRows(container)).toEqual([
      { text: answer.fields[0].label, printedValue: undefined },
    ]);
  });

  test("is not printed for a selection from a table the workflow's cells do not list", async () => {
    // MIS_PRD's score bins depend on the predictor and are not among its `cells`; the bin is still
    // stored under a table's field id, never a row's.
    const bin: FieldValue = fieldValue(
      { id: "mis_prd.bin", cell: "MIS_PRD.revel.t2", label: "0.773 - 0.878" },
      "mis_prd.bin.t2",
    );
    const container = await mountComparison([
      answers("a@x", {
        mis_prd: scored({ ...EMPTY, fields: [bin] }),
      }),
      answers("b@x", {}),
    ]);
    expect(fieldRows(container)).toEqual([
      { text: "0.773 - 0.878", printedValue: undefined },
    ]);
  });

  test("is printed for the two frequencies typed, and not for the row they derive", async () => {
    let answer = withField(EMPTY, FRQ_DAFT, "0.0153");
    answer = withDerivedBin(withField(answer, FRQ_FAF, "0.00984"));
    const derivedRow = answer.fields.find((f) => f.fieldId === "pop_frq.bin");
    if (derivedRow === undefined) throw new Error("no derived bin");

    const container = await mountComparison([
      answers("a@x", { pop_frq: scored(answer) }),
      answers("b@x", {}),
    ]);
    expect(fieldRows(container)).toEqual([
      { text: derivedRow.label, printedValue: undefined },
      {
        text: "Disease Allele Frequency Threshold0.0153",
        printedValue: "0.0153",
      },
      {
        text: "Variant GrpMax Filtering Allele Frequency0.00984",
        printedValue: "0.00984",
      },
    ]);
  });

  test("is printed for a count", async () => {
    const workflow = registered("cln_aff_arxl");
    const answer = withField(EMPTY, workflow.cells[0], "3");
    const container = await mountComparison([
      answers("a@x", { cln_aff_arxl: scored(answer) }),
      answers("b@x", {}),
    ]);
    expect(fieldRows(container)).toEqual([
      { text: `${answer.fields[0].label}3`, printedValue: "3" },
    ]);
  });

  test("is printed whole when pinned to an earlier transcription", async () => {
    const selection = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
    const earlier = await mountComparison([
      answers("a@x", { cds_fxn: scored(selection) }, "an-earlier-version"),
      answers("b@x", {}),
    ]);
    expect(fieldRows(earlier)[0]?.printedValue).toBe("cds_fxn.no");
  });

  test("is printed whole on a retired workflow", async () => {
    const selection = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
    const retired = await mountComparison([
      answers("a@x", { retired_wf: scored(selection) }),
      answers("b@x", {}),
    ]);
    expect(fieldRows(retired)[0]?.printedValue).toBe("cds_fxn.no");
  });
});

describe("a workflow's agreement", () => {
  const no = selected("cds_fxn", "cds_fxn", "cds_fxn.no");
  const yes = selected("cds_fxn", "cds_fxn", "cds_fxn.yes");
  const as = (
    status: AssessmentStatus,
    workflow: WorkflowAssessment = EMPTY,
  ): JsonValue => json({ case: "workflow", value: { ...workflow, status } });
  const { SCORED, NOT_APPLICABLE, NO_DATA, UNSPECIFIED } = AssessmentStatus;
  const counted = (n: string) =>
    withField(EMPTY, registered("cln_aff_arxl").cells[0], n);
  const frequency = (faf: string) =>
    withDerivedBin(
      withField(withField(EMPTY, FRQ_DAFT, "0.0153"), FRQ_FAF, faf),
    );

  test.each([
    [
      "the same row",
      "cds_fxn",
      [as(SCORED, no), as(SCORED, no)],
      true,
      "same answer",
    ],
    [
      "different rows",
      "cds_fxn",
      [as(SCORED, no), as(SCORED, yes)],
      false,
      "different cells",
    ],
    [
      "one status kept the fields the other scored",
      "cds_fxn",
      [as(SCORED, no), as(NOT_APPLICABLE, no)],
      false,
      "different status",
    ],
    [
      "the same unscored status",
      "cds_fxn",
      [as(NO_DATA), as(NO_DATA)],
      true,
      "same answer",
    ],
    [
      "one curator answered nothing",
      "cds_fxn",
      [as(NOT_APPLICABLE), undefined],
      false,
      "not answered by every curator",
    ],
    [
      "no curator chose a status",
      "cds_fxn",
      [as(UNSPECIFIED), as(UNSPECIFIED)],
      false,
      "not answered by every curator",
    ],
    [
      "one row holding different counts",
      "cln_aff_arxl",
      [as(SCORED, counted("3")), as(SCORED, counted("2"))],
      false,
      "different values",
    ],
    [
      "one row holding the same count",
      "cln_aff_arxl",
      [as(SCORED, counted("3")), as(SCORED, counted("3"))],
      true,
      "same answer",
    ],
    [
      "the same derived row from different frequencies",
      "pop_frq",
      [as(SCORED, frequency("0.00984")), as(SCORED, frequency("0.0104"))],
      false,
      "different values",
    ],
  ] as const)("reads %s", (_, workflowId, [first, second], isAgreed, label) => {
    const section = sectionOf(
      renderComparison([
        answers("a@x", first === undefined ? {} : { [workflowId]: first }),
        answers("b@x", second === undefined ? {} : { [workflowId]: second }),
      ]),
      `>${registered(workflowId).title}<`,
    );
    expect(section).toContain(`>${label}<`);
    expect(section.includes("border-amber-uncertainty-border")).toBe(!isAgreed);
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
      const section = sectionOf(
        renderComparison([
          answers("a@x", { [workflowId]: scored(first) }, "svcv4-earlier"),
          answers("b@x", { [workflowId]: scored(second) }),
        ]),
        `>${registered(workflowId).title}<`,
      );
      expect(section).toContain(`>${label}<`);
    },
  );
});

describe("an unscored answer", () => {
  const markup = renderComparison(
    ["a@x", "b@x"].map((curator) =>
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
    ),
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
    renderComparison([
      answers("a@x", { cds_fxn: scored(EMPTY) }, versions[0]),
      answers("b@x", { cds_fxn: scored(EMPTY) }, versions[1]),
    ]);

  test("is named beside each curator where the curators' differ", () => {
    const markup = render(["svcv4-old", WORKFLOWS_VERSION]);
    expect(markup).toContain("a@x · svcv4-old · scored");
    expect(markup).toContain(`b@x · ${WORKFLOWS_VERSION} · scored`);
    expect(markup).toContain(">a@x · svcv4-old<");
  });

  test("is not named where they share it", () => {
    expect(render([WORKFLOWS_VERSION, WORKFLOWS_VERSION])).not.toContain(
      WORKFLOWS_VERSION,
    );
  });
});

describe("a class-determinative list naming one workflow twice", () => {
  test("draws every occurrence without a duplicate-key error", async () => {
    const errorSpy = spyOn(console, "error").mockImplementation(() => {});
    const container = await mountComparison(
      ["a@x", "b@x"].map((curator) =>
        answers(curator, {
          [VERDICT_ID]: json({
            case: "verdict",
            value: { classDeterminative: ["cds_fxn", "pop_frq", "cds_fxn"] },
          }),
        }),
      ),
    );
    expect(errorSpy).not.toHaveBeenCalled();
    expect(
      container.textContent.split(registered("cds_fxn").title).length - 1,
    ).toBe(4);
  });
});
