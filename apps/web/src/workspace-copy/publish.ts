import { create } from "@bufbuild/protobuf";
import { type PublishIntent, PublishIntentSchema } from "@/models/sheaf";
import { COLLABORATIVE_BRANCH, splitPath, type WorkspaceCopy } from "./copy";
import { isTreeMode, type TreeEntry, utf8 } from "./git-objects";
import type { Hydrated } from "./hydrate";
import { sha256Hex } from "./pack";
import {
  PUBLISH_DAMAGED_AFTER_UNKNOWN,
  PUBLISH_OUTCOME_UNKNOWN,
} from "./protocol";
import { REFLOG_REF, recordEntry } from "./reflog";
import {
  PublishFaultError,
  PublishRefusedError,
  type Remote,
  WorkspaceDamagedError,
} from "./remote";

// A curator's edit as a commit on the collaborative branch, published with the store's own
// compare-and-swap (docs/design/workbench-workspace.md, "A curator's edit is a local commit" and
// "A curator's edit is rebased onto the tip, file by file"). A lost race rebuilds the commit on the
// new tip, with the curator's files swapped into its tree, while those files are there as the
// curator saw them.

/** How many publishes one edit may send before it is reported as not saved. */
export const PUBLISH_ATTEMPTS = 6;

/** Who the commit is authored and committed by: the curator the BFF verified. */
export interface Curator {
  name: string;
  email: string;
}

export interface Edit {
  /** The commit the curator's view was drawn from, where the files the edit replaces are as the
   *  curator saw them. */
  base: string;
  curator: Curator;
  /** The commit's message. */
  message: string;
  /** The new bytes of each file the edit replaces, keyed by repository path, as the curator's
   *  change made them from the file at `base`. A replaced file keeps its mode; a new one is a
   *  regular file. */
  files: ReadonlyMap<string, Uint8Array>;
}

/** How an edit ended. `landed` names the commit now on the branch that carries it; `unchanged`
 *  means the tip already holds the edit's bytes, so no commit was made. `fileChanged` means the
 *  branch is at `commit`, where the file at `path`, one the edit replaces, is not as it was at the
 *  edit's base: a different blob or mode, there on one side only, or blocked by a file along its
 *  path. The edit was not applied, so it cannot land on content the curator never saw; they redo it
 *  on what `commit` holds. */
export type EditOutcome =
  | { kind: "landed"; commit: string; generation: bigint }
  | { kind: "unchanged"; commit: string }
  | { kind: "fileChanged"; commit: string; path: string };

/** The edit's publishes were used up without one landing. */
export class RetryBudgetSpentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RetryBudgetSpentError";
  }
}

/** The edit's last publish went unanswered, and the document did not show it landed: it may yet,
 *  so the edit is neither landed nor refused, and sending it again could apply it twice. */
export class PublishOutcomeUnknownError extends Error {
  constructor(message: string, options?: { cause: unknown }) {
    super(message, options);
    this.name = PUBLISH_OUTCOME_UNKNOWN;
  }
}

/** The repository was found damaged after a send of the edit went unanswered: the edit may have
 *  landed before the damage, and nothing sent or read can settle it. */
export class PublishDamagedAfterUnknownError extends Error {
  constructor(message: string, options: { cause: WorkspaceDamagedError }) {
    super(message, options);
    this.name = PUBLISH_DAMAGED_AFTER_UNKNOWN;
  }
}

/** Brings the copy to the current document under the caller's lock, and returns it. */
export type Rehydrate = () => Promise<Hydrated>;

interface Prepared {
  commit: string;
  parent: string;
  intent: PublishIntent;
  pack: Uint8Array;
  packId: string;
}

/**
 * Commit `edit` on the collaborative branch and publish it. The first commit is built on the
 * branch's tip in `hydrated`, which is `edit.base` unless the copy has moved past it; each lost
 * race is answered as the design lists, within `PUBLISH_ATTEMPTS` publishes. The edit is built on a
 * tip other than `edit.base` only while every file it replaces is the same there, and otherwise
 * ends as `fileChanged`.
 *
 * The caller holds the copy's lock for the whole call and passes the document the copy was just
 * brought to. Raises before any read for an edit that replaces no file or names a path a
 * repository cannot hold. Raises `PublishRefusedError` for an edit over a ceiling or malformed,
 * which is never resent; `WorkspaceDamagedError` as soon as a publish, or a read of the document
 * before the edit is known to have landed, finds the repository damaged, with nothing sent again,
 * and `PublishDamagedAfterUnknownError` in its place when a send of the edit went unanswered first;
 * `PublishOutcomeUnknownError` when the attempts run out with the last one unanswered and not
 * shown landed; `RetryBudgetSpentError` when they run out otherwise; and any other failure as it
 * came.
 */
