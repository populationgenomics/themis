import {
  type DescMessage,
  fromJson,
  type JsonValue,
  type MessageShape,
} from "@bufbuild/protobuf";
import { createValidator } from "@bufbuild/protovalidate";
import { NextResponse } from "next/server";
import { ResourceNotFoundError, UnauthenticatedError } from "@/server/errors";

// Shared boundary handling for the BFF route handlers. Route bodies throw; `run`
// catches and `toErrorResponse` maps to a consistent `{error:{code,message}}` shape
// with the right status. Internal detail never reaches the client — the 500 branch
// logs server-side and returns a generic message. `parseMessage` is the inbound
// seam: it decodes proto3-JSON into the view-model message and enforces its
// protovalidate rules, both failing as 400.

/** An error carrying the HTTP status a route should reply with. */
export class HttpError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
  }
}

export class BadRequestError extends HttpError {
  constructor(message = "bad request") {
    super(400, "bad_request", message);
  }
}

// The protovalidate runtime compiles a message's rules on first use and caches them
// on the validator, so one instance is shared across requests.
const validator = createValidator();

/** Decode a request body's proto3-JSON into `schema`'s message and enforce its
 *  protovalidate rules. A malformed body or a rule violation is the caller's fault
 *  (400); a rule that fails to compile/evaluate is our bug and surfaces as a 500. */
export function parseMessage<Desc extends DescMessage>(
  schema: Desc,
  body: JsonValue,
): MessageShape<Desc> {
  let message: MessageShape<Desc>;
  try {
    message = fromJson(schema, body);
  } catch (error) {
    throw new BadRequestError(
      error instanceof Error ? error.message : "invalid request body",
    );
  }
  const result = validator.validate(schema, message);
  if (result.kind === "invalid") {
    throw new BadRequestError(
      result.violations.map((violation) => violation.message).join("; "),
    );
  }
  if (result.kind === "error") {
    throw result.error;
  }
  return message;
}

/** Map any thrown value to a JSON error response. Order matters: HttpError (its own
 *  status) first, then ResourceNotFoundError → 404 and UnauthenticatedError → 401 by
 *  type, then a generic 500 that never leaks internals. */
export function toErrorResponse(error: unknown): NextResponse {
  if (error instanceof HttpError) {
    return NextResponse.json(
      { error: { code: error.code, message: error.message } },
      { status: error.status },
    );
  }
  if (error instanceof ResourceNotFoundError) {
    return NextResponse.json(
      { error: { code: "not_found", message: "resource not found" } },
      { status: 404 },
    );
  }
  if (error instanceof UnauthenticatedError) {
    return NextResponse.json(
      { error: { code: "unauthenticated", message: "unauthenticated" } },
      { status: 401 },
    );
  }
  console.error("unhandled route error", error);
  return NextResponse.json(
    { error: { code: "internal", message: "internal server error" } },
    { status: 500 },
  );
}

/** Run a route body, converting any thrown value into an error response. Keeps
 *  each handler to its happy path. */
export async function run(
  fn: () => Promise<NextResponse>,
): Promise<NextResponse> {
  try {
    return await fn();
  } catch (error) {
    return toErrorResponse(error);
  }
}

/** Parse a JSON request body, failing with a 400 (not a 500) on malformed JSON. */
export async function readJson(request: Request): Promise<JsonValue> {
  try {
    return (await request.json()) as JsonValue;
  } catch {
    throw new BadRequestError("invalid JSON body");
  }
}

/** Read a required query param, failing with a 400 (not a 500) when it is absent. */
export function requiredParam(request: Request, name: string): string {
  const value = new URL(request.url).searchParams.get(name);
  if (value === null || value === "") {
    throw new BadRequestError(`missing required "${name}" query parameter`);
  }
  return value;
}
