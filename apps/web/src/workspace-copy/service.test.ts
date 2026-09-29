import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";
import { COLLABORATIVE_BRANCH, WorkspaceCopy } from "./copy";
import { FixtureWorkspace, fixtureRemote } from "./fixture-remote.test-support";
import { directoryStorage, scratchDir } from "./git.test-support";
import { decodeUtf8, utf8 } from "./git-objects";
import type { CopyLocks } from "./locks";
import { memoryLocks } from "./locks.test-support";
import type { Remote } from "./remote";
import type { CopyRecord, Ledger } from "./residency";
import {
  CommitNotInCopyError,
  CopyService,
  type CopyStorageFactory,
} from "./service";

// The SharedWorker's logic with its surroundings on the host: a directory per copy, a map for the
// ledger, the offline sheaf store for the relay.

const AUTHORED = new Date(1_727_163_000_000);

let scratch: ReturnType<typeof scratchDir>;
let store: FixtureWorkspace;
let ledger: Ledger & { records: Map<string, CopyRecord> };
let clock: number;
let locks: CopyLocks;
let opened: string[];

beforeEach(() => {
  scratch = scratchDir("service");
  store = new FixtureWorkspace();
  ledger = memoryLedger();
  clock = AUTHORED.getTime();
  locks = memoryLocks();
  opened = [];
});

afterEach(() => scratch.remove());

function memoryLedger(): Ledger & { records: Map<string, CopyRecord> } {
  const records = new Map<string, CopyRecord>();
  return {
    records,
    list: async () => [...records.values()],
    get: async (id) => records.get(id),
    put: async (record) => {
      records.set(record.analysisId, { ...record });
    },
    remove: async (id) => {
      records.delete(id);
    },
  };
}

function storage(): CopyStorageFactory {
  const root = path.join(scratch.dir, "copies");
  mkdirSync(root, { recursive: true });
  return {
    open: async (id) => {
      opened.push(id);
      return directoryStorage(path.join(root, id));
    },
    remove: async (id) =>
      rmSync(path.join(root, id), { recursive: true, force: true }),
    list: async () => readdirSync(root),
  };
}

/** An edit that sets state.txt to `state`: its message and its one file. */
function setState(state: string) {
  return [
    `Set the state to ${state}`,
    [{ path: "state.txt", bytes: utf8(`${state}\n`) }],
  ] as const;
}

/** A copy service as one SharedWorker holds it. Two built in one test share the storage, the
 *  ledger and the locks, as two builds' workers in one browser do. */
function service(
  limits?: { budgetBytes: number; ceilingBytes: number },
  releaseAfterIdleMs?: number,
  remote: (id: string) => Remote = (id) => fixtureRemote(store, id),
  workerLocks: CopyLocks = locks,
): CopyService {
  return new CopyService({
    storage: storage(),
    remote,
    ledger,
    locks: workerLocks,
    limits,
    now: () => clock,
    releaseAfterIdleMs,
  });
}

async function tipOf(id: string): Promise<string> {
  const snapshot = await fixtureRemote(store, id).readRefDoc();
  const target = snapshot.document?.refs[COLLABORATIVE_BRANCH]?.target;
  if (target?.case !== "oid") throw new Error(`no tip for ${id}`);
  return target.value;
}

