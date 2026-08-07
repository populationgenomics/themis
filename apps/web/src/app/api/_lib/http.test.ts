import { describe, expect, test } from "bun:test";
import { ResourceNotFoundError, UnauthenticatedError } from "@/server/errors";
import { run, toErrorResponse } from "./http";

// `toErrorResponse` is the only thing keeping a route body's thrown detail off the wire: it maps by
// type to a compact envelope and the catch-all 500 must never carry the original message.
describe("toErrorResponse", () => {
  test("ResourceNotFoundError → 404 with a generic message", async () => {
    const res = toErrorResponse(
      new ResourceNotFoundError("paper 123 is secret"),
    );
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({
      error: { code: "not_found", message: "resource not found" },
    });
  });

  test("UnauthenticatedError → 401", async () => {
    const res = toErrorResponse(new UnauthenticatedError("no IAP assertion"));
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({
      error: { code: "unauthenticated", message: "unauthenticated" },
    });
  });

  test("any other error → a 500 that does not leak the thrown message", async () => {
    const res = toErrorResponse(new Error("internal secret detail"));
    expect(res.status).toBe(500);
    expect(JSON.stringify(await res.json())).not.toContain("secret");
  });
});

describe("run", () => {
  test("returns the body's response unchanged on the happy path", async () => {
    const ok = new Response("ok", { status: 200 });
    expect(await run(async () => ok)).toBe(ok);
  });

  test("converts a thrown error into its mapped response", async () => {
    const res = await run(async () => {
      throw new ResourceNotFoundError("x");
    });
    expect(res.status).toBe(404);
  });
});
