import { afterEach, describe, expect, jest, spyOn, test } from "bun:test";
import { once } from "node:events";
import { Duplex } from "node:stream";
import type { Pool, PoolClient } from "pg";
import {
  postgresUrl,
  SKIP_WITHOUT_POSTGRES,
} from "./migrated-postgres.test-support";
import { createPool, inTransaction } from "./pg";

// `createPool` and `inTransaction` over real pg clients: a stand-in socket that plays a Postgres
// server which accepts the login and then goes silent, and a real server named by
// `THEMIS_TEST_PG_URL` (required in CI, which provides one; skipped elsewhere without it).

function message(type: string, body: Buffer): Buffer {
  const header = Buffer.alloc(5);
  header.write(type, 0, "ascii");
  header.writeInt32BE(body.length + 4, 1);
  return Buffer.concat([header, body]);
}

const AUTHENTICATION_OK = message("R", Buffer.from([0, 0, 0, 0]));
const READY_FOR_QUERY = message("Z", Buffer.from("I"));

/** A socket to a server that answers the login (unless `answersLogin` is false) and nothing after. */
class SilentServer extends Duplex {
  private greeted = false;

  constructor(private readonly answersLogin: boolean) {
    super();
  }

  connect(): this {
    queueMicrotask(() => this.emit("connect"));
    return this;
  }

  setNoDelay(): this {
    return this;
  }

  setKeepAlive(): this {
    return this;
  }

  override _read(): void {}

  override _write(
    _chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    if (this.answersLogin && !this.greeted) {
      this.greeted = true;
      this.push(Buffer.concat([AUTHENTICATION_OK, READY_FOR_QUERY]));
    }
    callback();
  }

  override _final(callback: (error?: Error | null) => void): void {
    callback();
    this.destroy();
  }
}

function silentServers(answersLogin: boolean) {
  const dialed: SilentServer[] = [];
  const stream = () => {
    const socket = new SilentServer(answersLogin);
    dialed.push(socket);
    return socket;
  };
  return { stream, dialed };
}

/** Let the sockets' and clients' pending events run; the fake timers leave setImmediate real. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** The outcome `promise` has reached so far: its rejection, "resolved", or "pending". */
function outcomeOf<T>(promise: Promise<T>): { current: unknown } {
  const outcome: { current: unknown } = { current: "pending" };
  promise.then(
    () => {
      outcome.current = "resolved";
    },
    (error: unknown) => {
      outcome.current = error;
    },
  );
  return outcome;
}

/** The pool's setting `name`, which must be set: an unset timeout would make these tests vacuous. */
function required(value: number | undefined, name: string): number {
  if (value === undefined || value <= 0) {
    throw new Error(`the pool sets no ${name}`);
  }
  return value;
}

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe("the Cloud SQL pool's waits", () => {
  test("a checkout with every connection taken rejects after connectionTimeoutMillis", async () => {
    jest.useFakeTimers();
    const { stream } = silentServers(true);
    const pool = createPool({ stream });
    const timeout = required(
      pool.options.connectionTimeoutMillis,
      "connectionTimeoutMillis",
    );
    const held = await Promise.all(
      Array.from({ length: pool.options.max }, () => pool.connect()),
    );

    const waiting = outcomeOf(pool.connect());
    jest.advanceTimersByTime(timeout);
    await settle();
    expect(waiting.current).toBeInstanceOf(Error);
    expect((waiting.current as Error).message).toBe(
      "timeout exceeded when trying to connect",
    );

    for (const client of held) client.release();
    await pool.end();
  });

  test("a dial the server never answers rejects after connectionTimeoutMillis", async () => {
    jest.useFakeTimers();
    const { stream, dialed } = silentServers(false);
    const pool = createPool({ stream });
    const timeout = required(
      pool.options.connectionTimeoutMillis,
      "connectionTimeoutMillis",
    );

    const dialing = outcomeOf(pool.connect());
    await settle();
    jest.advanceTimersByTime(timeout);
    await settle();
    expect(dialing.current).toBeInstanceOf(Error);
    expect((dialing.current as Error).message).toBe(
      "Connection terminated due to connection timeout",
    );
    expect(dialed[0]?.destroyed).toBe(true);
    expect(pool.totalCount).toBe(0);
    await pool.end();
  });

  test("a query that gets no reply rejects after query_timeout, and its connection is discarded", async () => {
    jest.useFakeTimers();
    const { stream, dialed } = silentServers(true);
    const pool = createPool({ stream });
    const timeout = required(pool.options.query_timeout, "query_timeout");

    const reading = outcomeOf(pool.query("SELECT 1"));
    await settle();
    jest.advanceTimersByTime(timeout);
    await settle();
    expect(reading.current).toBeInstanceOf(Error);
    expect((reading.current as Error).message).toBe("Query read timeout");
    expect(dialed[0]?.destroyed).toBe(true);
    expect(pool.totalCount).toBe(0);

    const next = await pool.connect();
    expect(dialed).toHaveLength(2);
    next.release();
    await pool.end();
  });
  test("a transaction whose statement gets no reply rejects after query_timeout, and its connection is discarded", async () => {
    jest.useFakeTimers();
    const { stream, dialed } = silentServers(true);
    const pool = createPool({ stream });
    const timeout = required(pool.options.query_timeout, "query_timeout");

    const writing = outcomeOf(
      inTransaction(pool, async (client) => {
        await client.query("SELECT 1");
      }),
    );
    await settle();
    jest.advanceTimersByTime(timeout);
    await settle();
    expect(writing.current).toBeInstanceOf(Error);
    expect((writing.current as Error).message).toBe("Query read timeout");
    expect(dialed[0]?.destroyed).toBe(true);
    expect(pool.totalCount).toBe(0);
    await pool.end();
  });
});