describe("the copy service", () => {
  test("syncs to the Poll's tip and reads the document there and at each earlier version", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n", "# v3\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    expect(await copies.readDocument("an_1", tip)).toBe("# v3\n");
    const history = await copies.history("an_1", tip);
    expect(history.map((v) => v.commit)[0]).toBe(tip);
    expect(
      await Promise.all(
        history.map((v) => copies.readDocument("an_1", v.commit)),
      ),
    ).toEqual(["# v3\n", "# v2\n", "# v1\n"]);
  });

  test("answers whether one commit it holds is another's ancestor, and refuses one it lacks", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const older = (await copies.history("an_1", tip))[1].commit;
    expect(await copies.isAncestor("an_1", older, tip)).toBe(true);
    expect(await copies.isAncestor("an_1", tip, older)).toBe(false);
    expect(await copies.isAncestor("an_1", tip, tip)).toBe(true);
    await expect(
      copies.isAncestor("an_1", "0".repeat(40), tip),
    ).rejects.toThrow("does not hold");
  });

  test("a commit without the working document reads as absent", async () => {
    store.agentPublishes(
      "an_1",
      COLLABORATIVE_BRANCH,
      { "notes.md": "scratch\n" },
      AUTHORED,
    );
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    expect(await copies.readDocument("an_1", tip)).toBeNull();
    expect(
      decodeUtf8(
        (await copies.readFile("an_1", tip, "notes.md")) ?? new Uint8Array(),
        "f",
      ),
    ).toBe("scratch\n");
  });

  test("a sync to the tip the copy already holds asks nothing of the relay", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    let reads = 0;
    const copies = new CopyService({
      storage: storage(),
      remote: (id) => {
        const remote = fixtureRemote(store, id);
        return {
          ...remote,
          readRefDoc: () => {
            reads += 1;
            return remote.readRefDoc();
          },
        };
      },
      ledger,
      locks,
    });
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    await Promise.all([copies.sync("an_1", tip), copies.sync("an_1", tip)]);
    expect(reads).toBe(1);
  });

  test("the copy outlives the worker: a new service reads it without downloading", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n"], AUTHORED);
    const tip = await tipOf("an_1");
    await service().sync("an_1", tip);
    const again = service();
    await again.reconcile();
    await again.sync("an_1", tip);
    expect(await again.readDocument("an_1", tip)).toBe("# v2\n");
  });

  test("over the budget, the least recently read copy is deleted whole", async () => {
    for (const id of ["an_1", "an_2", "an_3"]) {
      store.seedAgentHistory(id, [`# ${id}\n`], AUTHORED);
    }
    const probe = service();
    await probe.sync("an_1", await tipOf("an_1"));
    const oneCopy = ledger.records.get("an_1")?.bytes ?? 0;
    expect(oneCopy).toBeGreaterThan(0);
    rmSync(path.join(scratch.dir, "copies", "an_1"), { recursive: true });
    ledger.records.clear();

    // Room for two copies, not three.
    const copies = service({
      budgetBytes: Math.floor(oneCopy * 2.5),
      ceilingBytes: oneCopy * 2,
    });
    await copies.sync("an_1", await tipOf("an_1"));
    clock += 1000;
    await copies.sync("an_2", await tipOf("an_2"));
    clock += 1000;
    // Reading an_1 again makes an_2 the least recently read.
    await copies.readDocument("an_1", await tipOf("an_1"));
    clock += 1000;
    await copies.sync("an_3", await tipOf("an_3"));
    expect([...ledger.records.keys()].sort()).toEqual(["an_1", "an_3"]);
    expect(existsSync(path.join(scratch.dir, "copies", "an_2"))).toBe(false);
    // The evicted copy is only a download away.
    await copies.sync("an_2", await tipOf("an_2"));
    expect(await copies.readDocument("an_2", await tipOf("an_2"))).toBe(
      "# an_2\n",
    );
  });

  test("storage the ledger does not record is an orphan, and is deleted", async () => {
    const copies = service();
    mkdirSync(path.join(scratch.dir, "copies", "an_orphan"), {
      recursive: true,
    });
    await ledger.put({ analysisId: "an_gone", bytes: 10, lastReadMs: 0 });
    await copies.reconcile();
    expect(readdirSync(path.join(scratch.dir, "copies"))).toEqual([]);
    expect(ledger.records.size).toBe(0);
  });

  test("publishes an edit's files as a commit on the tip", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const outcome = await copies.publish(
      "an_1",
      tip,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect(await tipOf("an_1")).toBe(outcome.commit);
    expect((await copies.history("an_1", outcome.commit))[0].commit).toBe(
      outcome.commit,
    );
    expect(await copies.readFile("an_1", outcome.commit, "state.txt")).toEqual(
      utf8("reviewed\n"),
    );
  });

  test("an edit whose file the agent changed since its base resolves as fileChanged, at the new tip", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const base = await tipOf("an_1");
    await copies.sync("an_1", base);
    store.agentPublishes(
      "an_1",
      COLLABORATIVE_BRANCH,
      { "state.txt": "agent\n" },
      AUTHORED,
    );
    const moved = await tipOf("an_1");
    const outcome = await copies.publish(
      "an_1",
      base,
      "curator@example.org",
      ...setState("reviewed"),
    );
    expect(outcome).toEqual({
      kind: "fileChanged",
      commit: moved,
      path: "state.txt",
    });
    expect(await tipOf("an_1")).toBe(moved);
  });

  test("a publish on a copy that follows the tip sends the publish alone, edit after edit", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const reads = relay.calls.readRefDoc;
    const first = await copies.publish(
      "an_1",
      tip,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (first.kind !== "landed") throw new Error(`not landed: ${first.kind}`);
    const second = await copies.publish(
      "an_1",
      first.commit,
      "curator@example.org",
      ...setState("reviewed again"),
    );
    if (second.kind !== "landed") throw new Error(`not landed: ${second.kind}`);
    expect(relay.calls.readRefDoc).toBe(reads);
    expect(relay.calls.publishes).toBe(2);
    expect(await tipOf("an_1")).toBe(second.commit);
  });

  test("a publish on a copy behind the tip lands through the refusal that brings it up to date", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const base = await tipOf("an_1");
    await copies.sync("an_1", base);
    store.agentPublishes(
      "an_1",
      COLLABORATIVE_BRANCH,
      { "notes.md": "agent\n" },
      AUTHORED,
    );
    const moved = await tipOf("an_1");
    const reads = relay.calls.readRefDoc;
    const outcome = await copies.publish(
      "an_1",
      base,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(relay.calls.publishes).toBe(2);
    expect(relay.calls.readRefDoc).toBe(reads + 1);
    expect(await tipOf("an_1")).toBe(outcome.commit);
    expect(await copies.isAncestor("an_1", moved, outcome.commit)).toBe(true);
  });

  test("a publish on a copy whose document moved elsewhere lands through the refusal that brings it up to date", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    store.agentPublishes(
      "an_1",
      "refs/heads/scratch",
      { "notes.md": "agent\n" },
      AUTHORED,
    );
    const reads = relay.calls.readRefDoc;
    const outcome = await copies.publish(
      "an_1",
      tip,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(relay.calls.publishes).toBe(2);
    expect(relay.calls.readRefDoc).toBe(reads + 1);
    expect(await copies.isAncestor("an_1", tip, outcome.commit)).toBe(true);
  });

  test("a recorded state the service refuses as malformed is forgotten, and the next publish reads first", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const older = (await copies.history("an_1", tip))[1].commit;
    // A generation recorded beside refs it does not match: the current generation, the branch
    // set back to an older commit.
    const broken = await WorkspaceCopy.open(
      directoryStorage(path.join(scratch.dir, "copies", "an_1")),
    );
    const recorded = await broken.documentState();
    if (recorded === undefined) throw new Error("no document state");
    const refs = new Map(recorded.refs);
    refs.set(COLLABORATIVE_BRANCH, older);
    await broken.setRefs(refs, recorded.generation);
    await expect(
      copies.publish("an_1", older, "c@example.org", ...setState("x")),
    ).rejects.toMatchObject({ refusal: "malformed" });
    expect(await broken.documentState()).toBeUndefined();
    const reads = relay.calls.readRefDoc;
    const outcome = await copies.publish(
      "an_1",
      tip,
      "c@example.org",
      ...setState("x"),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(relay.calls.readRefDoc).toBe(reads + 1);
  });

  test("an edit whose file changed on a tip the copy already follows is not applied, and nothing is sent", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const base = await tipOf("an_1");
    await copies.sync("an_1", base);
    store.agentPublishes(
      "an_1",
      COLLABORATIVE_BRANCH,
      { "state.txt": "agent\n" },
      AUTHORED,
    );
    const moved = await tipOf("an_1");
    await copies.sync("an_1", moved);
    const outcome = await copies.publish(
      "an_1",
      base,
      "curator@example.org",
      ...setState("reviewed"),
    );
    expect(outcome).toEqual({
      kind: "fileChanged",
      commit: moved,
      path: "state.txt",
    });
    expect(relay.calls.publishes).toBe(0);
  });

  test("an edit drawn from a commit the copy lacks brings the copy up to date before its publish", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    await copies.sync("an_1", await tipOf("an_1"));
    store.agentPublishes(
      "an_1",
      COLLABORATIVE_BRANCH,
      { "notes.md": "agent\n" },
      AUTHORED,
    );
    const drawn = await tipOf("an_1");
    const reads = relay.calls.readRefDoc;
    const outcome = await copies.publish(
      "an_1",
      drawn,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(relay.calls.readRefDoc).toBe(reads + 1);
    expect(relay.calls.publishes).toBe(1);
  });

  test("a copy that records no document state reads the document before its publish", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const relay = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => relay);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    // What a copy written before the generation was recorded, or one whose refs a crash cut short,
    // holds: refs and no generation.
    rmSync(path.join(scratch.dir, "copies", "an_1", "THEMIS_GENERATION"));
    const reads = relay.calls.readRefDoc;
    const outcome = await copies.publish(
      "an_1",
      tip,
      "curator@example.org",
      ...setState("reviewed"),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(relay.calls.readRefDoc).toBe(reads + 1);
    expect(relay.calls.publishes).toBe(1);
  });

  test("an edit that replaces a file twice is refused before anything is written", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    const [message, [file]] = setState("x");
    await expect(
      copies.publish("an_1", tip, "c@example.org", message, [file, file]),
    ).rejects.toThrow(/replaces each file once/);
    expect(await tipOf("an_1")).toBe(tip);
  });

  test.each([
    ["an empty message", "", "state.txt", /commit message/],
    ["a message with a NUL", "Set\0", "state.txt", /commit message/],
    [
      "a path no repository holds",
      "Set",
      "../state.txt",
      /not a repository path/,
    ],
  ])(
    "an edit with %s is refused before the copy is touched",
    async (_, message, filePath, reason) => {
      store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
      const copies = service();
      const tip = await tipOf("an_1");
      await expect(
        copies.publish("an_1", tip, "c@example.org", message, [
          { path: filePath, bytes: utf8("x\n") },
        ]),
      ).rejects.toThrow(reason);
      // Refused before the lock is taken, so no copy was ever opened.
      expect(opened).toEqual([]);
    },
  );

  test("an edit is authored only as a well-formed email", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    for (const email of ["", "curator", "Curator <c@example.org>", "c@x\n"]) {
      await expect(
        copies.publish("an_1", tip, email, ...setState("x")),
      ).rejects.toThrow(/needs their email/);
    }
    expect(await tipOf("an_1")).toBe(tip);
  });

  test("a copy another worker is reading is never evicted", async () => {
    for (const id of ["an_1", "an_2", "an_3"]) {
      store.seedAgentHistory(id, [`# ${id}\n`], AUTHORED);
    }
    const probe = service();
    await probe.sync("an_1", await tipOf("an_1"));
    const oneCopy = ledger.records.get("an_1")?.bytes ?? 0;
    const limits = {
      budgetBytes: Math.floor(oneCopy * 2.5),
      ceilingBytes: oneCopy * 2,
    };
    const ours = service(limits);
    const theirs = service(limits);
    await ours.sync("an_2", await tipOf("an_2"));
    clock += 1000;
    await ours.sync("an_1", await tipOf("an_1"));
    clock += 1000;
    // an_2 is the least recently read, and the other worker is reading it.
    let finishRead = () => {};
    const reading = locks.hold(
      "an_2",
      "shared",
      () =>
        new Promise<void>((done) => {
          finishRead = done;
        }),
    );
    await theirs.sync("an_3", await tipOf("an_3"));
    expect([...ledger.records.keys()].sort()).toEqual(["an_2", "an_3"]);
    expect(existsSync(path.join(scratch.dir, "copies", "an_2"))).toBe(true);
    finishRead();
    await reading;
  });

  test("a copy a curator clears waits for the lock another worker holds, then is deleted whole", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const ours = service();
    await ours.sync("an_1", await tipOf("an_1"));
    let finishRead = () => {};
    const reading = locks.hold(
      "an_1",
      "shared",
      () =>
        new Promise<void>((done) => {
          finishRead = done;
        }),
    );
    let cleared = false;
    const clearing = ours.reset("an_1").then(() => {
      cleared = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Eviction would have skipped the copy; the clear is still waiting for it.
    expect(cleared).toBe(false);
    expect(existsSync(path.join(scratch.dir, "copies", "an_1"))).toBe(true);
    expect(ledger.records.has("an_1")).toBe(true);
    finishRead();
    await reading;
    await clearing;
    expect(existsSync(path.join(scratch.dir, "copies", "an_1"))).toBe(false);
    expect(ledger.records.has("an_1")).toBe(false);
  });

  test("after a clear, the next read hydrates the copy again, at one download", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const remote = fixtureRemote(store, "an_1");
    const copies = service(undefined, undefined, () => remote);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const downloads = remote.calls.downloads.length;
    await copies.sync("an_1", tip);
    expect(remote.calls.downloads).toHaveLength(downloads);
    await copies.reset("an_1");
    await expect(copies.readDocument("an_1", tip)).rejects.toThrow(
      CommitNotInCopyError,
    );
    await copies.sync("an_1", tip);
    expect(remote.calls.downloads).toHaveLength(downloads + 1);
    expect(await copies.readDocument("an_1", tip)).toBe("# v1\n");
  });

  test("a copy another worker deleted is recreated by the next write", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    store.seedAgentHistory("an_2", ["# v2\n"], AUTHORED);
    const ours = service();
    const tip = await tipOf("an_1");
    await ours.sync("an_1", tip);
    // The other worker evicts an_1 to make room, deleting its storage whole.
    const theirs = service({ budgetBytes: 1, ceilingBytes: 1024 * 1024 });
    clock += 1000;
    await theirs.sync("an_2", await tipOf("an_2"));
    expect(existsSync(path.join(scratch.dir, "copies", "an_1"))).toBe(false);
    await expect(ours.readDocument("an_1", tip)).rejects.toThrow(
      /does not hold/,
    );
    await ours.sync("an_1", tip);
    expect(await ours.readDocument("an_1", tip)).toBe("# v1\n");
  });

  test("a copy nothing has used for a while lets its storage and cache go", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service(undefined, 20);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    await copies.readDocument("an_1", tip);
    expect(opened).toEqual(["an_1"]);
    await Bun.sleep(60);
    expect(await copies.readDocument("an_1", tip)).toBe("# v1\n");
    expect(opened).toEqual(["an_1", "an_1"]);
  });

  test("a pending commit a lost worker left is cleared by the next sync", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    // What a worker lost mid-publish leaves: its commit on the local-only pending ref.
    const leftover = await WorkspaceCopy.open(
      directoryStorage(path.join(scratch.dir, "copies", "an_1")),
    );
    await leftover.setPending(tip);
    expect(await leftover.pending()).toBe(tip);
    await copies.sync("an_1", tip);
    expect(await leftover.pending()).toBeUndefined();
  });

  test("a sync to a tip the copy is already past asks nothing of the relay", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n"], AUTHORED);
    let reads = 0;
    const counting = service(undefined, undefined, (id) => {
      const remote = fixtureRemote(store, id);
      return {
        ...remote,
        readRefDoc: () => {
          reads += 1;
          return remote.readRefDoc();
        },
      };
    });
    const tip = await tipOf("an_1");
    await counting.sync("an_1", tip);
    const older = (await counting.history("an_1", tip))[1].commit;
    const before = reads;
    // A Poll that lags the copy — after a curator's publish, say — reports an older tip.
    await counting.sync("an_1", older);
    expect(reads).toBe(before);
  });

  test("the history of a copy whose branch is behind the tip is refused, though it holds the tip", async () => {
    store.seedAgentHistory("an_1", ["# v1\n", "# v2\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    const older = (await copies.history("an_1", tip))[1].commit;
    // A hydration that indexed the tip's pack and has not moved the refs yet.
    const halfway = await WorkspaceCopy.open(
      directoryStorage(path.join(scratch.dir, "copies", "an_1")),
    );
    const refs = await halfway.refs();
    refs.set(COLLABORATIVE_BRANCH, older);
    const recorded = await halfway.documentState();
    if (recorded === undefined) throw new Error("no document state");
    await halfway.setRefs(refs, recorded.generation);
    // Setting the refs was cut short, so no generation is recorded beside them.
    await halfway.forgetDocumentState();
    expect(await halfway.hasObject(tip)).toBe(true);
    await expect(copies.history("an_1", tip)).rejects.toBeInstanceOf(
      CommitNotInCopyError,
    );
  });

  test("the history of a copy that lacks the tip is refused, never read as empty", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    await expect(copies.history("an_1", "a".repeat(40))).rejects.toBeInstanceOf(
      CommitNotInCopyError,
    );
  });

  test("an idle release while a read waits for the lock leaves the read to reopen the copy", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service(undefined, 20);
    const tip = await tipOf("an_1");
    await copies.sync("an_1", tip);
    let release = () => {};
    const held = locks.hold(
      "an_1",
      "exclusive",
      () =>
        new Promise<void>((done) => {
          release = done;
        }),
    );
    const read = copies.readDocument("an_1", tip);
    await Bun.sleep(60);
    release();
    await held;
    expect(await read).toBe("# v1\n");
  });

  test("an email with a control character is refused", async () => {
    store.seedAgentHistory("an_1", ["# v1\n"], AUTHORED);
    const copies = service();
    const tip = await tipOf("an_1");
    for (const email of [
      "c\u0000@example.org",
      "c@example.org\u007f",
      "c\u001b@x.org",
    ]) {
      await expect(
        copies.publish("an_1", tip, email, ...setState("x")),
      ).rejects.toThrow(/needs their email/);
    }
  });
});
