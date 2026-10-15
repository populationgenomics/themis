import { describe, expect, test } from "bun:test";
import { FixtureCurationStore } from "./fixture";
import type { Entry } from "./model";
import { cellLabel } from "./ui/primitives";
import { withDerivedBin } from "./workflows/frequency";
import { POP_FRQ_ID } from "./workflows/pop";
import { workflowById } from "./workflows/registry";

// The fixture's seeded submissions are written out by hand, so they are held to what the worksheet
// would have stored: a field names a row of its workflow by the row's framework name, carries that
// row's label, and is stored either under the row's id (a count or typed value) or with the row's
// id as its value (a selection).

async function seededEntries(): Promise<Entry[]> {
  const store = new FixtureCurationStore();
  const entries: Entry[] = [];
  for (const variant of await store.listVariants()) {
    for (const worksheet of await store.worksheetsOfVariant(variant.id)) {
      for (const submission of await store.submissions(worksheet.id)) {
        entries.push(...(await store.assessments(submission.id)));
      }
    }
  }
  return entries;
}

describe("the fixture's seeded answers", () => {
  test("store each field as the worksheet would", async () => {
    const drifted: string[] = [];
    let fields = 0;
    for (const { workflowId, assessment } of await seededEntries()) {
      if (assessment.kind.case !== "workflow") continue;
      const workflow = workflowById(workflowId);
      if (workflow === undefined) {
        drifted.push(`${workflowId}: no such workflow`);
        continue;
      }
      for (const field of assessment.kind.value.fields) {
        fields += 1;
        const cell = [...workflow.cells, ...(workflow.inputs ?? [])].find(
          (c) => c.cell === field.cellId,
        );
        if (
          cell === undefined ||
          field.label !== cellLabel(cell) ||
          (field.fieldId !== cell.id && field.value !== cell.id)
        ) {
          drifted.push(`${workflowId}: ${field.fieldId}`);
        }
      }
    }
    expect(drifted).toEqual([]);
    // Non-empty rules out the pass where the seed holds no submitted field.
    expect(fields).toBeGreaterThan(0);
  });

  test("store the frequency row their two frequencies derive", async () => {
    const frequencies = (await seededEntries()).flatMap(
      ({ workflowId, assessment }) =>
        workflowId === POP_FRQ_ID && assessment.kind.case === "workflow"
          ? [assessment.kind.value]
          : [],
    );
    for (const answer of frequencies) {
      expect(withDerivedBin(answer).fields).toEqual(answer.fields);
    }
    expect(frequencies.length).toBeGreaterThan(0);
  });

  test("name only registered workflows as class-determinative", async () => {
    const named = (await seededEntries()).flatMap(({ assessment }) =>
      assessment.kind.case === "verdict"
        ? assessment.kind.value.classDeterminative
        : [],
    );
    expect(named.filter((id) => workflowById(id) === undefined)).toEqual([]);
    expect(named.length).toBeGreaterThan(0);
  });
});
