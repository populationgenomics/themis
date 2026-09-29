import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { devNull, tmpdir } from "node:os";
import path from "node:path";
import { COLLABORATIVE_BRANCH } from "../../workspace";

// The offline stand-in for the agent's pushes: a workspace repository's history built by the real
// `git`, one publish per working-document version, each carrying the pack of exactly the objects it
// adds and a reflog entry in sheaf's format (themis/sheaf/wire/reflog.py). Built in a scratch bare
// repository that is deleted afterwards; what survives is the publishes.

export const REFLOG_REF = "refs/sheaf/reflog";

/** The file the working document is at the branch tip (docs/design/workbench-workspace.md). */
export const WORKING_DOCUMENT_PATH = "working_document.md";

/** What an agent commit writes at a path: a regular file's content, content with the mode its tree
 *  entry gets, or null to delete the file there. */
export type AgentFile =
  | string
  | Uint8Array
  | { content: string | Uint8Array; mode: string }
  | null;

/** The files one agent commit writes, by repository path, over the tree it is built on. */
export type SeedFiles = Readonly<Record<string, AgentFile>>;

/** One publish of the agent's history: the refs it moves and the pack it carries. */
export interface SeedPublish {
  refUpdates: Record<string, { old?: string; new: string }>;
  pack: Uint8Array;
}

const EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
const ZERO_OID = "0".repeat(40);
/** Who a seeded commit is authored and committed as. */
export interface Identity {
  name: string;
  email: string;
}

export const AGENT: Identity = {
  name: "Themis agent",
  email: "agent@themis.invalid",
};

/** One commit of a seeded history: the files it writes over the previous commit's tree, who made it,
 *  and its message. */
export interface SeedCommit {
  files: SeedFiles;
  author: Identity;
  message: string;
}
const SHEAF = { name: "sheaf", email: "sheaf@localhost" };

type GitEnv = Record<string, string>;

/** The process's environment with every `GIT_*` variable replaced: a caller's repository (a git hook
 *  running the tests sets `GIT_DIR`) and a developer's own configuration (signing, a default object
 *  format) must not reach the seed. */
function gitEnvironment(overrides: GitEnv): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key.startsWith("GIT_")) delete env[key];
  }
  return {
    ...env,
    GIT_CONFIG_GLOBAL: devNull,
    GIT_CONFIG_NOSYSTEM: "1",
    LC_ALL: "C",
    ...overrides,
  };
}

/** Run `git` against the object database at `gitDir`, returning its stdout. */
function runGit(
  gitDir: string,
  args: readonly string[],
  input: string | Uint8Array = "",
  env: GitEnv = {},
): Buffer {
  try {
    return execFileSync("git", args, {
      input,
      env: gitEnvironment({ GIT_DIR: gitDir, ...env }),
      stdio: ["pipe", "pipe", "pipe"],
      maxBuffer: 64 * 1024 * 1024,
    });
  } catch (error) {
    throw new Error(
      `the fixture seeds each workspace repository with git, and \`git ${args[0]}\` failed`,
      { cause: error },
    );
  }
}

type Git = (
  args: readonly string[],
  input?: string | Uint8Array,
  env?: GitEnv,
) => string;

function identity(who: { name: string; email: string }, at: Date): GitEnv {
  const date = `${Math.floor(at.getTime() / 1000)} +0000`;
  return {
    GIT_AUTHOR_NAME: who.name,
    GIT_AUTHOR_EMAIL: who.email,
    GIT_AUTHOR_DATE: date,
    GIT_COMMITTER_NAME: who.name,
    GIT_COMMITTER_EMAIL: who.email,
    GIT_COMMITTER_DATE: date,
  };
}

/** Whether `tip` holds a file at `filePath`. */
function holdsFile(
  git: Git,
  tip: string | undefined,
  filePath: string,
): boolean {
  if (tip === undefined) return false;
  return git(["ls-tree", "--name-only", tip, "--", filePath]) === filePath;
}

