import { POLL_TIP_BUDGET_MS } from "@/lib/workspace-deadlines";
import {
  createAnalysisPorts,
  createLiterature,
  createMembership,
} from "./adapters";
import { AuthorizedBackend } from "./authorized-backend";
import { getUserIdentity } from "./identity";
import type { AnalysisPorts, LiteraturePort, ProjectMembership } from "./ports";

// The authenticated + authorized per-request context — the data-seam half of the
// request-auth chokepoint (docs/design/security.md; proxy.ts is the perimeter half).
// A handler obtains its backend only through `userContext`, so it cannot reach the
// data plane without the caller being verified (identity) and scoped to their
// Projects (AuthorizedBackend). The raw analysis ports and membership are memoized here,
// module-private and never exported, so there is no accessor a handler could import to
// go around the decorator.

interface Composition {
  analysisPorts?: AnalysisPorts;
  membership?: ProjectMembership;
  literature?: LiteraturePort;
}

// On `globalThis` so Next's dev HMR (which re-evaluates modules) does not reset the
// fixture's in-memory state, nor rebuild the live adapter's DB pool, between reloads.
function composition(): Composition {
  const holder = globalThis as typeof globalThis & {
    __themisComposition?: Composition;
  };
  if (!holder.__themisComposition) {
    holder.__themisComposition = {};
  }
  return holder.__themisComposition;
}

function analysisPorts(): AnalysisPorts {
  const c = composition();
  if (!c.analysisPorts) c.analysisPorts = createAnalysisPorts();
  return c.analysisPorts;
}

function membership(): ProjectMembership {
  const c = composition();
  if (!c.membership) c.membership = createMembership();
  return c.membership;
}

function literature(): LiteraturePort {
  const c = composition();
  if (!c.literature) c.literature = createLiterature();
  return c.literature;
}

/** The shared-corpus literature read surface. Deliberately not wrapped in `AuthorizedBackend`: a
 *  paper is IAP-only, not Project-scoped (document-pane.md §Backend seam), so — unlike `analysisPorts`/
 *  `membership` — exposing an accessor bypasses no per-Project decorator. The RPC identity
 *  interceptor still gates every call on a verified caller. */
export function literaturePort(): LiteraturePort {
  return literature();
}

export interface UserContext {
  readonly userEmail: string;
  readonly backend: AuthorizedBackend;
}

export interface LiteratureContext {
  readonly userEmail: string;
  readonly literature: LiteraturePort;
}

/** Verify the request's caller and return the authenticated, Project-scoped
 *  data-plane context. Throws UnauthenticatedError (mapped to `unauthenticated` by the
 *  RPC error interceptor) when the request carries no verifiable identity. */
export async function userContext(headers: Headers): Promise<UserContext> {
  const userEmail = await getUserIdentity().assertedEmail(headers);
  const { dataPlane, workspace } = analysisPorts();
  const backend = new AuthorizedBackend(
    dataPlane,
    workspace,
    membership(),
    userEmail,
    POLL_TIP_BUDGET_MS,
  );
  return { userEmail, backend };
}

/** Verify the request's caller and return the literature read surface. IAP-only: a paper is a
 *  shared-corpus resource, not Project-scoped, so this gates on a verified identity but does not
 *  wrap `AuthorizedBackend`. Throws UnauthenticatedError (→ 401) when the request carries no
 *  verifiable identity. */
export async function literatureContext(
  request: Request,
): Promise<LiteratureContext> {
  const userEmail = await getUserIdentity().assertedEmail(request.headers);
  return { userEmail, literature: literature() };
}
