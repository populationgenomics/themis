import {
  COLLABORATIVE_BRANCH,
  type CopyStorage,
  type FileAtCommit,
  type RecordedTip,
  WorkspaceCopy,
} from "./copy";
import { decodeUtf8, requireObjectId } from "./git-objects";
import { type Hydrated, hydrate } from "./hydrate";
import type { CopyLocks, LockMode } from "./locks";
import type { CopyRequest, CopyResults, EditFile } from "./protocol";
import { type EditOutcome, publishEdit, requireEditPaths } from "./publish";
import { PublishRefusedError, type Remote } from "./remote";
import { type Ledger, type Limits, Residency } from "./residency";

// Everything the SharedWorker does, with its surroundings injected: the storage each copy lives in,
// the relay each copy hydrates and publishes through, the ledger eviction reads, and the lock every
// use of a copy holds. One instance owns every copy one build's SharedWorker holds; the lock keeps it
// apart from another build's.

/** The file the working document is at a commit (docs/design/workbench-workspace.md). */
export const WORKING_DOCUMENT_PATH = "working_document.md";

/** How long a copy nothing has used keeps its open storage and isomorphic-git's cache, which holds
 *  every pack it has read in memory. A window rendering the copy reads it only when the tip or the
 *  pin moves, so an open but idle workbench also lets it go and pays one re-read later. */
export const RELEASE_AFTER_IDLE_MS = 5 * 60 * 1000;

/** Where copies live: one storage per Analysis, opened, deleted and listed whole. */
export interface CopyStorageFactory {
  open(analysisId: string): Promise<CopyStorage>;
  remove(analysisId: string): Promise<void>;
  /** The Analyses whose copies have storage, whether or not the ledger records them. */
  list(): Promise<string[]>;
}

/** Raises unless writing `after` at `path` over `before`, the file there at an edit's base, is an edit
 *  a curator may make. */
export type EditCheck = (
  path: string,
  before: FileAtCommit | undefined,
  after: Uint8Array,
) => void;

export interface CopyServiceOptions {
  storage: CopyStorageFactory;
  /** Run on every file a publish replaces, against the file at the edit's base, before the edit's
   *  commit is built or anything is sent; the publish stores the bytes as given, so the check refuses any it would not store as
   *  they are, such as bytes not in the one encoding every reader reads alike. The base is enough: an
   *  edit lands on a moved tip only where the file there is the base's. */
  checkEdit: EditCheck;
  remote: (analysisId: string) => Remote;
  ledger: Ledger;
  locks: CopyLocks;
  limits?: Limits;
  now?: () => number;
  releaseAfterIdleMs?: number;
}

/** A commit the copy does not hold. Transient: another worker may have evicted the copy since it was
 *  brought to the tip, and the next sync downloads it again. */
export class CommitNotInCopyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CommitNotInCopyError";
  }
}

const EMAIL = /^[^\s<>@]+@[^\s<>@]+$/;

/** Whether the copy's branch is at `tip` or has moved past it. */
async function holdsAtOrPast(
  copy: WorkspaceCopy,
  tip: string,
): Promise<boolean> {
  const branch = await copy.ref(COLLABORATIVE_BRANCH);
  if (branch === undefined || !(await copy.hasObject(tip))) return false;
  return copy.isAncestor(tip, branch);
}

function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

interface Resident {
  copy: Promise<WorkspaceCopy>;
  /** Uses in flight in this worker; the resident is released only once none is. */
  active: number;
  release?: ReturnType<typeof setTimeout>;
}

export class CopyService {
  private readonly residents = new Map<string, Resident>();
  private readonly residency: Residency;
  private readonly now: () => number;

  constructor(private readonly options: CopyServiceOptions) {
    this.now = options.now ?? Date.now;
    this.residency = new Residency(
      options.ledger,
      (analysisId) => this.evict(analysisId),
      options.limits,
      this.now,
    );
  }

  /** Delete storage the ledger does not record, and records whose storage is gone: a copy's record
   *  is written before its storage, so storage without one was never finished. Each under the
   *  copy's lock, skipped if another worker holds it. */
  async reconcile(): Promise<void> {
    const stored = new Set(await this.options.storage.list());
    const recorded = new Set(
      (await this.options.ledger.list()).map((r) => r.analysisId),
    );
    for (const analysisId of stored) {
      if (recorded.has(analysisId)) continue;
      await this.options.locks.holdIfFree(analysisId, () =>
        this.options.storage.remove(analysisId),
      );
    }
    for (const analysisId of recorded) {
      if (stored.has(analysisId)) continue;
      await this.options.locks.holdIfFree(analysisId, () =>
        this.options.ledger.remove(analysisId),
      );
    }
  }

