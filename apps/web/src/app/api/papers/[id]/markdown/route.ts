import { literatureContext } from "@/server/context";
import { run } from "../../../_lib/http";

/** GET /api/papers/[id]/markdown — the chosen markdown rendering. IAP-only; 302s to a signed GCS URL
 *  (live) or streams the fixture's bytes. NOT_FOUND when the paper has no markdown rendering. Figure
 *  refs are resolved to the files route by the renderer, not rewritten here (so `locate` offsets stay
 *  stable against the served text). */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  return run(async () => {
    const { literature } = await literatureContext(request);
    const { id } = await ctx.params;
    return literature.serveContent(id, { kind: "markdown" });
  });
}
