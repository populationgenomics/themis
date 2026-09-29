// Git's object formats, written byte for byte as `git mktree` and `git commit-tree` write them, so
// every tree and commit the browser's copy of a workspace repository creates is one git accepts
// (docs/design/workbench-workspace.md, "A curator's edit is a local commit").

const ENCODER = new TextEncoder();
const DECODER = new TextDecoder("utf-8", { fatal: true });

/** A tree's mode for a subdirectory, as git writes it (no leading zero). */
export const TREE_MODE = "40000";
/** The file modes a tree entry may carry besides a subdirectory's. */
export const FILE_MODES = ["100644", "100755", "120000", "160000"] as const;

/** The id git gives the tree with no entries, which every reflog entry names. */
export const EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";

const OBJECT_ID = /^[0-9a-f]{40}$/;

/** One entry of a tree: a name as git stores it (bytes, not necessarily UTF-8), a mode, an id. */
export interface TreeEntry {
  mode: string;
  name: Uint8Array;
  oid: string;
}

export function isTreeMode(mode: string): boolean {
  return mode === TREE_MODE || mode === "040000";
}

export function requireObjectId(oid: string, what: string): string {
  if (!OBJECT_ID.test(oid)) {
    throw new Error(`${what} is not an object id: ${JSON.stringify(oid)}`);
  }
  return oid;
}

export function utf8(text: string): Uint8Array {
  return ENCODER.encode(text);
}

/** Decode bytes that must be UTF-8; raises on any invalid sequence rather than substituting. */
export function decodeUtf8(bytes: Uint8Array, what: string): string {
  try {
    return DECODER.decode(bytes);
  } catch (error) {
    throw new Error(`${what} is not valid UTF-8`, { cause: error });
  }
}

/** Git's order of tree entries: their names' bytes compared as unsigned, a subdirectory's name as
 *  if followed by `/`. JavaScript's string comparison orders UTF-16 code units instead, which
 *  disagrees once a supplementary-plane character sits beside one near the top of the BMP. */
export function compareEntries(a: TreeEntry, b: TreeEntry): number {
  const length = Math.min(a.name.length, b.name.length);
  for (let i = 0; i < length; i += 1) {
    if (a.name[i] !== b.name[i]) return a.name[i] - b.name[i];
  }
  return terminator(a, length) - terminator(b, length);
}

function terminator(entry: TreeEntry, at: number): number {
  if (at < entry.name.length) return entry.name[at];
  return isTreeMode(entry.mode) ? 0x2f : 0;
}

/** A tree's content, entries in git's order. Raises on a name git cannot hold in a tree (empty,
 *  `.` or `..`, holding `/` or NUL), a mode git does not write, a malformed id, or two entries of
 *  one name. */
export function serializeTree(entries: readonly TreeEntry[]): Uint8Array {
  const names = new Set<string>();
  const parts: Uint8Array[] = [];
  for (const entry of [...entries].sort(compareEntries)) {
    validateEntry(entry);
    const key = bytesHex(entry.name);
    if (names.has(key)) {
      throw new Error(
        `a tree holds two entries named ${JSON.stringify(entryName(entry))}`,
      );
    }
    names.add(key);
    parts.push(utf8(`${entry.mode} `), entry.name, Uint8Array.of(0));
    parts.push(hexBytes(entry.oid));
  }
  return concat(parts);
}

/** A tree's entries, in the order stored. Raises on content that is not a tree. */
export function parseTree(content: Uint8Array): TreeEntry[] {
  const entries: TreeEntry[] = [];
  let at = 0;
  while (at < content.length) {
    const space = content.indexOf(0x20, at);
    const nul = content.indexOf(0, space + 1);
    if (space < 0 || nul < 0 || nul + 21 > content.length) {
      throw new Error(`a tree entry at byte ${at} is truncated`);
    }
    const mode = new TextDecoder().decode(content.subarray(at, space));
    const name = content.slice(space + 1, nul);
    const oid = bytesHex(content.subarray(nul + 1, nul + 21));
    entries.push({ mode, name, oid });
    at = nul + 21;
  }
  return entries;
}

function validateEntry(entry: TreeEntry): void {
  const name = entryName(entry);
  if (
    !isTreeMode(entry.mode) &&
    !(FILE_MODES as readonly string[]).includes(entry.mode)
  ) {
    throw new Error(
      `${JSON.stringify(name)} has a mode git does not write: ${entry.mode}`,
    );
  }
  if (entry.name.length === 0)
    throw new Error("a tree entry has an empty name");
  if (
    name === "." ||
    name === ".." ||
    entry.name.includes(0x2f) ||
    entry.name.includes(0)
  ) {
    throw new Error(`${JSON.stringify(name)} is not a name a tree can hold`);
  }
  requireObjectId(entry.oid, `the entry ${JSON.stringify(name)}`);
}

function entryName(entry: TreeEntry): string {
  return new TextDecoder().decode(entry.name);
}

/** Who made a commit and when, as a commit's author or committer line records it. */
export interface Signature {
  name: string;
  email: string;
  /** Seconds since the epoch. */
  timestamp: number;
}

/** A commit to write: its tree, its parents in order, its author and committer, its message. */
export interface CommitFields {
  tree: string;
  parents: readonly string[];
  author: Signature;
  committer: Signature;
  message: string;
}

