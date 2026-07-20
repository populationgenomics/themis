import type { AnalysisDataPlane, ProjectMembership } from "../ports";
import { createFixtureBackend, createFixtureMembership } from "./fixture";

export type Backend = "fixture" | "real";

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
  if (raw === "real") return "real";
  if (raw === "fixture") return "fixture";
  throw new Error(
    `THEMIS_BACKEND must be "fixture" or "real" (got ${JSON.stringify(raw)})`,
  );
}

/** Build a FRESH data plane. `context.ts` is the sole caller — it memoizes one and
 *  wraps it in an `AuthorizedBackend`, so routes never hold an unscoped backend.
 *  `real` fails loud: its adapter is not wired. */
export function createDataPlane(env: EnvLike = process.env): AnalysisDataPlane {
  if (selectedBackend(env) === "real") {
    throw new Error(
      "THEMIS_BACKEND=real: the real backend adapter is not wired yet",
    );
  }
  return createFixtureBackend();
}

/** Build a FRESH membership — the user↔Project mapping the `AuthorizedBackend`
 *  authorizes against. Memoized by `context.ts`. `real` fails loud: its adapter is
 *  not wired. */
export function createMembership(
  env: EnvLike = process.env,
): ProjectMembership {
  if (selectedBackend(env) === "real") {
    throw new Error(
      "THEMIS_BACKEND=real: the real membership adapter is not wired yet",
    );
  }
  return createFixtureMembership();
}
