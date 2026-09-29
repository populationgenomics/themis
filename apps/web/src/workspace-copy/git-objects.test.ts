import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import path from "node:path";
import { WorkspaceCopy } from "./copy";
import {
  directoryStorage,
  gitText,
  oracleRepo,
  runGit,
  scratchDir,
} from "./git.test-support";
import {
  EMPTY_TREE,
  serializeCommit,
  serializeTree,
  type TreeEntry,
  utf8,
} from "./git-objects";
import { recordEntry } from "./reflog";

// Every tree, commit and reflog entry the copy writes must be the object git writes for the same
// inputs, byte for byte: the store checks no objects, and a listing git rejects can never be removed.

let scratch: ReturnType<typeof scratchDir>;
let oracle: string;
let copy: WorkspaceCopy;
let copyDir: string;

beforeEach(async () => {
  scratch = scratchDir("objects");
  oracle = oracleRepo(scratch.dir);
  copyDir = path.join(scratch.dir, "copy.git");
  copy = await WorkspaceCopy.open(directoryStorage(copyDir));
});

afterEach(() => scratch.remove());

/** A blob with `content` in both repositories; its id. */
async function blob(content: string): Promise<string> {
  const expected = gitText(oracle, ["hash-object", "-w", "--stdin"], content);
  expect(await copy.writeBlob(utf8(content))).toBe(expected);
  return expected;
}

/** The tree `git mktree` builds from `entries`, and its bytes. */
function mktree(
  entries: readonly { mode: string; type: string; oid: string; name: string }[],
) {
  const input = entries
    .map((e) => `${e.mode} ${e.type} ${e.oid}\t${e.name}\n`)
    .join("");
  const oid = gitText(oracle, ["mktree"], input);
  return {
    oid,
    bytes: new Uint8Array(runGit(oracle, ["cat-file", "tree", oid])),
  };
}

describe("trees", () => {
  test.each([
    {
      case: "a directory sorts as if its name ended in a slash",
      names: [
        { name: "notes", dir: true },
        { name: "notes.md", dir: false },
        { name: "notes-x", dir: false },
      ],
    },
    {
      case: "a file sorts before a directory whose name it prefixes",
      names: [
        { name: "notes", dir: false },
        { name: "notes.md", dir: false },
        { name: "notes.d", dir: true },
      ],
    },
    {
      case: "names order by UTF-8 bytes, not UTF-16 code units",
      names: [
        { name: "😀.md", dir: false },
        { name: "ｆ.md", dir: false },
        { name: "a.md", dir: false },
      ],
    },
  ])("$case", async ({ names }) => {
    const leaf = await blob("leaf\n");
    const subtree = mktree([
      { mode: "100644", type: "blob", oid: leaf, name: "inner" },
    ]);
    expect(
      await copy.writeTree([
        { mode: "100644", name: utf8("inner"), oid: leaf },
      ]),
    ).toBe(subtree.oid);
    const entries = names.map(({ name, dir }) => ({
      mode: dir ? "40000" : "100644",
      type: dir ? "tree" : "blob",
      oid: dir ? subtree.oid : leaf,
      name,
    }));
    const expected = mktree(entries);
    const ours: TreeEntry[] = entries.map((e) => ({
      mode: e.mode,
      name: utf8(e.name),
      oid: e.oid,
    }));
    expect(serializeTree(ours)).toEqual(expected.bytes);
    expect(await copy.writeTree(ours)).toBe(expected.oid);
    // The copy is a bare repository git can read, so git checks the tree the copy stored.
    runGit(copyDir, ["fsck", "--strict", "--no-dangling"]);
  });

  test("each mode git writes for a file is kept", async () => {
    const content = await blob("#!/bin/sh\n");
    const target = await blob("working_document.md");
    const entries = [
      { mode: "100644", type: "blob", oid: content, name: "plain" },
      { mode: "100755", type: "blob", oid: content, name: "script" },
      { mode: "120000", type: "blob", oid: target, name: "link" },
    ];
    const expected = mktree(entries);
    const oid = await copy.writeTree(
      entries.map((e) => ({ mode: e.mode, name: utf8(e.name), oid: e.oid })),
    );
    expect(oid).toBe(expected.oid);
  });

  test("the empty tree is the one git knows", async () => {
    expect(serializeTree([])).toEqual(new Uint8Array());
    await copy.writeEmptyTree();
    expect(gitText(copyDir, ["cat-file", "-t", EMPTY_TREE])).toBe("tree");
  });

  test.each([
    { case: "two entries of one name", names: ["a", "a"], dirs: [false, true] },
    { case: "a name holding a slash", names: ["a/b"], dirs: [false] },
    { case: "a dot-dot name", names: [".."], dirs: [false] },
  ])("refuses $case", ({ names, dirs }) => {
    const oid = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391";
    expect(() =>
      serializeTree(
        names.map((name, i) => ({
          mode: dirs[i] ? "40000" : "100644",
          name: utf8(name),
          oid,
        })),
      ),
    ).toThrow();
  });
});

