import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  DAFT_BINNING_TABLES,
  DAFT_BINNING_VERBATIM,
  DAFT_PENETRANCE_COLUMNS,
} from "./daft-binning";
import { DAFT_CALCULATOR_VERBATIM, DAFT_FORMULA } from "./daft-calculator";

// The two DAFT references say what the calculator says.
//
// A manual gate: the calculator's page is a capture of a logged-in session and is not committed, so
// `THEMIS_SVCV4_CAPTURES` names a local directory holding it and these checks skip where it is unset.
// Run them by hand against that copy after changing a transcription.
//
// The thresholds carry a second check that needs no capture: `bun run daft` exports them and
// `themis/curation/tests/test_daft_tables.py` holds that export against the library's own reading of
// SM3's images.

const CAPTURES = process.env.THEMIS_SVCV4_CAPTURES;

if (CAPTURES === undefined) {
  console.warn(
    "reference.test.ts: THEMIS_SVCV4_CAPTURES is unset, so the DAFT references are not checked against the calculator's page; point it at a directory holding calculator-source.txt.",
  );
}

const CALCULATOR =
  CAPTURES === undefined
    ? ""
    : readFileSync(join(CAPTURES, "calculator-source.txt"), "utf-8");

/** Collapse whitespace, as `workflows/fidelity.test.ts` does for the same reason: the capture is a
 *  rendered page, so line breaks fall where the layout put them. Case is NOT folded: the table titles
 *  are the framework's capitals and `BRCA1`, `PMID` and `MDE` are its own, so a case change is a
 *  change. */
