import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import * as csp from "@/lib/csp";
import { isUnauthenticatedError } from "@/server/errors";
import type { UserIdentity } from "@/server/identity";
import { getUserIdentity } from "@/server/identity";

// The container liveness probe carries no IAP credential.
const PUBLIC_PATHS = ["/api/healthz"];

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
}

// Request-auth perimeter (docs/design/security.md): every matched path except
// PUBLIC_PATHS must present a verifiable IAP assertion. server/context.ts re-verifies
// at the data seam and is the authoritative check.
//
// The refusal is a Connect error body — top-level `code`/`message`. The perimeter sits in
// front of the RPC mount, so a refusal here has to parse as one: a shape the generated
// client cannot read reaches a curator as an error with no message.

/** The refusal for a request that presents no verifiable assertion, or `null` to serve it. Building
 *  the pass-through is the caller's, so no decision here can drop what the app sees. */
export async function enforceRequestAuth(
  request: NextRequest,
  identity: UserIdentity,
): Promise<NextResponse | null> {
  if (isPublic(request.nextUrl.pathname)) return null;
  try {
    await identity.assertedEmail(request.headers);
  } catch (error) {
    if (!isUnauthenticatedError(error)) throw error;
    return NextResponse.json(
      { code: "unauthenticated", message: "unauthenticated" },
      { status: 401 },
    );
  }
  return null;
}

/** The perimeter's answer to one request, under a fresh Content Security Policy: the app renders
 *  under the policy — Next takes the render nonce out of it — and the response declares the same
 *  one, refusal and pass-through alike. */
export async function enforceRequestPolicy(
  request: NextRequest,
  identity: UserIdentity,
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
    (await enforceRequestAuth(request, identity)) ??
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

/** The perimeter's answer to a static asset a worker can start from: the worker policy, which
 *  governs the one that is a worker's script. Every other asset there carries it inertly, so no
 *  asset has to be recognised as a worker's by its name. */
export async function enforceChunkPolicy(
  request: NextRequest,
  identity: UserIdentity,
  options: csp.PolicyOptions,
): Promise<NextResponse> {
  const response =
    (await enforceRequestAuth(request, identity)) ?? NextResponse.next();
  response.headers.set("content-security-policy", csp.workerPolicy(options));
  return response;
}

// Next's entry point. It calls this with a second argument of its own, so the identity is
// resolved here rather than taken as a parameter.
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const options: csp.PolicyOptions = {
    // Compared inline, so the bundler folds it to a literal and no runtime environment read can
    // reach the development concessions.
    development: process.env.NODE_ENV === "development",
    contentSources: csp.contentSources(),
    sheafSources: csp.sheafSources(),
  };
  if (servesWorkerScripts(request.nextUrl.pathname)) {
    return enforceChunkPolicy(request, getUserIdentity(), options);
  }
  return enforceRequestPolicy(request, getUserIdentity(), options);
}

// Skips Next's own asset serving but for where a worker's script can be, which carries the worker
// policy — a performance filter, not an auth exemption: PUBLIC_PATHS is the allowlist, and it
// matches exactly rather than by prefix. The matcher must be a static literal, so it spells out
// WORKER_SCRIPT_PREFIXES again.
export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico).*)",
    "/_next/static/chunks/:path*",
    "/_next/static/media/:path*",
  ],
};
