"use client";

import { type Cell, CountRows, FrameworkNote } from "../ui/primitives";
import type { WorkflowBodyProps } from "./types";

/** The shape most clinical and locus workflows take: criteria rows, each carrying a count of the
 *  individuals it applies to, and the calculator's notes underneath. */
export function countBody(
  cells: Cell[],
  countLabel: string,
  notes: string[] = [],
) {
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
