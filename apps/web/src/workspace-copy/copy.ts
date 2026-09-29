import * as git from "isomorphic-git";
import {
  type CommitFields,
  EMPTY_TREE,
  isTreeMode,
  parseCommit,
  parseTree,
  requireObjectId,
  serializeCommit,
  serializeTree,
  type TreeEntry,
  utf8,
} from "./git-objects";
import { indexObjectCount, packObjectCount } from "./pack";
import { REFLOG_REF, readRecord } from "./reflog";

// One Analysis's copy of its workspace repository: a bare git repository on a filesystem the caller
// supplies — LightningFS over IndexedDB in the SharedWorker, a directory in tests. Objects are read
// and written through isomorphic-git; trees, commits and refs are written here, byte for byte.

/** The filesystem calls the copy makes, in the shape of `fs.promises`. LightningFS's `mkdir` is
 *  not recursive, so the copy creates directories one level at a time. */
export interface CopyFsPromises {
  readFile(path: string, options?: unknown): Promise<Uint8Array | string>;
  writeFile(
    path: string,
    data: Uint8Array | string,
    options?: unknown,
  ): Promise<void>;
  unlink(path: string): Promise<void>;
  readdir(path: string): Promise<string[]>;
  mkdir(path: string): Promise<void>;
  rmdir(path: string): Promise<void>;
  stat(path: string): Promise<unknown>;
  lstat(path: string): Promise<unknown>;
  readlink(path: string): Promise<string>;
  symlink(target: string, path: string): Promise<void>;
  chmod?(path: string, mode: number): Promise<void>;
}

/** Where one copy lives. */
export interface CopyStorage {
  promises: CopyFsPromises;
  /** The git directory, absolute within `promises`. */
  gitdir: string;
  /** Make every write so far durable before anything names it: LightningFS saves its record of
   *  which files exist half a second after the last write, while overwriting a file is durable at
   *  once, so a ref written without this can name an object a crash loses. */
  flush(): Promise<void>;
  /** The bytes the copy holds. */
  size(): Promise<number>;
}

/** A file read at a commit: its bytes and the mode its tree gives it. */
export interface FileAtCommit {
  bytes: Uint8Array;
  mode: string;
}

/** One version the picker lists: a tip the reflog recorded for a branch, and when. */
export interface RecordedTip {
  commit: string;
  /** The reflog entry's committer time, in seconds: the time sheaf or the browser recorded the
   *  publish, never a time the commit's writer chose. */
  timestamp: number;
}

/** A pack the copy was handed that its index does not wholly cover. */
export class IncompletePackError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "IncompletePackError";
  }
}

/** The branch the working document lives on and a curator's edit lands on. Which branch is the
 *  collaborative one is open (docs/design/workbench-workspace.md, Open questions). */
export const COLLABORATIVE_BRANCH = "refs/heads/main";

const PACK_DIR = "objects/pack";
const PACK_NAME = /^pack-([0-9a-f]{64})\.idx$/;
const PACK_FILE = /^pack-([0-9a-f]{64})\.(?:idx|pack)$/;
/** The local-only ref holding a curator's commit while its publish is in flight; outside `refs/`
 *  so no ref a document names can collide with it. */
const PENDING = "PENDING_PUBLISH";
/** The generation of the ref document the refs were last set from, beside them and outside `refs/`.
 *  Absent while the refs are being set, so a crash never leaves it naming refs it does not match. */
const GENERATION = "THEMIS_GENERATION";

/** The ref document a copy's refs were set from: its generation, and the refs it names. */
export interface DocumentState {
  generation: bigint;
  refs: Map<string, string>;
}

export class WorkspaceCopy {
  /** isomorphic-git's per-copy cache of parsed packs. Without it each read re-reads and re-hashes
   *  every pack; after a failed hydration it is replaced, since a bad index would stay cached. */
  private cache: object = {};
  /** Whether this instance has looked for unreadable indexes yet. */
  private checked = false;

  private constructor(private readonly storage: CopyStorage) {}

