import { describe, expect, test } from "bun:test";
import { curationApiPath, curationPath, projectPath } from "./routes";

describe("a Project's path", () => {
  test("is the id under /project", () => {
    expect(projectPath("acute-care")).toBe("/project/acute-care");
  });

  test("keeps a reserved character inside its one segment", () => {
    const path = projectPath("cohort/2024?x#y");
    expect(path).toBe("/project/cohort%2F2024%3Fx%23y");
    const url = new URL(path, "https://themis.example");
    expect(url.pathname.split("/")).toEqual([
      "",
      "project",
      "cohort%2F2024%3Fx%23y",
    ]);
    expect(url.search).toBe("");
    expect(url.hash).toBe("");
  });
});

describe("a Project's curation paths", () => {
  test("sit under the Project's page and its API", () => {
    expect(curationPath("acute-care")).toBe("/project/acute-care/curation");
    expect(curationPath("acute-care", "compare", "var_1")).toBe(
      "/project/acute-care/curation/compare/var_1",
    );
    expect(curationApiPath("acute-care", "variants")).toBe(
      "/api/projects/acute-care/curation/variants",
    );
  });

  test("keep a reserved character inside its one segment", () => {
    expect(curationApiPath("a/b", "alleles", "CA1?x")).toBe(
      "/api/projects/a%2Fb/curation/alleles/CA1%3Fx",
    );
  });
});
