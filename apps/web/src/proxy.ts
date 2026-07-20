import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { UnauthenticatedError } from "@/server/errors";
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
export async function proxy(request: NextRequest): Promise<NextResponse> {
  if (!isPublic(request.nextUrl.pathname)) {
    try {
      await getUserIdentity().assertedEmail(request.headers);
    } catch (error) {
      if (!(error instanceof UnauthenticatedError)) throw error;
      return NextResponse.json(
        { error: { code: "unauthenticated", message: "unauthenticated" } },
        { status: 401 },
      );
    }
  }
  return NextResponse.next();
}

// Skips Next's own asset serving — a performance filter, not an auth exemption:
// PUBLIC_PATHS is the allowlist, and it matches exactly rather than by prefix.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