  /** A copy over `storage`, writing nothing: what a reader holding the lock shared may use. A
   *  writer calls `ensure` first. */
  static attach(storage: CopyStorage): WorkspaceCopy {
    return new WorkspaceCopy(storage);
  }

  /** A copy over `storage`, made ready to write. For a caller that holds the copy alone. */
  static async open(storage: CopyStorage): Promise<WorkspaceCopy> {
    const copy = WorkspaceCopy.attach(storage);
    await copy.ensure();
    return copy;
  }

  /** Make the copy ready to write; call it holding the copy's lock exclusively. Recreates the
   *  repository's skeleton if it is gone — another worker may have evicted the database since this
   *  one last held the lock — and, the first time for this instance, drops each pack whose index a
   *  crash left unreadable. */
  async ensure(): Promise<void> {
    let present = true;
    try {
      await this.storage.promises.stat(`${this.storage.gitdir}/config`);
    } catch (error) {
      if (!isMissing(error)) throw error;
      present = false;
    }
    if (!present) {
      await mkdirs(this.storage.promises, this.storage.gitdir);
      await git.init({
        fs: this.storage,
        gitdir: this.storage.gitdir,
        bare: true,
        defaultBranch: "main",
      });
      await this.storage.flush();
      this.resetCache();
    }
    if (!this.checked) {
      await this.dropUnreadablePacks();
      this.checked = true;
    }
  }

  resetCache(): void {
    this.cache = {};
  }

  size(): Promise<number> {
    return this.storage.size();
  }

  /** The ids of the packs indexed in this copy. */
  async indexedPacks(): Promise<Set<string>> {
    const names = await this.storage.promises.readdir(this.path(PACK_DIR));
    const ids = new Set<string>();
    for (const name of names) {
      const match = PACK_NAME.exec(name);
      if (match) ids.add(match[1]);
    }
    return ids;
  }

  /**
   * Store and index one pack under the id sheaf lists it by. The pack counts as present only once
   * its index is written and holds every object the pack declares; an index that falls short is
   * removed with its pack.
   *
   * Raises `IncompletePackError` when the index holds fewer objects than the pack's header
   * declares, which isomorphic-git reports no other way.
   */
  async addPack(packId: string, bytes: Uint8Array): Promise<void> {
    const declared = packObjectCount(bytes);
    const packPath = `${PACK_DIR}/pack-${packId}.pack`;
    const indexPath = `${PACK_DIR}/pack-${packId}.idx`;
    await this.storage.promises.writeFile(this.path(packPath), bytes);
    try {
      await git.indexPack({
        fs: this.storage,
        dir: this.storage.gitdir,
        gitdir: this.storage.gitdir,
        filepath: packPath,
        cache: this.cache,
      });
      const indexed = indexObjectCount(await this.readBytes(indexPath));
      if (indexed !== declared) {
        throw new IncompletePackError(
          `pack ${packId} declares ${declared} objects and its index holds ${indexed}: it is thin or incomplete`,
        );
      }
    } catch (error) {
      await removeIfPresent(this.storage.promises, this.path(indexPath));
      await removeIfPresent(this.storage.promises, this.path(packPath));
      throw error;
    }
  }

  /** The bytes of the packs among `packIds` this copy holds. */
  async packBytes(packIds: Iterable<string>): Promise<number> {
    const held = await this.indexedPacks();
    let total = 0;
    for (const packId of packIds) {
      if (!held.has(packId)) continue;
      const stat = await this.storage.promises.stat(
        this.path(`${PACK_DIR}/pack-${packId}.pack`),
      );
      const size = (stat as { size?: unknown }).size;
      if (typeof size !== "number") {
        throw new Error(`the filesystem gave pack ${packId} no size`);
      }
      total += size;
    }
    return total;
  }

