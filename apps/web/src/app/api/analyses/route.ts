import { create, toJson } from "@bufbuild/protobuf";
import { NextResponse } from "next/server";
import {
  CreateAnalysisRequestSchema,
  CreateAnalysisResponseSchema,
  ListAnalysesResponseSchema,
} from "@/models/workbench";
import { userContext } from "@/server/context";
import { parseMessage, readJson, requiredParam, run } from "../_lib/http";

/** GET /api/analyses?project=<id> — the Project's prior analyses (newest first) the
 *  session switcher browses. */
export async function GET(request: Request): Promise<NextResponse> {
  return run(async () => {
    const { backend } = await userContext(request);
    const projectId = requiredParam(request, "project");
    const analyses = await backend.listAnalyses(projectId);
    return NextResponse.json(
      toJson(
        ListAnalysesResponseSchema,
        create(ListAnalysesResponseSchema, { analyses }),
      ),
    );
  });
}

/** POST /api/analyses — create + kick off a run in the request's Project. Verifies
 *  the caller, decodes and validates the request (a blank prompt or project id is
 *  rejected by protovalidate as a 400), mints the analysis + its session, and returns
 *  the new id. */
export async function POST(request: Request): Promise<NextResponse> {
  return run(async () => {
    const { backend } = await userContext(request);
    const input = parseMessage(
      CreateAnalysisRequestSchema,
      await readJson(request),
    );
    const analysis = await backend.createAnalysis({
      prompt: input.prompt,
      projectId: input.projectId,
    });
    return NextResponse.json(
      toJson(
        CreateAnalysisResponseSchema,
        create(CreateAnalysisResponseSchema, { id: analysis.id }),
      ),
      { status: 201 },
    );
  });
}
