import {
  type CommitFields,
  EMPTY_TREE,
  requireObjectId,
  type Signature,
} from "./git-objects";

// The reflog ref, as sheaf writes it (themis/sheaf/wire/reflog.py): one commit per publish, parented
// on the previous entry and each new tip, with an empty tree and the transitions in its message. The
// browser writes entries for a curator's publish and reads them for the version picker, so both
// directions reproduce sheaf's format exactly.

export const REFLOG_REF = "refs/sheaf/reflog";

const ZERO = "0".repeat(40);
const SUMMARY_PREFIX = "sheaf: ";
const ROOT_SUBJECT = "sheaf: init";
const SHEAF = { name: "sheaf", email: "sheaf@localhost" } as const;

/** One ref moving in a publish; `old` absent for a ref the publish creates. */
export interface Transition {
  ref: string;
  old: string | undefined;
  new: string;
}

/** Where the browser writes the objects an entry needs. */
export interface CommitWriter {
  /** Store the empty tree, which every entry names. */
  emptyTree(): Promise<void>;
  /** Store a commit and return its id. */
  commit(fields: CommitFields): Promise<string>;
}

/** The objects one publish's entry adds: the entry, and on a repository's first publish the root
 *  entry and the empty tree it is parented on. */
export interface RecordedEntry {
  entry: string;
  /** Every object written that the store does not hold yet, the entry included. */
  added: string[];
}

/**
 * Write the reflog entry for one publish, as sheaf's `record` does.
 *
 * Args: `previous` is the current reflog commit, absent on a repository's first publish, when a
 * parentless root entry is written first; `timestamp` is the entry's time in seconds.
 *
 * Raises when `transitions` is empty: a publish moving no ref has nothing to log.
 */
export async function recordEntry(
  writer: CommitWriter,
  previous: string | undefined,
  transitions: readonly Transition[],
  timestamp: number,
): Promise<RecordedEntry> {
  if (transitions.length === 0) {
    throw new Error("a reflog entry records at least one ref transition");
  }
  const sheaf: Signature = { ...SHEAF, timestamp };
  const added: string[] = [];
  await writer.emptyTree();
  let base = previous;
  if (base === undefined) {
    added.push(EMPTY_TREE);
    base = await writer.commit(entryFields([], ROOT_SUBJECT, sheaf));
    added.push(base);
  }
  const parents = [base];
  for (const transition of transitions) {
    requireObjectId(transition.new, `${transition.ref}'s new tip`);
    if (!parents.includes(transition.new)) parents.push(transition.new);
  }
  const summary = transitions.map((t) => t.ref).join(", ");
  const body = transitions
    .map((t) => `${t.ref} ${t.old ?? ZERO} ${t.new}\n`)
    .join("");
  const entry = await writer.commit(
    entryFields(parents, `${SUMMARY_PREFIX}${summary}\n\n${body}`, sheaf),
  );
  added.push(entry);
  return { entry, added };
}

function entryFields(
  parents: readonly string[],
  message: string,
  sheaf: Signature,
): CommitFields {
  return {
    tree: EMPTY_TREE,
    parents,
    author: sheaf,
    committer: sheaf,
    message,
  };
}

/** What one commit on the reflog chain records: a publish's transitions, or the chain's root. */
export type ReflogRecord =
  | { kind: "root" }
  | { kind: "entry"; transitions: Transition[] };

/**
 * Read one commit of the reflog chain, as sheaf's `read` does. Raises for a commit sheaf did not
 * write: the chain is sheaf's own, so that is damage, not something to skip past.
 */
export function readRecord(
  oid: string,
  parents: readonly string[],
  message: string,
): ReflogRecord {
  const lines = message.split("\n").filter((line) => line !== "");
  if (parents.length === 0) {
    if (lines.length !== 1 || lines[0] !== ROOT_SUBJECT) {
      throw new Error(
        `the reflog chain ends on a commit sheaf did not write: ${oid}`,
      );
    }
    return { kind: "root" };
  }
  if (lines.length < 2 || !lines[0].startsWith(SUMMARY_PREFIX)) {
    throw new Error(`not a reflog entry: ${oid}`);
  }
  const transitions = lines.slice(1).map((line) => {
    const parts = line.split(" ");
    if (parts.length !== 3 || !parts[0].startsWith("refs/")) {
      throw new Error(
        `not a reflog entry line in ${oid}: ${JSON.stringify(line)}`,
      );
    }
    const [ref, old, next] = parts;
    return {
      ref,
      old: old === ZERO ? undefined : requireObjectId(old, `${oid}'s old tip`),
      new: requireObjectId(next, `${oid}'s new tip`),
    };
  });
  return { kind: "entry", transitions };
}
