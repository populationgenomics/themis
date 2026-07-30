import { serveRpc } from "@/server/rpc/handler";

// The BFF's data API: every Workbench method, served under /api/rpc by one catch-all. The
// matched segments are the method path, so the mount point is never spelled twice.
//
// Nothing here opts a GET into caching, which route handlers otherwise never do
// (docs/design/frontend-framework.md, Data fetching).

async function handle(
  request: Request,
  ctx: { params: Promise<{ connect: string[] }> },
): Promise<Response> {
  const { connect } = await ctx.params;
  return serveRpc(request, `/${connect.join("/")}`);
}

export { handle as GET, handle as POST };
