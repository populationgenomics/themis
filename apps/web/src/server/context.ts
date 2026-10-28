import { POLL_TIP_BUDGET_MS } from "@/lib/workspace-deadlines";
import { createAnalysisPorts, createLiterature } from "./adapters";
import { admit, getGate } from "./admission";
import { AuthorizedBackend } from "./authorized-backend";
import type { GrantedMembership } from "./membership";
import type { AccessPort, AnalysisPorts, LiteraturePort } from "./ports";

// The authenticated + authorized per-request context — the data-seam half of the
// request-auth chokepoint (docs/design/security.md; proxy.ts is the perimeter half).
// A handler obtains its backend only through `userContext`, so it cannot reach the
// data plane without the caller being verified, admitted, and scoped to the Projects
// the access answer granted (AuthorizedBackend). The seam runs admission itself rather
// than trusting the perimeter's, and builds the membership from that one answer for this
// request only. The raw analysis ports are memoized here, module-private and never
// exported, so there is no accessor a handler could import to go around the decorator.

interface Composition {
  analysisPorts?: AnalysisPorts;
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

function literature(): LiteraturePort {
  const c = composition();
  if (!c.literature) c.literature = createLiterature();
  return c.literature;
}

/** The shared-corpus literature read surface, for a call already admitted. Deliberately not wrapped
 *  in `AuthorizedBackend`: a paper is not Project-scoped (document-pane.md §Backend seam), so
 *  exposing an accessor bypasses no per-Project decorator. Its RPC callers are admitted by the
 *  identity interceptor, which builds a `userContext` for every call. */
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

/** Admit the request's caller and return the authenticated, Project-scoped data-plane context.
 *  Throws `UnauthenticatedError` (→ 401) for a request with no verifiable credential and
 *  `NotAdmittedError` (→ 403) for a caller the deployment does not admit. */
export async function userContext(headers: Headers): Promise<UserContext> {
  const { caller, membership } = await admit(headers, getGate());
  const { dataPlane, workspace, documents } = analysisPorts();
  const backend = new AuthorizedBackend(
    dataPlane,
    workspace,
    documents,
    membership,
    caller.email,
    POLL_TIP_BUDGET_MS,
  );
  return { userEmail: caller.email, backend };
}

/** Admit the request's caller and return the literature read surface. A paper is a shared-corpus
 *  resource, not Project-scoped, so admission is the whole check. Throws as `userContext` does. */
export async function literatureContext(
  request: Request,
): Promise<LiteratureContext> {
  const { caller } = await admit(request.headers, getGate());
  return { userEmail: caller.email, literature: literature() };
}

export interface MemberContext {
  readonly userEmail: string;
  readonly membership: GrantedMembership;
}

/** Admit the request's caller and return their Projects with their roles in each, for a surface
 *  that decides per Project what a role may do (the curation worksheets). Throws as `userContext`
 *  does. */
export async function memberContext(headers: Headers): Promise<MemberContext> {
  const { caller, membership } = await admit(headers, getGate());
  return { userEmail: caller.email, membership };
}

/** What the person behind `email` may reach, for a check about someone other than the caller: that
 *  an assignment targets a curator of the Project, say. */
export function accessPort(): AccessPort {
  return getGate().access;
}