/** A commit's content as `git commit-tree -m <message>` writes it: times in UTC, and the message
 *  given a final newline if it lacks one. Raises on a signature git would alter or refuse. */
export function serializeCommit(commit: CommitFields): Uint8Array {
  const lines = [`tree ${requireObjectId(commit.tree, "the commit's tree")}`];
  for (const parent of commit.parents) {
    lines.push(`parent ${requireObjectId(parent, "a commit's parent")}`);
  }
  lines.push(`author ${signatureLine(commit.author)}`);
  lines.push(`committer ${signatureLine(commit.committer)}`);
  const message = commit.message.endsWith("\n")
    ? commit.message
    : `${commit.message}\n`;
  return utf8(`${lines.join("\n")}\n\n${message}`);
}

function signatureLine(signature: Signature): string {
  for (const [field, value] of [
    ["name", signature.name],
    ["email", signature.email],
  ] as const) {
    // git strips surrounding whitespace and "crud" and refuses `<`, `>` and newlines; a value it
    // would rewrite is refused here, so the stored line is the one given.
    if (value === "" || value !== value.trim() || /[<>\n\0]/.test(value)) {
      throw new Error(
        `a commit signature's ${field} git would rewrite: ${JSON.stringify(value)}`,
      );
    }
  }
  if (!Number.isSafeInteger(signature.timestamp) || signature.timestamp < 0) {
    throw new Error(
      `a commit signature's time is not whole seconds: ${signature.timestamp}`,
    );
  }
  return `${signature.name} <${signature.email}> ${signature.timestamp} +0000`;
}

/** A commit's parts as stored: the headers `git commit-tree` writes, and the message. */
export interface ParsedCommit {
  tree: string;
  parents: string[];
  committerTimestamp: number;
  /** Decoded for display: bytes that are not UTF-8 read as U+FFFD, since a commit's message may be
   *  in any encoding its writer chose. */
  message: string;
}

const LOSSY = new TextDecoder("utf-8");
/** Bytes git defines as ASCII, as text. Raises on a byte outside ASCII: `TextDecoder("ascii")`
 *  would read one as windows-1252 instead. */
function ascii(bytes: Uint8Array, what: string): string {
  let text = "";
  for (const byte of bytes) {
    if (byte > 0x7f) throw new Error(`${what} is not ASCII`);
    text += String.fromCharCode(byte);
  }
  return text;
}

/** Parse a commit's content from its bytes. Only the headers git defines as ASCII are decoded as
 *  text; a name or message in another encoding does not make the commit unreadable. Raises on
 *  content that is not a commit. */
export function parseCommit(content: Uint8Array, oid: string): ParsedCommit {
  const split = headerEnd(content);
  if (split < 0) throw new Error(`commit ${oid} has no message`);
  let tree: string | undefined;
  const parents: string[] = [];
  let committerTimestamp: number | undefined;
  for (const line of lines(content.subarray(0, split))) {
    // A continuation line (a signature's) starts with a space and belongs to the header above.
    if (line.length === 0 || line[0] === 0x20) continue;
    const space = line.indexOf(0x20);
    if (space < 0) throw new Error(`commit ${oid} has a malformed header`);
    const key = ascii(line.subarray(0, space), `a header of commit ${oid}`);
    const value = line.subarray(space + 1);
    if (key === "tree") {
      tree = requireObjectId(
        ascii(value, `commit ${oid}'s tree`),
        `commit ${oid}'s tree`,
      );
    } else if (key === "parent") {
      parents.push(
        requireObjectId(
          ascii(value, `a parent of ${oid}`),
          `a parent of ${oid}`,
        ),
      );
    } else if (key === "committer") {
      committerTimestamp = signatureTime(value, oid);
    }
  }
  if (tree === undefined || committerTimestamp === undefined) {
    throw new Error(`commit ${oid} lacks a tree or a committer`);
  }
  return {
    tree,
    parents,
    committerTimestamp,
    message: LOSSY.decode(content.subarray(split + 2)),
  };
}

function headerEnd(content: Uint8Array): number {
  for (let i = 0; i + 1 < content.length; i += 1) {
    if (content[i] === 0x0a && content[i + 1] === 0x0a) return i;
  }
  return -1;
}

function lines(bytes: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = [];
  let start = 0;
  for (let i = 0; i <= bytes.length; i += 1) {
    if (i === bytes.length || bytes[i] === 0x0a) {
      out.push(bytes.subarray(start, i));
      start = i + 1;
    }
  }
  return out;
}

/** The time of a signature line's value, `<name> <<email>> <seconds> <zone>`: read after the last
 *  `>`, so the name's and email's bytes are never decoded. */
function signatureTime(value: Uint8Array, oid: string): number {
  const close = value.lastIndexOf(0x3e);
  const match = /^ (\d+) [+-]\d{4}$/.exec(
    ascii(value.subarray(close + 1), `commit ${oid}'s committer time`),
  );
  if (close < 0 || match === null) {
    throw new Error(`commit ${oid} has a malformed committer line`);
  }
  return Number(match[1]);
}

function hexBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

export function bytesHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

export function concat(parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(
    parts.reduce((total, part) => total + part.length, 0),
  );
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}
