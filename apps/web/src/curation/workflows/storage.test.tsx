import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { type Cell, withField } from "../ui/primitives";
import { ALL_WORKFLOWS } from "./registry";
import type { WorkflowDef } from "./types";

// Where a body stores what the curator entered, which the comparison page reads a stored field's
// kind from (`printedValue`): a count or a typed number under its own row's or input's id, a
// selection under a field id that is no row's or input's.
//
// Checked by drawing every body. A selection table marks the row whose id its field holds, and a
// count or value input shows the field it reads, which is the field it writes. So a body is drawn
// with every row and input id of its workflow filled in, and the radios marked and the inputs' text
// say which fields its controls use.
//
// Not reached: rows a table draws that the workflow's `cells` do not list (MIS_PRD's score bins),
// since no row id is known to fill a field with; and POP_FRQ's derived bin, whose radio is marked
// from the two typed frequencies rather than from a field (comparison.test.tsx covers it).

const EMPTY = create(WorkflowAssessmentSchema, {});

/** The answer with `cell` chosen in whichever table stores its selection under a prefix of the
 *  cell's id, which draws the tables that selection gates. */
function opened(cell: Cell): WorkflowAssessment {
  const parts = cell.id.split(".");
  let assessment = EMPTY;
  for (let i = 1; i < parts.length; i += 1) {
    const id = parts.slice(0, i).join(".");
    assessment = withField(assessment, { ...cell, id }, cell.id);
  }
  return assessment;
}

/** Every opening of every workflow, so a table gated on a sibling's answer — the RNA-assay tables
 *  under the splice prediction's branch — is drawn too. */
const OPENINGS = [EMPTY, ...ALL_WORKFLOWS.flatMap((w) => w.cells.map(opened))];

function rows(workflow: WorkflowDef): Cell[] {
  return [...workflow.cells, ...(workflow.inputs ?? [])];
}

function draw(
  workflow: WorkflowDef,
  assessment: WorkflowAssessment,
  opening: WorkflowAssessment,
): string {
  const siblings = Object.fromEntries(
    ALL_WORKFLOWS.map((w) => [w.id, opening]),
  );
  return renderToStaticMarkup(
    <workflow.Body
      assessment={assessment}
      siblings={siblings}
      onChange={() => {}}
      onBlur={() => {}}
    />,
  );
}

/** The openings that draw the workflow differently, one each. */
function distinctOpenings(workflow: WorkflowDef): WorkflowAssessment[] {
  const byMarkup = new Map<string, WorkflowAssessment>();
  for (const opening of OPENINGS) {
    const markup = draw(workflow, opening, opening);
    if (!byMarkup.has(markup)) byMarkup.set(markup, opening);
  }
  return [...byMarkup.values()];
}

function withRows(
  assessment: WorkflowAssessment,
  workflow: WorkflowDef,
  value: (cell: Cell) => string,
): WorkflowAssessment {
  return rows(workflow).reduce(
    (next, cell) => withField(next, cell, value(cell)),
    assessment,
  );
}

function inputs(markup: string, type: string): string[] {
  return (markup.match(/<input[^>]*>/g) ?? []).filter((tag) =>
    tag.includes(`type="${type}"`),
  );
}

function checkedRadios(markup: string): string[] {
  return inputs(markup, "radio")
    .filter((tag) => tag.includes('checked=""'))
    .map((tag) => tag.match(/aria-label="([^"]*)"/)?.[1] ?? tag);
}

const sentinel = (cell: Cell) => `#${cell.id}#`;

describe("what a workflow stores", () => {
  test("a count or typed value under its own row's or input's id", () => {
    const strays: string[] = [];
    let typed = 0;
    for (const workflow of ALL_WORKFLOWS) {
      const own = new Set(rows(workflow).map(sentinel));
      for (const opening of distinctOpenings(workflow)) {
        const markup = draw(
          workflow,
          withRows(opening, workflow, sentinel),
          opening,
        );
        for (const tag of inputs(markup, "text")) {
          typed += 1;
          const value = tag.match(/value="([^"]*)"/)?.[1];
          if (value === undefined || !own.has(value)) {
            strays.push(`${workflow.id}: ${tag}`);
          }
        }
      }
    }
    expect(strays).toEqual([]);
    // Non-empty rules out the pass where no input was drawn to check.
    expect(typed).toBeGreaterThan(0);
  });

  test(
    "a selection under an id that is no row's or input's",
    () => {
      // Filling every row and input id with one row's id marks that row in any table storing its
      // selection under such an id; a table storing it elsewhere is unmoved.
      const moved: string[] = [];
      let probed = 0;
      for (const workflow of ALL_WORKFLOWS) {
        for (const opening of distinctOpenings(workflow)) {
          const before = checkedRadios(draw(workflow, opening, opening));
          for (const cell of workflow.cells) {
            probed += 1;
            const filled = withRows(opening, workflow, () => cell.id);
            const after = checkedRadios(draw(workflow, filled, opening));
            if (after.join("\n") !== before.join("\n")) {
              moved.push(`${workflow.id}: ${cell.id}`);
            }
          }
        }
      }
      expect(moved).toEqual([]);
      // Non-empty rules out the pass where nothing was probed.
      expect(probed).toBeGreaterThan(0);
    },
    { timeout: 30_000 },
  );
});
