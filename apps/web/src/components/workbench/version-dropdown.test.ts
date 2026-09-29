import { describe, expect, test } from "bun:test";
import { versionLabel, versionMenuItems } from "./version-dropdown";
import { documentVersions } from "./working-document";

const commit = (c: string) => c.repeat(40);
// As the copy lists them: newest first.
const VERSIONS = documentVersions([
  { commit: commit("c"), timestamp: 1_727_163_300 },
  { commit: commit("b"), timestamp: 1_727_163_200 },
  { commit: commit("a"), timestamp: 1_727_163_100 },
]);

describe("versionMenuItems", () => {
  test("lists every recorded tip, newest first, numbered from the oldest", () => {
    const items = versionMenuItems(VERSIONS, commit("c"));
    expect(items.map((i) => i.label)).toEqual(["v3", "v2", "v1"]);
  });

  test("exactly the shown commit is selected", () => {
    const items = versionMenuItems(VERSIONS, commit("b"));
    expect(items.filter((i) => i.selected).map((i) => i.label)).toEqual(["v2"]);
  });

  test("the tip's row means follow the branch (null); every other row pins its commit", () => {
    const items = versionMenuItems(VERSIONS, commit("c"));
    expect(items[0].commit).toBeNull();
    expect(items.slice(1).map((i) => i.commit)).toEqual([
      commit("b"),
      commit("a"),
    ]);
  });

  test("a single version still offers the follow-the-branch row", () => {
    const items = versionMenuItems(VERSIONS.slice(0, 1), commit("c"));
    expect(items).toHaveLength(1);
    expect(items[0].selected).toBe(true);
    expect(items[0].commit).toBeNull();
  });
});

describe("versionLabel", () => {
  test("names a listed commit by its version", () => {
    expect(versionLabel(VERSIONS, commit("a"))).toBe("v1");
  });

  test("names a commit the list does not hold by its abbreviated id", () => {
    expect(versionLabel(VERSIONS, commit("d"))).toBe("ddddddd");
  });
});