  /** Delete every pack and index the copy holds that `listed` does not name — the packs a
   *  compaction replaced, or a download never indexed — and, if any went, drop the cache that may
   *  hold them. Flushes, so no crash leaves the record of files naming a deleted one. Resolves
   *  whether anything was deleted. */
  async prunePacks(listed: ReadonlySet<string>): Promise<boolean> {
    const doomed = (await this.storage.promises.readdir(this.path(PACK_DIR)))
      .filter((name) => {
        const match = PACK_FILE.exec(name);
        return match === null || !listed.has(match[1]);
      })
      // Every index before any pack: a crash between the two then leaves a pack no index names,
      // which reads never open, rather than an index over a pack whose bytes are gone.
      .sort(
        (a, b) => Number(!a.endsWith(".idx")) - Number(!b.endsWith(".idx")),
      );
    let pruned = false;
    for (const name of doomed) {
      await this.storage.promises.unlink(this.path(`${PACK_DIR}/${name}`));
      pruned = true;
    }
    if (pruned) {
      await this.storage.flush();
      this.resetCache();
    }
    return pruned;
  }

  /** Whether the copy holds packs `listed` does not name. */
  async holdsUnlisted(listed: ReadonlySet<string>): Promise<boolean> {
    for (const packId of await this.indexedPacks()) {
      if (!listed.has(packId)) return true;
    }
    return false;
  }

  /** Delete each pack whose index has no bytes or does not parse — LightningFS can record a file
   *  whose bytes a crash lost — so the next hydration downloads it again rather than every read
   *  failing on it. Any other failure to read an index is raised, not taken for damage. */
  private async dropUnreadablePacks(): Promise<void> {
    let dropped = false;
    for (const packId of await this.indexedPacks()) {
      const indexPath = this.path(`${PACK_DIR}/pack-${packId}.idx`);
      let bytes: Uint8Array | string | null | undefined;
      try {
        bytes = await this.storage.promises.readFile(indexPath);
      } catch (error) {
        if (isMissing(error)) continue;
        throw error;
      }
      if (bytes instanceof Uint8Array && parsesAsIndex(bytes)) continue;
      await removeIfPresent(this.storage.promises, indexPath);
      await removeIfPresent(
        this.storage.promises,
        this.path(`${PACK_DIR}/pack-${packId}.pack`),
      );
      dropped = true;
    }
    if (dropped) {
      await this.storage.flush();
      this.resetCache();
    }
  }

  /** Every ref under `refs/`, by full name. */
  async refs(): Promise<Map<string, string>> {
    const refs = new Map<string, string>();
    for (const name of await this.walk("refs")) {
      refs.set(name, await this.readRefFile(name));
    }
    return refs;
  }

  async ref(name: string): Promise<string | undefined> {
    try {
      return await this.readRefFile(name);
    } catch (error) {
      if (isMissing(error)) return undefined;
      throw error;
    }
  }

  /** Make the copy's refs exactly `target`, the refs of the ref document at `generation`: every ref
   *  it names set, every other ref removed. Flushes first, so no ref names an object that is not yet
   *  durable. */
  async setRefs(
    target: ReadonlyMap<string, string>,
    generation: bigint,
  ): Promise<void> {
    for (const [name, oid] of target) {
      validateRefName(name);
      requireObjectId(oid, name);
    }
    await this.storage.flush();
    // Each step is flushed before the next, so storage that persists buffered writes in any order
    // never keeps a new generation beside refs that did not move.
    await this.forgetDocumentState();
    const current = await this.refs();
    for (const name of current.keys()) {
      if (!target.has(name)) await this.deleteRefFile(name);
    }
    for (const [name, oid] of target) {
      if (current.get(name) !== oid) await this.writeRefFile(name, oid);
    }
    await this.storage.flush();
    await this.storage.promises.writeFile(
      this.path(GENERATION),
      `${generation}\n`,
    );
  }

  /** Stop recording which ref document the refs were set from, so the next publish reads the
   *  document first. Flushed before it returns. */
  async forgetDocumentState(): Promise<void> {
    await removeIfPresent(this.storage.promises, this.path(GENERATION));
    await this.storage.flush();
  }

