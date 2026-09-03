import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import { WorkflowAssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import {
  type Cell,
  ChoiceRows,
  CountRows,
  cellLabel,
  DerivedRows,
  sections,
} from "./primitives";

// What the calculator's rowspan has to survive transcription: the description stands over the rows
// it covers instead of being read as one of them, and every row it covers is still answerable.

const CRITERION = "Variants in the same donor region as the VBC";
const BENIGN = "Variants of the opposite consequence";

const CELLS: Cell[] = [
  {
    id: "t.p_first",
    cell: "T.p_first",
    group: CRITERION,
    label: "First P Variant",
  },
  {
    id: "t.lp_first",
    cell: "T.lp_first",
    group: CRITERION,
    label: "First LP Variant",
  },
  { id: "t.vus", cell: "T.vus", label: "VUS informative variants" },
  {
    id: "t.b_first",
    cell: "T.b_first",
    group: BENIGN,
    label: "First B Variant",
  },
];

const COUNT_LABEL = "Applicable variants";

/** What the calculator prints for this table, in order: each description once, then exactly the
 *  rows it covers, with the undescribed row standing on its own. */
const PRINTED = [
  CRITERION,
  "First P Variant",
  "First LP Variant",
  "VUS informative variants",
  BENIGN,
  "First B Variant",
];

function occurrences(markup: string, needle: string): number {
  return markup.split(needle).length - 1;
}

/** A heading stands in an element of its own, where the same wording inside an `aria-label` does
 *  not — which is how a printed-once heading is told from a repeated one. */
function headings(markup: string, group: string): number {
  return occurrences(markup, `>${group}<`);
}

/** The markup's printed text, in document order. Attribute values go with the tags that carry them,
 *  so an `aria-label` repeating a description does not read as a second printing of it. */
function printed(markup: string): string[] {
  return markup
    .split(/<[^>]*>/)
    .map((text) => text.trim())
    .filter((text) => text !== "");
}

function countMarkup(cells: Cell[]): string {
  return renderToStaticMarkup(
    <CountRows
      cells={cells}
      assessment={create(WorkflowAssessmentSchema, {})}
      onChange={() => {}}
      countLabel={COUNT_LABEL}
    />,
  );
}

function choiceMarkup(cells: Cell[]): string {
  return renderToStaticMarkup(
    <ChoiceRows name="t" cells={cells} value="" onChange={() => {}} />,
  );
}

function derivedMarkup(cells: Cell[]): string {
  return renderToStaticMarkup(
    <DerivedRows cells={cells} selected={null} note="" />,
  );
}

describe("a composed cell label", () => {
  test("reads as the calculator prints the row, left to right", () => {
    expect(
      cellLabel({
        id: "x",
        cell: "X",
        group: "Group",
        label: "Row",
        detail: "Qualifier",
      }),
    ).toBe("Group — Row — Qualifier");
  });

  test("skips the parts the row does not have", () => {
    expect(cellLabel({ id: "x", cell: "X", label: "Row" })).toBe("Row");
    expect(
      cellLabel({ id: "x", cell: "X", group: "Group", label: "Row" }),
    ).toBe("Group — Row");
  });
});

describe("the sections a table is split into", () => {
  test("are the maximal runs of one description", () => {
    expect(sections(CELLS).map((section) => section.group)).toEqual([
      CRITERION,
      undefined,
      BENIGN,
    ]);
  });

  test("carry every row once, in the order the calculator prints them", () => {
    expect(sections(CELLS).flatMap((section) => section.cells)).toEqual(CELLS);
  });

  test("hold rows that all belong to the section's own description", () => {
    for (const section of sections(CELLS)) {
      for (const cell of section.cells) {
        expect(cell.group).toBe(section.group);
      }
    }
  });
});

describe("a table of counted rows", () => {
  test("prints a description once, not once per row it covers", () => {
    // Printed per row, one description reads as three criteria rather than as one criterion
    // counted three ways.
    const markup = countMarkup(CELLS);
    expect(headings(markup, CRITERION)).toBe(1);
    expect(headings(markup, BENIGN)).toBe(1);
  });

  test("prints each row under its own description", () => {
    // Counting headings cannot tell one printed over the wrong rows from one printed over its own.
    expect(printed(countMarkup(CELLS))).toEqual([COUNT_LABEL, ...PRINTED]);
  });

  test("prints nothing over a run that has no description", () => {
    expect(printed(countMarkup([CELLS[2]]))).toEqual([
      COUNT_LABEL,
      CELLS[2].label,
    ]);
  });

  test("takes a count for every row, spanned or not", () => {
    expect(occurrences(countMarkup(CELLS), "<input")).toBe(CELLS.length);
  });

  test("names the description and the row in the input a curator answers", () => {
    // The input carries no visible label of its own, so what a screen reader reads is the whole of
    // what the count means.
    const markup = countMarkup(CELLS);
    for (const cell of CELLS) {
      expect(markup).toContain(
        `aria-label="${COUNT_LABEL}: ${cellLabel(cell)}"`,
      );
    }
  });

  test("indents the rows one description covers, and only those", () => {
    expect(occurrences(countMarkup(CELLS), "pl-4")).toBe(2);
    expect(occurrences(countMarkup([CELLS[2]]), "pl-4")).toBe(0);
  });
});

describe("a table of choices", () => {
  test("prints a description once, with a choice for every row", () => {
    const markup = choiceMarkup(CELLS);
    expect(headings(markup, CRITERION)).toBe(1);
    expect(headings(markup, BENIGN)).toBe(1);
    expect(occurrences(markup, 'type="radio"')).toBe(CELLS.length);
  });

  test("prints each row under its own description", () => {
    expect(printed(choiceMarkup(CELLS))).toEqual(PRINTED);
  });

  test("names the description and the row in the choice a curator makes", () => {
    // The label around a radio holds the row's own wording, which two branches of one table repeat
    // verbatim; without the description the two reach a screen reader as the same choice.
    const markup = choiceMarkup(CELLS);
    for (const cell of CELLS) {
      expect(markup).toContain(`aria-label="${cellLabel(cell)}"`);
    }
  });
});

describe("a table of rows a typed value selects", () => {
  test("prints each row under its own description", () => {
    expect(printed(derivedMarkup(CELLS))).toEqual(PRINTED);
  });

  test("names the description and the row in the row a curator reads", () => {
    // Nothing labels the disabled radio but its `aria-label`, so that is the whole of what a screen
    // reader has to go on.
    const markup = derivedMarkup(CELLS);
    for (const cell of CELLS) {
      expect(markup).toContain(`aria-label="${cellLabel(cell)}"`);
    }
  });
});
