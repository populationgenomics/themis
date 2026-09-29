import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { devNull, tmpdir } from "node:os";
import path from "node:path";
import { create } from "@bufbuild/protobuf";
import {
  type PublishIntent,
  PublishIntentSchema,
  type RefDocSnapshot,
} from "@/models/sheaf";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import {
  isResourceNotFoundError,
  isWorkspacePackNotListedError,
  isWorkspacePublishError,
  type WorkspacePublishFailure,
} from "../../errors";
import { branchTip, COLLABORATIVE_BRANCH } from "../../workspace";
import { FIXTURE_LIMITS, FixtureWorkspace, packPath } from "./workspace";
import {
  agentHistory,
  REFLOG_REF,
  type SeedPublish,
  WORKING_DOCUMENT_PATH,
} from "./workspace-seed";

// The offline repository has to be one a real reader accepts — its packs index with git and its
// history reads back — and its publish has to answer each outcome the Workbench contract names on
// the terms the sheaf service does, since the browser's retry logic is exercised against it.

const DOCUMENTS = ["# Draft\n", "# Draft\n\nPS3 applies.\n"];
const SEEDED = create(AnalysisSchema, { id: "an_1", sessionId: "sess_1" });
const EMPTY = create(AnalysisSchema, { id: "an_2", sessionId: "sess_2" });

/** A request that never goes away: these calls are not about cancellation. */
const NEVER = new AbortController().signal;

function seeded(): FixtureWorkspace {
  const workspace = new FixtureWorkspace();
  workspace.seedAgentHistory(SEEDED.id, DOCUMENTS, new Date(1_700_000_000_000));
  return workspace;
}

const oid = (c: string) => c.repeat(40);
const sha256 = (bytes: Uint8Array) =>
  createHash("sha256").update(bytes).digest("hex");

function directRef(snapshot: RefDocSnapshot, ref: string): string | undefined {
  const target = snapshot.document?.refs[ref]?.target;
  return target?.case === "oid" ? target.value : undefined;
}

/** A publish moving `main` (and the reflog) from what `snapshot` holds, carrying `pack`. */
function publishOn(
  snapshot: RefDocSnapshot,
  moves: Record<string, string>,
  pack: Uint8Array,
): PublishIntent {
  const refUpdates: Record<string, { old?: string; new: string }> = {};
  for (const [ref, next] of Object.entries({
    ...moves,
    [REFLOG_REF]: oid("f"),
  })) {
    refUpdates[ref] = { old: directRef(snapshot, ref), new: next };
  }
  return create(PublishIntentSchema, {
    baseGeneration: snapshot.generation,
    refUpdates,
    packs: [{ packId: sha256(pack), size: BigInt(pack.length) }],
  });
}

async function failure(
  promise: Promise<unknown>,
): Promise<WorkspacePublishFailure> {
  const error = await promise.then(
    () => {
      throw new Error("expected the publish to fail");
    },
    (e: unknown) => e,
  );
  if (!isWorkspacePublishError(error)) throw error;
  return error.failure;
}

async function packBytes(
  workspace: FixtureWorkspace,
  analysis: Analysis,
  packId: string,
): Promise<Uint8Array> {
  const response = await workspace.servePack(analysis, packId);
  return new Uint8Array(await response.arrayBuffer());
}

