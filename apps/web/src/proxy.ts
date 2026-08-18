import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
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
export async function enforceRequestAuth(
  request: NextRequest,
  identity: UserIdentity,
): Promise<NextResponse> {
  if (!isPublic(request.nextUrl.pathname)) {
    try {
      await identity.assertedEmail(request.headers);
    } catch (error) {
      if (!isUnauthenticatedError(error)) throw error;
      return NextResponse.json(
        { code: "unauthenticated", message: "unauthenticated" },
        { status: 401 },
      );
    }
  }
  return NextResponse.next();
}

// Next's entry point. It calls this with a second argument of its own, so the identity is
// resolved here rather than taken as a parameter.
export async function proxy(request: NextRequest): Promise<NextResponse> {
  return enforceRequestAuth(request, getUserIdentity());
}

// Skips Next's own asset serving — a performance filter, not an auth exemption:
// PUBLIC_PATHS is the allowlist, and it matches exactly rather than by prefix.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
