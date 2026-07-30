import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { ProjectSchema } from "@/models/workbench";
import { projectName } from "./app-bar";

const project = create(ProjectSchema, { id: "proj_a", name: "A project" });

describe("projectName", () => {
  test("an unanswered membership reads as pending", () => {
    expect(projectName({ status: "pending" }, null)).toBe("…");
  });

  test("an answered empty membership says so", () => {
    // Distinct from pending: the ellipsis would leave a caller who belongs to no Project
    // watching what reads as a spinner, and would have the accessible name assert
    // non-membership before the answer arrived.
    expect(projectName({ status: "ready", projects: [] }, null)).toBe(
      "No Project",
    );
  });

  test("a failed membership is neither pending nor empty", () => {
    // A query that has stopped retrying never resolves, so reporting it as pending hides the
    // failure for good and reporting it as empty asserts a membership nothing established.
    expect(projectName({ status: "error" }, null)).toBe("Unavailable");
  });

  test("an active Project is named", () => {
    expect(projectName({ status: "ready", projects: [project] }, project)).toBe(
      "A project",
    );
  });

  test("the ellipsis stands only for an unanswered query", () => {
    expect(
      projectName({ status: "ready", projects: [project] }, null),
    ).not.toBe("…");
  });
});
