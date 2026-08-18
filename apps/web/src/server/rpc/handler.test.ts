import { describe, expect, test } from "bun:test";
import { MethodOptions_IdempotencyLevel } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import { Workbench } from "@/models/workbench";
import { DOC_XML, XML_QUOTE } from "@/server/adapters/fixture/literature";
import { UnauthenticatedError } from "@/server/errors";
import type { FetchRouter } from "./fetch-router";
import { createFetchRouter } from "./fetch-router";
import { maskInternal, serveRpc } from "./handler";
import { errors, INTERNAL_MESSAGE, identity } from "./interceptors";
import { workbenchService } from "./service";

// Drives the real handler over real fetch Requests, so the adapter, the interceptor
// stack, and the service implementation are all on the path a browser call takes.
// The backend is named explicitly and `server/context.ts` memoizes whichever one the
// first request builds, so this must precede any call. The fixture's dev user belongs to
// one seeded Project.
process.env.THEMIS_BACKEND = "fixture";

const SERVICE = "/themis.workbench.rpc.Workbench";

interface RpcResult {
  status: number;
  /** Empty for a response the protocol rejects before producing an error message. */
  body: Record<string, unknown> | null;
  headers: Headers;
}

async function send(
  router: FetchRouter,
  method: string,
  message: unknown,
  init: RequestInit = {},
): Promise<RpcResult> {
  const path = `${SERVICE}/${method}`;
  const request = new Request(`http://localhost/api/rpc${path}`, {
    ...init,
    method: "POST",
    headers: { "content-type": "application/json", ...init.headers },
    body: JSON.stringify(message),
  });
  const response = await router(request, path);
  const text = await response.text();
  return {
    status: response.status,
    body: text === "" ? null : (JSON.parse(text) as Record<string, unknown>),
    headers: response.headers,
  };
}

const call = (method: string, message: unknown, init: RequestInit = {}) =>
  send(serveRpc, method, message, init);

/** The Connect `GET` form: the message rides in the query. */
function get(method: string, message: unknown): Promise<Response> {
  const path = `${SERVICE}/${method}`;
  const query = new URLSearchParams({
    connect: "v1",
    encoding: "json",
    message: JSON.stringify(message),
  });
  return serveRpc(
    new Request(`http://localhost/api/rpc${path}?${query}`),
    path,
  );
}

/** A router whose one implemented method raises, for the failure paths the fixture
 *  backend cannot produce. */
function raising(error: unknown): FetchRouter {
  return createFetchRouter({
    grpc: false,
    grpcWeb: false,
    interceptors: [errors(), identity()],
    routes: (router) => {
      router.service(Workbench, {
        listProjects() {
          throw error;
        },
      });
    },
  });
}

describe("the served surface", () => {
  test("a read is answered from the caller's own membership", async () => {
    const { status, body } = await call("ListProjects", {});
    expect(status).toBe(200);
    // Non-empty rules out a vacuous pass; every id is one the caller's membership grants.
    const projects = body?.projects as { id: string }[];
    expect(projects.length).toBeGreaterThan(0);
    expect(projects.every((project) => project.id !== "")).toBe(true);
  });

  test("a Project the caller does not belong to is not found", async () => {
    // Membership is what admits a read, and a non-member learns nothing about whether
    // the Project exists.
    const { status, body } = await call("ListAnalyses", {
      projectId: "proj_someone_else",
    });
    expect(status).toBe(404);
    expect(body?.code).toBe("not_found");
  });

  test("an analysis round-trips create → poll → document", async () => {
    const created = await call("CreateAnalysis", {
      prompt: "why is this variant pathogenic?",
      projectId: "proj_fixture",
    });
    expect(created.status).toBe(200);
    const id = created.body?.id as string;
    expect(id).toBeTruthy();

    expect((await call("Poll", { analysisId: id })).status).toBe(200);

    const document = await call("GetDocument", { analysisId: id });
    expect(document.status).toBe(200);
    // Not-produced is a represented absence: the field is unset, not an empty document.
    expect(document.body?.document).toBeUndefined();
  });

  test("no cache may store a reply", async () => {
    // `serveRpc` marks every reply unstorable whatever the verb: replies are per-caller, and
    // the IAP cookie that authenticates them is invisible to RFC 9111's `Authorization` rule,
    // so a cache keyed on the URL alone would cross curators.
    const { status, headers } = await call("ListProjects", {});
    expect(status).toBe(200);
    expect(headers.get("cache-control")).toBe("private, no-store");
  });

  // What puts the surface out of a browser's reach by `GET` is the route's export list
  // (`app/api/rpc/[...connect]/route.ts`), which this bypasses by calling `serveRpc` directly; the
  // level is read off the descriptor, so a method added later is covered without touching this
  // file. A method wanting the `GET` form changes both, and the runbook's `curl` with them.
  test.each(Workbench.methods.map((method) => [method.name, method] as const))(
    "%s declares no side-effect-free level, and admits no Connect GET",
    async (_name, method) => {
      expect(method.idempotency).not.toBe(
        MethodOptions_IdempotencyLevel.NO_SIDE_EFFECTS,
      );
      expect((await get(method.name, {})).status).toBe(405);
    },
  );

  test("an unknown method is not routed", async () => {
    const path = `${SERVICE}/Nonexistent`;
    const response = await serveRpc(
      new Request(`http://localhost/api/rpc${path}`, { method: "POST" }),
      path,
    );
    expect(response.status).toBe(404);
  });
});

