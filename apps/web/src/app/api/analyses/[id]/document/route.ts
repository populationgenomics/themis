import { toJson } from "@bufbuild/protobuf";
import { NextResponse } from "next/server";
import { DocumentResponseSchema } from "@/models/workbench";
import { userContext } from "@/server/context";
import { BadRequestError, run } from "../../../_lib/http";

/** GET /api/analyses/[id]/document[?version=] — the current working document as a
 *  produced|not-produced result, or a named historical version's markdown. */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  return run(async () => {
    const { backend } = await userContext(request);
    const { id } = await ctx.params;
    const versionParam = new URL(request.url).searchParams.get("version");
    const version =
      versionParam === null ? undefined : parseVersion(versionParam);
    const result = await backend.getDocument(id, version);
    return NextResponse.json(toJson(DocumentResponseSchema, result));
  });
}

function parseVersion(raw: string): number {
  const version = Number(raw);
  if (!Number.isInteger(version) || version < 1) {
    throw new BadRequestError(`invalid version: ${raw}`);
  }
  return version;
}
