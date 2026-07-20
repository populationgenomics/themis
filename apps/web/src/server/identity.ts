import { selectedBackend } from "./adapters";
import { DevUserIdentity } from "./adapters/fixture/identity";

// Request identity — the seam the routes resolve the caller through. The
// implementation follows the selected backend: the fixture dev identity
// (adapters/fixture/identity.ts) offline, the IAP-JWT verifier with the real one.

type EnvLike = Record<string, string | undefined>;

/** Resolves the verified user email for a request from its headers. */
export interface UserIdentity {
  assertedEmail(headers: Headers): Promise<string>;
}

/** Build a FRESH identity resolver for the selected backend. `real` fails loud:
 *  the IAP verifier is not wired. */
export function buildUserIdentity(env: EnvLike = process.env): UserIdentity {
  if (selectedBackend(env) === "real") {
    throw new Error(
      "THEMIS_BACKEND=real: the real IAP verifier is not wired yet",
    );
  }
  return new DevUserIdentity();
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
 *  Route handlers use this; the real verifier will cache IAP's JWKS on its
 *  instance, so re-building per request would refetch the key set every time. */
export function getUserIdentity(env: EnvLike = process.env): UserIdentity {
  const s = identitySingletons();
  if (!s.identity) s.identity = buildUserIdentity(env);
  return s.identity;
}
