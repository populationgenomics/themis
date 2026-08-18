// A key-value store in the browser, over one IndexedDB object store. Values are structured-cloned,
// so a caller stores the value it holds rather than a string, and a read returns `unknown` because a
// persisted record is untrusted input whatever wrote it.
//
// Every failure resolves rather than rejects: a blocked store (private mode, storage disabled, a
// version conflict with another tab) reads as absent and drops a write. Callers persist UI
// preferences, never load-bearing state, so a failure has to fall back to their default rather than
// take the surface down.

const DB_NAME = "themis";
const DB_VERSION = 1;
const STORE = "kv";

// One connection for the page: the arrangement is read on mount and written on every divider drag,
// and reopening per operation would serialise each behind a fresh handshake.
let connection: Promise<IDBDatabase | null> | null = null;

function connect(): Promise<IDBDatabase | null> {
  if (connection) return connection;
  let request: IDBOpenDBRequest;
  try {
    request = indexedDB.open(DB_NAME, DB_VERSION);
  } catch {
    // Opened outside the executor below: that executor runs during the `connection = new Promise(…)`
    // assignment, so clearing the cache from inside it would be undone by the assignment itself.
    return Promise.resolve(null);
  }
  connection = new Promise((resolve) => {
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE);
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      // Close on another tab's upgrade rather than blocking it; the next call reconnects.
      db.onversionchange = () => {
        db.close();
        connection = null;
      };
      resolve(db);
    };
    // A failed open is not cached: it can be transient, or another tab holding an older version
    // open, and a cached null would silently stop persisting for the life of the page.
    request.onerror = () => {
      connection = null;
      resolve(null);
    };
    request.onblocked = () => {
      connection = null;
      resolve(null);
    };
  });
  return connection;
}

/** The stored value for `key`, or null when absent or unreadable. Untyped by design — narrow it with
 *  a validator rather than asserting a type onto whatever the store returns. */
export async function readStore(key: string): Promise<unknown> {
  const db = await connect();
  if (!db) return null;
  return new Promise((resolve) => {
    try {
      const request = db
        .transaction(STORE, "readonly")
        .objectStore(STORE)
        .get(key);
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}

/** Store `value` under `key`. Resolves whether or not the write landed. */
export async function writeStore(key: string, value: unknown): Promise<void> {
  const db = await connect();
  if (!db) return;
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    } catch {
      resolve();
    }
  });
}