describe("a seeded repository", () => {
  test("is one git reads: its packs index, and its history is the documents in turn", async () => {
    const workspace = seeded();
    const snapshot = await workspace.readRefDoc(SEEDED, NEVER);
    const document = snapshot.document;
    if (document === undefined) throw new Error("no repository was seeded");
    // One publish per version, as the agent's pushes each carry one pack.
    expect(document.packs.length).toBe(DOCUMENTS.length);
    const scratch = mkdtempSync(path.join(tmpdir(), "themis-seed-check-"));
    const git = (args: string[]) =>
      execFileSync("git", args, {
        env: {
          ...process.env,
          GIT_DIR: scratch,
          GIT_CONFIG_GLOBAL: devNull,
          GIT_CONFIG_NOSYSTEM: "1",
        },
      })
        .toString("utf8")
        .trim();
    try {
      git(["init", "--bare", "--quiet", scratch]);
      for (const packId of document.packs) {
        const file = path.join(scratch, `${packId}.pack`);
        const bytes = await packBytes(workspace, SEEDED, packId);
        expect(sha256(bytes)).toBe(packId);
        writeFileSync(file, bytes);
        // Refused for a pack that is not self-contained.
        git([
          "index-pack",
          "--strict",
          "-o",
          `${scratch}/objects/pack/pack-${packId}.idx`,
          file,
        ]);
        execFileSync("mv", [
          file,
          `${scratch}/objects/pack/pack-${packId}.pack`,
        ]);
      }
      const tip = branchTip(snapshot);
      const reflog = directRef(snapshot, REFLOG_REF);
      if (tip === undefined || reflog === undefined) {
        throw new Error("the seeded document names no tip or reflog");
      }
      git(["update-ref", COLLABORATIVE_BRANCH, tip]);
      git(["update-ref", REFLOG_REF, reflog]);
      git(["fsck", "--strict", "--no-dangling"]);
      expect(git(["show", `${tip}:${WORKING_DOCUMENT_PATH}`])).toBe(
        DOCUMENTS[DOCUMENTS.length - 1].trim(),
      );
      expect(git(["rev-list", "--count", tip])).toBe(String(DOCUMENTS.length));
      // The chain is sheaf's own to its parentless root, one entry per publish.
      const subjects = git([
        "log",
        "--first-parent",
        "--format=%s",
        reflog,
      ]).split("\n");
      expect(subjects).toEqual([
        ...DOCUMENTS.map(() => `sheaf: ${COLLABORATIVE_BRANCH}`),
        "sheaf: init",
      ]);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });

  test("a history the store refuses part of is never visible, in whole or in part", async () => {
    // Its first publish is accepted and its second refused: the first alone must not be served as
    // what the agent published, on the read that found the refusal or on any after it.
    const [first] = agentHistory(["# Draft\n"], new Date(1_700_000_000_000));
    const refused: SeedPublish = {
      ...first,
      refUpdates: {
        [COLLABORATIVE_BRANCH]: {
          old: first.refUpdates[COLLABORATIVE_BRANCH].new,
          new: oid("9"),
        },
      },
    };
    const workspace = new FixtureWorkspace();
    workspace.seedHistory(SEEDED.id, () => [first, refused]);
    const firstPack = sha256(first.pack);
    for (let read = 0; read < 2; read += 1) {
      await expect(workspace.readRefDoc(SEEDED, NEVER)).rejects.toThrow(
        REFLOG_REF,
      );
      await expect(
        workspace.signPackUrls(SEEDED, [firstPack], NEVER),
      ).rejects.toThrow(REFLOG_REF);
      await expect(workspace.servePack(SEEDED, firstPack)).rejects.toThrow(
        REFLOG_REF,
      );
    }
  });

  test("an Analysis nothing was published for has no repository", async () => {
    const snapshot = await seeded().readRefDoc(EMPTY, NEVER);
    expect(snapshot.document).toBeUndefined();
    expect(snapshot.generation).toBe(BigInt(0));
  });
});

describe("signing pack URLs", () => {
  test("names the BFF's pack route and each pack's size", async () => {
    const workspace = seeded();
    const { document } = await workspace.readRefDoc(SEEDED, NEVER);
    const packIds = document?.packs ?? [];
    const { packs } = await workspace.signPackUrls(SEEDED, packIds, NEVER);
    expect(packs.map((pack) => pack.packId)).toEqual(packIds);
    for (const pack of packs) {
      expect(pack.url).toBe(packPath(SEEDED.id, pack.packId));
      expect(pack.size).toBe(
        BigInt((await packBytes(workspace, SEEDED, pack.packId)).length),
      );
    }
  });

  test("a pack the document does not list is a stale list", async () => {
    const error = await seeded()
      .signPackUrls(SEEDED, ["a".repeat(64)], NEVER)
      .catch((e: unknown) => e);
    expect(isWorkspacePackNotListedError(error)).toBe(true);
  });

  test("the pack route serves only packs the document lists", async () => {
    const error = await seeded()
      .servePack(SEEDED, "a".repeat(64))
      .catch((e: unknown) => e);
    expect(isResourceNotFoundError(error)).toBe(true);
  });
});

describe("a publish", () => {
  const PACK = new Uint8Array([80, 65, 67, 75, 1, 2, 3]);

  test("built on the current document lands, and the branch follows it", async () => {
    const workspace = seeded();
    const before = await workspace.readRefDoc(SEEDED, NEVER);
    const intent = publishOn(
      before,
      { [COLLABORATIVE_BRANCH]: oid("9") },
      PACK,
    );
    const { generation } = await workspace.publish(SEEDED, intent, [PACK]);
    const after = await workspace.readRefDoc(SEEDED, NEVER);
    expect(after.generation).toBe(generation);
    expect(generation).not.toBe(before.generation);
    expect(branchTip(after)).toBe(oid("9"));
    expect(after.document?.packs).toContain(sha256(PACK));
    expect(await packBytes(workspace, SEEDED, sha256(PACK))).toEqual(PACK);
  });

  test("retried after its response was lost, it succeeds with the current generation", async () => {
    const workspace = seeded();
    const before = await workspace.readRefDoc(SEEDED, NEVER);
    const intent = publishOn(
      before,
      { [COLLABORATIVE_BRANCH]: oid("9") },
      PACK,
    );
    const first = await workspace.publish(SEEDED, intent, [PACK]);
    const retry = await workspace.publish(SEEDED, intent, [PACK]);
    expect(retry.generation).toBe(first.generation);
  });

  test("losing to an unrelated publish is ABORTED", async () => {
    const workspace = seeded();
    const base = await workspace.readRefDoc(SEEDED, NEVER);
    const other = new Uint8Array([9, 9]);
    await workspace.publish(
      SEEDED,
      publishOn(base, { "refs/heads/notes": oid("7") }, other),
      [other],
    );
    const stale = publishOn(base, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK);
    expect(await failure(workspace.publish(SEEDED, stale, [PACK]))).toBe(
      "raceLost",
    );
  });

  test("built on a tip the branch has moved past is FAILED_PRECONDITION", async () => {
    const workspace = seeded();
    const base = await workspace.readRefDoc(SEEDED, NEVER);
    const agent = new Uint8Array([7, 7]);
    await workspace.publish(
      SEEDED,
      publishOn(base, { [COLLABORATIVE_BRANCH]: oid("8") }, agent),
      [agent],
    );
    const stale = publishOn(base, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK);
    expect(await failure(workspace.publish(SEEDED, stale, [PACK]))).toBe(
      "branchMoved",
    );
  });

  test("the first publish of a repository asserts it does not exist yet", async () => {
    const workspace = seeded();
    const empty = await workspace.readRefDoc(EMPTY, NEVER);
    const intent = publishOn(empty, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK);
    await workspace.publish(EMPTY, intent, [PACK]);
    const created = await workspace.readRefDoc(EMPTY, NEVER);
    expect(branchTip(created)).toBe(oid("9"));
    expect(created.document?.head?.target).toEqual({
      case: "ref",
      value: COLLABORATIVE_BRANCH,
    });
  });

  test.each<[string, (base: RefDocSnapshot) => [PublishIntent, Uint8Array[]]]>([
    [
      "one that forgets the reflog",
      (base) => {
        const intent = publishOn(
          base,
          { [COLLABORATIVE_BRANCH]: oid("9") },
          PACK,
        );
        delete intent.refUpdates[REFLOG_REF];
        return [intent, [PACK]];
      },
    ],
    [
      "one that deletes a ref",
      (base) => {
        const intent = publishOn(
          base,
          { [COLLABORATIVE_BRANCH]: oid("9") },
          PACK,
        );
        intent.refUpdates[COLLABORATIVE_BRANCH].new = undefined;
        return [intent, [PACK]];
      },
    ],
    [
      "one moving only sheaf's bookkeeping",
      (base) => [publishOn(base, {}, PACK), [PACK]],
    ],
    [
      "one naming a ref git cannot hold",
      (base) => [
        publishOn(base, { "refs/heads/two words": oid("9") }, PACK),
        [PACK],
      ],
    ],
    [
      "one whose old value the document it names does not hold",
      (base) => {
        const intent = publishOn(
          base,
          { [COLLABORATIVE_BRANCH]: oid("9") },
          PACK,
        );
        intent.refUpdates[COLLABORATIVE_BRANCH].old = oid("1");
        return [intent, [PACK]];
      },
    ],
    [
      "one whose bytes do not hash to the declared id",
      (base) => [
        publishOn(base, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK),
        [new Uint8Array([80, 65, 67, 75, 1, 2, 4])],
      ],
    ],
    [
      "one whose bytes are shorter than declared",
      (base) => [
        publishOn(base, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK),
        [PACK.subarray(1)],
      ],
    ],
  ])("%s is malformed and lands nothing", async (_name, build) => {
    const workspace = seeded();
    const base = await workspace.readRefDoc(SEEDED, NEVER);
    const [intent, packs] = build(base);
    expect(await failure(workspace.publish(SEEDED, intent, packs))).toBe(
      "malformed",
    );
    expect((await workspace.readRefDoc(SEEDED, NEVER)).generation).toBe(
      base.generation,
    );
  });

  test("one declaring more bytes than the ceiling is over it", async () => {
    const workspace = seeded();
    const base = await workspace.readRefDoc(SEEDED, NEVER);
    const intent = publishOn(base, { [COLLABORATIVE_BRANCH]: oid("9") }, PACK);
    intent.packs[0].size = BigInt(FIXTURE_LIMITS.maxPublishBytes + 1);
    expect(await failure(workspace.publish(SEEDED, intent, [PACK]))).toBe(
      "overCeiling",
    );
  });
});
