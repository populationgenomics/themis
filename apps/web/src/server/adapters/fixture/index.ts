import type { AnalysisDataPlane, ProjectMembership } from "../../ports";
import { FixtureBackend } from "./backend";
import { FixtureMembership } from "./membership";

export { FixtureBackend } from "./backend";
export { FixtureMembership } from "./membership";

/** A FRESH in-memory data plane. The runtime composition root (`../index.ts`)
 *  memoizes one instance so a POST that creates an analysis and the following
 *  polls share the same in-memory state. */
export function createFixtureBackend(): AnalysisDataPlane {
  return new FixtureBackend();
}

/** The seeded fixture membership. */
export function createFixtureMembership(): ProjectMembership {
  return new FixtureMembership();
}
