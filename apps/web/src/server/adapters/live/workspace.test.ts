import { afterEach, describe, expect, test } from "bun:test";
import http2 from "node:http2";
import type { AddressInfo } from "node:net";
import { create, fromBinary } from "@bufbuild/protobuf";
import {
  Code,
  ConnectError,
  createClient,
  decodeBinaryHeader,
  type ServiceImpl,
} from "@connectrpc/connect";
import {
  connectNodeAdapter,
  createGrpcTransport,
} from "@connectrpc/connect-node";
import { CallerClaimSchema } from "@/gen/themis/rpc/auth_pb";
import { CallingAs } from "@/gen/themis/rpc/sandbox_options_pb";
import {
  SHEAF_DEADLINES_MS,
  type SheafDeadlines,
} from "@/lib/workspace-deadlines";
import {
  type PublishIntent,
  PublishIntentSchema,
  type PublishRequest,
  Sheaf,
} from "@/models/sheaf";
import { AnalysisSchema } from "@/models/workbench";
import {
  isWorkspaceDamagedError,
  isWorkspacePackNotListedError,
  isWorkspacePublishError,
  type WorkspacePublishFailure,
} from "../../errors";
import { loadSheafConfig } from "./config";
import { earlyAnsweringServer } from "./sheaf.test-support";
import {
  CLAIM_METADATA,
  LiveWorkspace,
  PUBLISH_CHUNK_BYTES,
} from "./workspace";

// The live workspace repository against a gRPC server standing in for the sheaf service: which
// Analysis each call names, which of the service's codes reach the browser as themselves, and what a
// publish's stream carries.

const ANALYSIS = create(AnalysisSchema, {
  id: "an_7f3c",
  sessionId: "sesn_7f3c",
  projectId: "proj_a",
});
const deriver = { deriveBearer: async (id: string) => `bearer-for-${id}` };

const closers: (() => Promise<void>)[] = [];
afterEach(async () => {
  for (const close of closers.splice(0)) await close();
});

async function sheafServer(
  impl: Partial<ServiceImpl<typeof Sheaf>>,
  deadlines: SheafDeadlines = SHEAF_DEADLINES_MS,
): Promise<LiveWorkspace> {
  const server = http2.createServer(
    connectNodeAdapter({
      grpc: true,
      connect: false,
      grpcWeb: false,
      routes: (router) => router.service(Sheaf, impl),
    }),
  );
  const sessions = new Set<http2.ServerHttp2Session>();
  server.on("session", (session) => sessions.add(session));
  await new Promise<void>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve()),
  );
  closers.push(
    () =>
      new Promise((resolve) => {
        for (const session of sessions) session.destroy();
        server.close(() => resolve());
      }),
  );
  const { port } = server.address() as AddressInfo;
  return workspaceAt(`http://127.0.0.1:${port}`, deadlines);
}

function workspaceAt(
  url: string,
  deadlines: SheafDeadlines = SHEAF_DEADLINES_MS,
): LiveWorkspace {
  const client = createClient(Sheaf, createGrpcTransport({ baseUrl: url }));
  return new LiveWorkspace(client, deriver, deadlines);
}

/** A request that never goes away: these calls are not about cancellation. */
const NEVER = new AbortController().signal;

/** A handler that never answers, as a stalled service does. */
const stall = () => new Promise<never>(() => {});

const INTENT: PublishIntent = create(PublishIntentSchema, {
  baseGeneration: BigInt(1727163041502118),
  refUpdates: {
    "refs/heads/main": { old: "c".repeat(40), new: "9".repeat(40) },
  },
});

async function thrown(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error("expected a failure");
    },
    (error: unknown) => error,
  );
}

describe("the call names the Analysis", () => {
  test("as the web tier itself, with the session bearer derived from the row", async () => {
    let claimHeader: string | null = null;
    const workspace = await sheafServer({
      readRefDoc(_request, context) {
        claimHeader = context.requestHeader.get(CLAIM_METADATA);
        return {};
      },
    });
    await workspace.readRefDoc(ANALYSIS, NEVER);
    if (claimHeader === null) throw new Error("no claim on the call");
    const claim = fromBinary(
      CallerClaimSchema,
      decodeBinaryHeader(claimHeader),
    );
    expect(claim.callingAs).toBe(CallingAs.SELF);
    expect(claim.sessionToken).toBe(`bearer-for-${ANALYSIS.sessionId}`);
  });
});

