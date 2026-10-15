import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  ModeOfInheritance,
  ModeOfInheritanceSchema,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  MODES,
  modeColumn,
  modeFromColumn,
  modeFromName,
  modeName,
} from "./mode";

describe("the registered mode", () => {
  test("offers every member the contract names but the unstated one", () => {
    expect(MODES.map((m) => m.mode).sort()).toEqual(
      ModeOfInheritanceSchema.values
        .map((value) => value.number)
        .filter((n) => n !== ModeOfInheritance.UNSPECIFIED)
        .sort(),
    );
  });

  test.each(MODES.map((m) => m.mode))(
    "%p round-trips through the column and the request",
    (mode) => {
      expect(modeFromColumn(modeColumn(mode))).toBe(mode);
      expect(modeFromName(modeName(mode))).toBe(mode);
    },
  );

  test("stores exactly the names the column's CHECK admits", () => {
    expect(MODES.map((m) => modeColumn(m.mode)).sort()).toEqual(
      [...checkedModes()].sort(),
    );
  });

  test("refuses the unstated mode and anything the column could not hold", () => {
    expect(modeFromName("MODE_OF_INHERITANCE_UNSPECIFIED")).toBeUndefined();
    expect(modeFromName("AUTOSOMAL_DOMINANT")).toBeUndefined();
    expect(() => modeColumn(ModeOfInheritance.UNSPECIFIED)).toThrow();
    expect(() => modeFromColumn("UNSPECIFIED")).toThrow();
    expect(() => modeFromColumn("X_LINKED")).toThrow(/unknown mode/);
  });
});

const MIGRATIONS = join(
  import.meta.dir,
  "../../../../themis/migrate/migrations",
);

/** The values the migration adding `curation.variants.mode_of_inheritance` lets the column hold. */
function checkedModes(): string[] {
  const sql = readdirSync(MIGRATIONS)
    .map((name) => readFileSync(join(MIGRATIONS, name), "utf-8"))
    .find((text) => text.includes("ADD COLUMN mode_of_inheritance"));
  const list = sql?.match(/mode_of_inheritance IN \(([^)]*)\)/)?.[1];
  if (list === undefined) {
    throw new Error("no migration declares the mode_of_inheritance CHECK");
  }
  return [...list.matchAll(/'([A-Z_]+)'/g)].map((match) => match[1] as string);
}