export async function publishEdit(
  copy: WorkspaceCopy,
  remote: Remote,
  rehydrate: Rehydrate,
  hydrated: Hydrated,
  edit: Edit,
  now: () => number = Date.now,
): Promise<EditOutcome> {
  let state = hydrated;
  let prepared: Prepared | undefined;
  let resend = false;
  requireEditPaths([...edit.files.keys()]);
  await requireOnBranch(copy, edit.base, state);
  try {
    for (let attempt = 1; attempt <= PUBLISH_ATTEMPTS; attempt += 1) {
      const tip = state.refs.get(COLLABORATIVE_BRANCH);
      if (tip === undefined) {
        throw new Error(
          `${COLLABORATIVE_BRANCH} has no commit to build an edit on`,
        );
      }
      if (!resend) {
        let commit = prepared?.parent === tip ? prepared.commit : undefined;
        if (commit === undefined) {
          const changed = await changedSinceBase(copy, edit, tip);
          if (changed !== undefined)
            return { kind: "fileChanged", commit: tip, path: changed };
          commit = await buildCommit(copy, edit, tip, now);
          if (commit === undefined) return { kind: "unchanged", commit: tip };
        }
        prepared = await prepare(copy, state, tip, commit, now);
        await copy.setPending(commit);
      }
      if (prepared === undefined)
        throw new Error("unreachable: a resend follows a prepared publish");
      const resent = resend;
      resend = false;
      let generation: bigint;
      try {
        generation = await remote.publish(prepared.intent, prepared.pack);
      } catch (error) {
        if (error instanceof PublishFaultError) {
          resend = true;
          continue;
        }
        // Ahead of a resend's failure becoming an unknown outcome: damage ends the edit either way.
        if (error instanceof WorkspaceDamagedError)
          throw damaged(error, resent, prepared.commit);
        if (!(error instanceof PublishRefusedError)) {
          // A resend's failure says nothing of the publish before it, which went unanswered.
          if (resent) {
            throw new PublishOutcomeUnknownError(
              `${prepared.commit} was published and never answered, and its resend failed`,
              { cause: error },
            );
          }
          throw error;
        }
        if (error.refusal === "raceLost" || error.refusal === "branchMoved") {
          try {
            state = await rehydrate();
          } catch (failure) {
            // After a refused resend, only the document says whether the send before it landed.
            if (failure instanceof WorkspaceDamagedError)
              throw damaged(failure, resent, prepared.commit);
            throw failure;
          }
          const moved = state.refs.get(COLLABORATIVE_BRANCH);
          if (
            moved !== undefined &&
            (await copy.isAncestor(prepared.commit, moved))
          ) {
            return {
              kind: "landed",
              commit: prepared.commit,
              generation: state.generation,
            };
          }
          continue;
        }
        throw error;
      }
      if (resent) {
        // A resend the service answered as landed returns the current generation, whose document
        // may carry later publishes, so the copy reads it rather than assume it. The edit landed
        // either way: a copy that cannot be brought up to date now is brought there by its next
        // sync, and reporting the edit as failed would have the curator make it twice.
        try {
          state = await rehydrate();
        } catch (error) {
          console.error(
            `${prepared.commit} landed; the copy could not follow the document yet`,
            error,
          );
          return { kind: "landed", commit: prepared.commit, generation };
        }
        const landed = state.refs.get(COLLABORATIVE_BRANCH);
        if (
          landed === undefined ||
          !(await copy.isAncestor(prepared.commit, landed))
        ) {
          throw new Error(
            `the service accepted ${prepared.commit}, and ${COLLABORATIVE_BRANCH} does not reach it`,
          );
        }
        return {
          kind: "landed",
          commit: prepared.commit,
          generation: state.generation,
        };
      }
      // The document the publish wrote is the one it was built against plus its own moves, and its
      // one new pack is this one, so the copy follows it without a download.
      await copy.addPack(prepared.packId, prepared.pack);
      const refs = new Map(state.refs);
      for (const [name, update] of Object.entries(prepared.intent.refUpdates)) {
        if (update.new === undefined)
          throw new Error(`the intent deletes ${name}`);
        refs.set(name, update.new);
      }
      await copy.setRefs(refs, generation);
      return { kind: "landed", commit: prepared.commit, generation };
    }
    if (resend && prepared !== undefined) {
      // The last publish's outcome is unknown; a document showing it is the one answer that settles
      // it, since a publish the service is still running may land after the document is read.
      try {
        state = await rehydrate();
      } catch (error) {
        if (error instanceof WorkspaceDamagedError)
          throw damaged(error, true, prepared.commit);
        throw new PublishOutcomeUnknownError(
          `${prepared.commit} was published and never answered, and the document could not be read`,
          { cause: error },
        );
      }
      const tip = state.refs.get(COLLABORATIVE_BRANCH);
      if (tip !== undefined && (await copy.isAncestor(prepared.commit, tip))) {
        return {
          kind: "landed",
          commit: prepared.commit,
          generation: state.generation,
        };
      }
      throw new PublishOutcomeUnknownError(
        `${prepared.commit} was published ${PUBLISH_ATTEMPTS} times, the last never answered, and the document does not show it yet`,
      );
    }
    throw new RetryBudgetSpentError(
      `the edit was published ${PUBLISH_ATTEMPTS} times without landing`,
    );
  } finally {
    await copy.clearPending();
  }
}