function normalise(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

const CALCULATOR_TEXT = normalise(CALCULATOR);

/** The binning modal's own span of the capture, from its heading to the next modal's. Every number
 *  and label the modal renders is parsed out of this rather than compared string by string, so a
 *  value nobody transcribed is as visible as one transcribed wrongly. */
function binningSpan(): string {
  const from = CALCULATOR_TEXT.indexOf(
    "Disease Allele Frequency Threshold - Binning Approach",
  );
  const to = CALCULATOR_TEXT.indexOf(
    "Determine Maximum Credible Population Allele Frequency",
    from,
  );
  if (from < 0 || to <= from) {
    throw new Error("the capture holds no binning-approach modal");
  }
  return CALCULATOR_TEXT.slice(from, to);
}

const PREVALENCE = /^1\/[\d,]+$/;
const THRESHOLD = /^0\.\d+$/;
const COUNT = /^\d{1,3}(,\d{3})*$/;

interface ParsedRow {
  prevalence: string;
  cells: [string, string | null][];
}

/** Every row of one table as the capture prints it. Asserts its way forward rather than resyncing on
 *  a pattern: a parser that recovered from a surprise would manufacture agreement, which is the one
 *  thing a fidelity check must not do. An allele count absent between two thresholds is a cell the
 *  calculator printed without one. */
function parseTable(title: string): ParsedRow[] {
  const span = binningSpan();
  const start = span.indexOf(title);
  if (start < 0) throw new Error(`the capture holds no table titled ${title}`);
  const rest = span.slice(start + title.length);
  const tokens = rest.split(" ");
  const rows: ParsedRow[] = [];
  let i = tokens.findIndex((t) => PREVALENCE.test(t));
  while (i > 0 && i < tokens.length && rows.length < 8) {
    const prevalence = tokens[i];
    i += 1;
    const cells: [string, string | null][] = [];
    while (cells.length < 3) {
      const threshold = tokens[i];
      if (!THRESHOLD.test(threshold)) {
        throw new Error(
          `${title} ${prevalence}: expected a threshold, read ${threshold}`,
        );
      }
      i += 1;
      const next = tokens[i];
      const count =
        next !== undefined && COUNT.test(next) && !PREVALENCE.test(next)
          ? next
          : null;
      if (count !== null) i += 1;
      cells.push([threshold, count]);
    }
    rows.push({ prevalence, cells });
  }
  return rows;
}

describe.skipIf(CAPTURES === undefined)(
  "the DAFT references are transcribed verbatim",
  () => {
    test("the calculator's page is present and substantial", () => {
      // Rules out a vacuous pass if the fixture is ever truncated.
      expect(CALCULATOR_TEXT.length).toBeGreaterThan(10_000);
    });

    test.each([
      ["calculator approach", DAFT_CALCULATOR_VERBATIM],
      ["binning approach", DAFT_BINNING_VERBATIM],
    ])("every string of the %s is the calculator's", (_name, strings) => {
      expect(strings.length).toBeGreaterThan(5);
      const invented = strings.filter(
        (text) => !CALCULATOR_TEXT.includes(normalise(text)),
      );
      expect(invented).toEqual([]);
    });

    test("every prevalence row label is the calculator's", () => {
      // Including the male table's `1/1000` and `1/5000`, which the other five write with separators.
      const invented = DAFT_BINNING_TABLES.flatMap((table) =>
        table.rows
          .map((row) => row.prevalence)
          .filter((label) => !CALCULATOR_TEXT.includes(normalise(label))),
      );
      expect(invented).toEqual([]);
    });
  },
);

describe("the transcribed cells state what the framework gave", () => {
  test("a threshold the calculator prints with no allele count stays distinguishable", () => {
    // Two cells of the X-linked dominant combined table print a threshold and no count. `null` says
    // the framework gave none; a zero or a blank string would say somebody read one.
    const blank = DAFT_BINNING_TABLES.flatMap((table) =>
      table.rows.flatMap((row) =>
        row.cells
          .filter(([, ac]) => ac === null)
          .map(() => `${table.title}|${row.prevalence}`),
      ),
    );
    expect(blank).toEqual([
      "X-LINKED DOMINANT - COMBINED (combined male and female prevalence)|1/500",
      "X-LINKED DOMINANT - COMBINED (combined male and female prevalence)|1/1,000",
    ]);
  });
});

describe.skipIf(CAPTURES === undefined)(
  "every number the modal prints is the calculator's",
  () => {
    test("every cell of every table is the calculator's, allele counts included", () => {
      // The thresholds have a second reading to check against, in the library; the 144 allele counts
      // have none — they are in no supplement and no library. So they are read out of the capture
      // here. A one-digit slip in a count is invisible to every other check in this file.
      let compared = 0;
      for (const table of DAFT_BINNING_TABLES) {
        const printed = parseTable(table.title);
        expect(printed.map((row) => row.prevalence)).toEqual(
          table.rows.map((row) => row.prevalence),
        );
        for (const [index, row] of table.rows.entries()) {
          expect({
            table: table.title,
            row: row.prevalence,
            cells: row.cells,
          }).toEqual({
            table: table.title,
            row: row.prevalence,
            cells: printed[index].cells,
          });
          compared += row.cells.length * 2;
        }
      }
      // 6 tables x 8 rows x 3 columns x (threshold, count).
      expect(compared).toBe(288);
    });

    test("nothing of either modal is left untranscribed", () => {
      // The checks above catch invented text; this one catches dropped text. Subtract every string the
      // components render from the modal's own span of the capture, and what remains has to be the
      // chrome the dialog supplies itself. A dropped recommendation, heading, row or column leaves its
      // own words behind here.
      const rendered = [
        ...DAFT_CALCULATOR_VERBATIM,
        ...DAFT_BINNING_VERBATIM,
        ...DAFT_BINNING_TABLES.flatMap((table) =>
          table.rows.flatMap((row) => [
            row.prevalence,
            ...row.cells.flatMap(([daft, ac]) => (ac ? [daft, ac] : [daft])),
          ]),
        ),
        ...DAFT_PENETRANCE_COLUMNS,
      ];
      const from = CALCULATOR_TEXT.indexOf(
        "Disease Allele Frequency Threshold - Binning Approach",
      );
      const to = CALCULATOR_TEXT.indexOf("OK x --> Located unsaved data", from);
      let residual = CALCULATOR_TEXT.slice(from, to);
      // Longest first, so a string that contains another does not leave the shorter one orphaned.
      for (const text of [...rendered].sort((a, b) => b.length - a.length)) {
        residual = residual.split(normalise(text)).join(" ");
      }
      // `×` and `OK` are each modal's own close cross and dismiss button, which `reference-dialog.tsx`
      // supplies; `×` is also the formula's operator, and the formula is the disclosed exception below.
      const words = residual
        .split(/\s+/)
        .filter((word) => word !== "" && word !== "×" && word !== "OK");
      // All that may remain is the formula, letter-spaced by the capture's MathML stripping — and it has
      // to be the formula the component renders, letter for letter. That is what pins the framework's own
      // spelling of `Heterogenity`, which appears nowhere else and would otherwise be silently
      // correctable. Only the operators differ, since the linearisation adds the parens and the slash.
      const letters = (text: string) => text.replace(/[^A-Za-z]/g, "");
      expect(letters(words.join(""))).toBe(letters(DAFT_FORMULA));
    });
  },
);
