import { afterEach, describe, expect, jest, test } from "bun:test";
import { Duplex } from "node:stream";
import { createPool, inTransaction } from "./pg";

// createPool's timeouts, over real pg clients: a stand-in socket that plays a Postgres server which
// accepts the login and then goes silent, and a real server named by THEMIS_TEST_PG_URL (required
// in CI, which provides one; skipped elsewhere without it).

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

const POSTGRES_URL = process.env.THEMIS_TEST_PG_URL;

/** The test server's URL; CI must provide one, so a missing one there fails rather than skips. */
function postgresUrl(): string {
  if (POSTGRES_URL === undefined) {
    throw new Error("THEMIS_TEST_PG_URL is not set; CI provides a Postgres");
  }
  return POSTGRES_URL;
}

describe.skipIf(POSTGRES_URL === undefined && process.env.CI === undefined)(
  "against Postgres",
  () => {
    test("the server cancels a statement past statement_timeout, and the connection stays usable", async () => {
      const pool = createPool({ connectionString: postgresUrl() });
      const timeout = required(
        pool.options.statement_timeout || undefined,
        "statement_timeout",
      );
      const client = await pool.connect();
      try {
        const overrunSeconds = timeout / 1000 + 1;
        await expect(
          client.query("SELECT pg_sleep($1)", [overrunSeconds]),
        ).rejects.toMatchObject({ code: "57014" });

        const after = await client.query<{ one: number }>("SELECT 1 AS one");
        expect(after.rows).toEqual([{ one: 1 }]);
      } finally {
        client.release();
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

    test("a transaction's rows are visible once it commits, and absent when it fails", async () => {
      const pool = createPool({ connectionString: postgresUrl() });
      const table = `pool_test_${process.pid}_${Date.now()}`;
      try {
        await pool.query(`CREATE TABLE ${table} (id text PRIMARY KEY)`);
        await inTransaction(pool, async (client) => {
          await client.query(`INSERT INTO ${table} VALUES ('kept')`);
        });
        await expect(
          inTransaction(pool, async (client) => {
            await client.query(`INSERT INTO ${table} VALUES ('dropped')`);
            throw new Error("the body failed after its write");
          }),
        ).rejects.toThrow("the body failed");

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
  },
);
