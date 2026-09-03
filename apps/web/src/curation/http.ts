import { NextResponse } from "next/server";
import {
  ClientInputError,
  isClientInputError,
  isResourceNotFoundError,
  isUnauthenticatedError,
} from "@/server/errors";
import {
  enforceResourceIsolation,
  isCrossSiteRequestError,
} from "@/server/fetch-metadata";
import { getUserIdentity, type UserIdentity } from "@/server/identity";
import {
  accessFor,
  type CurationAccess,
  isCurationAccessError,
} from "./access";
import { curationStore, variantResolver } from "./index";
import {
  isAlleleNotResolvedError,
  isAlleleRegistryUnreachableError,
  type VariantResolver,
} from "./resolver";
import type { CurationStore } from "./store";

// The module's request boundary: reject a request another site caused the browser to send, resolve
// the caller to their scoped access, and map a thrown value to a status. Every curation request —
// route or page — passes through here, so the resource-isolation check lives here and nowhere else.
// Separate from `app/api/_lib/http.ts` because the curation surface raises errors the workbench
// routes never do — a cross-site request and a caller with no role (403), a malformed input (400) —
// and a route that mapped any of them to a 500 would tell a curator their own typo was a server
// fault.

/** What the boundary resolves a request through: the process-wide seams for a route, a test's own
 *  otherwise. */
export interface CurationBoundary {
  identity: UserIdentity;
  store: CurationStore;
  resolver: VariantResolver;
}

/** Resolve the verified caller and their curation access. Throws `CrossSiteRequestError` for a
 *  request another site caused — before the caller is resolved — `UnauthenticatedError` for an
 *  unverifiable request and `CurationAccessError` for a verified caller holding no role. */
export async function curationContext(
  request: Request,
  boundary: CurationBoundary = {
    identity: getUserIdentity(),
    store: curationStore(),
    resolver: variantResolver(),
  },
): Promise<CurationAccess> {
  enforceResourceIsolation(request);
  const email = await boundary.identity.assertedEmail(request.headers);
  return accessFor(boundary.store, email, boundary.resolver);
}

export function toCurationErrorResponse(error: unknown): NextResponse {
  if (isCrossSiteRequestError(error) || isCurationAccessError(error)) {
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
