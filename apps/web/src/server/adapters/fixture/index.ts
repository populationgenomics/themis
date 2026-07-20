import type { AnalysisBackend } from "../../ports";
import { FixtureBackend } from "./backend";

export { FixtureBackend } from "./backend";

/** A FRESH in-memory backend. The runtime composition root (`../index.ts`)
 *  memoizes one instance so a POST that creates an analysis and the following
 *  polls share the same in-memory state. */
export function createFixtureBackend(): AnalysisBackend {
  return new FixtureBackend();
}
