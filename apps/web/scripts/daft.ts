import { writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  DAFT_BINNING_TABLES,
  DAFT_PENETRANCE_COLUMNS,
} from "../src/curation/reference/daft-binning";

// Export this side's reading of SM3's six DAFT lookup tables, so a Python test can hold it against
// the library's reading of the same six.
//
// Two independent readings of one framework table is the whole point (docs/design/curation-surface.md
// §Its reference material is mirrored too): these thresholds were transcribed from the calculator's
// rendering, the library's from SM3's own images, and where they part the difference is a fact about
// the framework rather than a typo. The comparison runs in Python because that is where the library
// is, and this file is what crosses the wire.
//
// Each threshold is keyed by its penetrance column rather than by position, so a reordered column
// fails the join instead of quietly comparing against its neighbour.
//
// Every value is written inside an object, never a bare array, because biome collapses a short array
// onto one line and `JSON.stringify` does not — so regenerating stays idempotent against the
// formatter.
//
// Written outside `src/` for the same reason as `curation-cells.json`: nothing in the app imports it,
// and the image's build context is `apps/web`.

const OUT = join(import.meta.dir, "..", "daft-tables.json");

const tables = DAFT_BINNING_TABLES.map((table) => ({
  title: table.title,
  rows: table.rows.map((row) => ({
    prevalence: row.prevalence,
    // The allele counts beside them are the calculator's alone and have no second reading, so they
    // are checked against the capture rather than exported here.
    thresholds: Object.fromEntries(
      row.cells.map(([daft], column) => [
        DAFT_PENETRANCE_COLUMNS[column],
        daft,
      ]),
    ),
  })),
}));

writeFileSync(OUT, `${JSON.stringify({ tables }, null, 2)}\n`);
console.log(
  `${tables.length} tables x ${tables[0].rows.length} rows x ${DAFT_PENETRANCE_COLUMNS.length} columns -> ${OUT}`,
);
