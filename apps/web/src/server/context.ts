import { createDataPlane, createMembership } from "./adapters";
import { AuthorizedBackend } from "./authorized-backend";
import { getUserIdentity } from "./identity";
import type { AnalysisDataPlane, ProjectMembership } from "./ports";

// The authenticated + authorized per-request context — the data-seam half of the
// request-auth chokepoint (docs/design/security.md; proxy.ts is the perimeter half).
// A route obtains its backend only through `userContext`, so it cannot reach the
// data plane without the caller being verified (identity) and scoped to their
// Projects (AuthorizedBackend). The raw data plane and membership are memoized here,
// module-private and never exported, so there is no accessor a route could import to
// go around the decorator.

interface Composition {
  dataPlane?: AnalysisDataPlane;
  membership?: ProjectMembership;
}

// On `globalThis` so Next's dev HMR (which re-evaluates modules) does not reset the
// fixture's in-memory state, nor rebuild the real adapter's DB pool, between reloads.
function composition(): Composition {
  const holder = globalThis as typeof globalThis & {
    __themisComposition?: Composition;
  };
  if (!holder.__themisComposition) {
    holder.__themisComposition = {};
  }
  return holder.__themisComposition;
}

function dataPlane(): AnalysisDataPlane {
  const c = composition();
  if (!c.dataPlane) c.dataPlane = createDataPlane();
  return c.dataPlane;
}

function membership(): ProjectMembership {
  const c = composition();
  if (!c.membership) c.membership = createMembership();
  return c.membership;
}

export interface UserContext {
  readonly userEmail: string;
  readonly backend: AuthorizedBackend;
}

/** Verify the request's caller and return the authenticated, Project-scoped
 *  data-plane context. Throws UnauthenticatedError (mapped to 401 at the route
 *  boundary) when the request carries no verifiable identity. */
export async function userContext(request: Request): Promise<UserContext> {
  const userEmail = await getUserIdentity().assertedEmail(request.headers);
  const backend = new AuthorizedBackend(dataPlane(), membership(), userEmail);
  return { userEmail, backend };
}
