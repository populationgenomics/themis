import { literatureContext } from "@/server/context";
import { run } from "../../../../_lib/http";

/** GET /api/papers/[id]/files/[name] — an associated file (a figure the markdown references, or a
 *  supplementary file). IAP-only; 302s to a signed GCS URL (live) or streams the fixture's bytes.
 *  Disposition follows the media type, not the file's role: anything off the inline allowlist is a
 *  forced download, the rest render inline. NOT_FOUND for an unknown file. */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ id: string; name: string }> },
): Promise<Response> {
  return run(async () => {
    const { literature } = await literatureContext(request);
    const { id, name } = await ctx.params;
    return literature.serveContent(id, { kind: "file", name });
  });
}
