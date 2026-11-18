import { describe, expect, test } from "bun:test";
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

  test("refuses the unstated mode and anything the column could not hold", () => {
    expect(modeFromName("MODE_OF_INHERITANCE_UNSPECIFIED")).toBeUndefined();
    expect(modeFromName("AUTOSOMAL_DOMINANT")).toBeUndefined();
    expect(() => modeColumn(ModeOfInheritance.UNSPECIFIED)).toThrow();
    expect(() => modeFromColumn("UNSPECIFIED")).toThrow();
    expect(() => modeFromColumn("X_LINKED")).toThrow(/unknown mode/);
  });
});
