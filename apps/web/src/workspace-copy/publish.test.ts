import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import path from "node:path";
import { create } from "@bufbuild/protobuf";
import { PublishIntentSchema, type RefDocSnapshot } from "@/models/sheaf";
import type { AgentFile } from "@/server/adapters/fixture/workspace-seed";
import { COLLABORATIVE_BRANCH, WorkspaceCopy } from "./copy";
import {
  downloadPack,
  FixtureWorkspace,
  fixtureRemote,
} from "./fixture-remote.test-support";
import {
  directoryStorage,
  gitText,
  runGit,
  scratchDir,
} from "./git.test-support";
import { decodeUtf8, EMPTY_TREE, utf8 } from "./git-objects";
import { documentRefs, type Hydrated, hydrate } from "./hydrate";
import { sha256Hex } from "./pack";
import {
  type Edit,
  PUBLISH_ATTEMPTS,
  PublishOutcomeUnknownError,
  publishEdit,
  RetryBudgetSpentError,
} from "./publish";
import { REFLOG_REF, readRecord } from "./reflog";
import {
  PublishFaultError,
  PublishRefusedError,
  type Remote,
  WorkspaceDamagedError,
} from "./remote";

// The write path against the offline sheaf store, which decides each publish from the document as
// the service does. Every outcome the design lists is driven here, and what lands is checked with
// the real `git`.

const ANALYSIS = "an_7f3c";
const AUTHORED = new Date(1_727_163_000_000);
const OPEN = { admit: async () => {} };
const CURATOR = { name: "curator@example.org", email: "curator@example.org" };
const STATE = "assets/review_state.txt";

let scratch: ReturnType<typeof scratchDir>;
let copy: WorkspaceCopy;
let remote: ReturnType<typeof fixtureRemote>;

beforeEach(async () => {
  scratch = scratchDir("publish");
  copy = await WorkspaceCopy.open(
    directoryStorage(path.join(scratch.dir, "copy.git")),
  );
  const store = new FixtureWorkspace();
  store.seedAgentHistory(
    ANALYSIS,
    ["# Draft\n", "# Draft\n\nPS3 applies.\n"],
    AUTHORED,
  );
  remote = fixtureRemote(store, ANALYSIS);
});

afterEach(() => scratch.remove());

/** An edit drawn from `base` that replaces each of `files` (path to content). */
function editOf(base: string, files: Record<string, string>): Edit {
  return {
    base,
    curator: CURATOR,
    message: `Write ${Object.keys(files).join(", ")}`,
    files: new Map(
      Object.entries(files).map(([file, text]) => [file, utf8(text)]),
    ),
  };
}

/** "Mark `criterion` reviewed" as the widget makes it at `base`: the review state there with a
 *  line appended. */
async function markReviewed(criterion: string, base: string): Promise<Edit> {
  const current = await copy.readFile(base, STATE);
  const text = current === undefined ? "" : decodeUtf8(current.bytes, STATE);
  return {
    ...editOf(base, { [STATE]: `${text}${criterion} reviewed\n` }),
    message: `Mark ${criterion} reviewed`,
  };
}

function agentPushes(
  files: Record<string, AgentFile>,
  branch = COLLABORATIVE_BRANCH,
): void {
  remote.store.agentPublishes(
    ANALYSIS,
    branch,
    files,
    new Date(AUTHORED.getTime() + 3_600_000),
  );
}

async function hydrated(on: Remote = remote): Promise<Hydrated> {
  return hydrate(copy, on, OPEN);
}

function tipOf(state: Hydrated): string {
  const tip = state.refs.get(COLLABORATIVE_BRANCH);
  if (tip === undefined) throw new Error("no tip");
  return tip;
}

async function publish(edit: Edit, on: Remote = remote) {
  return publishEdit(
    copy,
    on,
    () => hydrate(copy, on, OPEN),
    await hydrated(on),
    edit,
  );
}

async function storeDocument(): Promise<RefDocSnapshot> {
  return remote.readRefDoc();
}