  /** The ref document the refs were last set from, or undefined when the copy records none: it was
   *  never brought up to date, or setting its refs was cut short. */
  async documentState(): Promise<DocumentState | undefined> {
    let text: string;
    try {
      const data = await this.storage.promises.readFile(
        this.path(GENERATION),
        "utf8",
      );
      text = typeof data === "string" ? data : new TextDecoder().decode(data);
    } catch (error) {
      if (isMissing(error)) return undefined;
      throw error;
    }
    if (!/^\d+\n$/.test(text)) {
      throw new Error(
        `${GENERATION} holds no generation: ${JSON.stringify(text)}`,
      );
    }
    return { generation: BigInt(text.trim()), refs: await this.refs() };
  }

  async pending(): Promise<string | undefined> {
    try {
      return await this.readRefFile(PENDING);
    } catch (error) {
      if (isMissing(error)) return undefined;
      throw error;
    }
  }

  /** Name `commit` as the curator's commit awaiting its publish. Flushes first. */
  async setPending(commit: string): Promise<void> {
    requireObjectId(commit, "the pending commit");
    await this.storage.flush();
    await this.storage.promises.writeFile(this.path(PENDING), `${commit}\n`);
  }

  async clearPending(): Promise<void> {
    await removeIfPresent(this.storage.promises, this.path(PENDING));
  }

  async hasObject(oid: string): Promise<boolean> {
    try {
      await git.readObject({
        fs: this.storage,
        gitdir: this.storage.gitdir,
        oid,
        format: "deflated",
        cache: this.cache,
      });
      return true;
    } catch (error) {
      // ENOENT: the repository itself is gone, as another worker's eviction leaves it.
      if (isNotFound(error) || isMissing(error)) return false;
      throw error;
    }
  }

  async readObject(
    oid: string,
    type: "blob" | "tree" | "commit",
  ): Promise<Uint8Array> {
    const read = await git.readObject({
      fs: this.storage,
      gitdir: this.storage.gitdir,
      oid,
      format: "content",
      cache: this.cache,
    });
    if (read.type !== type) {
      throw new Error(`${oid} is a ${read.type}, not a ${type}`);
    }
    return read.object as Uint8Array;
  }

  async readCommit(oid: string): Promise<ReturnType<typeof parseCommit>> {
    return parseCommit(await this.readObject(oid, "commit"), oid);
  }

  async readTree(oid: string): Promise<TreeEntry[]> {
    return parseTree(await this.readObject(oid, "tree"));
  }

  /** The file at `path` in `commit`, or undefined when no such file exists there. Raises when a
   *  component of the path is not a directory, or the path names one. */
  async readFile(
    commit: string,
    path: string,
  ): Promise<FileAtCommit | undefined> {
    const components = splitPath(path);
    let tree = (await this.readCommit(commit)).tree;
    for (const [depth, component] of components.entries()) {
      const name = utf8(component);
      const entry = (await this.readTree(tree)).find((e) =>
        sameBytes(e.name, name),
      );
      if (entry === undefined) return undefined;
      const last = depth === components.length - 1;
      if (!last) {
        if (!isTreeMode(entry.mode)) {
          throw new Error(
            `${components.slice(0, depth + 1).join("/")} in ${commit} is not a directory`,
          );
        }
        tree = entry.oid;
        continue;
      }
      if (isTreeMode(entry.mode)) {
        throw new Error(`${path} in ${commit} is a directory, not a file`);
      }
      if (entry.mode === "160000") {
        throw new Error(
          `${path} in ${commit} is a submodule, which the copy does not hold`,
        );
      }
      return {
        bytes: await this.readObject(entry.oid, "blob"),
        mode: entry.mode,
      };
    }
    throw new Error("unreachable: a path has at least one component");
  }