describe("the paper read surface", () => {
  test("describePaper returns a seeded paper's metadata", async () => {
    const { status, body } = await call("DescribePaper", { docId: DOC_XML });
    expect(status).toBe(200);
    expect(body?.title).toBeTruthy();
    expect(body?.hasMarkdown).toBe(true);
  });

  test("describePaper is not-found for an unknown doc_id, and never says which", async () => {
    const { status, body } = await call("DescribePaper", {
      docId: "99999999-9999-4999-8999-999999999999",
    });
    expect(status).toBe(404);
    expect(body?.code).toBe("not_found");
  });

  test("locate resolves a seeded quote to markdown offsets", async () => {
    const { status, body } = await call("Locate", {
      docId: DOC_XML,
      quote: XML_QUOTE,
      representation: "REPRESENTATION_MARKDOWN",
    });
    expect(status).toBe(200);
    const offsets = body?.offsets as { start?: number; end: number };
    expect(offsets).toBeDefined();
    expect(offsets.end).toBeGreaterThan(offsets.start ?? 0);
  });

  test("a quote absent from the paper is not-located, not not-found", async () => {
    // not-located is a first-class outcome (the pane shows a warning chip), distinct from a
    // broken-citation NOT_FOUND doc_id — so the call succeeds and carries the notLocated variant.
    const { status, body } = await call("Locate", {
      docId: DOC_XML,
      quote: "a phrase that appears nowhere in the seeded paper",
      representation: "REPRESENTATION_MARKDOWN",
    });
    expect(status).toBe(200);
    expect(body?.notLocated).toBeDefined();
  });

  test("locate without a representation is invalid_argument, not a masked 500", async () => {
    // The representation is the caller's field; an unspecified one is their contract slip, so it
    // surfaces as invalid_argument (from the ClientInputError → InvalidArgument mapping), never a
    // masked Internal that also log-spams. Omitting it sends the proto3 default (UNSPECIFIED).
    const { status, body } = await call("Locate", {
      docId: DOC_XML,
      quote: XML_QUOTE,
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a blank doc_id is invalid_argument", async () => {
    const { status, body } = await call("DescribePaper", { docId: "" });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });
});

describe("the request boundary", () => {
  test("a blank prompt is rejected by its protovalidate rule", async () => {
    const { status, body } = await call("CreateAnalysis", {
      prompt: "   ",
      projectId: "proj_fixture",
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
    // Validation is the one layer outside the mask, so the rejection reaches the caller as
    // raised — the violations name which field of their own message failed which rule.
    expect((body?.details as unknown[]).length).toBeGreaterThan(0);
  });

  test("a field the schema does not declare is rejected", async () => {
    // connect-es ignores unknown JSON fields by default; a misspelled `version` would
    // then quietly return the current document instead of the named one.
    const { status, body } = await call("GetDocument", {
      analysisId: "an_1",
      versoin: 2,
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a version below the declared minimum is rejected", async () => {
    const { status, body } = await call("GetDocument", {
      analysisId: "an_1",
      version: 0,
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  // The content types a cross-site form can send without a preflight. None may reach a
  // method, so the IAP cookie a curator's browser carries cannot be spent by one.
  test.each([
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
  ])("a %s body is refused, not parsed", async (contentType) => {
    const { status } = await call(
      "ListProjects",
      {},
      { headers: { "content-type": contentType } },
    );
    expect(status).toBe(415);
  });
});

describe("failures reaching the client", () => {
  test("an unknown analysis is not-found, and never says which", async () => {
    const absent = await call("Poll", { analysisId: "an_never_existed" });
    expect(absent.status).toBe(404);
    expect(absent.body?.code).toBe("not_found");
    // Byte-identical to a foreign one: a caller must not be able to tell "outside my
    // Projects" from "does not exist" by any part of the reply.
    const foreign = await call("Poll", { analysisId: "an_someone_elses" });
    expect(foreign.status).toBe(absent.status);
    expect(foreign.body).toEqual(absent.body);
  });

  test("a reply the protocol cannot serialize is masked, and logged", async () => {
    // Serialization runs outside the interceptor chain, so `errors` never sees it: an
    // out-of-range Timestamp — what a garbage `created_at` derives to — would otherwise
    // answer with the encoder's own text and no server-side signal at all.
    const router = createFetchRouter({
      grpc: false,
      grpcWeb: false,
      interceptors: [errors(), identity()],
      routes: (route) => {
        route.service(Workbench, {
          listAnalyses() {
            return {
              analyses: [
                { id: "an_1", createdAt: { seconds: BigInt(2 ** 50) } },
              ],
            };
          },
        });
      },
    });

    const logged: unknown[] = [];
    const wasError = console.error;
    console.error = (...args: unknown[]) => {
      logged.push(args);
    };
    let result: RpcResult;
    try {
      result = await send(
        async (request, path) => maskInternal(await router(request, path)),
        "ListAnalyses",
        { projectId: "proj_fixture" },
      );
    } finally {
      console.error = wasError;
    }

    expect(result.status).toBe(500);
    expect(result.body).toEqual({
      code: "internal",
      message: INTERNAL_MESSAGE,
    });
    expect(JSON.stringify(result.body)).not.toContain("Timestamp");
    expect(logged.length).toBe(1);
  });

  test("a foreign invalid-argument is masked, not relayed", async () => {
    // What a service this BFF calls answers when the BFF built its request wrong: the
    // caller's own message was fine, so the fault is internal and its text is not theirs
    // to read. Only the validation layer, which sits outside the mask, speaks for them.
    const router = raising(
      new ConnectError(
        "row_key: value must match /^an_[0-9a-f]{32}$/",
        Code.InvalidArgument,
      ),
    );
    const { status, body } = await send(router, "ListProjects", {});
    expect(status).toBe(500);
    expect(body?.code).toBe("internal");
    expect(JSON.stringify(body)).not.toContain("row_key");
  });

  test("an unrecognized failure is masked", async () => {
    const router = raising(
      new Error("connect ECONNREFUSED 10.1.2.3:5432 dsn=hunter2"),
    );
    const { status, body } = await send(router, "ListProjects", {});
    expect(status).toBe(500);
    expect(body?.code).toBe("internal");
    const wire = JSON.stringify(body);
    expect(wire).not.toContain("hunter2");
    expect(wire).not.toContain("ECONNREFUSED");
  });

  test("an unverifiable caller is unauthenticated, not internal", async () => {
    const router = raising(
      new UnauthenticatedError("no IAP assertion on the request"),
    );
    const { status, body } = await send(router, "ListProjects", {});
    expect(status).toBe(401);
    expect(body?.code).toBe("unauthenticated");
    expect(JSON.stringify(body)).not.toContain("IAP assertion");
  });

  test("a router without the identity layer serves nothing", async () => {
    // The chokepoint is wiring, so assert the failure mode when it is absent: handlers
    // fail closed rather than reaching the data plane with no caller.
    const unwired = createFetchRouter({
      grpc: false,
      grpcWeb: false,
      interceptors: [errors()],
      routes: (router) => {
        router.service(Workbench, workbenchService);
      },
    });
    const { status, body } = await send(unwired, "ListProjects", {});
    expect(status).toBe(500);
    expect(body?.code).toBe("internal");
  });
});
