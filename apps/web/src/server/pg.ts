import {
  AuthTypes,
  Connector,
  IpAddressTypes,
} from "@google-cloud/cloud-sql-connector";
import { type ClientConfig, Pool, type PoolClient, type PoolConfig } from "pg";

// The process-wide Cloud SQL pool, and the connection inputs that build it. Shared because a
// connector and pool are per-instance infrastructure, not per-caller state: a second set in the same
// Cloud Run instance is doubled connections against the same database for nothing.
//
// IAM database auth throughout — the connector supplies the credential, so no password exists.

type EnvLike = Record<string, string | undefined>;

/** Cloud SQL connector inputs: the instance the connector dials, the database, and the IAM DB-user
 *  login to authenticate as. */
export interface SqlConfig {
  connectionName: string;
  database: string;
  dbUser: string;
}

/** Read + validate the Cloud SQL connection inputs. A missing one is a fail-closed
 *  misconfiguration, never a silent default. */
export function loadSqlConfig(env: EnvLike = process.env): SqlConfig {
  const required = (name: string): string => {
    const value = env[name];
    if (value === undefined || value === "") {
      throw new Error(`${name} is not set — cannot connect to Cloud SQL`);
    }
    return value;
  };
  return {
    connectionName: required("THEMIS_SQL_CONNECTION_NAME"),
    database: required("THEMIS_SQL_DATABASE"),
    dbUser: required("THEMIS_DB_USER"),
  };
}

interface PoolSingletons {
  config?: SqlConfig;
  connector?: Connector;
  pool?: Promise<Pool>;
}

function singletons(): PoolSingletons {
  const holder = globalThis as typeof globalThis & {
    __themisPg?: PoolSingletons;
  };
  if (!holder.__themisPg) holder.__themisPg = {};
  return holder.__themisPg;
}

function sameConfig(a: SqlConfig, b: SqlConfig): boolean {
  return (
    a.connectionName === b.connectionName &&
    a.database === b.database &&
    a.dbUser === b.dbUser
  );
}

/** The shared pool, built on first use and memoized across requests and HMR reloads.
 *
 *  Raises when called with connection inputs differing from the ones the live pool was built
 *  from: the second caller would silently get the first caller's database, and a wrong-database
 *  read is the kind of fault that surfaces as missing rows rather than as an error. */
export async function getPool(config: SqlConfig): Promise<Pool> {
  const s = singletons();
  if (s.config && !sameConfig(s.config, config)) {
    throw new Error(
      `the Cloud SQL pool is already open against ${s.config.connectionName}/${s.config.database}; ` +
        `refusing to hand it to a caller asking for ${config.connectionName}/${config.database}`,
    );
  }
  if (!s.pool) {
    s.config = config;
    // Clear a rejected build rather than memoizing the failure: a cold-start blip in the Cloud SQL
    // Admin API would otherwise leave every later caller in this container holding that rejection.
    s.pool = buildPool(config, s).catch((error: unknown) => {
      s.pool = undefined;
      s.config = undefined;
      throw error;
    });
  }
  return s.pool;
}

async function buildPool(config: SqlConfig, s: PoolSingletons): Promise<Pool> {
  const connector = new Connector();
  s.connector = connector;
  const options = await connector.getOptions({
    instanceConnectionName: config.connectionName,
    authType: AuthTypes.IAM,
    ipType: IpAddressTypes.PUBLIC,
  });
  return createPool({
    ...options,
    user: config.dbUser,
    database: config.database,
  });
}

/** A pool over the given connection inputs (the connector's stream, the IAM user, the database).
 *
 *  An idle connection is kept for minutes, not pg-pool's default ten seconds, so the next request
 *  after a pause reuses it rather than paying a fresh Cloud SQL dial.
 *
 *  Every wait is bounded. A checkout that finds no free connection and no room to dial one, or a
 *  dial that stalls, rejects after `connectionTimeoutMillis`: two 2.5 s Poll ticks, against a
 *  slowest observed dial of about 2 s on a cold instance. The server cancels a statement that runs
 *  past `statement_timeout`, a lock wait included, and ends a session left idle inside a transaction
 *  past `idle_in_transaction_session_timeout`. pg rejects a query that gets no reply at all within
 *  `query_timeout`, which catches a connection the network dropped without a reset.
 *
 *  A connection that fails on its own, idle or checked out, is logged and discarded; it never
 *  reaches the process as an uncaught error. A query that fails is not retried: it rejects with the
 *  driver's error, its connection is discarded, and the next query takes another idle connection or
 *  dials a new one. A caller whose statement is safe to repeat retries it itself. */
export function createPool(
  connection: ClientConfig & Pick<PoolConfig, "Client">,
): Pool {
  const pool = new Pool({
    ...connection,
    max: 5,
    // pg-pool's reaper closes a connection idle this long, under Cloud Run's 10-min egress idle
    // timeout; a throttled CPU runs the timer late, so the reap can land on the next request.
    idleTimeoutMillis: 5 * 60 * 1000,
    // Bounds both the wait for a free connection and a dial.
    connectionTimeoutMillis: 5_000,
    // Sent in the startup packet, so it covers every statement on the connection.
    statement_timeout: 5_000,
    // Sent in the startup packet: the server ends a session whose close never reached it.
    idle_in_transaction_session_timeout: 10_000,
    // Above statement_timeout, so it fires only when the server's own cancellation never arrives.
    query_timeout: 10_000,
  });
  // pg-pool listens on a client only while it is idle; this listener covers a checked-out one too.
  pool.on("connect", (client) => {
    client.on("error", (error) => {
      console.error("Cloud SQL: a pooled connection failed:", error.message);
    });
  });
  // Already logged by the client's listener above, and pg-pool has discarded the client.
  pool.on("error", () => {});
  return pool;
}

/** Run `body` in a transaction on a connection `pool.connect()` checks out: a `Pool`, or a caller
 *  wrapping its checkout (in a span, say).
 *
 *  A failed transaction releases its connection with the failure, so pg-pool discards it rather
 *  than returning it: closing the session rolls the transaction back on the server, and a
 *  connection a timeout left with a statement outstanding is never reused. That includes a
 *  server-reported error or a body's own, which costs the next checkout a dial; failures are rare.
 *
 *  Raises when COMMIT reports a rollback, as it does for a transaction a caught error aborted. */
export async function inTransaction<T>(
  pool: { connect(): Promise<PoolClient> },
  body: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await pool.connect();
  let failure: Error | undefined;
  try {
    await client.query("BEGIN");
    const out = await body(client);
    const committed = await client.query("COMMIT");
    if (committed.command !== "COMMIT") {
      throw new Error(
        `the transaction was rolled back at COMMIT (${committed.command})`,
      );
    }
    return out;
  } catch (error) {
    failure = error instanceof Error ? error : new Error(String(error));
    throw error;
  } finally {
    client.release(failure);
  }
}

/** Close the pool and connector for a clean process shutdown. */
export async function closePool(): Promise<void> {
  const s = singletons();
  if (s.pool) {
    const pool = await s.pool;
    await pool.end();
  }
  s.connector?.close();
  s.pool = undefined;
  s.connector = undefined;
  s.config = undefined;
}
