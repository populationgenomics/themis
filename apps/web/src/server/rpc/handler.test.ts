import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { MethodOptions_IdempotencyLevel } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import { GET as getPack } from "@/app/api/workspaces/[analysisId]/packs/[packId]/route";
import { POLL_TIP_BUDGET_MS } from "@/lib/workspace-deadlines";
import type { RefDocSnapshot } from "@/models/sheaf";
import { Workbench } from "@/models/workbench";
import { FixtureDataPlane } from "@/server/adapters/fixture/data-plane";
import { DEV_USER_EMAIL } from "@/server/adapters/fixture/identity";
import { DOC_XML, XML_QUOTE } from "@/server/adapters/fixture/literature";
import { FixtureMembership } from "@/server/adapters/fixture/membership";
import { FixtureWorkspace } from "@/server/adapters/fixture/workspace";
import { AuthorizedBackend } from "@/server/authorized-backend";
import {
  UnauthenticatedError,
  WorkspaceDamagedError,
  WorkspacePackNotListedError,
  WorkspacePublishError,
} from "@/server/errors";
import { setUserContext } from "./context";
import type { FetchRouter } from "./fetch-router";
import { createFetchRouter } from "./fetch-router";
import { maskInternal, serveRpc, servingRpc } from "./handler";
import { errors, INTERNAL_MESSAGE, identity } from "./interceptors";
import { READ_MAX_BYTES } from "./limits";
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

/** A workspace repository whose every read of the ref document fails with `failure`. */
class UnreadableWorkspace extends FixtureWorkspace {
  constructor(private readonly failure: Error) {
    super();
  }
  override async readRefDoc(): Promise<RefDocSnapshot> {
    throw this.failure;
  }
}

/** A router serving the Workbench over the fixture backend, but with a workspace repository whose
 *  ref document never reads: the Poll's failure paths the fixture backend cannot produce. */