/** The failure the repository's damage ends an edit with: as it came, or, when `unanswered`, a send
 *  of `commit` went unanswered before it, marked so. */
function damaged(
  error: WorkspaceDamagedError,
  unanswered: boolean,
  commit: string,
): Error {
  return unanswered
    ? new PublishDamagedAfterUnknownError(
        `${commit} was published and never answered, and the repository is damaged`,
        { cause: error },
      )
    : error;
}

/** Raises unless `paths`, the files an edit replaces, name at least one file, each at a path a
 *  repository can hold, and none at a directory another of them runs through. Reads nothing. */
export function requireEditPaths(paths: readonly string[]): void {
  if (paths.length === 0) throw new Error("the edit replaces no file");
  const replaced = new Set(paths);
  for (const path of paths) {
    const components = splitPath(path);
    for (let depth = 1; depth < components.length; depth += 1) {
      const directory = components.slice(0, depth).join("/");
      if (replaced.has(directory)) {
        throw new Error(
          `the edit replaces ${directory} as a file and ${path} inside it`,
        );
      }
    }
  }
}

/** Raises unless `base` is a commit on the branch's history in `state`: an edit drawn from any
 *  other commit was drawn from something the branch never showed. */
async function requireOnBranch(
  copy: WorkspaceCopy,
  base: string,
  state: Hydrated,
): Promise<void> {
  const tip = state.refs.get(COLLABORATIVE_BRANCH);
  if (
    tip === undefined ||
    !(await copy.hasObject(base)) ||
    !(await copy.isAncestor(base, tip))
  ) {
    throw new Error(
      `the edit's base ${base} is not on ${COLLABORATIVE_BRANCH}`,
    );
  }
}

/** The first file `edit` replaces that differs between `edit.base` and `tip`: by blob or mode, by
 *  being at one of them only, or by a file along its path; undefined when none does. */
async function changedSinceBase(
  copy: WorkspaceCopy,
  edit: Edit,
  tip: string,
): Promise<string | undefined> {
  for (const path of edit.files.keys()) {
    const before = await copy.entryAlong(edit.base, path);
    const after = await copy.entryAlong(tip, path);
    if (
      before?.prefix !== after?.prefix ||
      before?.entry.oid !== after?.entry.oid ||
      before?.entry.mode !== after?.entry.mode
    ) {
      return path;
    }
  }
  return undefined;
}

/** The curator's commit on `tip`: the tip's tree with the edit's files swapped in. Undefined when
 *  the tip already holds those bytes. */
async function buildCommit(
  copy: WorkspaceCopy,
  edit: Edit,
  tip: string,
  now: () => number,
): Promise<string | undefined> {
  const base = (await copy.readCommit(tip)).tree;
  const tree = await writeChanges(copy, base, edit.files);
  if (tree === base) return undefined;
  const signature = { ...edit.curator, timestamp: seconds(now) };
  return copy.writeCommit({
    tree,
    parents: [tip],
    author: signature,
    committer: signature,
    message: edit.message,
  });
}

/** The reflog entry, pack and intent for publishing `commit` on `tip` against `state`. The pack
 *  holds exactly what the store lacks: the commit, the trees and blobs it changed, and the entry
 *  (with the root entry and the empty tree on a repository's first publish). */