  /** Where the walk down `path` in `commit` stops, with the prefix of `path` it stops at: the entry
   *  `path` names, whatever its kind, or the file or submodule the path runs through. Undefined
   *  when a component names nothing. */
  async entryAlong(
    commit: string,
    path: string,
  ): Promise<{ prefix: string; entry: TreeEntry } | undefined> {
    const components = splitPath(path);
    let tree = (await this.readCommit(commit)).tree;
    for (const [depth, component] of components.entries()) {
      const name = utf8(component);
      const entry = (await this.readTree(tree)).find((e) =>
        sameBytes(e.name, name),
      );
      if (entry === undefined) return undefined;
      if (depth === components.length - 1 || !isTreeMode(entry.mode)) {
        return { prefix: components.slice(0, depth + 1).join("/"), entry };
      }
      tree = entry.oid;
    }
    throw new Error("unreachable: a path has at least one component");
  }

  /** Every tip the reflog recorded for `branch`, newest first, each once, timed by its entry. Walks
   *  the reflog's first-parent chain to its root. Raises when a commit on the chain is not one sheaf
   *  wrote. */
  async recordedTips(branch: string): Promise<RecordedTip[]> {
    const tips: RecordedTip[] = [];
    const seen = new Set<string>();
    let at = await this.ref(REFLOG_REF);
    while (at !== undefined) {
      const entry = await this.readCommit(at);
      const record = readRecord(at, entry.parents, entry.message);
      if (record.kind === "root") break;
      for (const transition of record.transitions) {
        if (transition.ref !== branch || seen.has(transition.new)) continue;
        seen.add(transition.new);
        tips.push({
          commit: transition.new,
          timestamp: entry.committerTimestamp,
        });
      }
      at = entry.parents[0];
    }
    return tips;
  }

  /** Whether `ancestor` is `descendant` or reachable from it. */
  async isAncestor(ancestor: string, descendant: string): Promise<boolean> {
    if (ancestor === descendant) return true;
    return git.isDescendent({
      fs: this.storage,
      gitdir: this.storage.gitdir,
      oid: descendant,
      ancestor,
      depth: -1,
      cache: this.cache,
    });
  }

  writeBlob(bytes: Uint8Array): Promise<string> {
    return this.writeObject("blob", bytes);
  }

  /** Store a tree serialized in git's own entry order; isomorphic-git's tree writer orders entries
   *  as JavaScript strings, which git's `fsck` rejects for some names. */
  writeTree(entries: readonly TreeEntry[]): Promise<string> {
    return this.writeObject("tree", serializeTree(entries));
  }

  async writeEmptyTree(): Promise<void> {
    const oid = await this.writeTree([]);
    if (oid !== EMPTY_TREE) throw new Error(`the empty tree hashed to ${oid}`);
  }

  writeCommit(fields: CommitFields): Promise<string> {
    return this.writeObject("commit", serializeCommit(fields));
  }

  /** A pack of exactly `oids`, each stored whole: no object in it is a delta against another. */
  async pack(oids: readonly string[]): Promise<Uint8Array> {
    const { packfile } = await git.packObjects({
      fs: this.storage,
      gitdir: this.storage.gitdir,
      oids: [...oids],
      cache: this.cache,
    });
    if (packfile === undefined) throw new Error("isomorphic-git wrote no pack");
    return packfile;
  }

  private writeObject(
    type: "blob" | "tree" | "commit",
    content: Uint8Array,
  ): Promise<string> {
    return git.writeObject({
      fs: this.storage,
      gitdir: this.storage.gitdir,
      type,
      object: content,
      format: "content",
    });
  }

  private path(relative: string): string {
    return `${this.storage.gitdir}/${relative}`;
  }

  private async readBytes(relative: string): Promise<Uint8Array> {
    const data = await this.storage.promises.readFile(this.path(relative));
    if (typeof data === "string")
      throw new Error(`${relative} read back as text`);
    return data;
  }

  private async readRefFile(name: string): Promise<string> {
    const data = await this.storage.promises.readFile(this.path(name), "utf8");
    const text =
      typeof data === "string" ? data : new TextDecoder().decode(data);
    return requireObjectId(text.trim(), name);
  }

