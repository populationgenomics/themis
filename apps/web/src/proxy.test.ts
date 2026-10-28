import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { getScriptNonceFromHeader } from "next/dist/server/app-render/get-script-nonce-from-header";
import { NextRequest } from "next/server";
import {
  GetAccessResponseSchema,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import * as csp from "@/lib/csp";
import { Workbench } from "@/models/workbench";
import type { Gate } from "@/server/admission";
import { UnauthenticatedError } from "@/server/errors";
import type { Caller, UserIdentity } from "@/server/identity";
import type { AccessPort } from "@/server/ports";
import {
  CLOUD_RUN_HTTP1_REQUEST_MAX_BYTES,
  READ_MAX_BYTES,
} from "@/server/rpc/limits";
import nextConfig from "../next.config";
import {
  decide,
  enforceRequestPolicy,
  config as proxyConfig,
  servesWorkerScripts,
  servingChunk,
} from "./proxy";

// The perimeter with its identity and access answers supplied, so every outcome is reachable without
// the live adapters' env or a network call. The live identity's own checks are
// adapters/live/identity.test.ts's.

const EMAIL = "curator@example.org";

const AUTHENTICATING: UserIdentity = {
  async authenticate() {
    return { email: EMAIL };
  },
};

const REFUSING_IDENTITY: UserIdentity = {
  async authenticate() {
    throw new UnauthenticatedError("missing x-goog-iap-jwt-assertion");
  },
};

/** Fails the test if the perimeter consults it — for the path that must not. */
const UNREACHED_IDENTITY: UserIdentity = {
  async authenticate() {
    throw new Error("identity was consulted");
  },
};

const access = (admitted: boolean): AccessPort => ({
  async getAccess(email) {
    expect(email).toBe(EMAIL);
    return create(GetAccessResponseSchema, {
      projects: admitted
        ? [create(ProjectGrantSchema, { id: "asd", roles: [Role.CURATOR] })]
        : [],
    });
  },
});

const ADMITTING: Gate = { identity: AUTHENTICATING, access: access(true) };
const NOT_ADMITTING: Gate = {
  identity: AUTHENTICATING,
  access: access(false),
};
const REFUSING: Gate = { identity: REFUSING_IDENTITY, access: access(true) };
const UNREACHED: Gate = { identity: UNREACHED_IDENTITY, access: access(true) };

const ORIGIN = "https://themis.example";

const request = (
  path: string,
  init?: ConstructorParameters<typeof NextRequest>[1],
) => new NextRequest(new URL(path, ORIGIN), init);

/** A browser loading `path` as a page. */
const pageLoad = (path: string) =>
  request(path, {
    headers: {
      "sec-fetch-mode": "navigate",
      "sec-fetch-dest": "document",
      "sec-fetch-site": "none",
    },
  });

/** A script's request from one of the app's own pages. */
const fetched = (path: string, method = "POST") =>
  request(path, {
    method,
    headers: { "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin" },
  });

function refusal(decision: Response | null): Response {
  if (decision === null) throw new Error("the request was served");
  return decision;
}

const throughRefusingPerimeter = async (
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> =>
  (await decide(new NextRequest(new Request(input, init)), REFUSING)) ??
  new Response(null);

/** The generated client, talking to a perimeter that refuses every request. The transport
 *  never reaches the RPC mount, so what it parses is the refusal itself. */
const refusedClient = createClient(
  Workbench,
  createConnectTransport({
    baseUrl: `${ORIGIN}/api/rpc`,
    // The runtime's `fetch` type carries statics (`preconnect`) a plain function has not.
    fetch: throughRefusingPerimeter as typeof globalThis.fetch,
  }),
);

describe("the request-auth perimeter", () => {
  test("an unauthenticated call is refused in the client's own vocabulary", async () => {
    // The refusal lands in front of the RPC mount, so the client has to read it as a
    // Connect error: a body it cannot parse surfaces to a curator with no message at all.
    const error = await refusedClient
      .listProjects({})
      .then(() => undefined)
      .catch((thrown: unknown) => ConnectError.from(thrown));
    expect(error?.code).toBe(Code.Unauthenticated);
    expect(error?.rawMessage).not.toBe("");
  });

  test.each([
    ["a page load", pageLoad("/project/asd?tab=analyses")],
    ["a script's GET", fetched("/api/papers/p1/pdf", "GET")],
    [
      "a script's POST",
      fetched("/api/rpc/themis.workbench.rpc.Workbench/Poll"),
    ],
    ["a request with no Fetch Metadata", request("/")],
  ])("%s with no verifiable assertion is answered 401", async (_, req) => {
    // Only a request that went around IAP arrives without one, so there is no sign-in to send it to.
    const response = refusal(await decide(req, REFUSING));
    expect(response.status).toBe(401);
    expect(response.headers.get("location")).toBeNull();
    expect(await response.json()).toEqual({
      code: "unauthenticated",
      message: "unauthenticated",
    });
  });

  test("an unauthenticated request is refused before access is asked", async () => {
    const asking: Gate = {
      identity: REFUSING_IDENTITY,
      access: {
        async getAccess() {
          throw new Error("access was asked for an unauthenticated caller");
        },
      },
    };
    expect(refusal(await decide(fetched("/api/rpc/x"), asking)).status).toBe(
      401,
    );
  });

  test("a caller the deployment does not admit is refused 403", async () => {
    const response = refusal(
      await decide(fetched("/api/rpc/x"), NOT_ADMITTING),
    );
    expect(response.status).toBe(403);
    expect(((await response.json()) as { code: string }).code).toBe(
      "permission_denied",
    );
  });

  test("a page load by a caller not admitted is told so in words", async () => {
    // IAP signed the person in; the ACL is the decision, so there is nothing to redirect to.
    const response = refusal(await decide(pageLoad("/"), NOT_ADMITTING));
    expect(response.status).toBe(403);
    expect(response.headers.get("location")).toBeNull();
    expect(await response.text()).toContain("not admitted");
  });

  test("an admitted caller is served", async () => {
    expect(await decide(fetched("/api/rpc/x"), ADMITTING)).toBeNull();
  });

  test("the liveness probe is served without an assertion", async () => {
    expect(await decide(request("/api/healthz"), UNREACHED)).toBeNull();
  });

  test.each([
    "/api/healthz/x",
    "/api/healthzzz",
    "/api/healthz.json",
    "/auth/login",
    "/auth/callback",
  ])("%s is not exempt: the exemption matches exactly", async (path) => {
    expect(refusal(await decide(fetched(path, "GET"), REFUSING)).status).toBe(
      401,
    );
  });

  test("a refusal minted by another module graph is still the perimeter's 401", async () => {
    // The identity is memoized across Next's module graphs, so its refusal may carry the right name
    // on a foreign class object — the perimeter must not treat it as an outage to rethrow.
    const foreign: Gate = {
      identity: {
        async authenticate(): Promise<Caller> {
          throw Object.assign(new Error("no assertion"), {
            name: "UnauthenticatedError",
          });
        },
      },
      access: access(true),
    };
    expect(refusal(await decide(fetched("/api/rpc/x"), foreign)).status).toBe(
      401,
    );
  });

  test("an outage is not answered as an unverifiable caller", async () => {
    // IAP's key source or the auth service unreachable must not read as a refusal: the fault has
    // nothing to do with the caller's credential.
    const outage: Gate = {
      identity: AUTHENTICATING,
      access: {
        async getAccess() {
          throw new Error("auth service: ECONNREFUSED");
        },
      },
    };
    await expect(decide(fetched("/api/rpc/x"), outage)).rejects.toThrow(
      "ECONNREFUSED",
    );
  });

  test("an auth service serving a stale ACL is an outage, not a refusal", async () => {
    const stale: Gate = {
      identity: AUTHENTICATING,
      access: {
        async getAccess() {
          throw new ConnectError("ACL is stale", Code.Unavailable);
        },
      },
    };
    const error = await decide(fetched("/api/rpc/x"), stale).catch(
      (thrown: unknown) => thrown,
    );
    expect(ConnectError.from(error).code).toBe(Code.Unavailable);
  });
});

// The policy the perimeter declares, and the one the app renders under. Next encodes an overridden
// request header onto the pass-through response as `x-middleware-request-<name>` (it strips them
// again before the app's own response leaves), so both are observable from the one response.
const RESPONSE_POLICY = "content-security-policy";
const APP_POLICY = "x-middleware-request-content-security-policy";

/** The policy inputs are the composition root's to resolve; these tests are about the composition. */
const OFFLINE: csp.PolicyOptions = {
  development: false,
  contentSources: [],
  sheafSources: [],
};

const underPolicy = (req: NextRequest, gate: Gate) =>
  enforceRequestPolicy(req, gate, OFFLINE);

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
    const forged = request("/", {
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
    // would keep every offline test and fixture-mode run green while live refused everyone.
    const carrying = request("/", {
      headers: { "x-goog-iap-jwt-assertion": "an-assertion" },
    });
    const response = await underPolicy(carrying, ADMITTING);
    expect(
      response.headers.get("x-middleware-request-x-goog-iap-jwt-assertion"),
    ).toBe("an-assertion");
  });

  test("the proxy buffers the whole of any body that can reach the app", () => {
    // Past its limit the proxy drops whole chunks without failing the request. A limit only a chunk
    // above the router's would hand an oversized chunked publish over short, to fail parsing rather
    // than be answered resource_exhausted; nothing longer than Cloud Run's request limit arrives.
    const limit = nextConfig.experimental?.proxyClientMaxBodySize;
    if (typeof limit !== "number") {
      throw new Error("next.config.ts sets no numeric proxy body limit");
    }
    expect(limit).toBeGreaterThanOrEqual(CLOUD_RUN_HTTP1_REQUEST_MAX_BYTES);
    // And the router's own limit is one a request can exceed while still arriving whole.
    expect(READ_MAX_BYTES).toBeLessThan(CLOUD_RUN_HTTP1_REQUEST_MAX_BYTES);
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

describe("the worker policy", () => {
  test("a script chunk carries it, so the worker it starts runs under it", () => {
    // A worker takes the policy delivered with its own entry script, not the page's.
    const response = servingChunk(OFFLINE);
    expect(response.headers.get(RESPONSE_POLICY)).toBe(
      csp.workerPolicy(OFFLINE),
    );
  });

  test("a chunk is served like every other build asset, with no credential", () => {
    expect(servingChunk(OFFLINE).headers.get("x-middleware-next")).toBe("1");
  });
});

describe("where a worker's script can be", () => {
  test.each([
    "/_next/static/chunks/turbopack-worker-2gqdcwp7k90ea.js",
    "/_next/static/media/pdf.worker.min.2th4soq4xwzz7.mjs",
  ])("%s is served the worker policy, and the proxy runs for it", (path) => {
    // pdf.js starts its own worker from an emitted asset, which parses third-party PDF bytes.
    expect(servesWorkerScripts(path)).toBe(true);
    const prefix = path.slice(0, path.lastIndexOf("/") + 1);
    expect(proxyConfig.matcher).toContain(`${prefix}:path*`);
  });

  test("a page is not", () => {
    expect(servesWorkerScripts("/analysis/an_1")).toBe(false);
  });
});