function pollingPast(failure: Error): FetchRouter {
  const backend = new AuthorizedBackend(
    new FixtureDataPlane(new FixtureWorkspace()),
    new UnreadableWorkspace(failure),
    new FixtureMembership(),
    DEV_USER_EMAIL,
    POLL_TIP_BUDGET_MS,
  );
  return createFetchRouter({
    grpc: false,
    grpcWeb: false,
    interceptors: [
      errors(),
      (next) => async (req) => {
        setUserContext(req.contextValues, {
          userEmail: DEV_USER_EMAIL,
          backend,
        });
        return next(req);
      },
    ],
    routes: (router) => router.service(Workbench, workbenchService),
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

  test("an analysis round-trips create → poll", async () => {
    const created = await call("CreateAnalysis", {
      inputs: {
        variantClassification: {
          transcript: "NM_001382309.1",
          hgvsC: "c.332del",
          clinicalContext: "de novo, developmental delay",
        },
      },
      projectId: "proj_fixture",
    });
    expect(created.status).toBe(200);
    const id = created.body?.id as string;
    expect(id).toBeTruthy();

    expect((await call("Poll", { analysisId: id })).status).toBe(200);
  });

  test("a curator's turn joins the run it was sent to", async () => {
    const created = await call("CreateAnalysis", {
      inputs: { freeForm: { prompt: "classify the variant" } },
      projectId: "proj_fixture",
    });
    const id = created.body?.id as string;
    await call("Poll", { analysisId: id });

    const steer = await call("Steer", {
      analysisId: id,
      text: "Treat the exon as clinically relevant.",
    });
    expect(steer.status).toBe(200);

    const { body } = await call("Poll", { analysisId: id });
    const events = body?.events as { user?: { text: string } }[];
    expect(
      events.some(
        (event) =>
          event.user?.text === "Treat the exon as clinically relevant.",
      ),
    ).toBe(true);
  });

  test("a turn sent mid-step is refused typed; the interrupt clears the way", async () => {
    const created = await call("CreateAnalysis", {
      inputs: { freeForm: { prompt: "classify the variant" } },
      projectId: "proj_fixture",
    });
    const id = created.body?.id as string;
    // Seven ticks: the seventh reveals the edit call still awaiting its result.
    for (let i = 0; i < 7; i += 1) {
      await call("Poll", { analysisId: id });
    }

    // Refused as the state it is — actionable, never the masked internal error. The
    // Connect code is the discriminator the composer keys on; the protocol carries
    // failed_precondition over a plain 400.
    const refused = await call("Steer", { analysisId: id, text: "Most" });
    expect(refused.status).toBe(400);
    expect(refused.body?.code).toBe("failed_precondition");

    expect((await call("Interrupt", { analysisId: id })).status).toBe(200);
    const steer = await call("Steer", { analysisId: id, text: "Most" });
    expect(steer.status).toBe(200);

    const { body } = await call("Poll", { analysisId: id });
    const events = body?.events as {
      tool?: { result?: { isError?: boolean } };
      user?: { text?: string };
    }[];
    // The halted call closed with an error result, and the turn joined the run.
    expect(events.some((e) => e.tool?.result?.isError === true)).toBe(true);
    expect(events.some((e) => e.user?.text === "Most")).toBe(true);
  });

  test.each([
    ["a blank turn", { analysisId: "an_1", text: "   \n " }],
    ["a turn past its bound", { analysisId: "an_1", text: "x".repeat(10_001) }],
    ["a turn naming no analysis", { analysisId: "", text: "Most" }],
  ])("%s is invalid_argument, not a masked 500", async (_name, message) => {
    // Validation sits outside the error mask, so a caller's own malformed message comes
    // back describing itself rather than as a generic internal error.
    const { status, body } = await call("Steer", message);
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
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

// The fixture's first seed is a finished run in the dev user's Project, so its agent left a
// repository; a freshly created Analysis has published nothing.
const SEEDED = "an_1";
const MAIN = "refs/heads/main";
const REFLOG = "refs/sheaf/reflog";

interface RefDocJson {
  document?: {
    refs: Record<string, { oid?: string }>;
    packs: string[];
  };
  generation?: string;
}

const sha256 = (bytes: Uint8Array) =>
  createHash("sha256").update(bytes).digest("hex");

/** A PublishWorkspace message moving `main` on from what `snapshot` holds, as a browser sends it. */
function publishOn(
  analysisId: string,
  snapshot: RefDocJson,
  next: string,
  pack: Uint8Array,
): unknown {
  const holds = (ref: string) => snapshot.document?.refs[ref]?.oid;
  return {
    analysisId,
    intent: {
      baseGeneration: snapshot.generation ?? "0",
      refUpdates: {
        [MAIN]: { old: holds(MAIN), new: next },
        [REFLOG]: { old: holds(REFLOG), new: "f".repeat(40) },
      },
      packs: [{ size: String(pack.length), packId: sha256(pack) }],
    },
    packBytes: [Buffer.from(pack).toString("base64")],
  };
}

async function created(): Promise<string> {
  const { body } = await call("CreateAnalysis", {
    inputs: { freeForm: { prompt: "classify the variant" } },
    projectId: "proj_fixture",
  });
  return body?.id as string;
}

describe("the workspace repository surface", () => {
  test("the poll carries the commit the ref document names as the tip", async () => {
    const read = await call("ReadWorkspaceRefDoc", { analysisId: SEEDED });
    expect(read.status).toBe(200);
    const tip = (read.body as RefDocJson).document?.refs[MAIN]?.oid;
    expect(tip).toMatch(/^[0-9a-f]{40}$/);
    const poll = await call("Poll", { analysisId: SEEDED });
    expect(poll.body?.workspaceTip).toEqual({ commit: tip });
  });

  test("a repository nothing was published to has no document, and the poll no commit", async () => {
    const id = await created();
    const read = await call("ReadWorkspaceRefDoc", { analysisId: id });
    expect(read.status).toBe(200);
    expect(read.body?.document).toBeUndefined();
    const poll = await call("Poll", { analysisId: id });
    expect(poll.status).toBe(200);
    expect(poll.body?.workspaceTip).toEqual({ noCommit: {} });
  });

  test.each([
    [
      "an unreachable service",
      new Error("sheaf unavailable"),
      { unavailable: {} },
    ],
    [
      "a damaged ref document",
      new WorkspaceDamagedError("refs.pb: truncated"),
      { damaged: {} },
    ],
  ] as const)(
    "the poll over %s answers its tip as %o, with the events",
    async (_name, failure, tip) => {
      const logged: unknown[] = [];
      const wasError = console.error;
      console.error = (...args: unknown[]) => {
        logged.push(args);
      };
      let poll: RpcResult;
      try {
        poll = await send(pollingPast(failure), "Poll", { analysisId: SEEDED });
      } finally {
        console.error = wasError;
      }
      expect(poll.status).toBe(200);
      expect(poll.body?.workspaceTip).toEqual(tip);
      // Non-empty rules out a vacuous pass: the seeded Analysis has a conversation.
      expect((poll.body?.events as unknown[]).length).toBeGreaterThan(0);
      expect(logged).toHaveLength(1);
    },
  );

  test("a signed pack URL serves the bytes its id hashes", async () => {
    const read = await call("ReadWorkspaceRefDoc", { analysisId: SEEDED });
    const packIds = (read.body as RefDocJson).document?.packs ?? [];
    expect(packIds.length).toBeGreaterThan(0);
    const signed = await call("SignWorkspacePackUrls", {
      analysisId: SEEDED,
      packIds,
    });
    expect(signed.status).toBe(200);
    const packs = signed.body?.packs as {
      packId: string;
      url: string;
      size: string;
    }[];
    for (const pack of packs) {
      const response = await getPack(
        new Request(`http://localhost${pack.url}`),
        {
          params: Promise.resolve({ analysisId: SEEDED, packId: pack.packId }),
        },
      );
      expect(response.status).toBe(200);
      const bytes = new Uint8Array(await response.arrayBuffer());
      expect(sha256(bytes)).toBe(pack.packId);
      expect(String(bytes.length)).toBe(pack.size);
    }
  });

  test("a pack the document no longer lists is failed_precondition, not not_found", async () => {
    // NOT_FOUND on this surface means an Analysis outside the caller's membership.
    const { status, body } = await call("SignWorkspacePackUrls", {
      analysisId: SEEDED,
      packIds: ["a".repeat(64)],
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("failed_precondition");
  });

  test("a curator's publish lands, and a publish built on the tip it replaced is failed_precondition", async () => {
    const id = await created();
    const empty = (await call("ReadWorkspaceRefDoc", { analysisId: id }))
      .body as RefDocJson;
    const pack = new Uint8Array([80, 65, 67, 75, 0, 1]);
    const landed = await call(
      "PublishWorkspace",
      publishOn(id, empty, "9".repeat(40), pack),
    );
    expect(landed.status).toBe(200);
    expect((await call("Poll", { analysisId: id })).body?.workspaceTip).toEqual(
      { commit: "9".repeat(40) },
    );
    const stale = await call(
      "PublishWorkspace",
      publishOn(id, empty, "8".repeat(40), pack),
    );
    expect(stale.status).toBe(400);
    expect(stale.body?.code).toBe("failed_precondition");
    // Never read as the agent being busy: the code is the same, the method is not.
  });

  test.each([
    "ReadWorkspaceRefDoc",
    "SignWorkspacePackUrls",
    "PublishWorkspace",
  ])(
    "%s on an analysis the caller cannot reach is not-found",
    async (method) => {
      const message = {
        ReadWorkspaceRefDoc: { analysisId: "an_never_existed" },
        SignWorkspacePackUrls: {
          analysisId: "an_never_existed",
          packIds: ["a".repeat(64)],
        },
        PublishWorkspace: publishOn(
          "an_never_existed",
          {},
          "9".repeat(40),
          new Uint8Array([1]),
        ),
      }[method];
      const { status, body } = await call(method, message);
      expect(status).toBe(404);
      expect(body?.code).toBe("not_found");
    },
  );

  test("a publish over the body cap is resource_exhausted, before any handler", async () => {
    // Base64 inflates the pack by a third, so this pack's message is over the cap.
    const pack = new Uint8Array(Math.ceil((READ_MAX_BYTES * 3) / 4) + 1);
    const { body } = await call(
      "PublishWorkspace",
      publishOn(SEEDED, {}, "9".repeat(40), pack),
    );
    expect(body?.code).toBe("resource_exhausted");
  });
});

describe("the request boundary", () => {
  test("a scenario missing a field is rejected by its protovalidate rule", async () => {
    const { status, body } = await call("CreateAnalysis", {
      inputs: {
        variantClassification: {
          transcript: "NM_001382309.1",
          hgvsC: "c.332del",
          clinicalContext: "   ",
        },
      },
      projectId: "proj_fixture",
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
    // Validation is the one layer outside the mask, so the rejection reaches the caller as
    // raised — the violations name which field of their own message failed which rule.
    expect((body?.details as unknown[]).length).toBeGreaterThan(0);
  });

  test("inputs naming no scenario are rejected, not stored as an unreadable Analysis", async () => {
    // The oneof's `required` rule is what stops an Analysis existing that no surface can name.
    const { status, body } = await call("CreateAnalysis", {
      projectId: "proj_fixture",
      inputs: {},
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a create with no inputs at all is rejected", async () => {
    // `service.ts` guards this too; the guard is a fault path, and this is the rule that makes it
    // unreachable. Without the field rule the guard would surface as a masked 500, not a 400.
    const { status, body } = await call("CreateAnalysis", {
      projectId: "proj_fixture",
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a prose field past its bound is rejected", async () => {
    // The inputs are stored inline in the analyses row and rendered into the agent's opening
    // instruction, so the bound is what keeps both finite.
    const { status, body } = await call("CreateAnalysis", {
      projectId: "proj_fixture",
      inputs: {
        variantClassification: {
          transcript: "NM_001382309.1",
          hgvsC: "c.332del",
          clinicalContext: "x".repeat(10_001),
        },
      },
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a field the schema does not declare is rejected", async () => {
    // connect-es ignores unknown JSON fields by default; this request would then reach the
    // method, which answers an unknown analysis as not-found.
    const { status, body } = await call("GetThread", {
      analysisId: "an_1",
      threadId: "sthr_1",
      bogus: 1,
    });
    expect(status).toBe(400);
    expect(body?.code).toBe("invalid_argument");
  });

  test("a value below its declared bound is rejected", async () => {
    const { status, body } = await call("GetThread", {
      analysisId: "an_1",
      threadId: "",
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

  test("a turn sent to an analysis the caller cannot reach is not-found, and never says which", async () => {
    // A write into someone else's session must refuse on the same terms a read does:
    // learning that an analysis exists is the thing the refusal hides.
    const absent = await call("Steer", {
      analysisId: "an_never_existed",
      text: "Most",
    });
    expect(absent.status).toBe(404);
    expect(absent.body?.code).toBe("not_found");
    const foreign = await call("Steer", {
      analysisId: "an_someone_elses",
      text: "Most",
    });
    expect(foreign.status).toBe(absent.status);
    expect(foreign.body).toEqual(absent.body);
  });

  test("an interrupt on an analysis the caller cannot reach is not-found, and never says which", async () => {
    const absent = await call("Interrupt", { analysisId: "an_never_existed" });
    expect(absent.status).toBe(404);
    expect(absent.body?.code).toBe("not_found");
    const foreign = await call("Interrupt", {
      analysisId: "an_someone_elses",
    });
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

  test("a call whose request went is cancelled, and not logged as a fault", async () => {
    // A browser leaving mid-Poll is ordinary; the sheaf call it cancels fails as Canceled.
    let entered: () => void = () => {};
    const inside = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const router = createFetchRouter({
      grpc: false,
      grpcWeb: false,
      interceptors: [errors()],
      routes: (route) => {
        route.service(Workbench, {
          async listProjects(_request, ctx) {
            entered();
            await new Promise((resolve) =>
              ctx.signal.addEventListener("abort", resolve),
            );
            throw new ConnectError("sheaf call cancelled", Code.Canceled);
          },
        });
      },
    });
    const logged: unknown[] = [];
    const wasError = console.error;
    console.error = (...args: unknown[]) => {
      logged.push(args);
    };
    const browser = new AbortController();
    const path = `${SERVICE}/ListProjects`;
    try {
      const answered = router(
        new Request(`http://localhost/api/rpc${path}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
          signal: browser.signal,
        }),
        path,
      );
      await inside;
      browser.abort();
      const response = await answered;
      const body = (await response.json()) as { code?: string };
      expect(body.code).toBe("canceled");
    } finally {
      console.error = wasError;
    }
    expect(logged).toEqual([]);
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

  test.each([
    [
      "a damaged workspace",
      new WorkspaceDamagedError("refs.pb: truncated"),
      500,
      "data_loss",
    ],
    [
      "a stale pack list",
      new WorkspacePackNotListedError("not listed"),
      400,
      "failed_precondition",
    ],
    [
      "a lost race",
      new WorkspacePublishError("raceLost", "generation 9904"),
      409,
      "aborted",
    ],
    [
      "a moved branch",
      new WorkspacePublishError("branchMoved", "refs/heads/main moved"),
      400,
      "failed_precondition",
    ],
    [
      "a publish over a ceiling",
      new WorkspacePublishError("overCeiling", "313 MiB"),
      429,
      "resource_exhausted",
    ],
    [
      "a malformed publish",
      new WorkspacePublishError("malformed", "pack 0 hashes to 3be0…"),
      400,
      "invalid_argument",
    ],
  ] as const)(
    "%s reaches the browser as the sheaf service's own code",
    async (_name, error, wantStatus, wantCode) => {
      const { status, body } = await send(raising(error), "ListProjects", {});
      expect(status).toBe(wantStatus);
      expect(body?.code).toBe(wantCode);
    },
  );

  test("behind the boundary, a damaged workspace still reaches the caller as data_loss", async () => {
    // Connect answers data_loss with a 500, the status the boundary masks.
    const { status, body, headers } = await send(
      servingRpc(raising(new WorkspaceDamagedError("refs.pb: truncated"))),
      "ListProjects",
      {},
    );
    expect(status).toBe(500);
    expect(body?.code).toBe("data_loss");
    expect(JSON.stringify(body)).not.toContain("refs.pb");
    expect(headers.get("cache-control")).toBe("private, no-store");
  });

  test.each([
    ["an unrecognized failure", new Error("dsn=hunter2")],
    [
      "a service's own data_loss",
      new ConnectError("sheaf detail", Code.DataLoss),
    ],
  ])("behind the boundary, %s is still masked", async (_name, error) => {
    const { status, body } = await send(
      servingRpc(raising(error)),
      "ListProjects",
      {},
    );
    expect(status).toBe(500);
    expect(body).toEqual({ code: "internal", message: INTERNAL_MESSAGE });
  });

  test("a malformed publish keeps the service's reason; the others a fixed phrase", async () => {
    // The reason describes the caller's own intent and pack; the rest carry nothing they act on.
    const malformed = await send(
      raising(new WorkspacePublishError("malformed", "pack 0 hashes to 3be0")),
      "ListProjects",
      {},
    );
    expect(malformed.body?.message).toBe("pack 0 hashes to 3be0");
    const damaged = await send(
      raising(new WorkspaceDamagedError("refs.pb: truncated at 4 bytes")),
      "ListProjects",
      {},
    );
    expect(JSON.stringify(damaged.body)).not.toContain("refs.pb");
  });

  test.each([
    Code.DataLoss,
    Code.Aborted,
    Code.FailedPrecondition,
    Code.Unavailable,
  ])("an upstream code %s the adapter did not type is masked", async (code) => {
    // Only the adapter's typed errors pass; the sheaf service's own ConnectError never does.
    const { status, body } = await send(
      raising(new ConnectError("sheaf detail", code)),
      "ListProjects",
      {},
    );
    expect(status).toBe(500);
    expect(body).toEqual({ code: "internal", message: INTERNAL_MESSAGE });
  });

  test.each([
    ["ResourceNotFoundError", 404, "not_found"],
    ["UnauthenticatedError", 401, "unauthenticated"],
    ["SessionBusyError", 400, "failed_precondition"],
    ["ClientInputError", 400, "invalid_argument"],
    ["WorkspaceDamagedError", 500, "data_loss"],
    ["WorkspacePackNotListedError", 400, "failed_precondition"],
  ] as const)(
    "a %s minted by another module graph still maps by name",
    async (name, wantStatus, wantCode) => {
      // The backend is memoized on `globalThis` and its instances cross Next's
      // page/route module graphs, so a thrown error can reach the interceptor
      // carrying the right name on a foreign class object — `instanceof` is false
      // there, and the mapping must not fall back to the internal mask.
      const foreign = Object.assign(new Error("thrown across the graph seam"), {
        name,
      });
      const { status, body } = await send(raising(foreign), "ListProjects", {});
      expect(status).toBe(wantStatus);
      expect(body?.code).toBe(wantCode);
    },
  );

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
