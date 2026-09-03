import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { type Cell, fieldValue, sections, withField } from "../ui/primitives";
import { ALL_WORKFLOWS, refuseUnreadableDescriptions } from "./registry";
import type { WorkflowDef } from "./types";

// The transcribed tables hold the calculator's rowspans, and a stored answer says what the curator
// read under one.

/** Rows the calculator only ever prints under a section description: on their own they name a
 *  position in a count, not a criterion, so one reaching a curator undescribed is a question with no
 *  subject. */
const COUNTED_ROW =
  /^(First (P|LP|B|LB) Variant|Additional (P\/LP|B\/LB) variants)/;

function stub(cells: Cell[], inputs?: Cell[]): WorkflowDef {
  return {
    id: "stub",
    code: "SPL_INF",
    title: "Workflow for Informative Variants",
    cells,
    inputs,
    applies: () => true,
    Body: () => null,
  };
}

/** The answer a body reads back after the curator picks `cell`. Written under every prefix of the
 *  cell's id, which is where the bodies that store a whole table's answer under one field put it —
 *  the predictor of `MIS_PRD`, the step 1 of `LOC_PHE`. */
function chosen(cell: Cell): WorkflowAssessment {
  const parts = cell.id.split(".");
  let assessment = create(WorkflowAssessmentSchema, {});
  for (let i = 1; i <= parts.length; i += 1) {
    const id = parts.slice(0, i).join(".");
    assessment = withField(assessment, { ...cell, id }, cell.id);
  }
  return assessment;
}

/** Every state the worksheet's own answers can put a body in: nothing answered, and each cell of
 *  the registry chosen. A table drawn only once something else is answered — the score bins under a
 *  predictor, the assay table under a prediction branch — is drawn in one of these. */
function states(): WorkflowAssessment[] {
  return [
    create(WorkflowAssessmentSchema, {}),
    ...ALL_WORKFLOWS.flatMap((workflow) => workflow.cells.map(chosen)),
  ];
}

describe("the transcribed tables", () => {
  test("carry the description over every row that means nothing without one", () => {
    const orphaned: string[] = [];
    let counted = 0;
    for (const workflow of ALL_WORKFLOWS) {
      for (const cell of workflow.cells) {
        if (!COUNTED_ROW.test(cell.label)) continue;
        counted += 1;
        if (cell.group === undefined || cell.group === "") {
          orphaned.push(cell.cell);
        }
      }
    }
    expect(orphaned).toEqual([]);
    // Non-empty rules out the pass where the rows stopped matching and nothing was checked.
    expect(counted).toBeGreaterThan(0);
  });

  test("store the description a row was answered under, not the row alone", () => {
    let described = 0;
    for (const workflow of ALL_WORKFLOWS) {
      for (const cell of [...workflow.cells, ...(workflow.inputs ?? [])]) {
        const stored = fieldValue(cell, "2").label;
        expect(stored).toContain(cell.label);
        if (cell.group === undefined) continue;
        described += 1;
        expect(stored).toContain(cell.group);
      }
    }
    // Non-empty rules out the pass where no row carries a description at all.
    expect(described).toBeGreaterThan(0);
  });

  test(
    "draw in every state an answer can put them in",
    () => {
      // A table whose sections cannot be drawn throws where it is rendered, so every table the
      // registry can reach has to be rendered for that to be a build-time failure rather than a
      // curator's blank screen.
      const answers = states();
      let drawn = 0;
      for (const assessment of answers) {
        const siblings = Object.fromEntries(
          ALL_WORKFLOWS.map((workflow) => [workflow.id, assessment]),
        );
        for (const workflow of ALL_WORKFLOWS) {
          renderToStaticMarkup(
            <workflow.Body
              assessment={assessment}
              siblings={siblings}
              onChange={() => {}}
              onBlur={() => {}}
            />,
          );
          drawn += 1;
        }
      }
      expect(drawn).toBe(answers.length * ALL_WORKFLOWS.length);
      // Non-empty rules out the pass where the registry held nothing to draw.
      expect(drawn).toBeGreaterThan(0);
    },
    // Every body in every state is several thousand renders: seconds on a CI runner.
    { timeout: 30_000 },
  );
});

describe("a table whose sections cannot be drawn", () => {
  test("is refused when one description resumes after another", () => {
    // Two runs of one description would print the same heading twice, with nothing to say which
    // rows each covers.
    expect(() =>
      sections([
        { id: "a", cell: "T.a", group: "First", label: "One" },
        { id: "b", cell: "T.b", group: "Second", label: "Two" },
        { id: "c", cell: "T.c", group: "First", label: "Three" },
      ]),
    ).toThrow(/resumes a section/);
  });

  test("is refused when one description resumes after an undescribed row", () => {
    expect(() =>
      sections([
        { id: "a", cell: "T.a", group: "First", label: "One" },
        { id: "b", cell: "T.b", label: "Two" },
        { id: "c", cell: "T.c", group: "First", label: "Three" },
      ]),
    ).toThrow(/resumes a section/);
  });

  test("is refused when a description is blank", () => {
    expect(() =>
      sections([{ id: "a", cell: "T.a", group: "   ", label: "One" }]),
    ).toThrow(/blank section description/);
  });

  test("is drawable with an undescribed row between two descriptions", () => {
    expect(() =>
      sections([
        { id: "a", cell: "T.a", group: "First", label: "One" },
        { id: "b", cell: "T.b", label: "Ungrouped" },
        { id: "c", cell: "T.c", group: "Second", label: "Two" },
      ]),
    ).not.toThrow();
  });
});

describe("a description a curator would never read", () => {
  test("is refused when it is blank", () => {
    expect(() =>
      refuseUnreadableDescriptions([
        stub([{ id: "a", cell: "T.a", group: " ", label: "One" }]),
      ]),
    ).toThrow(/blank section description/);
  });

  test("is refused on a control that answers no row", () => {
    // `ValueField` prints its cell's label and nothing over it, so a description there would reach
    // the stored answer without ever reaching the screen.
    expect(() =>
      refuseUnreadableDescriptions([
        stub(
          [{ id: "a", cell: "T.a", label: "One" }],
          [{ id: "b", cell: "T.b", group: "Heading", label: "A number" }],
        ),
      ]),
    ).toThrow(/no table prints/);
  });

  test("is not what one description on two of a workflow's tables is", () => {
    // `cells` is the union of every table a workflow renders, so a description reused by a second
    // table reappears in it. Whether each table can draw its own sections is that table's business.
    expect(() =>
      refuseUnreadableDescriptions([
        stub([
          { id: "a", cell: "T.a", group: "First", label: "One" },
          { id: "b", cell: "U.b", group: "Second", label: "Two" },
          { id: "c", cell: "V.c", group: "First", label: "Three" },
        ]),
      ]),
    ).not.toThrow();
  });
});
