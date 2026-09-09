import { describe, expect, test } from "bun:test";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { getScriptNonceFromHeader } from "next/dist/server/app-render/get-script-nonce-from-header";
import { NextRequest } from "next/server";
import type * as csp from "@/lib/csp";
import { Workbench } from "@/models/workbench";
import { UnauthenticatedError } from "@/server/errors";
import type { UserIdentity } from "@/server/identity";
import nextConfig from "../next.config";
import { enforceRequestAuth, enforceRequestPolicy } from "./proxy";

// The perimeter with its identity supplied, so both outcomes are reachable without the
// live verifier's env or a network call.

const REFUSING: UserIdentity = {
  async assertedEmail() {
    throw new UnauthenticatedError("missing x-goog-iap-jwt-assertion");
  },
};

const ADMITTING: UserIdentity = {
  async assertedEmail() {
    return "curator@example.org";
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
  enforceRequestAuth(new NextRequest(new Request(input, init)), REFUSING).then(
    (refusal) => refusal ?? new Response(null),
  );

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
    expect(
      await enforceRequestAuth(request("/api/rpc/x"), ADMITTING),
    ).toBeNull();
  });

  test("the liveness probe is served without an assertion", async () => {
    // It reaches the container directly, bypassing the load balancer, so it carries none.
    expect(
      await enforceRequestAuth(request("/api/healthz"), UNREACHED),
    ).toBeNull();
  });

  test("a path that merely starts with a public one is not public", async () => {
    // The allowlist matches a path or a segment below it, never a prefix of a longer name.
    const refusal = await enforceRequestAuth(
      request("/api/healthzzz"),
      REFUSING,
    );
    expect(refusal?.status).toBe(401);
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
    const refusal = await enforceRequestAuth(
      request("/api/rpc/x"),
      foreignRefusing,
    );
    expect(refusal?.status).toBe(401);
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

// The policy the perimeter declares, and the one the app renders under. Next encodes an overridden
// request header onto the pass-through response as `x-middleware-request-<name>` (it strips them
// again before the app's own response leaves), so both are observable from the one response.
const RESPONSE_POLICY = "content-security-policy";
const APP_POLICY = "x-middleware-request-content-security-policy";

/** The policy inputs are the composition root's to resolve; these tests are about the composition. */
const OFFLINE: csp.PolicyOptions = { development: false, contentSources: [] };

const underPolicy = (request: NextRequest, identity: UserIdentity) =>
  enforceRequestPolicy(request, identity, OFFLINE);

describe("the content security policy", () => {
  test("a served request renders under the policy its response declares", async () => {
    const response = await underPolicy(request("/"), ADMITTING);
    const declared = response.headers.get(RESPONSE_POLICY);
    expect(declared).toBeTruthy();
    expect(response.headers.get(APP_POLICY)).toBe(declared);
    // `getScriptNonceFromHeader` is the parser Next's app render reads the nonce with; a nonce it
    // cannot parse is dropped, and the page renders with no script the policy admits.
    expect(getScriptNonceFromHeader(declared ?? "")).toBeTruthy();
  });

  test("a refused request carries the policy too", async () => {
    const response = await underPolicy(request("/api/rpc/x"), REFUSING);
    expect(response.status).toBe(401);
    expect(response.headers.get(RESPONSE_POLICY)).toBeTruthy();
  });

  test("no two requests share a nonce", async () => {
    const nonces = await Promise.all(
      Array.from({ length: 8 }, async () => {
        const response = await underPolicy(request("/"), ADMITTING);
        return getScriptNonceFromHeader(
          response.headers.get(RESPONSE_POLICY) ?? "",
        );
      }),
    );
    expect(new Set(nonces).size).toBe(nonces.length);
  });

  test("a caller cannot choose the nonce the app renders with", async () => {
    // Otherwise an attacker who can set a request header signs their own inline script.
    const forged = new NextRequest(new URL("/", "https://themis.example"), {
      headers: { "content-security-policy": "script-src 'nonce-forged'" },
    });
    const response = await underPolicy(forged, ADMITTING);
    const rendered = getScriptNonceFromHeader(
      response.headers.get(APP_POLICY) ?? "",
    );
    expect(rendered).toBe(
      getScriptNonceFromHeader(response.headers.get(RESPONSE_POLICY) ?? ""),
    );
    expect(rendered).not.toBe("forged");
  });

  test("the app still sees the headers the caller sent", async () => {
    // The perimeter overrides the request's headers to carry the policy. Seeding that override from
    // anything but the wire headers would strip the IAP assertion, which server/context.ts
    // re-verifies from what the app can see — and the fixture identity, which reads no headers,
    // would keep every offline test and fixture-mode run green while live IAP refused everyone.
    const carrying = new NextRequest(new URL("/", "https://themis.example"), {
      headers: { "x-goog-iap-jwt-assertion": "an-assertion" },
    });
    const response = await underPolicy(carrying, ADMITTING);
    expect(
      response.headers.get("x-middleware-request-x-goog-iap-jwt-assertion"),
    ).toBe("an-assertion");
  });

  test("the perimeter is the only source of a policy", async () => {
    // Two Content-Security-Policy headers are intersected by the browser, and the policy actually in
    // force becomes one that neither of them states.
    const { headers } = nextConfig;
    if (headers === undefined) {
      throw new Error("next.config.ts declares no response headers");
    }
    const groups = await headers();
    const configured = groups.flatMap((group) =>
      group.headers.map((header) => header.key.toLowerCase()),
    );
    expect(configured).not.toContain("content-security-policy");
    // Framing is refused in the perimeter's policy; this covers browsers that read only the header.
    expect(configured).toContain("x-frame-options");
  });
});
