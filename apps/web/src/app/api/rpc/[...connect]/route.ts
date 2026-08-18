import { serveRpc } from "@/server/rpc/handler";

// The BFF's data API: every Workbench method, served under /api/rpc by one catch-all. The
// matched segments are the method path, so the mount point is never spelled twice.
//
// POST is the only verb exported, which is what holds the surface POST-only: an unexported verb is
// Next's own 405, whatever a method declares (docs/design/proto.md, bucket 2).

async function handle(
  request: Request,
  ctx: { params: Promise<{ connect: string[] }> },
): Promise<Response> {
  const { connect } = await ctx.params;
  return serveRpc(request, `/${connect.join("/")}`);
}

export { handle as POST };