  handle(request: CopyRequest): Promise<CopyResults[CopyRequest["method"]]> {
    switch (request.method) {
      case "ping":
        return Promise.resolve(undefined);
      case "sync":
        return this.sync(request.analysisId, request.tip);
      case "readDocument":
        return this.readDocument(request.analysisId, request.commit);
      case "readFile":
        return this.readFile(request.analysisId, request.commit, request.path);
      case "history":
        return this.history(request.analysisId, request.tip);
      case "reset":
        return this.reset(request.analysisId);
      case "isAncestor":
        return this.isAncestor(
          request.analysisId,
          request.ancestor,
          request.descendant,
        );
      case "publish":
        return this.publish(
          request.analysisId,
          request.base,
          request.curatorEmail,
          request.message,
          request.files,
        );
    }
  }

  /**
   * Bring the copy to the current document unless its branch is already at `tip` or past it, which
   * a read under the shared lock decides. A pending commit found here is one no publish is working
   * on — every publish holds the lock exclusively — left by a worker lost mid-publish, and is
   * cleared: its outcome is not recovered, and the next Poll shows the tip it moved to if it
   * landed. Raises when the copy does not hold `tip` afterwards: a tip the Poll reported is never
   * unreachable.
   */
  async sync(analysisId: string, tip: string): Promise<undefined> {
    requireObjectId(tip, "the tip to sync to");
    const current = await this.use(
      analysisId,
      "shared",
      async (copy) =>
        (await copy.pending()) === undefined &&
        (await holdsAtOrPast(copy, tip)),
    ).catch(
      // Whatever the shared read could not decide — a copy another worker deleted, a pack a crash
      // left unreadable — the exclusive path below repairs or reports.
      () => false,
    );
    if (current) return undefined;
    await this.use(analysisId, "exclusive", async (copy) => {
      await copy.clearPending();
      if (!(await holdsAtOrPast(copy, tip)))
        await this.hydrate(analysisId, copy);
      if (!(await copy.hasObject(tip))) {
        throw new Error(
          `the copy of ${analysisId} does not hold the tip ${tip} the Poll reported`,
        );
      }
    });
    return undefined;
  }

  async readDocument(
    analysisId: string,
    commit: string,
  ): Promise<string | null> {
    const file = await this.readFile(analysisId, commit, WORKING_DOCUMENT_PATH);
    return file === null
      ? null
      : decodeUtf8(file.bytes, `${WORKING_DOCUMENT_PATH} at ${commit}`);
  }

  async readFile(
    analysisId: string,
    commit: string,
    path: string,
  ): Promise<FileAtCommit | null> {
    requireObjectId(commit, "the commit to read");
    return this.use(analysisId, "shared", async (copy) => {
      if (!(await copy.hasObject(commit))) {
        throw new CommitNotInCopyError(
          `the copy of ${analysisId} does not hold ${commit}`,
        );
      }
      return (await copy.readFile(commit, path)) ?? null;
    });
  }

  /** The tips the reflog recorded, read once the copy's branch is at `tip` or past it. Raises
   *  `CommitNotInCopyError` otherwise: another worker may have evicted or be re-hydrating the copy
   *  since its sync, and a list read from it would be cached as the branch's history. */
  async history(analysisId: string, tip: string): Promise<RecordedTip[]> {
    requireObjectId(tip, "the tip the history is read at");
    return this.use(analysisId, "shared", async (copy) => {
      if (!(await holdsAtOrPast(copy, tip))) {
        throw new CommitNotInCopyError(
          `the copy of ${analysisId} is not at or past ${tip}`,
        );
      }
      return copy.recordedTips(COLLABORATIVE_BRANCH);
    });
  }

  /** Whether `ancestor` is `descendant` or reachable from it. Raises `CommitNotInCopyError` unless
   *  the copy holds both. */
  async isAncestor(
    analysisId: string,
    ancestor: string,
    descendant: string,
  ): Promise<boolean> {
    requireObjectId(ancestor, "the ancestor");
    requireObjectId(descendant, "the descendant");
    return this.use(analysisId, "shared", async (copy) => {
      for (const commit of [ancestor, descendant]) {
        if (!(await copy.hasObject(commit))) {
          throw new CommitNotInCopyError(
            `the copy of ${analysisId} does not hold ${commit}`,
          );
        }
      }
      return copy.isAncestor(ancestor, descendant);
    });
  }

