import { describe, expect, test } from "bun:test";
import { ResourceNotFoundError, UnauthenticatedError } from "@/server/errors";
import { BadRequestError, toErrorResponse } from "./http";

// The error boundary is where a non-member and an unknown analysis must become
// indistinguishable: AuthorizedBackend throws `ResourceNotFoundError` for both,
// each message naming the id, and `toErrorResponse` must strip it.

describe("toErrorResponse", () => {
  test("a non-member and an unknown analysis are an identical 404 that leaks no id", async () => {
    const fixed = {
      error: { code: "not_found", message: "resource not found" },
    };
    for (const id of ["an_someone_elses", "an_never_existed"]) {
      const response = toErrorResponse(
        new ResourceNotFoundError(`analysis not found: ${id}`),
      );
      expect(response.status).toBe(404);
      const json = await response.json();
      expect(json).toEqual(fixed);
      // The caller must not be able to tell "not yours" from "does not exist".
      expect(JSON.stringify(json)).not.toContain(id);
    }
  });

  test("an unverifiable caller is a 401", async () => {
    const response = toErrorResponse(
      new UnauthenticatedError("missing assertion"),
    );
    expect(response.status).toBe(401);
  });

  test("an internal error is masked as a generic 500", async () => {
    const response = toErrorResponse(new Error("db exploded: dsn=secret"));
    expect(response.status).toBe(500);
    const json = await response.json();
    expect(json).toEqual({
      error: { code: "internal", message: "internal server error" },
    });
    expect(JSON.stringify(json)).not.toContain("secret");
  });

  test("a typed HttpError keeps its status and code", async () => {
    const response = toErrorResponse(new BadRequestError("invalid version"));
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({
      error: { code: "bad_request", message: "invalid version" },
    });
  });
});
