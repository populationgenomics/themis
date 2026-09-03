import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cellLabel } from "../ui/primitives";
import { ALL_WORKFLOWS } from "./registry";

// The transcription says what the framework says.
//
// That is the whole basis for a curator answering these workflows without a second manual, and for
// their answer being comparable to one given in the calculator itself. It is also the one property
// no other check reaches: a paraphrased row still typechecks, still renders, still stores, and is
// silently a different question.
//
// Three sources, because the framework speaks in three places. `calculator-source.txt` is the
// ClinGen Pilot Calculator's rendered text (tags stripped, input values and placeholders kept,
// whitespace collapsed); `supplement-sm4.txt` and `supplement-sm5.txt` are Supplementary Materials 4
// and 5, which carry the codes the calculator scores without printing a workflow for them.
//
// A label has to be verbatim in one of them, and which one is reported: a workflow that leaves the
// calculator for a supplement is a provenance claim, not a detail.
//
// A manual gate: one source is a capture of a logged-in page and two are supplements' running text,
// and none of them is committed — this repository carries the framework's names and values and no
// supplement prose. `THEMIS_SVCV4_CAPTURES` names a local directory holding the three, and these
// checks skip where it is unset. Run them by hand after changing a transcription.

const CALCULATOR = "calculator-source.txt";
const CAPTURES = process.env.THEMIS_SVCV4_CAPTURES;

if (CAPTURES === undefined) {
  console.warn(
    `fidelity.test.ts: THEMIS_SVCV4_CAPTURES is unset, so the transcription is not checked against the framework's own text; point it at a directory holding ${CALCULATOR}, supplement-sm4.txt and supplement-sm5.txt.`,
  );
}

const SOURCES =
  CAPTURES === undefined
    ? []
    : [CALCULATOR, "supplement-sm4.txt", "supplement-sm5.txt"].map((name) => ({
        name,
        text: normalise(readFileSync(join(CAPTURES, name), "utf-8")),
      }));

/** Collapse whitespace and the spacing around comparison operators, which the calculator is not
 *  internally consistent about (`>=( 1.5x` in one bin, `>=(15x` in the next). Nothing else is
 *  softened: wording, punctuation and parenthesisation all have to match. */