/** An agent commit's title, naming the files it writes and the files it deletes. */
function publishTitle(files: Readonly<Record<string, AgentFile>>): string {
  const paths = Object.entries(files);
  const written = paths.filter(([, file]) => file !== null).map(([p]) => p);
  const deleted = paths.filter(([, file]) => file === null).map(([p]) => p);
  return [
    ...(written.length > 0 ? [`Write ${written.join(", ")}`] : []),
    ...(deleted.length > 0 ? [`Delete ${deleted.join(", ")}`] : []),
  ].join("; ");
}

function commitTree(
  git: Git,
  tree: string,
  parents: readonly string[],
  message: string,
  env: GitEnv,
): string {
  const args = ["commit-tree", tree];
  for (const parent of parents) args.push("-p", parent);
  args.push("-m", message);
  return git(args, undefined, env);
}

/** The reflog entry for one publish moving `ref` from `old` to `next`, as sheaf's `record` writes
 *  it: parented on the previous entry and the new tip, the transition in its message. */
function reflogEntry(
  git: Git,
  previous: string,
  ref: string,
  old: string | undefined,
  next: string,
  at: Date,
): string {
  return commitTree(
    git,
    EMPTY_TREE,
    [previous, next],
    `sheaf: ${ref}\n\n${ref} ${old ?? ZERO_OID} ${next}\n`,
    identity(SHEAF, at),
  );
}

/** The tree of `base` (none: the empty tree) with `files` written over it, built through a scratch
 *  index so a path may name directories. */
function writeTree(
  git: Git,
  scratch: string,
  base: string | undefined,
  files: SeedFiles,
): string {
  const index = { GIT_INDEX_FILE: path.join(scratch, "agent.index") };
  if (base === undefined) git(["read-tree", "--empty"], undefined, index);
  else git(["read-tree", base], undefined, index);
  for (const [filePath, file] of Object.entries(files)) {
    if (file === null) {
      if (!holdsFile(git, base, filePath)) {
        throw new Error(
          `the agent deletes ${filePath}, which the tip does not hold`,
        );
      }
      // Mode 0 removes the entry; --force-remove would need a work tree.
      git(
        ["update-index", "--index-info"],
        `0 ${"0".repeat(40)}\t${filePath}\n`,
        index,
      );
      continue;
    }
    const { content, mode } =
      typeof file === "string" || file instanceof Uint8Array
        ? { content: file, mode: "100644" }
        : file;
    const blob = git(["hash-object", "-w", "--stdin"], content);
    git(
      ["update-index", "--add", "--cacheinfo", `${mode},${blob},${filePath}`],
      undefined,
      index,
    );
  }
  return git(["write-tree"], undefined, index);
}

/** The history `commits` make, oldest first, as its writers' publishes would have left it: publish
 *  `i` commits `commits[i]`'s files over the previous commit's tree on the collaborative branch, as
 *  its author. Commit `i` is dated `i` minutes after `authoredAt`.
 *
 *  Raises when `commits` is empty or one writes no file, or when `git` is missing or fails. */
