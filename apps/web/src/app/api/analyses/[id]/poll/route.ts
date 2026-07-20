import { toJson } from "@bufbuild/protobuf";
import { NextResponse } from "next/server";
import { PollResponseSchema } from "@/models/workbench";
import { userContext } from "@/server/context";
import { run } from "../../../_lib/http";

/** GET /api/analyses/[id]/poll — the liveness tick: the full projected event list
 *  and the working-document version signal. */
export async function GET(
  request: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  return run(async () => {
    const { backend } = await userContext(request);
    const { id } = await ctx.params;
    const response = await backend.pollEvents(id);
    return NextResponse.json(toJson(PollResponseSchema, response));
  });
}