describe("the service's codes", () => {
  test.each(["a read", "signing", "a publish"] as const)(
    "DATA_LOSS on %s is damage the browser is told of",
    async (call) => {
      const damaged = () => {
        throw new ConnectError("refs.pb does not parse", Code.DataLoss);
      };
      const workspace = await sheafServer({
        readRefDoc: damaged,
        signPackUrls: damaged,
        publish: damaged,
      });
      const run = {
        "a read": () => workspace.readRefDoc(ANALYSIS, NEVER),
        signing: () =>
          workspace.signPackUrls(ANALYSIS, ["a".repeat(64)], NEVER),
        "a publish": () => workspace.publish(ANALYSIS, INTENT, []),
      }[call];
      expect(isWorkspaceDamagedError(await thrown(run()))).toBe(true);
    },
  );

  test("NOT_FOUND on signing is a stale pack list", async () => {
    const workspace = await sheafServer({
      signPackUrls() {
        throw new ConnectError("not listed", Code.NotFound);
      },
    });
    expect(
      isWorkspacePackNotListedError(
        await thrown(workspace.signPackUrls(ANALYSIS, ["a".repeat(64)], NEVER)),
      ),
    ).toBe(true);
  });

  test.each([
    [Code.Aborted, "raceLost"],
    [Code.FailedPrecondition, "branchMoved"],
    [Code.ResourceExhausted, "overCeiling"],
    [Code.InvalidArgument, "malformed"],
  ] as const)(
    "%s on a publish is %s",
    async (code, failure: WorkspacePublishFailure) => {
      const workspace = await sheafServer({
        async publish() {
          throw new ConnectError("refused", code);
        },
      });
      const error = await thrown(workspace.publish(ANALYSIS, INTENT, []));
      expect(isWorkspacePublishError(error)).toBe(true);
      expect((error as { failure: string }).failure).toBe(failure);
    },
  );

  test.each([
    ["a read", Code.Unavailable],
    ["a read", Code.PermissionDenied],
    ["signing", Code.InvalidArgument],
    ["a publish", Code.Unavailable],
  ] as const)("%s's %s stays the service's own fault", async (call, code) => {
    // Propagated untyped, so the error interceptor masks it as INTERNAL.
    const fail = () => {
      throw new ConnectError("fault", code);
    };
    const workspace = await sheafServer({
      readRefDoc: fail,
      signPackUrls: fail,
      publish: fail,
    });
    const run = {
      "a read": () => workspace.readRefDoc(ANALYSIS, NEVER),
      signing: () => workspace.signPackUrls(ANALYSIS, ["a".repeat(64)], NEVER),
      "a publish": () => workspace.publish(ANALYSIS, INTENT, []),
    }[call];
    const error = await thrown(run());
    expect(error).toBeInstanceOf(ConnectError);
    expect((error as ConnectError).code).toBe(code);
  });
});

describe("a stalled service", () => {
  const SHORT: SheafDeadlines = {
    readRefDoc: 50,
    signPackUrls: 50,
    publish: 50,
  };

  test.each([
    ["a read", (w: LiveWorkspace) => w.readRefDoc(ANALYSIS, NEVER)],
    [
      "signing",
      (w: LiveWorkspace) => w.signPackUrls(ANALYSIS, ["a".repeat(64)], NEVER),
    ],
    ["a publish", (w: LiveWorkspace) => w.publish(ANALYSIS, INTENT, [])],
  ] as const)("%s gives up at its deadline", async (_name, call) => {
    // A read rides every Poll, so an unbounded one would hold the conversation with it.
    const workspace = await sheafServer(
      { readRefDoc: stall, signPackUrls: stall, publish: stall },
      SHORT,
    );
    const error = await thrown(call(workspace));
    expect(error).toBeInstanceOf(ConnectError);
    expect((error as ConnectError).code).toBe(Code.DeadlineExceeded);
  });

  test.each([
    ["a read", (w: LiveWorkspace, s: AbortSignal) => w.readRefDoc(ANALYSIS, s)],
    [
      "signing",
      (w: LiveWorkspace, s: AbortSignal) =>
        w.signPackUrls(ANALYSIS, ["a".repeat(64)], s),
    ],
  ] as const)(
    "%s in flight stops when the browser's request goes",
    async (_name, call) => {
      let received: () => void = () => {};
      const arrived = new Promise<void>((resolve) => {
        received = resolve;
      });
      const stallOnceReceived = () => {
        received();
        return stall();
      };
      const workspace = await sheafServer({
        readRefDoc: stallOnceReceived,
        signPackUrls: stallOnceReceived,
      });
      const request = new AbortController();
      const pending = thrown(call(workspace, request.signal));
      // The service holds the call before the request goes, so the rpc itself is what is cancelled.
      await arrived;
      request.abort();
      const error = await pending;
      expect(error).toBeInstanceOf(ConnectError);
      expect((error as ConnectError).code).toBe(Code.Canceled);
    },
  );
});

