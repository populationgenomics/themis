import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { create } from "@bufbuild/protobuf";
import { timestampFromMs } from "@bufbuild/protobuf/wkt";
import { type RefDocSnapshot, SignedPackSchema } from "@/models/sheaf";
import {
  COLLABORATIVE_BRANCH,
  IncompletePackError,
  WorkspaceCopy,
} from "./copy";
import {
  downloadPack,
  FixtureWorkspace,
  fixtureRemote,
} from "./fixture-remote.test-support";
import {
  directoryStorage,
  gitText,
  oracleRepo,
  runGit,
  scratchDir,
} from "./git.test-support";
import { decodeUtf8 } from "./git-objects";
import { documentRefs, HydrationError, hydrate } from "./hydrate";
import { packObjectCount, sha256Hex } from "./pack";
import {
  DownloadError,
  type Remote,
  StalePackListError,
  WorkspaceDamagedError,
} from "./remote";
import { CopyTooLargeError, Residency } from "./residency";

// Hydration against the offline sheaf store, whose repositories the real `git` built.

const ANALYSIS = "an_7f3c";
const AUTHORED = new Date(1_727_163_000_000);
const OPEN = { admit: async () => {} };

let scratch: ReturnType<typeof scratchDir>;
let copyDir: string;
let storage: ReturnType<typeof directoryStorage>;
let copy: WorkspaceCopy;

beforeEach(async () => {
  scratch = scratchDir("hydrate");
  copyDir = path.join(scratch.dir, "copy.git");
  storage = directoryStorage(copyDir);
  copy = await WorkspaceCopy.open(storage);
});

afterEach(() => scratch.remove());

function documents(count: number): string[] {
  return Array.from(
    { length: count },
    (_, i) => `# Working document\n\nVersion ${i + 1}.\n`,
  );
}

function seeded(count: number): ReturnType<typeof fixtureRemote> {
  const store = new FixtureWorkspace();
  store.seedAgentHistory(ANALYSIS, documents(count), AUTHORED);
  return fixtureRemote(store, ANALYSIS);
}

async function documentAt(commit: string): Promise<string | undefined> {
  const file = await copy.readFile(commit, "working_document.md");
  return file === undefined
    ? undefined
    : decodeUtf8(file.bytes, "the document");
}

describe("the document state a copy records", () => {
  test("follows each hydration, and is absent while setting the refs fails part way", async () => {
    const remote = seeded(2);
    const state = await hydrate(copy, remote, OPEN);
    expect(await copy.documentState()).toEqual(state);
    const refs = new Map(state.refs);
    refs.set("refs/heads/second", state.refs.get(COLLABORATIVE_BRANCH) ?? "");
    const writeFile = storage.promises.writeFile;
    storage.promises.writeFile = async (file, data, options) => {
      if (String(file).endsWith("/refs/heads/second")) {
        throw new Error("the disk filled");
      }
      return writeFile(file, data, options);
    };
    await expect(
      copy.setRefs(refs, state.generation + BigInt(1)),
    ).rejects.toThrow("the disk filled");
    storage.promises.writeFile = writeFile;
    expect(await copy.documentState()).toBeUndefined();
  });
});