  /** Publish an edit authored and committed as `curatorEmail`: the email the BFF verified for the
   *  window's page, which the window passes on and nothing else supplies. The first publish goes out
   *  against the document the copy last followed, with no read of the current one, unless the copy
   *  records none or its branch has not reached `base`. `files` holds each file
   *  the edit replaces, with the bytes the window made from it at `base`. Raises before taking the
   *  copy's lock on a malformed email, message or path; under it, before anything is sent, when the
   *  copy's branch does not reach `base` once it follows the document, or `checkEdit` refuses a
   *  file. */
  async publish(
    analysisId: string,
    base: string,
    curatorEmail: string,
    message: string,
    files: readonly EditFile[],
  ): Promise<EditOutcome> {
    if (!EMAIL.test(curatorEmail) || hasControlCharacter(curatorEmail)) {
      throw new Error(
        `a curator's commit needs their email, not ${JSON.stringify(curatorEmail)}`,
      );
    }
    if (message === "" || message.includes("\0")) {
      throw new Error(
        `a commit message is non-empty text with no NUL, not ${JSON.stringify(message)}`,
      );
    }
    requireEditPaths(files.map((file) => file.path));
    const replaced = new Map(files.map((file) => [file.path, file.bytes]));
    if (replaced.size !== files.length) {
      throw new Error("an edit replaces each file once");
    }
    return this.use(analysisId, "exclusive", async (copy) => {
      const rehydrate = () => this.hydrate(analysisId, copy);
      // The document the copy last followed is published against as it is: a service that has
      // moved on refuses the publish, and the refusal brings the copy up to date.
      const known = await copy.documentState();
      const current =
        known !== undefined && (await holdsAtOrPast(copy, base))
          ? known
          : await rehydrate();
      if (!(await holdsAtOrPast(copy, base))) {
        throw new Error(
          `the copy of ${analysisId} does not hold ${base} on its branch`,
        );
      }
      for (const file of files) {
        this.options.checkEdit(
          file.path,
          await copy.readFile(base, file.path),
          file.bytes,
        );
      }
      try {
        return await publishEdit(
          copy,
          this.options.remote(analysisId),
          rehydrate,
          current,
          {
            base,
            // IAP verifies an email and no name, so the email is both.
            curator: { name: curatorEmail, email: curatorEmail },
            message,
            files: replaced,
          },
          this.now,
        );
      } catch (error) {
        // A publish refused as malformed can come from a recorded state that no longer matches the
        // refs beside it; the next publish reads the document rather than send it again.
        if (
          error instanceof PublishRefusedError &&
          error.refusal === "malformed"
        ) {
          await copy.forgetDocumentState();
        }
        throw error;
      }
    });
  }

  private async hydrate(
    analysisId: string,
    copy: WorkspaceCopy,
  ): Promise<Hydrated> {
    const hydrated = await hydrate(
      copy,
      this.options.remote(analysisId),
      { admit: (bytes) => this.residency.admit(analysisId, bytes) },
      this.now,
    );
    // Indexing adds to what admission counted, so the budget is checked again at the real size.
    const bytes = await copy.size();
    await this.residency.recordSize(analysisId, bytes);
    await this.residency.makeRoom(analysisId, bytes);
    return hydrated;
  }

  /** Run `task` on `analysisId`'s copy holding its lock in `mode`. A write first recreates a copy
   *  another worker may have deleted while this one did not hold the lock. */
  private use<T>(
    analysisId: string,
    mode: LockMode,
    task: (copy: WorkspaceCopy) => Promise<T>,
  ): Promise<T> {
    return this.options.locks.hold(analysisId, mode, async () => {
      const resident = this.resident(analysisId);
      resident.active += 1;
      clearTimeout(resident.release);
      try {
        const copy = await resident.copy;
        if (mode === "exclusive") await copy.ensure();
        await this.residency.touch(analysisId);
        return await task(copy);
      } finally {
        resident.active -= 1;
        this.scheduleRelease(analysisId, resident);
      }
    });
  }

  private scheduleRelease(analysisId: string, resident: Resident): void {
    if (resident.active > 0) return;
    resident.release = setTimeout(() => {
      if (
        resident.active === 0 &&
        this.residents.get(analysisId) === resident
      ) {
        this.residents.delete(analysisId);
      }
    }, this.options.releaseAfterIdleMs ?? RELEASE_AFTER_IDLE_MS);
  }

  private resident(analysisId: string): Resident {
    const existing = this.residents.get(analysisId);
    if (existing !== undefined) return existing;
    const resident: Resident = {
      // The ledger record comes first, so storage the ledger does not name is always an orphan.
      copy: this.residency
        .touch(analysisId)
        .then(() => this.options.storage.open(analysisId))
        .then((storage) => WorkspaceCopy.attach(storage)),
      active: 0,
    };
    // A copy that failed to open is dropped, so the next request tries again from nothing.
    resident.copy.catch(() => {
      if (this.residents.get(analysisId) === resident)
        this.residents.delete(analysisId);
    });
    this.residents.set(analysisId, resident);
    return resident;
  }

  /** Delete `analysisId`'s copy whole at a curator's request, waiting for its lock, in this worker
   *  or another, rather than skipping a copy in use as eviction does. The next read hydrates from
   *  nothing; a pending commit goes with the copy. */
  async reset(analysisId: string): Promise<undefined> {
    await this.options.locks.hold(analysisId, "exclusive", () =>
      this.remove(analysisId),
    );
    return undefined;
  }

  /** Delete `analysisId`'s copy whole, unless something holds its lock, in this worker or another. */
  private evict(analysisId: string): Promise<boolean> {
    return this.options.locks.holdIfFree(analysisId, () =>
      this.remove(analysisId),
    );
  }

  /** Delete `analysisId`'s copy, its storage and its ledger record, under its exclusive lock. */
  private async remove(analysisId: string): Promise<void> {
    const resident = this.residents.get(analysisId);
    if (resident !== undefined) {
      clearTimeout(resident.release);
      this.residents.delete(analysisId);
    }
    await this.options.storage.remove(analysisId);
    await this.options.ledger.remove(analysisId);
  }
}
