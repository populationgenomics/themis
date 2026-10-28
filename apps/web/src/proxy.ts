import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import * as csp from "@/lib/csp";
import { admit, type Gate, getGate } from "@/server/admission";
import { isNotAdmittedError, isUnauthenticatedError } from "@/server/errors";

// Request-auth perimeter (docs/design/access-control.md, docs/design/security.md): every path but the
// liveness probe and Next's build assets must carry an IAP assertion the identity verifies, from a
// caller the deployment admits. server/context.ts runs the same admission again at the data seam, on
// its own.
//
// The refusal of a request that is not a page load is a Connect error body — top-level
// `code`/`message`. The perimeter sits in front of the RPC mount, so a refusal here has to parse as
// one: a shape the generated client cannot read reaches a curator as an error with no message.

/** The paths served to a caller with no assertion: the container's liveness probe, which reaches the
 *  container directly rather than through IAP. Matched exactly. */
const EXEMPT_PATHS: ReadonlySet<string> = new Set(["/api/healthz"]);

/** A browser loading a page, as opposed to a script's request. */
function isPageLoad(request: NextRequest): boolean {
  return (
    (request.method === "GET" || request.method === "HEAD") &&
    request.headers.get("sec-fetch-mode") === "navigate"
  );
}

/** The refusal of a request with no verifiable IAP assertion. IAP signs in every browser it lets
 *  through, so only a request that went around it lands here, page load or not. */
function unauthenticated(): NextResponse {
  return NextResponse.json(
    { code: "unauthenticated", message: "unauthenticated" },
    { status: 401 },
  );
}

function notAdmitted(request: NextRequest): NextResponse {
  if (isPageLoad(request)) {
    return new NextResponse(
      "Your Google account is not admitted to this Themis deployment.",
      { status: 403, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }
  return NextResponse.json(
    { code: "permission_denied", message: "not admitted" },
    { status: 403 },
  );
}

/** The refusal for one request, or `null` to serve it. Building the pass-through is the caller's, so
 *  no decision here can drop what the app sees. A failure that is neither an unverifiable nor an
 *  unadmitted caller — the auth service or IAP's key source unreachable — propagates as the outage
 *  it is. */
export async function decide(
  request: NextRequest,
  gate: Gate,
): Promise<NextResponse | null> {
  if (EXEMPT_PATHS.has(request.nextUrl.pathname)) return null;
  try {
    await admit(request.headers, gate);
  } catch (error) {
    if (isUnauthenticatedError(error)) return unauthenticated();
    if (isNotAdmittedError(error)) return notAdmitted(request);
    throw error;
  }
  return null;
}

/** The perimeter's answer to one request, under a fresh Content Security Policy: the app renders
 *  under the policy — Next takes the render nonce out of it — and the response declares the same
 *  one, refusal and pass-through alike. */
export async function enforceRequestPolicy(
  request: NextRequest,
  gate: Gate,
  options: csp.PolicyOptions,
): Promise<NextResponse> {
  const policy = csp.policy(csp.mintNonce(), options);
  // The wire headers plus the policy, which is where Next reads the render nonce from. `set`, not
  // `append`: appended, a caller's own header of this name would be the one it reads. Anything but
  // the wire headers underneath would drop the IAP assertion that server/context.ts re-verifies
  // from what the app can see.
  const appHeaders = new Headers(request.headers);
  appHeaders.set("content-security-policy", policy);
  const response =
    (await decide(request, gate)) ??
    NextResponse.next({ request: { headers: appHeaders } });
  response.headers.set("content-security-policy", policy);
  return response;
}

/** Where the bundler serves what a worker can start from: script chunks, among them the entry script
 *  every bundled worker starts from, and emitted assets, among them pdf.js's worker, which pdf.js
 *  starts itself. Everything else served from either (other chunks, stylesheets, fonts, images) is
 *  loaded by a document, which ignores a policy on it. */
const WORKER_SCRIPT_PREFIXES = [
  "/_next/static/chunks/",
  "/_next/static/media/",
];

/** Whether `pathname` is served from where a worker's script can be. */
export function servesWorkerScripts(pathname: string): boolean {
  return WORKER_SCRIPT_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

/** The answer to a static asset a worker can start from: served to anyone, like the rest of Next's
 *  build assets, under the worker policy, which governs the one that is a worker's script. Every
 *  other asset there carries it inertly, so no asset has to be recognised as a worker's by its name. */
export function servingChunk(options: csp.PolicyOptions): NextResponse {
  const response = NextResponse.next();
  response.headers.set("content-security-policy", csp.workerPolicy(options));
  return response;
}

// Next's entry point. It calls this with a second argument of its own, so the gate is resolved here
// rather than taken as a parameter.
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const options: csp.PolicyOptions = {
    // Compared inline, so the bundler folds it to a literal and no runtime environment read can
    // reach the development concessions.
    development: process.env.NODE_ENV === "development",
    contentSources: csp.contentSources(),
    sheafSources: csp.sheafSources(),
  };
  if (servesWorkerScripts(request.nextUrl.pathname)) {
    return servingChunk(options);
  }
  return enforceRequestPolicy(request, getGate(), options);
}

// Every path but Next's build assets under `/_next/static/`, which are the same for every caller; IAP
// guards them like every other path, and the app checks no assertion on them. Of those, where a
// worker's script can be is matched again, only to carry the worker policy. The matcher must be a static literal, so it spells out
// WORKER_SCRIPT_PREFIXES again.
export const config = {
  matcher: [
    "/((?!_next/static/).*)",
    "/_next/static/chunks/:path*",
    "/_next/static/media/:path*",
  ],
};