function normalise(text: string): string {
  return text
    .replace(/\s*([<>=])\s*/g, "$1")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

/** A composed label — a group heading joined to its row, or three nested conditions joined by AND,
 *  as several tables print them — is checked part by part. EVERY part must be the framework's, not
 *  merely one of them: a check that accepted one matching fragment passed a label whose typos had
 *  been silently corrected and whose stray word had been dropped, which is the paraphrase it exists
 *  to catch. Only the connectives between parts are the transcription's. */
function fragments(label: string): string[] {
  let parts = [label];
  for (const separator of ["—", ":", " AND "]) {
    parts = parts.flatMap((part) => part.split(separator));
  }
  return parts.map((part) => part.trim()).filter((part) => part.length >= 12);
}

/** The sources a fragment appears verbatim in. Empty means it appears in none, which is the failure
 *  this file exists for. */
function sourcesFor(part: string): string[] {
  const needle = normalise(part);
  return SOURCES.filter((source) => source.text.includes(needle)).map(
    (source) => source.name,
  );
}

/** The section descriptions no source can be searched for whole.
 *
 *  A description a table spans over its rows is held to the same standard as a row: it appears in a
 *  source, or it is a paraphrase — and a paraphrase written over a table is the one a fragment-wise
 *  check cannot see, because its halves are both the framework's ("Splicing change likely" over "NMD
 *  not predicted"). Where the calculator nests one rowspan inside another the transcription joins
 *  the levels with ` — `, and the source still shows them as one run, since a rowspan's text sits
 *  with the first row it covers. The six below head a nesting's later sections, where the source
 *  prints the outer level's earlier rows between the levels, so no containment check can find them
 *  however faithful the wording is. Listed in full — the wording is the only thing that separates a
 *  nesting the calculator draws from a description invented over the table. */
const NESTED_SECTIONS = [
  "Affects Partial Gene — VBC is NOT a proved tandem AND Both breakpoints of the VBC are inside the start and end points of the CDS — Single or Multi exon duplication not predicted to disrupt reading frame OR Single or Multi exon duplication predicted to disrupt reading frame but introduced PTC is not >50 nt Upstream of last exon–exon boundary (NMD not predicted)",
  "Affects Partial Gene — VBC is NOT a proved tandem AND Both breakpoints of the VBC are inside the start and end points of the CDS — Single or multi exon duplication predicted to disrupt reading frame AND Introduced PTC >50 nt upstream of last exon–exon boundary (NMD predicted)",
  "Affects Partial Gene — VBC is NOT a proved tandem AND Both breakpoints of the VBC are not inside the start or end points of the CDS",
  "Affects Partial Gene — VBC proved tandem AND Both breakpoints of the VBC are inside the start and end points of the CDS — Single or Multi exon duplication not predicted to disrupt reading frame OR Single or Multi exon duplication predicted to disrupt reading frame but introduced PTC is not >50 nt Upstream of last exon–exon boundary (NMD not predicted)",
  "Affects Partial Gene — VBC proved tandem AND Both breakpoints of the VBC are not inside the start or end points of the CDS",
  "Splicing data is available for VBC showing an inferred variant-specific impact (compared to controls) — Splicing data and PRD are NOT concordant with regards to impact",
];

/** Every distinct description a table spans over its rows. */
function sectionDescriptions(): string[] {
  const found = new Set<string>();
  for (const workflow of ALL_WORKFLOWS) {
    for (const cell of [...workflow.cells, ...(workflow.inputs ?? [])]) {
      if (cell.group !== undefined) found.add(cell.group);
    }
  }
  return [...found];
}

/** Whether a source prints this description. Nested levels joined with ` — ` read in the source as
 *  one run: the connective is the transcription's, the words on either side are not. */
function isPrinted(group: string): boolean {
  return (
    sourcesFor(group).length > 0 ||
    sourcesFor(group.split(" — ").join(" ")).length > 0
  );
}

/** The workflows a supplement carries. A workflow counts as one only where the calculator cannot
 *  account for a fragment: short fragments recur across the framework, so a workflow sharing wording
 *  with a supplement it was not transcribed from says nothing. */
function supplementSourced(): Set<string> {
  const found = new Set<string>();
  for (const workflow of ALL_WORKFLOWS) {
    for (const cell of [...workflow.cells, ...(workflow.inputs ?? [])]) {
      for (const part of fragments(cellLabel(cell))) {
        const sources = sourcesFor(part);
        if (sources.length > 0 && !sources.includes(CALCULATOR)) {
          found.add(workflow.id);
        }
      }
    }
  }
  return found;
}

describe.skipIf(CAPTURES === undefined)("the transcription is verbatim", () => {
  test("each source is present and substantial", () => {
    // Rules out a vacuous pass if a fixture is ever truncated or emptied.
    for (const source of SOURCES) {
      expect(source.text.length).toBeGreaterThan(10_000);
    }
  });

  test("every cell's wording appears in one of the sources", () => {
    const paraphrased: string[] = [];
    for (const workflow of ALL_WORKFLOWS) {
      // `inputs` alongside `cells`: a control that answers no decision-tree row still asks the
      // framework's question, and POP_FRQ's two frequencies are what its row is derived from.
      for (const cell of [...workflow.cells, ...(workflow.inputs ?? [])]) {
        for (const part of fragments(cellLabel(cell))) {
          if (sourcesFor(part).length === 0) {
            paraphrased.push(`${cell.cell}: ${part.slice(0, 90)}`);
          }
        }
      }
    }
    expect(paraphrased).toEqual([]);
  });

  test("every spanned description is one a source prints", () => {
    // Both directions. A description a source cannot show is a paraphrase unless it is one of the
    // nestings below; a listed one a source can now show is an exemption nothing needs any more.
    const unfindable = sectionDescriptions().filter(
      (group) => !isPrinted(group),
    );
    expect(unfindable.sort()).toEqual(NESTED_SECTIONS);
  });

  test("each listed nesting is the framework's at every level", () => {
    // What the list cannot be used to smuggle in: an invented description whose levels are nobody's.
    const invented: string[] = [];
    for (const nesting of NESTED_SECTIONS) {
      for (const part of fragments(nesting)) {
        if (sourcesFor(part).length === 0) invented.push(part.slice(0, 90));
      }
    }
    expect(invented).toEqual([]);
  });

  test("a workflow's declared source is the source it came from", () => {
    // Asserted, not reported. `WorkflowDef.source` is load-bearing — the rarity gate applies only to
    // workflows the calculator prints — so an undeclared supplement workflow would be barred by a note
    // written before it existed, and a falsely declared one would escape a note that does cover it.
    const declared = new Set(
      ALL_WORKFLOWS.filter((w) => w.source === "supplement").map((w) => w.id),
    );
    const actual = supplementSourced();
    expect([...declared].sort()).toEqual([...actual].sort());
    // Non-empty rules out the pass where both sides are empty because the check stopped working.
    expect(actual.size).toBeGreaterThan(0);
  });

  test("every workflow title appears in one of the sources", () => {
    const invented = ALL_WORKFLOWS.filter((workflow) => {
      const parts = fragments(workflow.title);
      return parts.length > 0 && !parts.some((p) => sourcesFor(p).length > 0);
    }).map((workflow) => `${workflow.id}: ${workflow.title}`);
    expect(invented).toEqual([]);
  });
});

describe("a derived row states what it derives with", () => {
  test("a cell defined by a ratio states that ratio first in its own label", () => {
    // The multiple is what selects the row, and the label is what the verbatim checks pin to the
    // framework. Tying the two means the arithmetic cannot drift from the wording a curator reads.
    //
    // Anchored on the label's FIRST multiple, not on containment. A row labelled `>=(15x of DAFT)`
    // contains `5x`, so a containment check passes a 5-and-15 swap between two rows — which is the
    // one drift that changes which row an ordinary frequency lands in, and the swap a careless edit
    // to this table would actually make.
    const untethered: string[] = [];
    let checked = 0;
    for (const workflow of ALL_WORKFLOWS) {
      for (const cell of workflow.cells) {
        if (!cell.ratio) continue;
        // The lowest row lies below every threshold, so it names no multiple of its own.
        if (cell.ratio.minMultiple === 0) continue;
        checked += 1;
        const first = cell.label.match(/([\d.]+)x/);
        if (first?.[1] !== String(cell.ratio.minMultiple)) {
          untethered.push(
            `${cell.cell}: states ${first?.[1] ?? "no"}x, derives ${cell.ratio.minMultiple}x`,
          );
        }
      }
    }
    expect(untethered).toEqual([]);
    // The three rows that name a multiple; the fourth is the skipped floor.
    expect(checked).toBe(3);
  });
});
