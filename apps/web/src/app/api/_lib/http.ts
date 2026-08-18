import { NextResponse } from "next/server";
import {
  isResourceNotFoundError,
  isUnauthenticatedError,
} from "@/server/errors";

// The error boundary shared by the paper-content routes. `run` wraps a route body; `toErrorResponse`
// maps a thrown value to a compact `{error:{code,message}}` with the right status — internal detail
// never reaches the client (the 500 branch logs and returns a generic message). Backend-agnostic:
// resolving and serving the object is the literature port's job (`serveContent` returns the `Response`),
// so this only shapes what a route body *throws*.

/** Map a thrown value to a compact JSON error response. `ResourceNotFoundError` → 404,
 *  `UnauthenticatedError` → 401, then a generic 500 that never leaks internals. */
export function toErrorResponse(error: unknown): NextResponse {
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
  console.error("unhandled route error", error);
  return NextResponse.json(
    { error: { code: "internal", message: "internal server error" } },
    { status: 500 },
  );
}

/** Run a route body, converting any thrown value into an error response. Keeps each handler to its
 *  happy path. */
export async function run(fn: () => Promise<Response>): Promise<Response> {
  try {
    return await fn();
  } catch (error) {
    return toErrorResponse(error);
  }
}
