import { NextResponse } from "next/server";

// Container liveness probe; not behind the BFF's auth/projection layer. Exists to
// exercise the App Router route-handler shape the BFF (data API, webhook receiver,
// session relay) builds on — see docs/design/frontend-framework.md.
export async function GET() {
  return NextResponse.json({ status: "ok" });
}