describe("hydration", () => {
  test("from one pack, the refs are the document's and the tip reads back", async () => {
    const remote = seeded(1);
    const hydrated = await hydrate(copy, remote, OPEN);
    const snapshot = await remote.readRefDoc();
    expect(hydrated.generation).toBe(snapshot.generation);
    expect(await copy.refs()).toEqual(documentRefs(requireDocument(snapshot)));
    const tip = hydrated.refs.get(COLLABORATIVE_BRANCH);
    if (tip === undefined) throw new Error("no tip");
    expect(await documentAt(tip)).toBe(documents(1)[0]);
    // The copy is a bare repository git reads: every object the refs reach is there and sound.
    runGit(copyDir, ["fsck", "--strict", "--no-dangling"]);
  });

  test("from many packs, the history is each publish's tip, newest first, timed by its entry", async () => {
    const remote = seeded(5);
    await hydrate(copy, remote, OPEN);
    const tips = await copy.recordedTips(COLLABORATIVE_BRANCH);
    expect(tips).toHaveLength(5);
    for (const [i, tip] of tips.entries()) {
      expect(await documentAt(tip.commit)).toBe(documents(5)[4 - i]);
      expect(tip.timestamp).toBe(AUTHORED.getTime() / 1000 + (4 - i) * 60);
    }
    expect(tips[0].commit).toBe((await copy.ref(COLLABORATIVE_BRANCH)) ?? "");
  });

  test("a repository that does not exist leaves the copy with no refs", async () => {
    const remote = fixtureRemote(new FixtureWorkspace(), ANALYSIS);
    const hydrated = await hydrate(copy, remote, OPEN);
    expect(hydrated.generation).toBe(BigInt(0));
    expect(await copy.refs()).toEqual(new Map());
  });

  test("a copy already up to date downloads nothing", async () => {
    const remote = seeded(3);
    await hydrate(copy, remote, OPEN);
    const downloads = remote.calls.downloads.length;
    await hydrate(copy, remote, OPEN);
    expect(remote.calls.downloads.length).toBe(downloads);
  });

  test("a failed download is signed afresh before it is tried again", async () => {
    const remote = seeded(1);
    remote.failDownloads(1);
    await hydrate(copy, remote, OPEN);
    const [packId] = requireDocument(await remote.readRefDoc()).packs;
    expect(remote.calls.signed.filter((id) => id === packId)).toHaveLength(2);
    expect(remote.calls.downloads).toHaveLength(2);
  });

  test("a download whose bytes do not hash to the pack's id is never indexed", async () => {
    const remote = seeded(1);
    remote.corruptNextDownload();
    await hydrate(copy, remote, OPEN);
    expect(remote.calls.downloads).toHaveLength(2);
    runGit(copyDir, ["fsck", "--strict", "--no-dangling"]);
  });

  test("a pack that never downloads fails the hydration and moves no ref", async () => {
    const remote = seeded(1);
    remote.failDownloads(10);
    await expect(hydrate(copy, remote, OPEN)).rejects.toThrow(HydrationError);
    expect(await copy.refs()).toEqual(new Map());
  });

  test("a hydration cut short resumes: indexed packs stay, and only the rest are asked for", async () => {
    const remote = seeded(3);
    const packs = requireDocument(await remote.readRefDoc()).packs;
    // The first pack downloads; every attempt at the second fails.
    const flaky: Remote = {
      ...remote,
      download: async (url, size) => {
        if (remote.calls.downloads.length >= 1) {
          remote.calls.downloads.push(url);
          throw new DownloadError("offline");
        }
        return remote.download(url, size);
      },
    };
    await expect(hydrate(copy, flaky, OPEN)).rejects.toThrow(HydrationError);
    expect(await copy.refs()).toEqual(new Map());
    expect((await copy.indexedPacks()).size).toBe(1);
    const indexed = [...(await copy.indexedPacks())][0];
    remote.calls.downloads.length = 0;
    await hydrate(copy, remote, OPEN);
    expect(remote.calls.downloads).toHaveLength(packs.length - 1);
    expect(remote.calls.downloads.some((url) => url.endsWith(indexed))).toBe(
      false,
    );
    expect((await copy.indexedPacks()).size).toBe(packs.length);
  });

  test("a signing that finds the repository damaged stops the hydration, with nothing signed or read again", async () => {
    const remote = seeded(3);
    let signings = 0;
    const damaged: Remote = {
      ...remote,
      signPackUrls: async () => {
        signings += 1;
        throw new WorkspaceDamagedError("a listed pack is not held");
      },
    };
    await expect(hydrate(copy, damaged, OPEN)).rejects.toBeInstanceOf(
      WorkspaceDamagedError,
    );
    expect(signings).toBe(1);
    expect(remote.calls.readRefDoc).toBe(1);
    expect(remote.calls.downloads).toHaveLength(0);
    expect(await copy.refs()).toEqual(new Map());
  });

  test("a re-signing after a failed download that finds the repository damaged stops the hydration", async () => {
    const remote = seeded(1);
    remote.failDownloads(1);
    let signings = 0;
    const damaged: Remote = {
      ...remote,
      signPackUrls: async (packIds) => {
        signings += 1;
        if (signings > 1)
          throw new WorkspaceDamagedError("a listed pack is not held");
        return remote.signPackUrls(packIds);
      },
    };
    await expect(hydrate(copy, damaged, OPEN)).rejects.toBeInstanceOf(
      WorkspaceDamagedError,
    );
    expect(signings).toBe(2);
    expect(remote.calls.downloads).toHaveLength(1);
    expect(await copy.refs()).toEqual(new Map());
  });

  test("a document read that finds the repository damaged stops the hydration, read once", async () => {
    const remote = seeded(1);
    let reads = 0;
    const damaged: Remote = {
      ...remote,
      readRefDoc: async () => {
        reads += 1;
        throw new WorkspaceDamagedError("the stored document does not parse");
      },
    };
    await expect(hydrate(copy, damaged, OPEN)).rejects.toBeInstanceOf(
      WorkspaceDamagedError,
    );
    expect(reads).toBe(1);
    expect(remote.calls.signed).toHaveLength(0);
  });

  test("a stale pack list is answered by reading the document again", async () => {
    const remote = seeded(1);
    let reads = 0;
    const stale: Remote = {
      ...remote,
      readRefDoc: async () => {
        const snapshot = await remote.readRefDoc();
        reads += 1;
        if (reads > 1) return snapshot;
        // A document read before a compaction replaced the pack it lists.
        const document = requireDocument(snapshot);
        return {
          ...snapshot,
          document: { ...document, packs: ["e".repeat(64)] },
        };
      },
    };
    await hydrate(copy, stale, OPEN);
    expect(reads).toBe(2);
    expect((await copy.indexedPacks()).size).toBe(1);
  });

  test("every ref is written only after a flush that follows the objects it names", async () => {
    const remote = seeded(3);
    await hydrate(copy, remote, OPEN);
    let unflushedObjects = false;
    for (const event of storage.events) {
      if (event.kind === "flush") {
        unflushedObjects = false;
      } else if (event.path.startsWith("objects/")) {
        unflushedObjects = true;
      } else if (event.path.startsWith("refs/")) {
        expect(unflushedObjects).toBe(false);
      }
    }
    expect(
      storage.events.some(
        (e) => e.kind === "write" && e.path.startsWith("refs/"),
      ),
    ).toBe(true);
  });

  test("a copy over the ceiling is refused before any pack is downloaded", async () => {
    const remote = seeded(2);
    const residency = new Residency(
      {
        list: async () => [],
        get: async () => undefined,
        put: async () => {},
        remove: async () => {},
      },
      async () => false,
      { budgetBytes: 1024 * 1024, ceilingBytes: 64 },
    );
    await expect(
      hydrate(copy, remote, {
        admit: (bytes) => residency.admit(ANALYSIS, bytes),
      }),
    ).rejects.toThrow(CopyTooLargeError);
    expect(remote.calls.downloads).toHaveLength(0);
  });
});

