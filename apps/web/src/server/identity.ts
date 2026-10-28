import * as fixture from "./adapters/fixture";
import * as live from "./adapters/live";
import { selectedBackend } from "./backend";

// Request authentication: who is calling. Two implementations selected by `THEMIS_BACKEND`: the
// fixture's dev identity (adapters/fixture/identity.ts; no IAP offline, every request the seed dev
// user), and the live IAP-assertion verifier (adapters/live/identity.ts), which fails closed on any
// request it cannot verify. What the caller may reach is the auth service's answer, not this seam's
// (server/admission.ts).

/** The email every request is attributed to under the fixture backend. Re-exported here so a
 *  caller needing the offline identity does not reach into the adapter that implements it. */
export { DEV_USER_EMAIL } from "./adapters/fixture/identity";

type EnvLike = Record<string, string | undefined>;

/** An authenticated caller. */
export interface Caller {
  /** The caller's email address, lowercase. */
  readonly email: string;
}

/** Authenticates a request from its headers. */
export interface UserIdentity {
  /** The verified caller. Raises `UnauthenticatedError` for a request that presents no credential
   *  or one that fails verification; any other failure (a key that cannot be fetched) is an outage
   *  and propagates as itself. */
  authenticate(headers: Headers): Promise<Caller>;
}

/** Build a FRESH identity for the selected backend. */
export function buildUserIdentity(env: EnvLike = process.env): UserIdentity {
  return selectedBackend(env) === "live"
    ? live.createIdentity(env)
    : fixture.createIdentity();
}
