import LightningFS from "@isomorphic-git/lightning-fs";
import type { CopyFsPromises, CopyStorage } from "./copy";
import type { CopyRecord, Ledger } from "./residency";
import type { CopyStorageFactory } from "./service";

// Where the SharedWorker keeps the copies: one LightningFS database per Analysis, so an eviction
// deletes a copy whole, and a ledger of their sizes and last reads in a database of its own. Both are
// load-bearing for the copy, so unlike `lib/browser-store.ts` a failure here rejects.

const COPY_PREFIX = "themis-copy:";
/** LightningFS's lock database, which it opens beside a copy's where `navigator.locks` is absent. */
const LOCK_SUFFIX = "_lock";
const GITDIR = "/repository";

const LEDGER_DB = "themis-copies";
const LEDGER_VERSION = 1;
const LEDGER_STORE = "copies";

function databaseName(analysisId: string): string {
  return `${COPY_PREFIX}${encodeURIComponent(analysisId)}`;
}

export function lightningStorage(): CopyStorageFactory {
  return {
    async open(analysisId) {
      const fs = new LightningFS(databaseName(analysisId));
      const storage: CopyStorage = {
        promises: fs.promises as unknown as CopyFsPromises,
        gitdir: GITDIR,
        flush: () => fs.promises.flush(),
        size: async () => (await fs.promises.du("/")) as number,
      };
      return storage;
    },
    async remove(analysisId) {
      // LightningFS closes its connection half a second after its last operation; the deletion
      // waits for that rather than failing.
      await deleteDatabase(databaseName(analysisId));
      await deleteDatabase(`${databaseName(analysisId)}${LOCK_SUFFIX}`);
    },
    async list() {
      const databases = await indexedDB.databases();
      return databases
        .map((database) => database.name)
        .filter(
          (name): name is string =>
            name?.startsWith(COPY_PREFIX) === true &&
            !name.endsWith(LOCK_SUFFIX),
        )
        .map((name) => decodeURIComponent(name.slice(COPY_PREFIX.length)));
    },
  };
}

function deleteDatabase(name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () =>
      reject(request.error ?? new Error(`could not delete ${name}`));
  });
}

export function indexedDbLedger(): Ledger {
  let connection: Promise<IDBDatabase> | undefined;
  const database = (): Promise<IDBDatabase> => {
    connection ??= new Promise((resolve, reject) => {
      const request = indexedDB.open(LEDGER_DB, LEDGER_VERSION);
      request.onupgradeneeded = () => {
        request.result.createObjectStore(LEDGER_STORE, {
          keyPath: "analysisId",
        });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => {
        connection = undefined;
        reject(request.error ?? new Error("could not open the copy ledger"));
      };
      request.onblocked = () => {
        connection = undefined;
        reject(new Error("the copy ledger is held open at an older version"));
      };
    });
    return connection;
  };
  const run = async <T>(
    mode: IDBTransactionMode,
    operation: (store: IDBObjectStore) => IDBRequest<T>,
  ): Promise<T> => {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(LEDGER_STORE, mode);
      const request = operation(transaction.objectStore(LEDGER_STORE));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () =>
        reject(transaction.error ?? new Error("a ledger write failed"));
      transaction.onabort = () =>
        reject(transaction.error ?? new Error("a ledger write aborted"));
    });
  };
  return {
    list: async () =>
      (await run("readonly", (store) => store.getAll())).map(record),
    get: async (analysisId) => {
      const found: unknown = await run("readonly", (store) =>
        store.get(analysisId),
      );
      return found === undefined ? undefined : record(found);
    },
    put: async (entry) => {
      await run("readwrite", (store) => store.put(entry));
    },
    remove: async (analysisId) => {
      await run("readwrite", (store) => store.delete(analysisId));
    },
  };
}

/** A stored record, checked: what IndexedDB returns is untrusted, whatever wrote it. */
function record(value: unknown): CopyRecord {
  const { analysisId, bytes, lastReadMs } = (value ??
    {}) as Partial<CopyRecord>;
  if (
    typeof analysisId !== "string" ||
    typeof bytes !== "number" ||
    typeof lastReadMs !== "number"
  ) {
    throw new Error(`a malformed copy ledger record: ${JSON.stringify(value)}`);
  }
  return { analysisId, bytes, lastReadMs };
}
