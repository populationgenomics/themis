import { describe, expect, test } from "bun:test";
import { versionMenuItems } from "./version-dropdown";

describe("versionMenuItems", () => {
  test("lists v1..latest, latest first", () => {
    const items = versionMenuItems(4, 4);
    expect(items.map((i) => i.label)).toEqual(["v4", "v3", "v2", "v1"]);
  });

  test("exactly the shown version is selected", () => {
    const items = versionMenuItems(4, 2);
    expect(items.filter((i) => i.selected).map((i) => i.label)).toEqual(["v2"]);
  });

  test("the latest row means follow-current (null); every other row pins itself", () => {
    const items = versionMenuItems(3, 3);
    expect(items[0].version).toBeNull();
    expect(items.slice(1).map((i) => i.version)).toEqual([2, 1]);
  });

  test("a single-version document still offers the follow-current row", () => {
    const items = versionMenuItems(1, 1);
    expect(items).toHaveLength(1);
    expect(items[0].selected).toBe(true);
    expect(items[0].version).toBeNull();
  });
});
