import { selectedBackend } from "./adapters";
import * as fixture from "./adapters/fixture";
import * as live from "./adapters/live";

// Request identity — the seam the routes resolve the caller through. Two
// implementations selected by `THEMIS_BACKEND`: the fixture dev identity
// (adapters/fixture/identity.ts; no IAP offline, every request the seed dev user),
// and the live IAP-JWT verifier (adapters/live/identity.ts), which fails closed on
// any request it cannot verify.

type EnvLike = Record<string, string | undefined>;

/** Resolves the verified user email for a request from its headers. */
export interface UserIdentity {
  assertedEmail(headers: Headers): Promise<string>;
}

/** Build a FRESH identity resolver for the selected backend — the live IAP
 *  verifier or the dev identity. */
export function buildUserIdentity(env: EnvLike = process.env): UserIdentity {
  return selectedBackend(env) === "live"
    ? live.createIdentity(env)
    : fixture.createIdentity();
}

interface IdentitySingletons {
  identity?: UserIdentity;
}

function identitySingletons(): IdentitySingletons {
  const holder = globalThis as typeof globalThis & {
    __themisIdentity?: IdentitySingletons;
  };
  if (!holder.__themisIdentity) {
    holder.__themisIdentity = {};
  }
  return holder.__themisIdentity;
}

/** The process-wide identity resolver (memoized across requests and HMR reloads).
 *  Route handlers use this. */
export function getUserIdentity(env: EnvLike = process.env): UserIdentity {
  const s = identitySingletons();
  if (!s.identity) s.identity = buildUserIdentity(env);
  return s.identity;
}