async function storeRefs(): Promise<Map<string, string>> {
  const { document } = await storeDocument();
  if (document === undefined) throw new Error("no document");
  return documentRefs(document);
}

/** A bare repository built by `git` from every pack the store holds, with the store's refs. */
async function mirror(): Promise<string> {
  const gitDir = path.join(scratch.dir, `mirror-${Date.now()}.git`);
  runGit(gitDir, ["init", "--bare", "--quiet", gitDir]);
  const snapshot = await storeDocument();
  const document = snapshot.document;
  if (document === undefined) throw new Error("no document");
  for (const packId of document.packs) {
    const bytes = await downloadPack(remote, packId);
    runGit(gitDir, ["index-pack", "--stdin"], bytes);
  }
  for (const [ref, oid] of documentRefs(document))
    runGit(gitDir, ["update-ref", ref, oid]);
  return gitDir;
}

async function stateAt(commit: string): Promise<string> {
  const file = await copy.readFile(commit, STATE);
  if (file === undefined) throw new Error(`no ${STATE} at ${commit}`);
  return decodeUtf8(file.bytes, STATE);
}

describe("a curator's edit", () => {
  test("accepted: the branch moves to a commit git accepts, authored as the curator", async () => {
    const before = tipOf(await hydrated());
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    const refs = await storeRefs();
    expect(refs.get(COLLABORATIVE_BRANCH)).toBe(outcome.commit);
    expect(outcome.generation).toBe((await storeDocument()).generation);
    // The copy's refs follow the document it wrote, and the pending commit is gone.
    expect(await copy.refs()).toEqual(refs);
    expect(await copy.pending()).toBeUndefined();

    const git = await mirror();
    runGit(git, ["fsck", "--strict", "--no-dangling"]);
    expect(
      gitText(git, [
        "log",
        "-1",
        "--format=%an <%ae>|%cn <%ce>|%P|%s",
        outcome.commit,
      ]),
    ).toBe(
      `${CURATOR.name} <${CURATOR.email}>|${CURATOR.name} <${CURATOR.email}>|${before}|Mark PS3 reviewed`,
    );
    expect(gitText(git, ["show", `${outcome.commit}:${STATE}`])).toBe(
      "PS3 reviewed",
    );
    // The reflog entry is one sheaf's reader parses: the transition from the old tip to the new.
    const reflog = refs.get(REFLOG_REF) ?? "";
    const entry = await copy.readCommit(reflog);
    expect(readRecord(reflog, entry.parents, entry.message)).toEqual({
      kind: "entry",
      transitions: [
        { ref: COLLABORATIVE_BRANCH, old: before, new: outcome.commit },
      ],
    });
    // The picker lists the curator's commit as the newest version.
    expect((await copy.recordedTips(COLLABORATIVE_BRANCH))[0].commit).toBe(
      outcome.commit,
    );
  });

  test("the pack carries exactly the objects the store lacks, each stored whole", async () => {
    const before = tipOf(await hydrated());
    const packsBefore = new Set((await storeDocument()).document?.packs);
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    const ours =
      (await storeDocument()).document?.packs.filter(
        (id) => !packsBefore.has(id),
      ) ?? [];
    expect(ours).toHaveLength(1);
    const bytes = await downloadPack(remote, ours[0]);
    const git = path.join(scratch.dir, "inspect.git");
    runGit(git, ["init", "--bare", "--quiet", git]);
    const name = gitText(git, ["index-pack", "--stdin"], bytes).split("\t")[1];
    const rows = gitText(git, [
      "verify-pack",
      "-v",
      path.join(git, "objects", "pack", `pack-${name}.idx`),
    ])
      .split("\n")
      .filter((line) => /^[0-9a-f]{40} /.test(line))
      .map((line) => line.split(/\s+/));
    // A delta's row names its base; every row here is an object stored whole.
    expect(rows.every((row) => row.length === 5)).toBe(true);
    // The changed blob, the two trees above it, the commit and the reflog entry — nothing the
    // store already held.
    const kinds = rows.map((row) => row[1]).sort();
    expect(kinds).toEqual(["blob", "commit", "commit", "tree", "tree"]);
    expect(rows.some((row) => row[0] === outcome.commit)).toBe(true);
  });

  test("ABORTED: an unrelated publish landed first; the same commit lands with a new reflog entry", async () => {
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () =>
      agentPushes({ "scratch.txt": "x\n" }, "refs/heads/scratch"),
    );
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect(remote.calls.publishes).toBe(2);
    const refs = await storeRefs();
    expect(refs.get(COLLABORATIVE_BRANCH)).toBe(outcome.commit);
    expect(refs.has("refs/heads/scratch")).toBe(true);
    expect((await copy.readCommit(outcome.commit)).parents).toEqual([before]);
    runGit(await mirror(), ["fsck", "--strict", "--no-dangling"]);
  });

  test("FAILED_PRECONDITION: the branch moved elsewhere; the edit's files are swapped into the new tip", async () => {
    agentPushes({ [STATE]: "PM2 reviewed\n" });
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () =>
      agentPushes({ "working_document.md": "# Draft\n\nPS3 and PM2 apply.\n" }),
    );
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    const agentTip = (await copy.readCommit(outcome.commit)).parents[0];
    expect(agentTip).not.toBe(before);
    expect((await copy.readCommit(agentTip)).parents).toEqual([before]);
    // The agent's change survives beside the curator's: the commit is rebuilt on the agent's tree.
    const git = await mirror();
    expect(
      gitText(git, ["show", `${outcome.commit}:working_document.md`]),
    ).toBe("# Draft\n\nPS3 and PM2 apply.");
    expect(await stateAt(outcome.commit)).toBe("PM2 reviewed\nPS3 reviewed\n");
    runGit(git, ["fsck", "--strict", "--no-dangling"]);
  });

  /** The edit ended as `fileChanged` at the branch's tip in the store, which carries no commit of
   *  the curator's, and the copy is left with nothing pending. */
  async function expectNotApplied(
    outcome: Awaited<ReturnType<typeof publish>>,
    changed: string,
  ): Promise<void> {
    const tip = (await storeRefs()).get(COLLABORATIVE_BRANCH);
    if (tip === undefined) throw new Error("no tip");
    expect(outcome).toEqual({
      kind: "fileChanged",
      commit: tip,
      path: changed,
    });
    expect(await copy.pending()).toBeUndefined();
    expect(
      gitText(await mirror(), [
        "rev-list",
        "--count",
        `--author=${CURATOR.email}`,
        COLLABORATIVE_BRANCH,
      ]),
    ).toBe("0");
  }

  test("FAILED_PRECONDITION: the agent changed the edited file; the edit is not applied", async () => {
    agentPushes({ [STATE]: "PM2 reviewed\n" });
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () =>
      agentPushes({ [STATE]: "PM2 reviewed\nPM1 reviewed\n" }),
    );
    const outcome = await publish(await markReviewed("PS3", before));
    expect(remote.calls.publishes).toBe(1);
    await expectNotApplied(outcome, STATE);
  });

  test("FAILED_PRECONDITION: the agent deleted the edited file; the edit is not applied", async () => {
    agentPushes({ [STATE]: "PM2 reviewed\n" });
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () => agentPushes({ [STATE]: null }));
    const outcome = await publish(await markReviewed("PS3", before));
    expect(remote.calls.publishes).toBe(1);
    await expectNotApplied(outcome, STATE);
    if (outcome.kind !== "fileChanged") throw new Error("applied");
    expect(await copy.readFile(outcome.commit, STATE)).toBeUndefined();
  });

  test("ABORTED, then a rehydrate showing the agent changed the edited file: the edit is not applied", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const racing: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        agentPushes({ [STATE]: "PM2 reviewed\n" });
        throw new PublishRefusedError("raceLost", "lost");
      },
    };
    const outcome = await publish(await markReviewed("PS3", before), racing);
    expect(publishes).toBe(1);
    await expectNotApplied(outcome, STATE);
  });

  test("an edit whose file only changed mode since its base is not applied", async () => {
    agentPushes({ [STATE]: "PM2 reviewed\n" });
    const before = tipOf(await hydrated());
    agentPushes({ [STATE]: { content: "PM2 reviewed\n", mode: "100755" } });
    const outcome = await publish(await markReviewed("PS3", before));
    expect(remote.calls.publishes).toBe(0);
    await expectNotApplied(outcome, STATE);
  });

  test("an edit whose path the agent made run through a file is not applied", async () => {
    const before = tipOf(await hydrated());
    agentPushes({ assets: "not a directory\n" });
    const outcome = await publish(await markReviewed("PS3", before));
    expect(remote.calls.publishes).toBe(0);
    await expectNotApplied(outcome, STATE);
  });

  test("ABORTED, then a rehydrate showing the agent changed another file: the edit is rebuilt on it", async () => {
    const before = tipOf(await hydrated());
    let refusals = 0;
    const racing: Remote = {
      ...remote,
      publish: async (intent, pack) => {
        if (refusals > 0) return remote.publish(intent, pack);
        refusals += 1;
        agentPushes({ "working_document.md": "# Draft\n\nPM2 applies.\n" });
        throw new PublishRefusedError("raceLost", "lost");
      },
    };
    const outcome = await publish(await markReviewed("PS3", before), racing);
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    const agentTip = (await copy.readCommit(outcome.commit)).parents[0];
    expect((await copy.readCommit(agentTip)).parents).toEqual([before]);
    expect(await stateAt(outcome.commit)).toBe("PS3 reviewed\n");
  });

  test("a refusal after a publish that did land, with the edited file changed on top, is recognised as landed", async () => {
    const before = tipOf(await hydrated());
    let landed: string | undefined;
    const echoing: Remote = {
      ...remote,
      publish: async (intent, pack) => {
        await remote.publish(intent, pack);
        landed = intent.refUpdates[COLLABORATIVE_BRANCH]?.new;
        agentPushes({ [STATE]: "PS3 reviewed\nPM2 reviewed\n" });
        throw new PublishRefusedError("raceLost", "lost");
      },
    };
    const outcome = await publish(await markReviewed("PS3", before), echoing);
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    expect(outcome.commit).toBe(landed ?? "");
  });

  test("an edit whose file the agent created after its base is not applied, and nothing is sent", async () => {
    const before = tipOf(await hydrated());
    agentPushes({ [STATE]: "PM2 reviewed\n" });
    const outcome = await publish(await markReviewed("PS3", before));
    expect(remote.calls.publishes).toBe(0);
    await expectNotApplied(outcome, STATE);
  });

  test("a lost response, retried after the agent changed the edited file on top, is recognised as landed", async () => {
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () => {
      remote.loseNextResponse();
      remote.beforeNextPublish(async () =>
        agentPushes({ [STATE]: "PS3 reviewed\nPM2 reviewed\n" }),
      );
    });
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect(remote.calls.publishes).toBe(2);
    const tip = (await storeRefs()).get(COLLABORATIVE_BRANCH) ?? "";
    expect(tip).not.toBe(outcome.commit);
    expect((await copy.readCommit(tip)).parents).toEqual([outcome.commit]);
    // Exactly one curator commit exists on the branch: nothing was built twice.
    const git = await mirror();
    expect(
      gitText(git, ["rev-list", "--count", `--author=${CURATOR.email}`, tip]),
    ).toBe("1");
  });

  test.each([
    ["overCeiling", "RESOURCE_EXHAUSTED"],
    ["malformed", "INVALID_ARGUMENT"],
  ] as const)("%s (%s) is surfaced and never resent", async (refusal) => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const refusing: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        throw new PublishRefusedError(refusal, "refused");
      },
    };
    const error = await publish(
      await markReviewed("PS3", before),
      refusing,
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(PublishRefusedError);
    expect((error as PublishRefusedError).refusal).toBe(refusal);
    expect(publishes).toBe(1);
    expect(await copy.pending()).toBeUndefined();
    expect(tipOf(await hydrated())).toBe(before);
  });

  test("DATA_LOSS answering the first send is not sent again, and the edit ends with the damage", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const damaged: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        throw new WorkspaceDamagedError("the stored document does not parse");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), damaged),
    ).rejects.toBeInstanceOf(WorkspaceDamagedError);
    expect(publishes).toBe(1);
    expect(await copy.pending()).toBeUndefined();
    expect((await storeRefs()).get(COLLABORATIVE_BRANCH)).toBe(before);
  });

  test("a publish never answered whose resend finds the repository damaged ends as damaged, not unknown", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const damaged: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        if (publishes === 1) throw new PublishFaultError("no answer");
        throw new WorkspaceDamagedError("the stored document does not parse");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), damaged),
    ).rejects.toBeInstanceOf(WorkspaceDamagedError);
    expect(publishes).toBe(2);
    expect(await copy.pending()).toBeUndefined();
  });

  test("a lost race whose read of the document finds the repository damaged is not sent again", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const damaged: Remote = {
      ...remote,
      readRefDoc: async () => {
        if (publishes > 0)
          throw new WorkspaceDamagedError("the stored document does not parse");
        return remote.readRefDoc();
      },
      publish: async () => {
        publishes += 1;
        throw new PublishRefusedError("branchMoved", "moved");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), damaged),
    ).rejects.toBeInstanceOf(WorkspaceDamagedError);
    expect(publishes).toBe(1);
  });

  test("a publish never answered whose document read finds the repository damaged ends as damaged", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const damaged: Remote = {
      ...remote,
      readRefDoc: async () => {
        if (publishes > 0)
          throw new WorkspaceDamagedError("the stored document does not parse");
        return remote.readRefDoc();
      },
      publish: async () => {
        publishes += 1;
        throw new PublishFaultError("no answer");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), damaged),
    ).rejects.toBeInstanceOf(WorkspaceDamagedError);
    expect(await copy.pending()).toBeUndefined();
  });

  test("a publish that keeps losing races spends its budget and says so", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const contended: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        throw new PublishRefusedError("raceLost", "lost");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), contended),
    ).rejects.toThrow(RetryBudgetSpentError);
    expect(publishes).toBe(PUBLISH_ATTEMPTS);
    expect(await copy.pending()).toBeUndefined();
  });

  test("a publish never answered and not shown landed ends with its outcome unknown", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const silent: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        throw new PublishFaultError("no answer");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), silent),
    ).rejects.toThrow(PublishOutcomeUnknownError);
    expect(publishes).toBe(PUBLISH_ATTEMPTS);
    expect(await copy.pending()).toBeUndefined();
  });

  test("a publish never answered whose resend fails otherwise ends with its outcome unknown", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const lapsed: Remote = {
      ...remote,
      publish: async () => {
        publishes += 1;
        if (publishes === 1) throw new PublishFaultError("no answer");
        throw new Error("the session expired");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), lapsed),
    ).rejects.toThrow(PublishOutcomeUnknownError);
    expect(publishes).toBe(2);
  });

  test("a publish never answered whose document cannot be read ends with its outcome unknown", async () => {
    const before = tipOf(await hydrated());
    let publishes = 0;
    const silent: Remote = {
      ...remote,
      readRefDoc: async () => {
        if (publishes > 0) throw new Error("the document could not be read");
        return remote.readRefDoc();
      },
      publish: async () => {
        publishes += 1;
        throw new PublishFaultError("no answer");
      },
    };
    await expect(
      publish(await markReviewed("PS3", before), silent),
    ).rejects.toThrow(PublishOutcomeUnknownError);
    expect(await copy.pending()).toBeUndefined();
  });

  test("a replaced file keeps its mode, and a new one is a regular file", async () => {
    agentPushes({ "tools/run.sh": { content: "#!/bin/sh\n", mode: "100755" } });
    const before = tipOf(await hydrated());
    const outcome = await publish(
      editOf(before, {
        "tools/run.sh": "#!/bin/sh\nexit 0\n",
        "notes.md": "n\n",
      }),
    );
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    const git = await mirror();
    const mode = (file: string) =>
      gitText(git, ["ls-tree", outcome.commit, "--", file]).split(" ")[0];
    expect(mode("tools/run.sh")).toBe("100755");
    expect(mode("notes.md")).toBe("100644");
  });

  test("an edit whose bytes the tip already holds makes no commit", async () => {
    const before = tipOf(await hydrated());
    const file = await copy.readFile(before, "working_document.md");
    if (file === undefined) throw new Error("no document");
    const noop = editOf(before, {
      "working_document.md": decodeUtf8(file.bytes, "working_document.md"),
    });
    expect(await publish(noop)).toEqual({ kind: "unchanged", commit: before });
    expect(remote.calls.publishes).toBe(0);
  });

  test.each([
    ["replaces no file", {}, /replaces no file/],
    [
      "names a path no repository holds",
      { "../x": "x\n" },
      /not a repository path/,
    ],
  ] as const)(
    "an edit that %s is refused before anything is read",
    async (_, files, reason) => {
      // A base the copy does not hold: a check that read the copy first would fail on it instead.
      const edit = editOf("a".repeat(40), files);
      await expect(publish(edit)).rejects.toThrow(reason);
      expect(remote.calls.publishes).toBe(0);
    },
  );

  test("an edit that replaces a file and a file inside it is refused before anything is read", async () => {
    // A base the copy does not hold: a check that read the copy first would fail on it instead.
    const edit = editOf("a".repeat(40), {
      assets: "x\n",
      "assets/a.txt": "y\n",
    });
    await expect(publish(edit)).rejects.toThrow(
      /replaces assets as a file and assets\/a.txt inside it/,
    );
    expect(remote.calls.publishes).toBe(0);
  });

  test("an edit that writes over a symlink is refused and sends nothing", async () => {
    agentPushes({
      "assets/link": { content: "../working_document.md", mode: "120000" },
    });
    const before = tipOf(await hydrated());
    await expect(
      publish(editOf(before, { "assets/link": "PS3 reviewed\n" })),
    ).rejects.toThrow(/assets\/link is not a file an edit can write/);
    expect(remote.calls.publishes).toBe(0);
    expect(await copy.pending()).toBeUndefined();
  });

  test("an edit drawn from a commit the branch never held is refused", async () => {
    const edit = editOf("a".repeat(40), { [STATE]: "PS3 reviewed\n" });
    await expect(publish(edit)).rejects.toThrow(/not on refs\/heads\/main/);
    expect(remote.calls.publishes).toBe(0);
  });

  test("an edit drawn from an older version is built on the tip, never on the stale commit", async () => {
    const state = await hydrated();
    const tip = tipOf(state);
    const older = (await copy.readCommit(tip)).parents[0];
    const outcome = await publish(await markReviewed("PS3", older));
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect((await copy.readCommit(outcome.commit)).parents).toEqual([tip]);
  });

  test("two changed paths with the same bytes put that blob in the pack once", async () => {
    const before = tipOf(await hydrated());
    const packsBefore = new Set((await storeDocument()).document?.packs);
    const twin = editOf(before, {
      "assets/a.txt": "same\n",
      "assets/b.txt": "same\n",
    });
    const outcome = await publish(twin);
    if (outcome.kind !== "landed") throw new Error("not landed");
    const git = await mirror();
    const ours =
      (await storeDocument()).document?.packs.filter(
        (id) => !packsBefore.has(id),
      ) ?? [];
    const bytes = await downloadPack(remote, ours[0]);
    // --strict refuses a pack that names an object twice; every object it links to is in the mirror.
    runGit(git, ["index-pack", "--stdin", "--strict"], bytes);
    runGit(git, ["fsck", "--strict", "--no-dangling"]);
  });

  test("a resend answered as landed after another ref moved leaves the copy at the current document", async () => {
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () => {
      remote.loseNextResponse();
      remote.beforeNextPublish(async () =>
        agentPushes({ "scratch.txt": "x\n" }, "refs/heads/scratch"),
      );
    });
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect(remote.calls.publishes).toBe(2);
    const refs = await storeRefs();
    expect(refs.get(COLLABORATIVE_BRANCH)).toBe(outcome.commit);
    expect(await copy.refs()).toEqual(refs);
    expect(outcome.generation).toBe((await storeDocument()).generation);
  });
});

