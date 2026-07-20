import { create, toJson } from "@bufbuild/protobuf";
import { NextResponse } from "next/server";
import { ListProjectsResponseSchema } from "@/models/workbench";
import { userContext } from "@/server/context";
import { run } from "../_lib/http";

/** GET /api/projects — the Projects the caller belongs to; the app-bar's Project
 *  selector and the scope for create/list. */
export async function GET(request: Request): Promise<NextResponse> {
  return run(async () => {
    const { backend } = await userContext(request);
    const projects = await backend.listProjects();
    return NextResponse.json(
      toJson(
        ListProjectsResponseSchema,
        create(ListProjectsResponseSchema, { projects }),
      ),
    );
  });
}