describe.skipIf(SKIP_WITHOUT_POSTGRES)("against Postgres", () => {
  test("reuses one connection across a minute's pause, and closes it before the egress timeout", async () => {
    const url = postgresUrl();
    jest.useFakeTimers();
    const pool = createPool({ connectionString: url });
    let reapTimer: ReturnType<typeof setTimeout> | undefined;
    try {
      const first = await pool.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      expect(first.rows[0]?.pid).toBeDefined();
      jest.advanceTimersByTime(60 * 1000);
      const second = await pool.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      expect(second.rows[0]?.pid).toBe(first.rows[0]?.pid);

      // `remove` fires after `client.end()` finishes closing the reaped connection's socket.
      const abort = new AbortController();
      const removed = once(pool, "remove", { signal: abort.signal });
      // Cloud Run closes an outbound connection after 10 minutes idle (run/docs/container-contract).
      jest.advanceTimersByTime(10 * 60 * 1000 - 1);
      expect(pool.totalCount).toBe(0);
      jest.useRealTimers();
      reapTimer = setTimeout(
        () =>
          abort.abort(
            new Error("timed out waiting for reaped client to finish end()"),
          ),
        5_000,
      );
      await removed;
    } finally {
      clearTimeout(reapTimer);
      jest.useRealTimers();
      await pool.end();
    }
  });

  test("an idle connection's failure is logged once, and the next query dials a fresh one", async () => {
    const url = postgresUrl();
    let pool: Pool | undefined;
    let admin: Pool | undefined;
    const logged = spyOn(console, "error").mockImplementation(() => {});
    try {
      pool = createPool({ connectionString: url });
      admin = createPool({ connectionString: url });
      const first = await pool.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      const firstPid = first.rows[0]?.pid;
      expect(firstPid).toBeDefined();

      await Promise.all([
        once(pool, "error", { signal: AbortSignal.timeout(5_000) }),
        admin.query("SELECT pg_terminate_backend($1)", [firstPid]),
      ]);
      expect(pool.totalCount).toBe(0);
      expect(logged).toHaveBeenCalledTimes(1);
      expect(logged.mock.calls[0]?.join(" ")).toMatch(
        /pooled connection failed.*\S/i,
      );

      const second = await pool.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      expect(second.rows[0]?.pid).toBeDefined();
      expect(second.rows[0]?.pid).not.toBe(firstPid);
    } finally {
      await admin?.end();
      await pool?.end();
      logged.mockRestore();
    }
  });

  test("a checked-out connection's failure is logged, and releasing it discards it", async () => {
    const url = postgresUrl();
    let pool: Pool | undefined;
    let admin: Pool | undefined;
    let client: PoolClient | undefined;
    let released = false;
    const logged = spyOn(console, "error").mockImplementation(() => {});
    try {
      pool = createPool({ connectionString: url });
      admin = createPool({ connectionString: url });
      client = await pool.connect();
      const { rows } = await client.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      const firstPid = rows[0]?.pid;
      expect(firstPid).toBeDefined();
      await Promise.all([
        once(client, "error", { signal: AbortSignal.timeout(5_000) }),
        admin.query("SELECT pg_terminate_backend($1)", [firstPid]),
      ]);
      expect(logged).toHaveBeenCalled();
      for (const args of logged.mock.calls) {
        expect(args.join(" ")).toMatch(/pooled connection failed.*\S/i);
      }

      client.release();
      released = true;
      expect(pool.totalCount).toBe(0);

      const second = await pool.query<{ pid: number }>(
        "SELECT pg_backend_pid() AS pid",
      );
      expect(second.rows[0]?.pid).toBeDefined();
      expect(second.rows[0]?.pid).not.toBe(firstPid);
    } finally {
      if (!released) client?.release(true);
      await admin?.end();
      await pool?.end();
      logged.mockRestore();
    }
  });

  test("the server cancels a statement past statement_timeout, and the connection stays usable", async () => {
    const pool = createPool({ connectionString: postgresUrl() });
    let client: PoolClient | undefined;
    try {
      const timeout = required(
        pool.options.statement_timeout || undefined,
        "statement_timeout",
      );
      client = await pool.connect();
      const overrunSeconds = timeout / 1000 + 1;
      await expect(
        client.query("SELECT pg_sleep($1)", [overrunSeconds]),
      ).rejects.toMatchObject({ code: "57014" });

      const after = await client.query<{ one: number }>("SELECT 1 AS one");
      expect(after.rows).toEqual([{ one: 1 }]);
    } finally {
      client?.release();
      await pool.end();
    }
  }, 30_000);

  test("the server holds the idle-in-transaction timeout the pool sets", async () => {
    const pool = createPool({ connectionString: postgresUrl() });
    const timeout = required(
      pool.options.idle_in_transaction_session_timeout,
      "idle_in_transaction_session_timeout",
    );
    try {
      const shown = await pool.query<{ ms: string }>(
        "SELECT setting AS ms FROM pg_settings WHERE name = 'idle_in_transaction_session_timeout'",
      );
      expect(Number(shown.rows[0]?.ms)).toBe(timeout);
    } finally {
      await pool.end();
    }
  });

  test("a transaction's rows are visible once it commits, and absent when it fails and its connection is discarded", async () => {
    const pool = createPool({ connectionString: postgresUrl() });
    const table = `pool_test_${process.pid}_${Date.now()}`;
    try {
      await pool.query(`CREATE TABLE ${table} (id text PRIMARY KEY)`);
      await inTransaction(pool, async (client) => {
        await client.query(`INSERT INTO ${table} VALUES ('kept')`);
      });
      expect(pool.idleCount).toBe(1);

      await expect(
        inTransaction(pool, async (client) => {
          await client.query(`INSERT INTO ${table} VALUES ('dropped')`);
          throw new Error("the body failed after its write");
        }),
      ).rejects.toThrow("the body failed");
      expect(pool.totalCount).toBe(0);

      const rows = await pool.query<{ id: string }>(
        `SELECT id FROM ${table} ORDER BY id`,
      );
      expect(rows.rows).toEqual([{ id: "kept" }]);
    } finally {
      await pool.query(`DROP TABLE IF EXISTS ${table}`);
      await pool.end();
    }
  });

  test("a transaction a caught error aborted raises at COMMIT", async () => {
    const pool = createPool({ connectionString: postgresUrl() });
    try {
      await expect(
        inTransaction(pool, async (client) => {
          await client.query("SELECT 1 / 0").catch(() => undefined);
        }),
      ).rejects.toThrow("rolled back at COMMIT");
    } finally {
      await pool.end();
    }
  });
});