describe("a resend the service answered as landed", () => {
  test("is landed though the copy cannot follow the document after it", async () => {
    const before = tipOf(await hydrated());
    remote.beforeNextPublish(async () => {
      remote.loseNextResponse();
      remote.beforeNextPublish(async () =>
        remote.failDownloads(PUBLISH_ATTEMPTS * 10),
      );
    });
    const outcome = await publish(await markReviewed("PS3", before));
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect((await storeRefs()).get(COLLABORATIVE_BRANCH)).toBe(outcome.commit);
  });
});

describe("a commit whose headers and message are not UTF-8", () => {
  test("is read, and a curator's edit lands on it", async () => {
    const store = new FixtureWorkspace();
    const latin = fixtureRemote(store, "an_latin");
    const oracle = path.join(scratch.dir, "latin.git");
    runGit(oracle, [
      "init",
      "--bare",
      "--quiet",
      "--object-format=sha1",
      oracle,
    ]);
    const blob = gitText(
      oracle,
      ["hash-object", "-w", "--stdin"],
      "# Bericht\n",
    );
    const tree = gitText(
      oracle,
      ["mktree"],
      `100644 blob ${blob}\tworking_document.md\n`,
    );
    const latin1 = (text: string) =>
      Uint8Array.from(text, (c) => c.charCodeAt(0));
    const commit = gitText(
      oracle,
      ["hash-object", "-t", "commit", "-w", "--stdin"],
      latin1(
        `tree ${tree}\nauthor J\u00f6rg <j@example.org> 1727163000 +0200\n` +
          `committer J\u00f6rg <j@example.org> 1727163000 +0200\nencoding ISO-8859-1\n\n` +
          "F\u00fcr den Bericht\n",
      ),
    );
    const sheaf = {
      GIT_AUTHOR_NAME: "sheaf",
      GIT_AUTHOR_EMAIL: "sheaf@localhost",
      GIT_COMMITTER_NAME: "sheaf",
      GIT_COMMITTER_EMAIL: "sheaf@localhost",
    };
    gitText(oracle, ["hash-object", "-w", "-t", "tree", "--stdin"]);
    const root = gitText(
      oracle,
      ["commit-tree", EMPTY_TREE, "-m", "sheaf: init"],
      "",
      sheaf,
    );
    const entry = gitText(
      oracle,
      [
        "commit-tree",
        EMPTY_TREE,
        "-p",
        root,
        "-p",
        commit,
        "-m",
        `sheaf: ${COLLABORATIVE_BRANCH}\n\n${COLLABORATIVE_BRANCH} ${"0".repeat(40)} ${commit}\n`,
      ],
      "",
      sheaf,
    );
    const pack = new Uint8Array(
      runGit(
        oracle,
        ["pack-objects", "--revs", "--stdout", "--quiet"],
        `${entry}\n`,
      ),
    );
    await store.publish(
      latin.analysis,
      create(PublishIntentSchema, {
        baseGeneration: BigInt(0),
        refUpdates: {
          [COLLABORATIVE_BRANCH]: { new: commit },
          [REFLOG_REF]: { new: entry },
        },
        packs: [{ size: BigInt(pack.length), packId: await sha256Hex(pack) }],
      }),
      [pack],
    );
    const state = await hydrate(copy, latin, OPEN);
    expect((await copy.readCommit(commit)).message).toContain("den Bericht");
    expect(
      decodeUtf8(
        (await copy.readFile(commit, "working_document.md"))?.bytes ??
          new Uint8Array(),
        "doc",
      ),
    ).toBe("# Bericht\n");
    const outcome = await publishEdit(
      copy,
      latin,
      () => hydrate(copy, latin, OPEN),
      state,
      await markReviewed("PS3", commit),
    );
    if (outcome.kind !== "landed") throw new Error("not landed");
    expect((await copy.readCommit(outcome.commit)).parents).toEqual([commit]);
  });
});