describe("a compaction", () => {
  test("replaces the copy's packs with the one it wrote, and admits the copy by that one", async () => {
    const remote = seeded(4);
    await hydrate(copy, remote, OPEN);
    const before = await copy.recordedTips(COLLABORATIVE_BRANCH);
    const snapshot = await remote.readRefDoc();
    const document = requireDocument(snapshot);
    // What compaction writes: one pack of every object the document's packs hold, deltas and all.
    const oracle = oracleRepo(scratch.dir);
    for (const packId of document.packs) {
      runGit(
        oracle,
        ["index-pack", "--stdin"],
        await downloadPack(remote, packId),
      );
    }
    for (const [ref, oid] of documentRefs(document)) {
      runGit(oracle, ["update-ref", ref, oid]);
    }
    const compacted = new Uint8Array(
      runGit(
        oracle,
        ["pack-objects", "--all", "--revs", "--stdout", "--quiet"],
        "",
      ),
    );
    const compactedId = await sha256Hex(compacted);
    const compactedRemote: Remote = {
      ...remote,
      readRefDoc: async () => ({
        ...snapshot,
        generation: snapshot.generation + BigInt(1),
        document: { ...document, packs: [compactedId] },
      }),
      signPackUrls: async (packIds) =>
        packIds.map((packId) => {
          if (packId !== compactedId) throw new StalePackListError(packId);
          return create(SignedPackSchema, {
            packId,
            url: "compacted",
            size: BigInt(compacted.length),
            expireTime: timestampFromMs(Date.now() + 3_600_000),
          });
        }),
      download: async () => compacted,
    };
    // A ceiling the compacted pack fits under, and the copy's old packs beside it would not.
    const residency = new Residency(
      {
        list: async () => [],
        get: async () => undefined,
        put: async () => {},
        remove: async () => {},
      },
      async () => false,
      { budgetBytes: 1024 * 1024 * 1024, ceilingBytes: compacted.length + 1 },
    );
    await hydrate(copy, compactedRemote, {
      admit: (bytes) => residency.admit(ANALYSIS, bytes),
    });
    expect(await copy.indexedPacks()).toEqual(new Set([compactedId]));
    expect(readdirSync(path.join(copyDir, "objects", "pack")).sort()).toEqual([
      `pack-${compactedId}.idx`,
      `pack-${compactedId}.pack`,
    ]);
    expect(await copy.packBytes([compactedId])).toBe(compacted.length);
    expect(await copy.recordedTips(COLLABORATIVE_BRANCH)).toEqual(before);
    for (const tip of before)
      expect(await documentAt(tip.commit)).toBeDefined();
    runGit(copyDir, ["fsck", "--strict", "--no-dangling"]);
  });
});

