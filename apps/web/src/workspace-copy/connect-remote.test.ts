import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import path from "node:path";
import { create } from "@bufbuild/protobuf";
import { type Client, Code, ConnectError } from "@connectrpc/connect";
import { PublishIntentSchema } from "@/models/sheaf";
import type { Workbench } from "@/models/workbench";
import { connectRemote, DEADLINES, publishFailure } from "./connect-remote";
import { COLLABORATIVE_BRANCH } from "./copy";
import { FixtureWorkspace, fixtureRemote } from "./fixture-remote.test-support";
import { directoryStorage, scratchDir } from "./git.test-support";
import { HydrationError } from "./hydrate";
import { memoryLocks } from "./locks.test-support";
import {
  DownloadError,
  PublishFaultError,
  PublishRefusedError,
  WorkspaceDamagedError,
} from "./remote";
import type { CopyRecord } from "./residency";
import { CopyService } from "./service";

// The relay passes four refusals through as the sheaf service's own codes; the browser has to tell
// them from a fault, after which the identical publish may be sent again, and from anything else.

describe("a failed publish", () => {
  test.each([
    [Code.Aborted, "raceLost"],
    [Code.FailedPrecondition, "branchMoved"],
    [Code.ResourceExhausted, "overCeiling"],
    [Code.InvalidArgument, "malformed"],
  ] as const)("code %s is the refusal %s", (code, refusal) => {
    const error = publishFailure(new ConnectError("refused", code));
    expect(error).toBeInstanceOf(PublishRefusedError);
    expect((error as PublishRefusedError).refusal).toBe(refusal);
  });

  test.each([
    Code.Unknown,
    Code.Internal,
    Code.Unavailable,
    Code.DeadlineExceeded,
  ])("code %s leaves the outcome unknown", (code) => {
    expect(publishFailure(new ConnectError("lost", code))).toBeInstanceOf(
      PublishFaultError,
    );
  });

  test("a network failure leaves the outcome unknown", () => {
    expect(publishFailure(new TypeError("Failed to fetch"))).toBeInstanceOf(
      PublishFaultError,
    );
  });

  test.each([Code.NotFound, Code.Unauthenticated, Code.PermissionDenied])(
    "code %s is neither",
    (code) => {
      const error = publishFailure(new ConnectError("no", code));
      expect(error).not.toBeInstanceOf(PublishRefusedError);
      expect(error).not.toBeInstanceOf(PublishFaultError);
      expect(error).not.toBeInstanceOf(WorkspaceDamagedError);
    },
  );
});

describe("a DATA_LOSS answer", () => {
  // Every relayed call answers the repository's damage the same way.
  const damaged = async (): Promise<never> => {
    throw new ConnectError("the stored document does not parse", Code.DataLoss);
  };
  const remote = connectRemote(
    {
      readWorkspaceRefDoc: damaged,
      signWorkspacePackUrls: damaged,
      publishWorkspace: damaged,
    } as unknown as Client<typeof Workbench>,
    "an_7f3c",
    "http://localhost",
    DEADLINES,
  );

  test.each([
    ["reading the document", () => remote.readRefDoc()],
    ["signing", () => remote.signPackUrls(["e".repeat(64)])],
    [
      "publishing",
      () => remote.publish(create(PublishIntentSchema, {}), new Uint8Array()),
    ],
  ] as const)("to %s is a damaged repository", async (_call, call) => {
    await expect(call()).rejects.toBeInstanceOf(WorkspaceDamagedError);
  });

  test("to a publish is neither a refusal nor a fault", () => {
    const error = publishFailure(new ConnectError("damaged", Code.DataLoss));
    expect(error).toBeInstanceOf(WorkspaceDamagedError);
    expect(error).not.toBeInstanceOf(PublishRefusedError);
    expect(error).not.toBeInstanceOf(PublishFaultError);
  });
});

/** Deadlines a test can wait out. */
const QUICK = {
  rpcMs: { readRefDoc: 1_000, signPackUrls: 1_000, publish: 1_000 },
  stallMs: 50,
};

/** A fetch whose body delivers `head` and then nothing, until the request is aborted. */
function stalling(head: Uint8Array): typeof fetch {
  return (async (_url: unknown, init?: RequestInit) =>
    new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(head);
          init?.signal?.addEventListener("abort", () =>
            controller.error(new DOMException("aborted", "AbortError")),
          );
        },
      }),
    )) as unknown as typeof fetch;
}