describe("a stalled bearer derivation", () => {
  // KMS answers the derivation, before the rpc is issued; the call's deadline and request cover it.
  const stalledDeriver = { deriveBearer: stall };
  const SHORT: SheafDeadlines = {
    readRefDoc: 50,
    signPackUrls: 50,
    publish: 50,
  };
  const unreachable = (deadlines: SheafDeadlines) =>
    new LiveWorkspace(
      createClient(
        Sheaf,
        createGrpcTransport({ baseUrl: "http://127.0.0.1:9" }),
      ),
      stalledDeriver,
      deadlines,
    );

  test.each([
    ["a read", (w: LiveWorkspace) => w.readRefDoc(ANALYSIS, NEVER)],
    [
      "signing",
      (w: LiveWorkspace) => w.signPackUrls(ANALYSIS, ["a".repeat(64)], NEVER),
    ],
    ["a publish", (w: LiveWorkspace) => w.publish(ANALYSIS, INTENT, [])],
  ] as const)("holds %s no longer than its deadline", async (_name, call) => {
    const error = await thrown(call(unreachable(SHORT)));
    expect(error).toBeInstanceOf(ConnectError);
    expect((error as ConnectError).code).toBe(Code.DeadlineExceeded);
  });

  test("stops when the browser's request goes", async () => {
    const request = new AbortController();
    const pending = thrown(
      unreachable(SHEAF_DEADLINES_MS).readRefDoc(ANALYSIS, request.signal),
    );
    request.abort();
    const error = await pending;
    expect(error).toBeInstanceOf(ConnectError);
    expect((error as ConnectError).code).toBe(Code.Canceled);
  });
});

describe("a publish's stream", () => {
  test("is the intent, then each pack in chunks under the message limit", async () => {
    const pack = new Uint8Array(PUBLISH_CHUNK_BYTES * 2 + 17).map(
      (_, i) => i % 251,
    );
    const seen: PublishRequest[] = [];
    const workspace = await sheafServer({
      async publish(requests) {
        for await (const request of requests) seen.push(request);
        return { generation: BigInt(42) };
      },
    });
    const response = await workspace.publish(ANALYSIS, INTENT, [pack]);
    expect(response.generation).toBe(BigInt(42));
    expect(seen[0]?.message.case).toBe("intent");
    const chunks = seen.slice(1).map((request) => {
      if (request.message.case !== "chunk") throw new Error("not a chunk");
      return request.message.value;
    });
    expect(chunks.every((chunk) => chunk.pack === 0)).toBe(true);
    expect(
      chunks.every((chunk) => chunk.content.length <= PUBLISH_CHUNK_BYTES),
    ).toBe(true);
    expect(Buffer.concat(chunks.map((chunk) => chunk.content))).toEqual(
      Buffer.from(pack),
    );
  });
});

describe("a publish the service answers before its last chunk", () => {
  // Far past HTTP/2's initial 64 KiB flow-control window, so the client is still writing — blocked
  // on a window the server will never open — when the answer arrives.
  const PACK = new Uint8Array(8 * 1024 * 1024);

  test.each([
    ["headers", Code.Aborted, "raceLost"],
    ["firstMessage", Code.Aborted, "raceLost"],
    ["firstMessage", Code.FailedPrecondition, "branchMoved"],
    ["firstMessage", Code.ResourceExhausted, "overCeiling"],
    ["firstMessage", Code.InvalidArgument, "malformed"],
  ] as const)(
    "answered after its %s with %s is %s, not a transport error",
    async (after, code, failure) => {
      const server = await earlyAnsweringServer({
        after,
        code,
        message: "early",
      });
      closers.push(server.close);
      const error = await thrown(
        workspaceAt(server.url).publish(ANALYSIS, INTENT, [PACK]),
      );
      expect(isWorkspacePublishError(error)).toBe(true);
      expect((error as { failure: string }).failure).toBe(failure);
      // Rules out a vacuous pass: the answer did arrive before the pack was sent.
      expect(server.received()).toBeLessThan(PACK.length);
    },
  );

  test("answered after its first message with DATA_LOSS is damage, not a transport error", async () => {
    const server = await earlyAnsweringServer({
      after: "firstMessage",
      code: Code.DataLoss,
      message: "early",
    });
    closers.push(server.close);
    const error = await thrown(
      workspaceAt(server.url).publish(ANALYSIS, INTENT, [PACK]),
    );
    expect(isWorkspaceDamagedError(error)).toBe(true);
    expect(server.received()).toBeLessThan(PACK.length);
  });

  test("answered with success, as a publish that already landed is, returns its generation", async () => {
    const server = await earlyAnsweringServer({
      after: "firstMessage",
      code: 0,
      generation: BigInt(9904),
    });
    closers.push(server.close);
    const response = await workspaceAt(server.url).publish(ANALYSIS, INTENT, [
      PACK,
    ]);
    expect(response.generation).toBe(BigInt(9904));
    expect(server.received()).toBeLessThan(PACK.length);
  });
});

describe("loadSheafConfig", () => {
  test("reads the service URL, and nothing the relay does not use", () => {
    expect(
      loadSheafConfig({ THEMIS_SHEAF_URL: "https://sheaf.example" }),
    ).toEqual({ sheafUrl: "https://sheaf.example" });
  });

  test("fails loud when the URL is unset", () => {
    expect(() => loadSheafConfig({})).toThrow("THEMIS_SHEAF_URL");
  });
});
