import {
  AuthTypes,
  Connector,
  IpAddressTypes,
} from "@google-cloud/cloud-sql-connector";
import { Pool } from "pg";

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
  return new Pool({
    ...options,
    user: config.dbUser,
    database: config.database,
    max: 5,
  });
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