describe("a pack download", () => {
  const client = {} as Parameters<typeof connectRemote>[0];
  const realFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  function answering(response: () => Response): void {
    globalThis.fetch = (async () => response()) as unknown as typeof fetch;
  }

  test("reset partway through its body is a failed download, which is signed again", async () => {
    answering(
      () =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new Uint8Array([80, 65, 67, 75]));
              controller.error(new TypeError("network error"));
            },
          }),
        ),
    );
    const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
    await expect(remote.download("/pack", 4)).rejects.toBeInstanceOf(
      DownloadError,
    );
  });

  test("answered with an error status is a failed download", async () => {
    answering(() => new Response("gone", { status: 403 }));
    const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
    await expect(remote.download("/pack", 4)).rejects.toBeInstanceOf(
      DownloadError,
    );
  });

  test("that never connects is a failed download", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
    await expect(remote.download("/pack", 4)).rejects.toBeInstanceOf(
      DownloadError,
    );
  });

  test("delivered whole is its bytes", async () => {
    answering(() => new Response(new Uint8Array([1, 2, 3])));
    const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
    expect(await remote.download("/pack", 3)).toEqual(
      new Uint8Array([1, 2, 3]),
    );
  });

  test.each([
    ["longer", 2],
    ["shorter", 5],
  ])(
    "whose body is %s than its signing declared is a failed download",
    async (_case, size) => {
      answering(() => new Response(new Uint8Array([1, 2, 3])));
      const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
      await expect(remote.download("/pack", size)).rejects.toThrow(/declared/);
    },
  );

  test("that stalls partway is a failed download once the watchdog fires", async () => {
    globalThis.fetch = stalling(new Uint8Array([80, 65, 67, 75]));
    const remote = connectRemote(client, "an_1", "http://localhost", QUICK);
    const started = Date.now();
    await expect(remote.download("/pack", 8)).rejects.toThrow(/stalled/);
    expect(Date.now() - started).toBeLessThan(2_000);
  });
});

describe("a hydration whose download stalls", () => {
  const realFetch = globalThis.fetch;
  let scratch: ReturnType<typeof scratchDir>;

  beforeEach(() => {
    scratch = scratchDir("stall");
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
    scratch.remove();
  });

  test("fails and releases the copy's lock, so the next sync runs", async () => {
    const store = new FixtureWorkspace();
    store.seedAgentHistory("an_1", ["# v1\n"], new Date(1_727_163_000_000));
    const fixture = fixtureRemote(store, "an_1");
    const client = {
      readWorkspaceRefDoc: () => fixture.readRefDoc(),
      signWorkspacePackUrls: async ({ packIds }: { packIds: string[] }) => ({
        packs: await fixture.signPackUrls(packIds),
      }),
    } as unknown as Parameters<typeof connectRemote>[0];
    const records = new Map<string, CopyRecord>();
    const copies = new CopyService({
      checkEdit: () => {},
      storage: {
        open: async (id) => directoryStorage(path.join(scratch.dir, id)),
        remove: async () => {},
        list: async () => [],
      },
      remote: (id) => connectRemote(client, id, "http://localhost", QUICK),
      ledger: {
        list: async () => [...records.values()],
        get: async (id) => records.get(id),
        put: async (record) => {
          records.set(record.analysisId, record);
        },
        remove: async (id) => {
          records.delete(id);
        },
      },
      locks: memoryLocks(),
    });
    const snapshot = await fixture.readRefDoc();
    const target = snapshot.document?.refs[COLLABORATIVE_BRANCH]?.target;
    if (target?.case !== "oid") throw new Error("no tip");

    globalThis.fetch = stalling(new Uint8Array([80]));
    await expect(copies.sync("an_1", target.value)).rejects.toThrow(
      HydrationError,
    );

    globalThis.fetch = (async (url: URL | string) =>
      fixture.store.servePack(
        fixture.analysis,
        String(url).split("/").at(-1) ?? "",
      )) as unknown as typeof fetch;
    await copies.sync("an_1", target.value);
    expect(await copies.readDocument("an_1", target.value)).toBe("# v1\n");
  });
});
