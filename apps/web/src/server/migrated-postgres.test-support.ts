import * as fs from "node:fs";
import * as path from "node:path";
import { Pool } from "pg";
import { createPool } from "./pg";

// Ephemeral Postgres databases migrated from `themis/migrate/migrations/` against the server named
// by `THEMIS_TEST_PG_URL` (required in CI; skipped locally without it, matching `pg-timeouts.test.ts`).

export const POSTGRES_URL = process.env.THEMIS_TEST_PG_URL;
export const SKIP_WITHOUT_POSTGRES =
  POSTGRES_URL === undefined && process.env.CI === undefined;

const MIGRATIONS_DIR = path.resolve(
  import.meta.dir,
  "../../../../themis/migrate/migrations",
);

const DB_USERS: Readonly<Record<string, string>> = {
  AUTH_DB_USER: "themis-auth@example.iam",
  WEB_DB_USER: "themis-web@example.iam",
  INGEST_DB_USER: "themis-ingest@example.iam",
  EVIDENCE_DB_USER: "themis-evidence@example.iam",
};

let nextDatabaseId = 0;

export function postgresUrl(): string {
  if (POSTGRES_URL === undefined) {
    throw new Error("THEMIS_TEST_PG_URL is not set; CI provides a Postgres");
  }
  return POSTGRES_URL;
}

function loadMigrations(): { filename: string; sql: string }[] {
  const filenames = fs
    .readdirSync(MIGRATIONS_DIR)
    .filter((name) => name.endsWith(".sql"))
    .sort();
  return filenames.map((filename, index) => {
    const match = /^(\d{4})_[a-z0-9_]+\.sql$/.exec(filename);
    const expected = index + 1;
    if (match === null || Number(match[1]) !== expected) {
      throw new Error(
        `expected contiguous migration ${expected}, got ${filename}`,
      );
    }
    const raw = fs.readFileSync(path.join(MIGRATIONS_DIR, filename), "utf8");
    const sql = raw.replace(/\$\{(\w+)\}/g, (_, key: string) => {
      const value = DB_USERS[key];
      if (value === undefined) {
        throw new Error(`no substitution for \${${key}} in ${filename}`);
      }
      return value;
    });
    return { filename, sql };
  });
}

function sqlLiteral(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

export interface MigratedDatabase {
  /** Superuser pool for seeding corrupt rows or truncating tables between tests. */
  readonly adminPool: Pool;
  /** Pool authenticated as `WEB_DB_USER`, subject to the committed table and schema grants. */
  readonly webPool: Pool;
  close(): Promise<void>;
}

export async function openMigratedDatabase(): Promise<MigratedDatabase> {
  const baseUrl = new URL(postgresUrl());
  const database = `themis_web_${process.pid}_${Date.now()}_${++nextDatabaseId}`;
  const rolePassword =
    decodeURIComponent(baseUrl.password) || "themis-test-password";

  const serverPool = new Pool({ connectionString: baseUrl.toString(), max: 1 });
  try {
    for (const login of Object.values(DB_USERS)) {
      const quoted = `"${login}"`;
      await serverPool.query(
        `DO $$ BEGIN
           CREATE ROLE ${quoted} LOGIN PASSWORD ${sqlLiteral(rolePassword)};
         EXCEPTION WHEN duplicate_object OR unique_violation THEN
           ALTER ROLE ${quoted} LOGIN PASSWORD ${sqlLiteral(rolePassword)};
         END $$`,
      );
    }
    await serverPool.query(`CREATE DATABASE ${database}`);
  } finally {
    await serverPool.end();
  }

  const adminUrl = new URL(baseUrl);
  adminUrl.pathname = `/${database}`;
  const adminPool = createPool({ connectionString: adminUrl.toString() });

  try {
    for (const { filename, sql } of loadMigrations()) {
      try {
        await adminPool.query(sql);
      } catch (error) {
        const code = (error as { code?: string }).code;
        // Stock `postgres:16` in `web.yml` neither installs nor preloads pgAudit.
        if (
          filename === "0020_pgaudit.sql" &&
          (code === "0A000" || code === "58P01" || code === "55000")
        ) {
          continue;
        }
        throw error;
      }
    }
  } catch (error) {
    await adminPool.end();
    const dropPool = new Pool({ connectionString: baseUrl.toString(), max: 1 });
    await dropPool
      .query(`DROP DATABASE IF EXISTS ${database} WITH (FORCE)`)
      .finally(() => dropPool.end());
    throw error;
  }

  const webUrl = new URL(adminUrl);
  webUrl.username = DB_USERS.WEB_DB_USER;
  webUrl.password = rolePassword;
  const webPool = createPool({ connectionString: webUrl.toString() });

  return {
    adminPool,
    webPool,
    async close() {
      await webPool.end();
      await adminPool.end();
      const dropPool = new Pool({
        connectionString: baseUrl.toString(),
        max: 1,
      });
      try {
        await dropPool.query(
          `DROP DATABASE IF EXISTS ${database} WITH (FORCE)`,
        );
      } finally {
        await dropPool.end();
      }
    },
  };
}
