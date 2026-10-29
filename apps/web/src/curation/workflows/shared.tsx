"use client";

import type { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import { type Cell, CountRows, FrameworkNote } from "../ui/primitives";
import type { Routing, WorkflowBodyProps } from "./types";

/** An `applies` that puts a workflow on screen for exactly the modes named. Positive by design: a
 *  predicate written as "every mode but autosomal dominant" would route an X-linked dominant entity
 *  into the recessive branch. */
export function coversModes(
  ...modes: ModeOfInheritance[]
): (routing: Routing) => boolean {
  return ({ modeOfInheritance }) => modes.includes(modeOfInheritance);
}

/** The shape most clinical and locus workflows take: criteria rows, each carrying a count of the
 *  individuals it applies to, and the calculator's notes underneath. */
export function countBody(
  cells: Cell[],
  countLabel: string,
  notes: string[] = [],
) {
  // biome-ignore lint/correctness/noComponentHookFactories: called once per workflow while its module builds the workflow table, never during render, so each Body keeps one identity
  return function Body({ assessment, onChange, onBlur }: WorkflowBodyProps) {
    return (
      <div>
        <CountRows
          cells={cells}
          assessment={assessment}
          onChange={onChange}
          countLabel={countLabel}
          onBlur={onBlur}
        />
        {notes.map((note) => (
          <FrameworkNote key={note}>{note}</FrameworkNote>
        ))}
      </div>
    );
  };
}

/** Cells for a table whose rows are a zygosity or phase under a spanned phenotype condition, as the
 *  affected-observation and unaffected-observation workflows print them. */
export function crossCells(
  prefix: string,
  groups: { id: string; label: string }[],
  variants: { id: string; label: string }[],
): Cell[] {
  return groups.flatMap((group) =>
    variants.map((variant) => ({
      id: `${prefix}.${group.id}.${variant.id}`,
      cell: `${prefix}.${group.id}.${variant.id}`,
      group: group.label,
      label: variant.label,
    })),
  );
}
