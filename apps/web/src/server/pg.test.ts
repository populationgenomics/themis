import { afterEach, describe, expect, jest, spyOn, test } from "bun:test";
import { EventEmitter } from "node:events";
import type { PoolConfig } from "pg";
import { createPool, inTransaction } from "./pg";

// pg-pool under `createPool`'s settings, over a stand-in for pg's `Client`.

const MINUTE = 60 * 1000;

// Cloud Run closes an outbound connection after this long idle (run/docs/container-contract).
const EGRESS_IDLE_TIMEOUT_MS = 10 * MINUTE;

interface QueryResult {
  command: string;
  rows: unknown[];
}

/** A stand-in for pg's `Client` with the surface pg-pool drives, recording every dial. */
function fakeDriver() {
  const dialed: FakeClient[] = [];
  class FakeClient extends EventEmitter {
    _queryable = true;
    _ending = false;
    ended = false;
    readonly statements: string[] = [];

    constructor() {
      super();
      dialed.push(this);
    }

    connect(callback: (error?: Error) => void): void {
      queueMicrotask(() => callback());
    }

    /** pg-pool's `pool.query` passes a callback; a caller holding a client awaits the promise. */
    query(
      text: string,
      _values?: unknown[],
      callback?: (error: Error | undefined, result: QueryResult) => void,
    ): Promise<QueryResult> | undefined {
      this.statements.push(text);
      const result = { command: text.split(" ")[0] ?? "", rows: [{ one: 1 }] };
      if (callback === undefined) return Promise.resolve(result);
      queueMicrotask(() => callback(undefined, result));
      return undefined;
    }

    end(callback?: () => void): void {
      this._ending = true;
      this._queryable = false;
      this.ended = true;
      queueMicrotask(() => callback?.());
    }

    /** What pg's client does when the server or network drops its connection: it stops being
     *  queryable and emits 'error' synchronously. */
    fail(): void {
      this._queryable = false;
      this.emit("error", new Error("Connection terminated unexpectedly"));
    }
  }
  const connection = {
    Client: FakeClient as unknown as PoolConfig["Client"],
  };
  return { connection, dialed };
}

afterEach(() => {
  jest.useRealTimers();
});

describe("the Cloud SQL pool", () => {
  test("is bounded, and its idle lifetime and timeouts sit in the ranges the traffic needs", async () => {
    const pool = createPool(fakeDriver().connection);
    // Cloud Run caps an instance at 100 connections to a Cloud SQL database.
    expect(pool.options.max).toBeLessThanOrEqual(100);
    // A floor would hold connections the idle timeout cannot close.
    expect(pool.options.min).toBe(0);
    expect(pool.options.idleTimeoutMillis).toBeGreaterThan(MINUTE);
    expect(pool.options.idleTimeoutMillis).toBeLessThan(EGRESS_IDLE_TIMEOUT_MS);
    // The client gives up only after the server's own cancellation would have arrived.
    expect(pool.options.query_timeout).toBeGreaterThan(
      Number(pool.options.statement_timeout),
    );
    await pool.end();
  });

  test("reuses one connection across a minute's pause, and closes it before the egress timeout", async () => {
    jest.useFakeTimers();
    const { connection, dialed } = fakeDriver();
    const pool = createPool(connection);

    await pool.query("SELECT 1");
    jest.advanceTimersByTime(MINUTE);
    await pool.query("SELECT 1");
    expect(dialed).toHaveLength(1);

    jest.advanceTimersByTime(EGRESS_IDLE_TIMEOUT_MS - 1);
    expect(dialed[0]?.ended).toBe(true);
    expect(pool.totalCount).toBe(0);
    await pool.end();
  });

  test("an idle connection's failure is logged, and the next query dials a fresh one", async () => {
    const logged = spyOn(console, "error").mockImplementation(() => {});
    const { connection, dialed } = fakeDriver();
    const pool = createPool(connection);
    try {
      await pool.query("SELECT 1");
      const idle = dialed[0];
      if (idle === undefined) {
        throw new Error("the first query dialed no client");
      }

      expect(() => idle.fail()).not.toThrow();
      expect(logged).toHaveBeenCalledTimes(1);
      expect(pool.totalCount).toBe(0);

      const result = await pool.query("SELECT 1");
      expect(result.rows).toEqual([{ one: 1 }]);
      expect(dialed).toHaveLength(2);
      expect(dialed[1]?.ended).toBe(false);
    } finally {
      logged.mockRestore();
      await pool.end();
    }
  });

  test("a checked-out connection's failure is logged, and releasing it discards it", async () => {
    const logged = spyOn(console, "error").mockImplementation(() => {});
    const { connection, dialed } = fakeDriver();
    const pool = createPool(connection);
    const client = await pool.connect();
    let released = false;
    try {
      const checkedOut = dialed[0];
      if (checkedOut === undefined) {
        throw new Error("the checkout dialed no client");
      }

      expect(() => checkedOut.fail()).not.toThrow();
      expect(logged).toHaveBeenCalledTimes(1);

      client.release();
      released = true;
      expect(pool.totalCount).toBe(0);
      expect(checkedOut.ended).toBe(true);
    } finally {
      logged.mockRestore();
      // pool.end() waits for every checked-out client.
      if (!released) client.release();
      await pool.end();
    }
  });

  test("a transaction commits, and one that fails is not committed and its connection is discarded", async () => {
    const { connection, dialed } = fakeDriver();
    const pool = createPool(connection);
    try {
      await inTransaction(pool, async (client) => {
        await client.query("SELECT 1");
      });
      expect(dialed[0]?.statements).toEqual(["BEGIN", "SELECT 1", "COMMIT"]);
      expect(pool.idleCount).toBe(1);

      const failed = inTransaction(pool, async () => {
        throw new Error("duplicate key value violates unique constraint");
      });
      await expect(failed).rejects.toThrow("duplicate key");
      expect(dialed[0]?.statements.slice(3)).toEqual(["BEGIN"]);
      expect(pool.totalCount).toBe(0);
      expect(dialed[0]?.ended).toBe(true);
    } finally {
      await pool.end();
    }
  });
});
