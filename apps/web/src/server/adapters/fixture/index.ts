import type { AnalysisDataPlane, ProjectMembership } from "../../ports";
import { FixtureDataPlane } from "./data-plane";
import { FixtureMembership } from "./membership";

export { FixtureDataPlane } from "./data-plane";
export { FixtureMembership } from "./membership";

/** A FRESH in-memory data plane. The runtime composition root (`../index.ts`)
 *  memoizes one instance so a POST that creates an analysis and the following
 *  polls share the same in-memory state. */
export function createFixtureDataPlane(): AnalysisDataPlane {
  return new FixtureDataPlane();
}

/** The seeded fixture membership. */
export function createFixtureMembership(): ProjectMembership {
  return new FixtureMembership();
}
