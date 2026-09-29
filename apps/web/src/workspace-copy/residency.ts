// How much of the browser the copies may hold. Neither isomorphic-git nor LightningFS expires
// anything, so the SharedWorker records each copy's size and when it was last read, refuses a copy
// over the per-copy ceiling before downloading any of it, and past the budget deletes the least
// recently read copies whole (docs/design/workbench-workspace.md, "Where things are stored").

/** The bytes every copy together may hold before the least recently read are evicted. */
export const STORAGE_BUDGET_BYTES = 512 * 1024 * 1024;
/** The largest one copy may be. Indexing a pack peaks at several times its size in memory — a
 *  116 MB pack peaked near 680 MB in Chrome — so this bounds that peak as much as the storage. */
export const COPY_CEILING_BYTES = 128 * 1024 * 1024;

/** One copy as the ledger records it. */
export interface CopyRecord {
  analysisId: string;
  bytes: number;
  lastReadMs: number;
}

/** Where the records live: an IndexedDB store in the SharedWorker, a map in tests. A record is
 *  written before its copy's storage is created, so storage without a record is an orphan. */
export interface Ledger {
  list(): Promise<CopyRecord[]>;
  get(analysisId: string): Promise<CopyRecord | undefined>;
  put(record: CopyRecord): Promise<void>;
  remove(analysisId: string): Promise<void>;
}

export class CopyTooLargeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CopyTooLargeError";
  }
}

export interface Limits {
  budgetBytes: number;
  ceilingBytes: number;
}

/** Deletes another copy whole if nothing is using it; resolves whether it did. */
export type Evict = (analysisId: string) => Promise<boolean>;

export class Residency {
  constructor(
    private readonly ledger: Ledger,
    private readonly evict: Evict,
    private readonly limits: Limits = {
      budgetBytes: STORAGE_BUDGET_BYTES,
      ceilingBytes: COPY_CEILING_BYTES,
    },
    private readonly now: () => number = Date.now,
  ) {}

  /** Record that `analysisId`'s copy was read now, creating its record if it has none. */
  async touch(analysisId: string): Promise<void> {
    const record = await this.ledger.get(analysisId);
    await this.ledger.put({
      analysisId,
      bytes: record?.bytes ?? 0,
      lastReadMs: this.now(),
    });
  }

  async recordSize(analysisId: string, bytes: number): Promise<void> {
    const record = await this.ledger.get(analysisId);
    if (record === undefined)
      throw new Error(`no ledger record for ${analysisId}'s copy`);
    await this.ledger.put({ ...record, bytes });
  }

  /** Admit `analysisId`'s copy at `bytes`, before it grows to that: refuse it over the ceiling,
   *  else make room for it. */
  async admit(analysisId: string, bytes: number): Promise<void> {
    if (bytes > this.limits.ceilingBytes) {
      throw new CopyTooLargeError(
        `the workspace needs ${bytes} bytes, over the browser's ${this.limits.ceilingBytes}-byte ceiling for one copy`,
      );
    }
    await this.makeRoom(analysisId, bytes);
  }

  /** Evict the least recently read copies other than `analysisId`'s until every copy fits the
   *  budget beside one of `bytes`. A copy in use is skipped, so the budget can be exceeded while
   *  one is. */
  async makeRoom(analysisId: string, bytes: number): Promise<void> {
    const others = (await this.ledger.list())
      .filter((record) => record.analysisId !== analysisId)
      .sort((a, b) => a.lastReadMs - b.lastReadMs);
    let total = bytes + others.reduce((sum, record) => sum + record.bytes, 0);
    for (const record of others) {
      if (total <= this.limits.budgetBytes) return;
      if (await this.evict(record.analysisId)) total -= record.bytes;
    }
  }
}