async function prepare(
  copy: WorkspaceCopy,
  state: Hydrated,
  tip: string,
  commit: string,
  now: () => number,
): Promise<Prepared> {
  const previous = state.refs.get(REFLOG_REF);
  const recorded = await recordEntry(
    {
      emptyTree: () => copy.writeEmptyTree(),
      commit: (fields) => copy.writeCommit(fields),
    },
    previous,
    [{ ref: COLLABORATIVE_BRANCH, old: tip, new: commit }],
    seconds(now),
  );
  // Once each: two changed paths may hold the same bytes, and git refuses a pack naming an object
  // twice.
  const added = [
    ...new Set([...(await newObjects(copy, tip, commit)), ...recorded.added]),
  ];
  const pack = await copy.pack(added);
  const packId = await sha256Hex(pack);
  const intent = create(PublishIntentSchema, {
    baseGeneration: state.generation,
    refUpdates: {
      [COLLABORATIVE_BRANCH]: { old: tip, new: commit },
      [REFLOG_REF]: { old: previous, new: recorded.entry },
    },
    packs: [{ size: BigInt(pack.length), packId }],
  });
  return { commit, parent: tip, intent, pack, packId };
}

/** The objects `commit` reaches that its parent `tip` does not: the commit and every tree and blob
 *  whose id differs from the one at the same path in the tip. */
async function newObjects(
  copy: WorkspaceCopy,
  tip: string,
  commit: string,
): Promise<string[]> {
  const added = [commit];
  const walk = async (
    ours: string,
    theirs: string | undefined,
  ): Promise<void> => {
    if (ours === theirs) return;
    added.push(ours);
    const before = new Map<string, TreeEntry>();
    if (theirs !== undefined) {
      for (const entry of await copy.readTree(theirs))
        before.set(key(entry.name), entry);
    }
    for (const entry of await copy.readTree(ours)) {
      const old = before.get(key(entry.name));
      if (old?.oid === entry.oid) continue;
      if (isTreeMode(entry.mode)) {
        await walk(
          entry.oid,
          old !== undefined && isTreeMode(old.mode) ? old.oid : undefined,
        );
      } else if (entry.mode !== "160000") {
        added.push(entry.oid);
      }
    }
  };
  await walk(
    (await copy.readCommit(commit)).tree,
    (await copy.readCommit(tip)).tree,
  );
  return added;
}

/** Write the trees `changes` makes of the tree `base` and return the new root's id. A changed
 *  file keeps its mode; a new one is a regular file. Raises when a path runs through a file, names
 *  a directory, or names a submodule or a symlink. */
async function writeChanges(
  copy: WorkspaceCopy,
  base: string | undefined,
  changes: ReadonlyMap<string, Uint8Array>,
  prefix = "",
): Promise<string> {
  const entries = new Map<string, TreeEntry>();
  if (base !== undefined) {
    for (const entry of await copy.readTree(base))
      entries.set(key(entry.name), entry);
  }
  const files = new Map<string, Uint8Array>();
  const directories = new Map<string, Map<string, Uint8Array>>();
  for (const [path, bytes] of changes) {
    const [head, ...rest] = splitPath(path);
    if (rest.length === 0) {
      files.set(head, bytes);
    } else {
      const inner = directories.get(head) ?? new Map<string, Uint8Array>();
      inner.set(rest.join("/"), bytes);
      directories.set(head, inner);
    }
  }
  for (const [name, bytes] of files) {
    const existing = entries.get(key(utf8(name)));
    if (
      existing !== undefined &&
      (isTreeMode(existing.mode) ||
        existing.mode === "160000" ||
        existing.mode === "120000")
    ) {
      throw new Error(`${prefix}${name} is not a file an edit can write`);
    }
    if (directories.has(name)) {
      throw new Error(
        `${prefix}${name} is written as both a file and a directory`,
      );
    }
    entries.set(key(utf8(name)), {
      mode: existing?.mode ?? "100644",
      name: utf8(name),
      oid: await copy.writeBlob(bytes),
    });
  }
  for (const [name, inner] of directories) {
    const existing = entries.get(key(utf8(name)));
    if (existing !== undefined && !isTreeMode(existing.mode)) {
      throw new Error(
        `${prefix}${name} is a file, not a directory an edit can write into`,
      );
    }
    entries.set(key(utf8(name)), {
      mode: existing?.mode ?? "40000",
      name: utf8(name),
      oid: await writeChanges(copy, existing?.oid, inner, `${prefix}${name}/`),
    });
  }
  return copy.writeTree([...entries.values()]);
}

function key(name: Uint8Array): string {
  return Array.from(name, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

function seconds(now: () => number): number {
  return Math.floor(now() / 1000);
}
