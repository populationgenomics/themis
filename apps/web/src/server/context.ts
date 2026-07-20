import { getBackend } from "./adapters";
import { getUserIdentity } from "./identity";
import type { AnalysisBackend } from "./ports";

// The authenticated per-request context. A route obtains its backend through here,
// so it cannot reach the data plane without the caller being verified — the
// data-seam half of the request-auth chokepoint (docs/design/security.md). proxy.ts
// is the perimeter half; this re-verifies rather than trusting it.

export interface UserContext {
  readonly userEmail: string;
  readonly backend: AnalysisBackend;
}

/** Verify the request's caller and return the authenticated data-plane context.
 *  Throws UnauthenticatedError (mapped to 401 at the route boundary) when the
 *  request carries no verifiable identity. */
export async function userContext(request: Request): Promise<UserContext> {
  const userEmail = await getUserIdentity().assertedEmail(request.headers);
  return { userEmail, backend: getBackend() };
}
