import { type Backend, selectedBackend } from "../backend";
import type {
  AnalysisDataPlane,
  ContentPort,
  LiteraturePort,
  ProjectMembership,
} from "../ports";
import * as fixture from "./fixture";
import * as live from "./live";

export { type Backend, selectedBackend };

// A narrow env shape so callers/tests need not supply a full ProcessEnv.
type EnvLike = Record<string, string | undefined>;

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

/** Build a FRESH content port — the generic GCS-object serving surface (signed-URL 302 live, seeded
 *  bytes offline), reusable by any surface that serves content off a bucket. */
export function createContent(env: EnvLike = process.env): ContentPort {
  return selectedBackend(env) === "live"
    ? live.createContent()
    : fixture.createContent();
}

/** Build a FRESH literature port — the literature read surface, IAP-gated (not Project-scoped) — with
 *  the matching content port injected. Memoized by `context.ts`. */
export function createLiterature(env: EnvLike = process.env): LiteraturePort {
  const content = createContent(env);
  return selectedBackend(env) === "live"
    ? live.createLiterature(content)
    : fixture.createLiterature(content);
}
