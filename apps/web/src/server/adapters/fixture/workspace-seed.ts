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

/** One publish of the agent's history: the refs it moves and the pack it carries. */
export interface SeedPublish {
  refUpdates: Record<string, { old?: string; new: string }>;
  pack: Uint8Array;
}

const EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
const ZERO_OID = "0".repeat(40);
const AGENT = { name: "Themis agent", email: "agent@themis.invalid" };
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

type Git = (args: readonly string[], input?: string, env?: GitEnv) => string;

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

/** The agent's history for `documents`, oldest first: publish `i` commits `documents[i]` as the
 *  working document on the collaborative branch. Commit `i` is dated `i` minutes after
 *  `authoredAt`.
 *
 *  Raises when `documents` is empty, or when `git` is missing or fails. */
export function agentHistory(
  documents: readonly string[],
  authoredAt: Date,
): SeedPublish[] {
  if (documents.length === 0) {
    throw new Error("an agent history publishes at least one document");
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
    for (const [index, markdown] of documents.entries()) {
      const at = new Date(authoredAt.getTime() + index * 60_000);
      const blob = git(["hash-object", "-w", "--stdin"], markdown);
      const tree = git(
        ["mktree"],
        `100644 blob ${blob}\t${WORKING_DOCUMENT_PATH}\n`,
      );
      const commit = commitTree(
        git,
        tree,
        tip === undefined ? [] : [tip],
        `Working document, version ${index + 1}`,
        identity(AGENT, at),
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

/** What an agent publish writes at a path: a regular file's content, content with the mode its
 *  tree entry gets, or null to delete the file there. */
export type AgentFile = string | { content: string; mode: string } | null;

/** The agent's next publish on `state`: one commit on `state.branch`'s tip writing `files` over the
 *  tip's tree, dated `at`, as a real agent's pull-then-push would leave it. Built in a scratch
 *  repository holding `state`'s packs, so it builds on whatever landed last, a curator's commit
 *  included.
 *
 *  Raises when `files` is empty, when it deletes a file the tip does not hold, or when `git` is
 *  missing or fails. */
export function agentPublish(
  state: RepositoryState,
  files: Readonly<Record<string, AgentFile>>,
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
    const index = { GIT_INDEX_FILE: path.join(scratch, "agent.index") };
    if (state.tip !== undefined)
      git(["read-tree", state.tip], undefined, index);
    for (const [filePath, content] of Object.entries(files)) {
      if (content === null) {
        if (!holdsFile(git, state.tip, filePath)) {
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
      const { content: bytes, mode } =
        typeof content === "string" ? { content, mode: "100644" } : content;
      const blob = git(["hash-object", "-w", "--stdin"], bytes);
      git(
        ["update-index", "--add", "--cacheinfo", `${mode},${blob},${filePath}`],
        undefined,
        index,
      );
    }
    const tree = git(["write-tree"], undefined, index);
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
