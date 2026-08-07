import { literatureContext } from "@/server/context";
import { run } from "../../../_lib/http";

/** GET /api/papers/[id]/pdf — the PDF revision. IAP-only; 302s to a signed GCS URL (live) or streams
 *  the fixture's bytes. NOT_FOUND when the paper has no PDF. */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  return run(async () => {
    const { literature } = await literatureContext(request);
    const { id } = await ctx.params;
    return literature.serveContent(id, { kind: "pdf" });
  });
}