  private async writeRefFile(name: string, oid: string): Promise<void> {
    const parts = name.split("/");
    await mkdirs(
      this.storage.promises,
      this.path(parts.slice(0, -1).join("/")),
    );
    await this.storage.promises.writeFile(this.path(name), `${oid}\n`);
  }

  /** Remove a ref and every directory its removal leaves empty, so a later ref may take a removed
   *  directory's name. */
  private async deleteRefFile(name: string): Promise<void> {
    await this.storage.promises.unlink(this.path(name));
    const parts = name.split("/");
    for (let depth = parts.length - 1; depth > 1; depth -= 1) {
      const dir = this.path(parts.slice(0, depth).join("/"));
      if ((await this.storage.promises.readdir(dir)).length > 0) return;
      await this.storage.promises.rmdir(dir);
    }
  }

  private async walk(relative: string): Promise<string[]> {
    let names: string[];
    try {
      names = await this.storage.promises.readdir(this.path(relative));
    } catch (error) {
      if (isMissing(error)) return [];
      throw error;
    }
    const found: string[] = [];
    for (const name of names.sort()) {
      const child = `${relative}/${name}`;
      const children = await this.readdirIfDirectory(child);
      if (children === undefined) found.push(child);
      else found.push(...(await this.walk(child)));
    }
    return found;
  }

  private async readdirIfDirectory(
    relative: string,
  ): Promise<string[] | undefined> {
    try {
      return await this.storage.promises.readdir(this.path(relative));
    } catch (error) {
      if (isNotDirectory(error)) return undefined;
      throw error;
    }
  }
}

/** A repository path's components. Raises on a path git or isomorphic-git cannot hold: empty
 *  components, `.`, `..`, `.git`, a backslash or a NUL. */
export function splitPath(path: string): string[] {
  const components = path.split("/");
  for (const component of components) {
    if (
      component === "" ||
      component === "." ||
      component === ".." ||
      component.toLowerCase() === ".git" ||
      component.includes("\\") ||
      component.includes("\0")
    ) {
      throw new Error(`not a repository path: ${JSON.stringify(path)}`);
    }
  }
  return components;
}

/** The subset of `git check-ref-format` a document's refs keep to (themis/sheaf/refdoc.py). */
function validateRefName(name: string): void {
  if (
    !name.startsWith("refs/") ||
    name
      .split("/")
      .some(
        (part) => part === "" || part.startsWith(".") || part.endsWith(".lock"),
      ) ||
    /\.\.|[:?[\\^~ \t\n*]|@\{/.test(name)
  ) {
    throw new Error(`not a ref name the copy writes: ${JSON.stringify(name)}`);
  }
}

async function mkdirs(fs: CopyFsPromises, path: string): Promise<void> {
  let at = "";
  for (const part of path.split("/")) {
    at =
      at === "" && part === ""
        ? "/"
        : at === "/"
          ? `/${part}`
          : `${at}/${part}`;
    if (part === "") continue;
    try {
      await fs.mkdir(at);
    } catch (error) {
      if (!isExisting(error)) throw error;
    }
  }
}

async function removeIfPresent(
  fs: CopyFsPromises,
  path: string,
): Promise<void> {
  try {
    await fs.unlink(path);
  } catch (error) {
    if (!isMissing(error)) throw error;
  }
}

function parsesAsIndex(bytes: Uint8Array): boolean {
  try {
    indexObjectCount(bytes);
    return true;
  } catch {
    return false;
  }
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, i) => byte === b[i]);
}

function errorCode(error: unknown): unknown {
  return typeof error === "object" && error !== null
    ? (error as { code?: unknown }).code
    : undefined;
}

function isMissing(error: unknown): boolean {
  return errorCode(error) === "ENOENT";
}

function isExisting(error: unknown): boolean {
  return errorCode(error) === "EEXIST";
}

function isNotDirectory(error: unknown): boolean {
  return errorCode(error) === "ENOTDIR";
}

function isNotFound(error: unknown): boolean {
  return errorCode(error) === "NotFoundError";
}
