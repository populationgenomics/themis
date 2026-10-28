import { createAccess } from "./adapters";
import { NotAdmittedError } from "./errors";
import { buildUserIdentity, type Caller, type UserIdentity } from "./identity";
import { GrantedMembership } from "./membership";
import type { AccessPort } from "./ports";

// The one admission check (docs/design/access-control.md): authenticate the request, then ask what the
// caller may reach. The perimeter (proxy.ts) and the data seam (context.ts) each run it on their own,
// and neither reuses the other's answer.

type EnvLike = Record<string, string | undefined>;

/** What admission consults: who the caller is, and what they may reach. */
export interface Gate {
  readonly identity: UserIdentity;
  readonly access: AccessPort;
}

/** An admitted caller and the Projects, with their roles, one access answer granted them. */
export interface Admission {
  readonly caller: Caller;
  readonly membership: GrantedMembership;
}

/** Authenticate the request and require its caller to be admitted to the deployment: to hold a role
 *  on at least one Project. Raises `UnauthenticatedError` for a request with no verifiable credential
 *  and `NotAdmittedError` for a caller the access answer grants no Project; any other failure,
 *  a malformed answer included, propagates as the outage it is. */
export async function admit(headers: Headers, gate: Gate): Promise<Admission> {
  const caller = await gate.identity.authenticate(headers);
  const access = await gate.access.getAccess(caller.email);
  if (access.projects.length === 0) {
    throw new NotAdmittedError("the caller is not admitted to this deployment");
  }
  return { caller, membership: new GrantedMembership(access.projects) };
}

/** Build a FRESH gate for the selected backend. */
export function buildGate(env: EnvLike = process.env): Gate {
  return { identity: buildUserIdentity(env), access: createAccess(env) };
}

/** The process-wide gate, memoized on `globalThis` so the auth service's transport survives Next's
 *  dev HMR. */
export function getGate(env: EnvLike = process.env): Gate {
  const holder = globalThis as typeof globalThis & { __themisGate?: Gate };
  if (!holder.__themisGate) holder.__themisGate = buildGate(env);
  return holder.__themisGate;
}