export function seededHistory(
  commits: readonly SeedCommit[],
  authoredAt: Date,
): SeedPublish[] {
  if (commits.length === 0) {
    throw new Error("a seeded history publishes at least one commit");
  }
  if (commits.some((commit) => Object.keys(commit.files).length === 0)) {
    throw new Error("a seeded commit writes at least one file");
  }
  const scratch = mkdtempSync(path.join(tmpdir(), "themis-fixture-workspace-"));
  try {
    const git: Git = (args, input, env) =>
      runGit(scratch, args, input, env).toString("utf8").trim();
    git(["init", "--bare", "--quiet", "--object-format=sha1", scratch]);
    git(["hash-object", "-w", "-t", "tree", "--stdin"]);
    const publishes: SeedPublish[] = [];
    let tip: string | undefined;
    let reflog: string | undefined;
    for (const [index, seeded] of commits.entries()) {
      const at = new Date(authoredAt.getTime() + index * 60_000);
      const tree = writeTree(git, scratch, tip, seeded.files);
      const commit = commitTree(
        git,
        tree,
        tip === undefined ? [] : [tip],
        seeded.message,
        identity(seeded.author, at),
      );
      // A repository's first entry is parented on a parentless root sheaf wrote, so a first-parent
      // walk of the chain ends on sheaf's own commit.
      const previous =
        reflog ??
        commitTree(git, EMPTY_TREE, [], "sheaf: init", identity(SHEAF, at));
      const entry = reflogEntry(
        git,
        previous,
        COLLABORATIVE_BRANCH,
        tip,
        commit,
        at,
      );
      // `--revs` from the new entry, less everything the previous one reaches: exactly the objects
      // this publish adds, the root entry included on the first.
      const pack = new Uint8Array(
        runGit(
          scratch,
          ["pack-objects", "--revs", "--stdout", "--quiet"],
          reflog === undefined ? `${entry}\n` : `${entry}\n^${reflog}\n`,
        ),
      );
      publishes.push({
        refUpdates: {
          [COLLABORATIVE_BRANCH]: { old: tip, new: commit },
          [REFLOG_REF]: { old: reflog, new: entry },
        },
        pack,
      });
      tip = commit;
      reflog = entry;
    }
    return publishes;
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

/** What a repository holds before an agent's next publish: its packs, the tip of the branch the
 *  publish moves and the reflog's, each absent before that ref's first publish. */
export interface RepositoryState {
  packs: readonly Uint8Array[];
  branch: string;
  tip: string | undefined;
  reflog: string | undefined;
}

/** The agent's next publish on `state`: one commit on `state.branch`'s tip writing `files` over the
 *  tip's tree, dated `at`, as a real agent's pull-then-push would leave it. Built in a scratch
 *  repository holding `state`'s packs, so it builds on whatever landed last, a curator's commit
 *  included.
 *
 *  Raises when `files` is empty, when it deletes a file the tip does not hold, or when `git` is
 *  missing or fails. */
export function agentPublish(
  state: RepositoryState,
  files: SeedFiles,
  at: Date,
): SeedPublish {
  if (Object.keys(files).length === 0) {
    throw new Error("an agent publish writes at least one file");
  }
  const scratch = mkdtempSync(path.join(tmpdir(), "themis-fixture-workspace-"));
  try {
    const git: Git = (args, input, env) =>
      runGit(scratch, args, input, env).toString("utf8").trim();
    git(["init", "--bare", "--quiet", "--object-format=sha1", scratch]);
    git(["hash-object", "-w", "-t", "tree", "--stdin"]);
    for (const pack of state.packs) {
      runGit(scratch, ["index-pack", "--stdin"], pack);
    }
    const tree = writeTree(git, scratch, state.tip, files);
    const commit = commitTree(
      git,
      tree,
      state.tip === undefined ? [] : [state.tip],
      publishTitle(files),
      identity(AGENT, at),
    );
    const previous =
      state.reflog ??
      commitTree(git, EMPTY_TREE, [], "sheaf: init", identity(SHEAF, at));
    const entry = reflogEntry(
      git,
      previous,
      state.branch,
      state.tip,
      commit,
      at,
    );
    const pack = new Uint8Array(
      runGit(
        scratch,
        ["pack-objects", "--revs", "--stdout", "--quiet"],
        state.reflog === undefined
          ? `${entry}\n`
          : `${entry}\n^${state.reflog}\n`,
      ),
    );
    return {
      refUpdates: {
        [state.branch]: { old: state.tip, new: commit },
        [REFLOG_REF]: { old: state.reflog, new: entry },
      },
      pack,
    };
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}
