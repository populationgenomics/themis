import {
  create,
  type DescMessage,
  fromJson,
  type JsonValue,
  type MessageShape,
  toJson,
} from "@bufbuild/protobuf";
import {
  CreateAnalysisRequestSchema,
  type CreateAnalysisResponse,
  CreateAnalysisResponseSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type ListAnalysesResponse,
  ListAnalysesResponseSchema,
  type ListProjectsResponse,
  ListProjectsResponseSchema,
  type PollResponse,
  PollResponseSchema,
} from "@/models/workbench";

// Thin typed fetch wrappers over the three BFF routes, one per endpoint. The UI's
// TanStack Query hooks wrap these — they never call `fetch` directly, so the wire
// contract lives in one place. The wire is proto3-JSON: each response is decoded
// with `fromJson` against its schema (real runtime validation, not a bare cast), so
// a shape mismatch fails loud here rather than surfacing as a blank render.

/** A non-2xx response from a BFF route, carrying the route's JSON error code and
 *  message (never internal detail). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function fetchMessage<Desc extends DescMessage>(
  schema: Desc,
  path: string,
  init?: RequestInit,
): Promise<MessageShape<Desc>> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw await toApiError(response);
  }
  return fromJson(schema, (await response.json()) as JsonValue);
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = "error";
  let message = response.statusText;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
    };
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    // No JSON body; fall back to the status text set above.
  }
  return new ApiError(response.status, code, message);
}

const enc = encodeURIComponent;

export const api = {
  listProjects: (): Promise<ListProjectsResponse> =>
    fetchMessage(ListProjectsResponseSchema, "/api/projects"),

  createAnalysis: (input: {
    prompt: string;
    projectId: string;
  }): Promise<CreateAnalysisResponse> =>
    fetchMessage(CreateAnalysisResponseSchema, "/api/analyses", {
      method: "POST",
      body: JSON.stringify(
        toJson(
          CreateAnalysisRequestSchema,
          create(CreateAnalysisRequestSchema, input),
        ),
      ),
    }),

  listAnalyses: (projectId: string): Promise<ListAnalysesResponse> =>
    fetchMessage(
      ListAnalysesResponseSchema,
      `/api/analyses?project=${enc(projectId)}`,
    ),

  pollAnalysis: (id: string): Promise<PollResponse> =>
    fetchMessage(PollResponseSchema, `/api/analyses/${enc(id)}/poll`),

  getDocument: (id: string, version?: number): Promise<DocumentResponse> =>
    fetchMessage(
      DocumentResponseSchema,
      version === undefined
        ? `/api/analyses/${enc(id)}/document`
        : `/api/analyses/${enc(id)}/document?version=${version}`,
    ),
};
