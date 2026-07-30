import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { ProjectSchema } from "@/models/workbench";
import { projectParams, resolveProject } from "./workbench";

const first = create(ProjectSchema, { id: "proj_a", name: "A project" });
const second = create(ProjectSchema, { id: "proj_b", name: "B project" });
const projects = [first, second];

describe("resolveProject", () => {
  test("a Project the URL names is the active one", () => {
    // Without this a shared link cannot carry which Project it refers to.
    expect(resolveProject(projects, "proj_b")).toEqual(second);
  });

  test("no Project named falls back to the first", () => {
    expect(resolveProject(projects, null)).toEqual(first);
  });

  test("a Project the caller does not belong to falls back to the first", () => {
    // The id arrives from a hand-edited or shared URL. Resolving to the first keeps
    // the workbench on a Project the caller can actually read, and stops the bogus
    // id reaching the analyses query.
    expect(resolveProject(projects, "proj_not_mine")).toEqual(first);
  });

  test("belonging to no Project resolves to none", () => {
    // Default-deny: membership is what grants reach, so an empty list is not a
    // reason to invent a landing Project.
    expect(resolveProject([], "proj_a")).toBeNull();
  });

  test("Projects not yet loaded resolve to none", () => {
    expect(resolveProject(undefined, "proj_a")).toBeNull();
  });
});

describe("projectParams", () => {
  test("switching Projects drops the open Analysis", () => {
    // An Analysis belongs to one Project, so a carried-over id would poll an analysis the
    // new Project does not contain. This is the behaviour the selector exists to get right.
    const params = projectParams(
      new URLSearchParams("project=proj_a&analysis=an_stale"),
      "proj_b",
      "proj_a",
    );
    expect(params?.get("project")).toBe("proj_b");
    expect(params?.has("analysis")).toBe(false);
  });

  test("re-selecting the active Project changes nothing", () => {
    // The menu fires on the ticked row too. Treating that as a switch would close the open
    // Analysis and leave a history entry to undo, for a selection that selects what is already
    // selected — which `menuitemradio` semantics make a no-op.
    expect(
      projectParams(
        new URLSearchParams("project=proj_a&analysis=an_x"),
        "proj_a",
        "proj_a",
      ),
    ).toBeNull();
  });

  test("unrelated params survive the switch", () => {
    // Only the Analysis is Project-scoped; dropping anything else would be collateral.
    const params = projectParams(
      new URLSearchParams("tab=document"),
      "proj_b",
      "proj_a",
    );
    expect(params?.get("tab")).toBe("document");
    expect(params?.get("project")).toBe("proj_b");
  });

  test("the caller's params are not mutated", () => {
    const current = new URLSearchParams("project=proj_a&analysis=an_stale");
    projectParams(current, "proj_b", "proj_a");
    expect(current.get("project")).toBe("proj_a");
    expect(current.get("analysis")).toBe("an_stale");
  });
});