describe("the packs a hydration leaves", () => {
  test("are deleted and flushed when the document drops them, and left alone when it does not", async () => {
    const remote = seeded(2);
    await hydrate(copy, remote, OPEN);
    const listed = new Set(requireDocument(await remote.readRefDoc()).packs);
    expect(await copy.prunePacks(listed)).toBe(false);
    const [kept] = listed;
    storage.events.length = 0;
    expect(await copy.prunePacks(new Set([kept]))).toBe(true);
    const lastUnlink = storage.events.findLastIndex((e) => e.kind === "unlink");
    expect(lastUnlink).toBeGreaterThanOrEqual(0);
    expect(
      storage.events.slice(lastUnlink).some((e) => e.kind === "flush"),
    ).toBe(true);
  });

  test("are deleted index first, whatever order the filesystem lists them in", async () => {
    const remote = seeded(2);
    await hydrate(copy, remote, OPEN);
    const readdir = storage.promises.readdir;
    // LightningFS lists a directory in the order its files were created: each pack before its index.
    storage.promises.readdir = async (p) =>
      (await readdir(p)).sort(
        (a, b) => Number(b.endsWith(".pack")) - Number(a.endsWith(".pack")),
      );
    storage.events.length = 0;
    expect(await copy.prunePacks(new Set())).toBe(true);
    const unlinked = storage.events.flatMap((e) =>
      e.kind === "unlink" ? [e.path] : [],
    );
    const firstPack = unlinked.findIndex((p) => p.endsWith(".pack"));
    const lastIndex = unlinked.findLastIndex((p) => p.endsWith(".idx"));
    expect(unlinked).toHaveLength(4);
    expect(lastIndex).toBeLessThan(firstPack);
  });

  test("an index that fails to read for another reason is raised, not taken for damage", async () => {
    const remote = seeded(1);
    await hydrate(copy, remote, OPEN);
    const readFile = storage.promises.readFile;
    storage.promises.readFile = async (p, options) => {
      if (String(p).endsWith(".idx")) {
        throw Object.assign(new Error("a transient read failure"), {
          code: "EIO",
        });
      }
      return readFile(p, options);
    };
    await expect(WorkspaceCopy.open(storage)).rejects.toThrow(/transient/);
    storage.promises.readFile = readFile;
    expect((await copy.indexedPacks()).size).toBe(1);
  });

  test("an index whose bytes were lost is dropped when the copy opens, and the next hydration heals it", async () => {
    const remote = seeded(2);
    await hydrate(copy, remote, OPEN);
    const tip = (await copy.ref(COLLABORATIVE_BRANCH)) ?? "";
    const [lost] = requireDocument(await remote.readRefDoc()).packs;
    // What LightningFS leaves after a crash: the file is recorded, its bytes are not.
    writeFileSync(
      path.join(copyDir, "objects", "pack", `pack-${lost}.idx`),
      new Uint8Array(),
    );
    const reopened = await WorkspaceCopy.open(storage);
    expect((await reopened.indexedPacks()).has(lost)).toBe(false);
    remote.calls.downloads.length = 0;
    await hydrate(reopened, remote, OPEN);
    expect(remote.calls.downloads).toHaveLength(1);
    expect(await reopened.readFile(tip, "working_document.md")).toBeDefined();
    runGit(copyDir, ["fsck", "--strict", "--no-dangling"]);
  });
});

describe("a pack's index", () => {
  test("that holds fewer objects than the pack declares fails loudly, and the pack is dropped", async () => {
    const oracle = oracleRepo(scratch.dir);
    const env = {
      GIT_AUTHOR_NAME: "a",
      GIT_AUTHOR_EMAIL: "a@x",
      GIT_COMMITTER_NAME: "a",
      GIT_COMMITTER_EMAIL: "a@x",
    };
    const text = Array.from(
      { length: 400 },
      (_, i) => `line ${i} of a document long enough to delta\n`,
    ).join("");
    const commitOf = (content: string, parents: string[]) => {
      const blob = gitText(oracle, ["hash-object", "-w", "--stdin"], content);
      const tree = gitText(oracle, ["mktree"], `100644 blob ${blob}\tdoc.md\n`);
      return gitText(
        oracle,
        ["commit-tree", tree, ...parents.flatMap((p) => ["-p", p]), "-m", "c"],
        "",
        env,
      );
    };
    const first = commitOf(text, []);
    const second = commitOf(`${text}one more line\n`, [first]);
    // Thin: the changed blob is a delta against one the pack leaves out.
    const thin = new Uint8Array(
      runGit(
        oracle,
        ["pack-objects", "--revs", "--thin", "--stdout", "--quiet"],
        `${second}\n^${first}\n`,
      ),
    );
    const packId = "d".repeat(64);
    await expect(copy.addPack(packId, thin)).rejects.toThrow(
      IncompletePackError,
    );
    expect(await copy.indexedPacks()).toEqual(new Set());
    expect(readdirSync(path.join(copyDir, "objects", "pack"))).toEqual([]);
    expect(packObjectCount(thin)).toBe(3);
  });
});

function requireDocument(snapshot: RefDocSnapshot) {
  if (snapshot.document === undefined) throw new Error("no document");
  return snapshot.document;
}