describe("commits", () => {
  const curator = { name: "curator@example.org", email: "curator@example.org" };

  test("a commit is the one commit-tree writes for the same inputs", async () => {
    const tree = mktree([
      { mode: "100644", type: "blob", oid: await blob("v1\n"), name: "f" },
    ]);
    const parent = gitText(
      oracle,
      ["commit-tree", tree.oid, "-m", "first"],
      "",
      dated(curator, 1_727_000_000),
    );
    const expected = gitText(
      oracle,
      ["commit-tree", tree.oid, "-p", parent, "-m", "Set PS3 reviewed"],
      "",
      dated(curator, 1_727_000_100),
    );
    const fields = {
      tree: tree.oid,
      parents: [parent],
      author: { ...curator, timestamp: 1_727_000_100 },
      committer: { ...curator, timestamp: 1_727_000_100 },
      message: "Set PS3 reviewed",
    };
    expect(serializeCommit(fields)).toEqual(
      new Uint8Array(runGit(oracle, ["cat-file", "commit", expected])),
    );
    expect(await copy.writeCommit(fields)).toBe(expected);
  });

  test.each([
    { case: "an email holding an angle bracket", email: "a<b@example.org" },
    { case: "a name with surrounding space", name: " curator" },
    { case: "a name holding a newline", name: "cur\nator" },
  ])("refuses a signature git would rewrite: $case", ({ name, email }) => {
    const signature = {
      name: name ?? "curator",
      email: email ?? "c@example.org",
      timestamp: 1,
    };
    expect(() =>
      serializeCommit({
        tree: EMPTY_TREE,
        parents: [],
        author: signature,
        committer: signature,
        message: "m",
      }),
    ).toThrow();
  });
});

describe("reflog entries", () => {
  const sheaf = { name: "sheaf", email: "sheaf@localhost" };
  const at = 1_727_163_041;

  /** What sheaf's `record` writes (themis/sheaf/wire/reflog.py), with its time fixed. */
  function sheafRecord(
    previous: string | undefined,
    ref: string,
    old: string | undefined,
    next: string,
  ): string {
    const env = dated(sheaf, at);
    const base =
      previous ??
      gitText(
        oracle,
        ["commit-tree", EMPTY_TREE, "-m", "sheaf: init"],
        "",
        env,
      );
    const parents = next === base ? [base] : [base, next];
    return gitText(
      oracle,
      [
        "commit-tree",
        EMPTY_TREE,
        ...parents.flatMap((p) => ["-p", p]),
        "-m",
        `sheaf: ${ref}\n\n${ref} ${old ?? "0".repeat(40)} ${next}\n`,
      ],
      "",
      env,
    );
  }

  async function tipCommit(): Promise<string> {
    const tree = mktree([
      {
        mode: "100644",
        type: "blob",
        oid: await blob("doc\n"),
        name: "working_document.md",
      },
    ]);
    return gitText(
      oracle,
      ["commit-tree", tree.oid, "-m", "agent"],
      "",
      dated(sheaf, at),
    );
  }

  const writer = () => ({
    emptyTree: () => copy.writeEmptyTree(),
    commit: (fields: Parameters<WorkspaceCopy["writeCommit"]>[0]) =>
      copy.writeCommit(fields),
  });

  test("a repository's first entry is parented on a root entry, which it adds with the empty tree", async () => {
    gitText(oracle, ["hash-object", "-w", "-t", "tree", "--stdin"]);
    const tip = await tipCommit();
    const expected = sheafRecord(undefined, "refs/heads/main", undefined, tip);
    const recorded = await recordEntry(
      writer(),
      undefined,
      [{ ref: "refs/heads/main", old: undefined, new: tip }],
      at,
    );
    expect(recorded.entry).toBe(expected);
    const root = gitText(oracle, ["rev-parse", `${expected}^1`]);
    expect(recorded.added).toEqual([EMPTY_TREE, root, expected]);
  });

  test("a later entry is parented on the previous one and adds only itself", async () => {
    gitText(oracle, ["hash-object", "-w", "-t", "tree", "--stdin"]);
    const first = await tipCommit();
    const previous = sheafRecord(
      undefined,
      "refs/heads/main",
      undefined,
      first,
    );
    const tree = gitText(oracle, ["rev-parse", `${first}^{tree}`]);
    const second = gitText(
      oracle,
      ["commit-tree", tree, "-p", first, "-m", "curator"],
      "",
      dated(sheaf, at),
    );
    const expected = sheafRecord(previous, "refs/heads/main", first, second);
    const recorded = await recordEntry(
      writer(),
      previous,
      [{ ref: "refs/heads/main", old: first, new: second }],
      at,
    );
    expect(recorded).toEqual({ entry: expected, added: [expected] });
  });

  test("an entry records at least one transition", async () => {
    await expect(recordEntry(writer(), undefined, [], at)).rejects.toThrow();
  });
});

function dated(
  who: { name: string; email: string },
  seconds: number,
): Record<string, string> {
  const date = `${seconds} +0000`;
  return {
    GIT_AUTHOR_NAME: who.name,
    GIT_AUTHOR_EMAIL: who.email,
    GIT_AUTHOR_DATE: date,
    GIT_COMMITTER_NAME: who.name,
    GIT_COMMITTER_EMAIL: who.email,
    GIT_COMMITTER_DATE: date,
  };
}
