import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  AuthTypes,
  Connector,
  IpAddressTypes,
} from "@google-cloud/cloud-sql-connector";
import { Pool } from "pg";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import { ResourceNotFoundError } from "../../errors";
import type { SqlConfig } from "./config";

// Cloud SQL (Postgres) persistence for the analysis-session lifecycle. Connects
// through the Cloud SQL Node connector with IAM database auth (no password — the
// connector supplies the IAM credential) over a lazily-built pool.
//
// The create write is one transaction over two rows: the `analyses` row and the
// `session_context` row `(token_hash, project_id, analysis_id)` the store resolves
// a session bearer against. No working-document SQL — the document lives in GCS and
// is read directly (see gcs.ts).

const ANALYSIS_COLUMNS = "id, session_id, project_id, prompt, created_at";

interface AnalysisRow {
  id: string;
  session_id: string;
  project_id: string;
  prompt: string;
  created_at: Date;
}

/** The create write: the `analyses` row plus its `session_context` grant row. */
export interface InsertAnalysisInput {
  id: string;
  sessionId: string;
  projectId: string;
  prompt: string;
  createdBy: string;
  tokenHash: string;
}

export class Sql {
  private poolPromise?: Promise<Pool>;
  private connector?: Connector;

  constructor(private readonly config: SqlConfig) {}

  private async pool(): Promise<Pool> {
    if (!this.poolPromise) {
      this.poolPromise = this.buildPool();
    }
    return this.poolPromise;
  }

  private async buildPool(): Promise<Pool> {
    this.connector = new Connector();
    const options = await this.connector.getOptions({
      instanceConnectionName: this.config.connectionName,
      authType: AuthTypes.IAM,
      ipType: IpAddressTypes.PUBLIC,
    });
    return new Pool({
      ...options,
      user: this.config.iamUser,
      database: this.config.database,
      max: 5,
    });
  }

  /** Close the pool and connector for a clean process shutdown. */
  async close(): Promise<void> {
    if (this.poolPromise) {
      const pool = await this.poolPromise;
      await pool.end();
    }
    this.connector?.close();
  }

  private async query<R>(text: string, values: unknown[] = []): Promise<R[]> {
    const pool = await this.pool();
    const result = await pool.query(text, values);
    return result.rows as R[];
  }

  /** Insert the analysis and its session-context grant in one transaction, so a
   *  created session always has a resolvable bearer (or neither row exists).
   *
   *  Returns the stored `created_at`: the column is database-assigned, so the
   *  create response and every later read of the row carry the same instant. */
  async insertAnalysis(input: InsertAnalysisInput): Promise<Date> {
    const pool = await this.pool();
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      const inserted = await client.query<{ created_at: Date }>(
        `INSERT INTO analyses (id, session_id, project_id, prompt, created_by)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING created_at`,
        [
          input.id,
          input.sessionId,
          input.projectId,
          input.prompt,
          input.createdBy,
        ],
      );
      const createdAt = inserted.rows[0]?.created_at;
      if (createdAt === undefined) {
        throw new Error(
          `analyses INSERT returned no created_at for ${input.id}`,
        );
      }
      await client.query(
        `INSERT INTO session_context (token_hash, project_id, analysis_id)
         VALUES ($1, $2, $3)`,
        [input.tokenHash, input.projectId, input.id],
      );
      await client.query("COMMIT");
      return createdAt;
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  /** The analysis row by id. Unknown id → a typed not-found (→ 404). A drifted
   *  column fails loud through the row mapping rather than shaping a
   *  malformed model. */
  async getAnalysis(id: string): Promise<Analysis> {
    const rows = await this.query<AnalysisRow>(
      `SELECT ${ANALYSIS_COLUMNS} FROM analyses WHERE id = $1`,
      [id],
    );
    const row = rows[0];
    if (row === undefined) {
      throw new ResourceNotFoundError(`analysis not found: ${id}`);
    }
    return parseAnalysis(row);
  }

  /** Analyses in the given Projects, newest first — the session switcher's source.
   *  An empty Project set yields no rows (the query is short-circuited). A drifted
   *  column in any row fails loud through the row mapping. */
  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    if (projectIds.length === 0) return [];
    const rows = await this.query<AnalysisRow>(
      `SELECT ${ANALYSIS_COLUMNS} FROM analyses
       WHERE project_id = ANY($1::text[]) ORDER BY created_at DESC`,
      [projectIds],
    );
    return rows.map(parseAnalysis);
  }

  /** The Project owning an analysis. Unknown id → a typed not-found (→ 404). */
  async projectOfAnalysis(id: string): Promise<string> {
    const rows = await this.query<{ project_id: string }>(
      `SELECT project_id FROM analyses WHERE id = $1`,
      [id],
    );
    const row = rows[0];
    if (row === undefined) {
      throw new ResourceNotFoundError(`analysis not found: ${id}`);
    }
    return row.project_id;
  }

  /** Whether the user is a member of the Project. */
  async isMember(userEmail: string, projectId: string): Promise<boolean> {
    const rows = await this.query<{ one: number }>(
      `SELECT 1 AS one FROM project_members
       WHERE user_email = $1 AND project_id = $2`,
      [userEmail, projectId],
    );
    return rows.length > 0;
  }

  /** Every Project the user belongs to (id + display name), joined to the Project
   *  registry. */
  async projectsOf(userEmail: string): Promise<{ id: string; name: string }[]> {
    return this.query<{ id: string; name: string }>(
      `SELECT p.id, p.name FROM projects p
       JOIN project_members m ON m.project_id = p.id
       WHERE m.user_email = $1
       ORDER BY p.name`,
      [userEmail],
    );
  }
}

function parseAnalysis(row: AnalysisRow): Analysis {
  return create(AnalysisSchema, {
    id: row.id,
    sessionId: row.session_id,
    projectId: row.project_id,
    prompt: row.prompt,
    // pg hands back timestamptz as a Date; the wire carries a Timestamp.
    createdAt: timestampFromDate(row.created_at),
  });
}
