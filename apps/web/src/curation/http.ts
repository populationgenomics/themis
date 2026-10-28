import { Code, ConnectError } from "@connectrpc/connect";
import { NextResponse } from "next/server";
import { Role } from "@/gen/themis/rpc/auth_pb";
import { accessPort, memberContext } from "@/server/context";
import {
  ClientInputError,
  isClientInputError,
  isNotAdmittedError,
  isResourceNotFoundError,
  isUnauthenticatedError,
} from "@/server/errors";
import {
  enforceResourceIsolation,
  isCrossSiteRequestError,
} from "@/server/fetch-metadata";
import { GrantedMembership } from "@/server/membership";
import type { AccessPort } from "@/server/ports";
import {
  accessFor,
  type CurationAccess,
  type CuratorCheck,
  isCurationAccessError,
  type ProjectRoles,
} from "./access";
import { curationStore, variantResolver } from "./index";
import {
  isAlleleNotResolvedError,
  isAlleleRegistryUnreachableError,
  type VariantResolver,
} from "./resolver";
import { type CurationStore, isDuplicateRegistrationError } from "./store";

// The module's request boundary: reject a request another site caused the browser to send, resolve
// the caller to their access in the Project the request names, and map a thrown value to a status.
// Every curation request — route or page — passes through here, so the resource-isolation check
// lives here and nowhere else. Separate from `app/api/_lib/http.ts` because the curation surface
// raises errors the workbench routes never do — a cross-site request and an operation the caller's
// role does not allow (403), a malformed input (400) — and a route that mapped any of them to a 500
// would tell a curator their own typo was a server fault.

/** What the boundary resolves a request through: the process-wide seams for a route, a test's own
 *  otherwise. */
export interface CurationBoundary {
  /** The admitted caller and their roles in each Project; raises for a caller who is not
   *  authenticated or not admitted. */
  member: (headers: Headers) => Promise<{ email: string; roles: ProjectRoles }>;
  /** Whether someone other than the caller curates a Project. */
  isCurator: CuratorCheck;
  store: CurationStore;
  resolver: VariantResolver;
}

/** Whether `email` holds the curator role on `projectId`, by the access answer for that person. */
export function curatorCheckThrough(access: AccessPort): CuratorCheck {
  return async (email, projectId) =>
    new GrantedMembership((await access.getAccess(email)).projects).hasRole(
      projectId,
      Role.CURATOR,
    );
}

function defaultBoundary(): CurationBoundary {
  return {
    member: async (headers) => {
      const { userEmail, membership } = await memberContext(headers);
      return { email: userEmail, roles: membership };
    },
    isCurator: curatorCheckThrough(accessPort()),
    store: curationStore(),
    resolver: variantResolver(),
  };
}

/** Resolve the admitted caller and their curation access in `projectId`. Throws
 *  `CrossSiteRequestError` for a request another site caused — before the caller is resolved —
 *  `UnauthenticatedError` for an unverifiable request, `NotAdmittedError` for a caller the
 *  deployment does not admit, and `ResourceNotFoundError` for a Project the caller holds no role
 *  on. */
export async function curationContext(
  request: Request,
  projectId: string,
  boundary: CurationBoundary = defaultBoundary(),
): Promise<CurationAccess> {
  enforceResourceIsolation(request);
  const caller = await boundary.member(request.headers);
  return accessFor(
    boundary.store,
    caller,
    projectId,
    boundary.resolver,
    boundary.isCurator,
  );
}

export function toCurationErrorResponse(error: unknown): NextResponse {
  if (
    isCrossSiteRequestError(error) ||
    isCurationAccessError(error) ||
    isNotAdmittedError(error)
  ) {
    return NextResponse.json(
      { error: { code: "forbidden", message: error.message } },
      { status: 403 },
    );
  }
  if (isClientInputError(error)) {
    return NextResponse.json(
      { error: { code: "invalid_argument", message: error.message } },
      { status: 400 },
    );
  }
  if (isDuplicateRegistrationError(error)) {
    return NextResponse.json(
      { error: { code: "already_exists", message: error.message } },
      { status: 409 },
    );
  }
  if (isAlleleNotResolvedError(error)) {
    // Its message reaches the caller, unlike the masked not-found below: see `resolver.ts`.
    return NextResponse.json(
      { error: { code: "not_found", message: error.message } },
      { status: 404 },
    );
  }
  if (isAlleleRegistryUnreachableError(error)) {
    // Not the caller's fault and not permanent, so neither a 400 nor a masked 500.
    return NextResponse.json(
      { error: { code: "unavailable", message: error.message } },
      { status: 503 },
    );
  }
  if (error instanceof ConnectError && error.code === Code.Unavailable) {
    // The auth service, stale or unreachable, while a check asked it about someone: transient, as
    // the registry's outage is, and its own wording stays in the log.
    console.error("curation: the auth service is unavailable", error);
    return NextResponse.json(
      {
        error: {
          code: "unavailable",
          message: "access could not be checked just now; try again shortly",
        },
      },
      { status: 503 },
    );
  }
  if (isResourceNotFoundError(error)) {
    return NextResponse.json(
      { error: { code: "not_found", message: "resource not found" } },
      { status: 404 },
    );
  }
  if (isUnauthenticatedError(error)) {
    return NextResponse.json(
      { error: { code: "unauthenticated", message: "unauthenticated" } },
      { status: 401 },
    );
  }
  console.error("unhandled curation route error", error);
  return NextResponse.json(
    { error: { code: "internal", message: "internal server error" } },
    { status: 500 },
  );
}

/** Run a route body, converting any thrown value into an error response. */
export async function runCuration(
  fn: () => Promise<Response>,
): Promise<Response> {
  try {
    return await fn();
  } catch (error) {
    return toCurationErrorResponse(error);
  }
}

/** Parse a JSON body, failing as the caller's fault rather than as a server error. */
export async function jsonBody(
  request: Request,
): Promise<Record<string, unknown>> {
  let parsed: unknown;
  try {
    parsed = await request.json();
  } catch {
    throw new ClientInputError("the request body is not valid JSON");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ClientInputError("the request body must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

export function requiredString(
  body: Record<string, unknown>,
  key: string,
): string {
  const value = body[key];
  if (typeof value !== "string" || value.trim() === "") {
    throw new ClientInputError(`${key} is required`);
  }
  return value;
}

export function optionalString(
  body: Record<string, unknown>,
  key: string,
): string {
  const value = body[key];
  if (value === undefined || value === null) return "";
  if (typeof value !== "string") {
    throw new ClientInputError(`${key} must be a string`);
  }
  return value;
}
