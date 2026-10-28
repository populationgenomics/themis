import { type Backend, selectedBackend } from "../backend";
import type {
  AccessPort,
  AnalysisPorts,
  ContentPort,
  LiteraturePort,
} from "../ports";
import * as fixture from "./fixture";
import * as live from "./live";

export { type Backend, selectedBackend };

// A narrow env shape so callers/tests need not supply a full ProcessEnv.
type EnvLike = Record<string, string | undefined>;

/** Build a FRESH data plane and workspace repository. Built together because each backend's pair
 *  shares a part: the live pair one session-bearer deriver, the fixture pair the scripted run, which
 *  is what writes the repository offline. `context.ts` is the sole caller — it memoizes the pair and
 *  wraps it in an `AuthorizedBackend`, so routes never hold an unscoped backend. */
export function createAnalysisPorts(env: EnvLike = process.env): AnalysisPorts {
  return selectedBackend(env) === "live"
    ? live.createAnalysisPorts()
    : fixture.createAnalysisPorts();
}

/** Build a FRESH access port — what a person may reach, which both the perimeter and the data seam
 *  ask on every request. Memoized by `admission.ts`. */
export function createAccess(env: EnvLike = process.env): AccessPort {
  return selectedBackend(env) === "live"
    ? live.createAccess(env)
    : fixture.createAccess();
}

/** Build a FRESH content port — the generic GCS-object serving surface (signed-URL 302 live, seeded
 *  bytes offline), reusable by any surface that serves content off a bucket. */
export function createContent(env: EnvLike = process.env): ContentPort {
  return selectedBackend(env) === "live"
    ? live.createContent()
    : fixture.createContent();
}

/** Build a FRESH literature port — the literature read surface, gated on admission (not Project-scoped) — with
 *  the matching content port injected. Memoized by `context.ts`. */
export function createLiterature(env: EnvLike = process.env): LiteraturePort {
  const content = createContent(env);
  return selectedBackend(env) === "live"
    ? live.createLiterature(content)
    : fixture.createLiterature(content);
}
