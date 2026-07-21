import type { AnalysisDataPlane, ProjectMembership } from "../ports";
import * as fixture from "./fixture";
import * as live from "./live";

export type Backend = "fixture" | "live";

// A narrow env shape so callers/tests need not supply a full ProcessEnv.
type EnvLike = Record<string, string | undefined>;

/** Which backend to build, named explicitly by `THEMIS_BACKEND`. There is no
 *  default, in either direction: the fixture's identity resolver attributes every
 *  request to the seed dev user without verifying an assertion, so a deploy that
 *  lost the variable would authenticate everyone rather than fail. Selecting a
 *  backend is a deliberate act; an absent or unrecognised value is a
 *  misconfiguration. */
export function selectedBackend(env: EnvLike = process.env): Backend {
  const raw = env.THEMIS_BACKEND;
  if (raw === "live") return "live";
  if (raw === "fixture") return "fixture";
  throw new Error(
    `THEMIS_BACKEND must be "fixture" or "live" (got ${JSON.stringify(raw)})`,
  );
}

/** Build a FRESH data plane. `context.ts` is the sole caller — it memoizes one and
 *  wraps it in an `AuthorizedBackend`, so routes never hold an unscoped backend. */
export function createDataPlane(env: EnvLike = process.env): AnalysisDataPlane {
  return selectedBackend(env) === "live"
    ? live.createDataPlane()
    : fixture.createDataPlane();
}

/** Build a FRESH membership — the user↔Project mapping the `AuthorizedBackend`
 *  authorizes against. Memoized by `context.ts`. */
export function createMembership(
  env: EnvLike = process.env,
): ProjectMembership {
  return selectedBackend(env) === "live"
    ? live.createMembership()
    : fixture.createMembership();
}
