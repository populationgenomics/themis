import { describe, expect, test } from "bun:test";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { NextRequest } from "next/server";
import { Workbench } from "@/models/workbench";
import { UnauthenticatedError } from "@/server/errors";
import type { UserIdentity } from "@/server/identity";
import { enforceRequestAuth } from "./proxy";

// The perimeter with its identity supplied, so both outcomes are reachable without the
// live verifier's env or a network call.

const REFUSING: UserIdentity = {
  async assertedEmail() {
    throw new UnauthenticatedError("missing x-goog-iap-jwt-assertion");
  },
};

/** Fails the test if the perimeter consults it — for the paths that must not. */
const UNREACHED: UserIdentity = {
  async assertedEmail() {
    throw new Error("identity was consulted");
  },
};

const request = (path: string) =>
  new NextRequest(new URL(path, "https://themis.example"), {
    headers: { cookie: "GCP_IAAP_AUTH_TOKEN=expired" },
  });

const throughRefusingPerimeter = async (
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> =>
  enforceRequestAuth(new NextRequest(new Request(input, init)), REFUSING);

/** The generated client, talking to a perimeter that refuses every request. The transport
 *  never reaches the RPC mount, so what it parses is the refusal itself. */
const refusedClient = createClient(
  Workbench,
  createConnectTransport({
    baseUrl: "https://themis.example/api/rpc",
    // The runtime's `fetch` type carries statics (`preconnect`) a plain function has not.
    fetch: throughRefusingPerimeter as typeof globalThis.fetch,
  }),
);

describe("the request-auth perimeter", () => {
  test("an unverifiable caller is refused in the client's own vocabulary", async () => {
    // The refusal lands in front of the RPC mount, so the client has to read it as a
    // Connect error: a body it cannot parse surfaces to a curator with no message at all.
    const error = await refusedClient
      .listProjects({})
      .then(() => undefined)
      .catch((thrown: unknown) => ConnectError.from(thrown));
    expect(error?.code).toBe(Code.Unauthenticated);
    expect(error?.rawMessage).not.toBe("");
  });

  test("a verified caller is passed through", async () => {
    const response = await enforceRequestAuth(request("/api/rpc/x"), {
      async assertedEmail() {
        return "curator@example.org";
      },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  test("the liveness probe is served without an assertion", async () => {
    // It reaches the container directly, bypassing the load balancer, so it carries none.
    const response = await enforceRequestAuth(
      request("/api/healthz"),
      UNREACHED,
    );
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  test("a path that merely starts with a public one is not public", async () => {
    // The allowlist matches a path or a segment below it, never a prefix of a longer name.
    const response = await enforceRequestAuth(
      request("/api/healthzzz"),
      REFUSING,
    );
    expect(response.status).toBe(401);
  });

  test("a refusal minted by another module graph is still the perimeter's 401", async () => {
    // The identity can be memoized across Next's module graphs, so its refusal may
    // carry the right name on a foreign class object — the perimeter must not treat
    // it as an outage to rethrow.
    const foreignRefusing: UserIdentity = {
      async assertedEmail(): Promise<string> {
        throw Object.assign(new Error("missing assertion"), {
          name: "UnauthenticatedError",
        });
      },
    };
    const response = await enforceRequestAuth(
      request("/api/rpc/x"),
      foreignRefusing,
    );
    expect(response.status).toBe(401);
  });

  test("a failure that is not an unverifiable caller is not answered as one", async () => {
    // An outage reaching IAP's keys must not read as a refusal — the caller would be
    // told to re-authenticate over a fault that has nothing to do with their credential.
    const outage: UserIdentity = {
      async assertedEmail() {
        throw new Error("getIapPublicKeys: ECONNREFUSED");
      },
    };
    await expect(
      enforceRequestAuth(request("/api/rpc/x"), outage),
    ).rejects.toThrow("ECONNREFUSED");
  });
});
