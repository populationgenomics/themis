import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import type { Interceptor } from "@connectrpc/connect";
import { Code, createClient } from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";
import { createValidateInterceptor } from "@connectrpc/validate";
import {
  POLL_TIP_BUDGET_MS,
  SHEAF_DEADLINES_MS,
} from "@/lib/workspace-deadlines";
import { Sheaf } from "@/models/sheaf";
import { Workbench } from "@/models/workbench";
import { FixtureDataPlane } from "@/server/adapters/fixture/data-plane";
import { DEV_USER_EMAIL } from "@/server/adapters/fixture/identity";
import { FixtureMembership } from "@/server/adapters/fixture/membership";
import { FixtureWorkspace } from "@/server/adapters/fixture/workspace";
import {
  type EarlyAnswer,
  earlyAnsweringServer,
} from "@/server/adapters/live/sheaf.test-support";
import {
  LiveWorkspace,
  PUBLISH_CHUNK_BYTES,
} from "@/server/adapters/live/workspace";
import { AuthorizedBackend } from "@/server/authorized-backend";
import { setUserContext } from "./context";
import { createFetchRouter } from "./fetch-router";
import { errors } from "./interceptors";
import { READ_MAX_BYTES } from "./limits";
import { workbenchService } from "./service";

// A browser's PublishWorkspace, relayed by the BFF into a sheaf service that answers before the last
// chunk has been sent — as the service does for a publish that already landed, one that lost its
// race, and one refused on its intent alone. The browser must see that answer, not a transport
// error: the path is the real one from the Connect router through the live adapter's gRPC client.

const closers: (() => Promise<void>)[] = [];
afterEach(async () => {
  for (const close of closers.splice(0)) await close();
});

async function relayingTo(answer: EarlyAnswer) {
  const server = await earlyAnsweringServer(answer);
  closers.push(server.close);
  const workspace = new LiveWorkspace(
    createClient(Sheaf, createGrpcTransport({ baseUrl: server.url })),
    { deriveBearer: async (sessionId) => `bearer-for-${sessionId}` },
    SHEAF_DEADLINES_MS,
  );
  const backend = new AuthorizedBackend(
    new FixtureDataPlane(new FixtureWorkspace()),
    workspace,
    new FixtureMembership(),
    DEV_USER_EMAIL,
    POLL_TIP_BUDGET_MS,
  );
  const identity: Interceptor = (next) => async (req) => {
    setUserContext(req.contextValues, { userEmail: DEV_USER_EMAIL, backend });
    return next(req);
  };
  const router = createFetchRouter({
    grpc: false,
    grpcWeb: false,
    readMaxBytes: READ_MAX_BYTES,
    interceptors: [createValidateInterceptor(), errors(), identity],
    routes: (route) => route.service(Workbench, workbenchService),
  });
  return { router, server };
}

// Five chunks, the first alone past HTTP/2's initial window: the relay is still writing when the
// answer lands, and has chunks left it must stop drawing.
const PACK = new Uint8Array(5 * PUBLISH_CHUNK_BYTES).map((_, i) => i % 253);

async function publish(
  router: Awaited<ReturnType<typeof relayingTo>>["router"],
) {
  const path = "/themis.workbench.rpc.Workbench/PublishWorkspace";
  const response = await router(
    new Request(`http://localhost/api/rpc${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        // The fixture's first seed: an Analysis in the dev user's Project.
        analysisId: "an_1",
        intent: {
          baseGeneration: "1727163041502118",
          refUpdates: {
            "refs/heads/main": { old: "c".repeat(40), new: "9".repeat(40) },
            "refs/sheaf/reflog": { old: "0b".repeat(20), new: "71".repeat(20) },
          },
          packs: [
            {
              size: String(PACK.length),
              packId: createHash("sha256").update(PACK).digest("hex"),
            },
          ],
        },
        packBytes: [Buffer.from(PACK).toString("base64")],
      }),
    }),
    path,
  );
  return {
    status: response.status,
    body: (await response.json()) as Record<string, unknown>,
  };
}

describe("a publish the sheaf service answers early", () => {
  test.each([
    [Code.Aborted, "aborted"],
    [Code.FailedPrecondition, "failed_precondition"],
    [Code.ResourceExhausted, "resource_exhausted"],
    [Code.InvalidArgument, "invalid_argument"],
  ] as const)("reaches the browser as its own code: %s", async (code, wire) => {
    const { router, server } = await relayingTo({
      after: "firstMessage",
      code,
      message: "answered early",
    });
    const { body } = await publish(router);
    expect(body.code).toBe(wire);
    expect(server.received()).toBeLessThan(PACK.length);
  });

  test("an early success, a publish that already landed, reaches the browser as one", async () => {
    const { router, server } = await relayingTo({
      after: "firstMessage",
      code: 0,
      generation: BigInt(9904),
    });
    const { status, body } = await publish(router);
    expect(status).toBe(200);
    expect(body.generation).toBe("9904");
    expect(server.received()).toBeLessThan(PACK.length);
  });

  test("an early fault the contract does not pass through is still masked", async () => {
    const { router } = await relayingTo({
      after: "firstMessage",
      code: Code.Unavailable,
      message: "storage fault detail",
    });
    const { status, body } = await publish(router);
    expect(status).toBe(500);
    expect(body.code).toBe("internal");
    expect(JSON.stringify(body)).not.toContain("storage fault detail");
  });
});
