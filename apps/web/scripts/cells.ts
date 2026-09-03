import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { cellLabel } from "../src/curation/ui/primitives";
import { WORKFLOWS_VERSION } from "../src/curation/version";
import { ALL_WORKFLOWS } from "../src/curation/workflows/registry";

// Export every decision-tree cell the transcribed workflows can emit.
//
// Only this side knows them: `crossCells` builds ids programmatically, so a phenotype row crossed
// with five zygosity columns yields twenty-five ids that appear nowhere as literals and cannot be
// grepped out of the source. The library prices cells and the evaluation projection reads them, both
// in Python, so the inventory is what lets a Python test check that every cell a curator can answer
// is one the framework can price.
//
// Indented to match biome, so regenerating is idempotent against the formatter.
//
// Written outside `src/` on purpose: nothing in the app imports it, and the image's build context is
// `apps/web`, so a generated file inside the build graph is a file the build has to resolve.

const OUT = join(import.meta.dir, "..", "curation-cells.json");

const cells = ALL_WORKFLOWS.flatMap((workflow) =>
  workflow.cells.map((cell) => ({
    workflow: workflow.id,
    code: workflow.code,
    cell: cell.cell,
    label: cellLabel(cell),
    // Where the framework defines the row by a ratio, the ratio the worksheet derives it with, so
    // the Python join can check it against the same bins the library scores from.
    ...(cell.ratio
      ? {
          min_multiple: cell.ratio.minMultiple,
          bars_rarity_gated_codes: cell.ratio.barsRarityGatedCodes,
        }
      : {}),
  })),
);

const duplicates = cells
  .map((c) => c.cell)
  .filter((cell, i, all) => all.indexOf(cell) !== i);
if (duplicates.length > 0) {
  throw new Error(
    `cells claimed more than once, which would make a stored answer ambiguous: ${[
      ...new Set(duplicates),
    ].join(", ")}`,
  );
}

writeFileSync(
  OUT,
  `${JSON.stringify({ workflows_version: WORKFLOWS_VERSION, cells }, null, 2)}\n`,
);
console.log(
  `${cells.length} cells across ${ALL_WORKFLOWS.length} workflows -> ${OUT}`,
);
