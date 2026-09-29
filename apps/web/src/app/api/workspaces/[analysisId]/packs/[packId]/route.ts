import { userContext } from "@/server/context";
import { run } from "../../../../_lib/http";

/** GET /api/workspaces/[analysisId]/packs/[packId] — one pack of the Analysis's workspace repository,
 *  the URL the offline backend's SignWorkspacePackUrls names. Membership-scoped like every point
 *  access: NOT_FOUND for an Analysis outside the caller's Projects, a pack the ref document does not
 *  list, and on the live backend, whose pack URLs point at the bucket instead. */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ analysisId: string; packId: string }> },
): Promise<Response> {
  return run(async () => {
    const { backend } = await userContext(request.headers);
    const { analysisId, packId } = await ctx.params;
    return backend.serveWorkspacePack(analysisId, packId);
  });
}
