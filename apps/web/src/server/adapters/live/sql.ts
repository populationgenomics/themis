import { create, fromBinary, toBinary } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import type { Attributes } from "@opentelemetry/api";
import type { PoolClient } from "pg";
import {
  type Analysis,
  type AnalysisInputs,
  AnalysisInputsSchema,
  AnalysisSchema,
} from "@/models/workbench";
import {
  isUndecodableAnalysisError,
  ResourceNotFoundError,
  UndecodableAnalysisError,
} from "../../errors";
import { getPool, inTransaction, type SqlConfig } from "../../pg";
import { clientSpan } from "../../tracing/spans";

// Cloud SQL (Postgres) persistence for the analysis-session lifecycle, over the shared
// process-wide pool (`server/pg.ts`).
//
// The create write is one transaction over two rows: the `analyses` row and the
// `session_context` row `(token_hash, project_id, analysis_id)` the auth service
// resolves a session bearer against. No working-document SQL: the document is the
// workspace repository's, which the browser reads from its own copy.
//
// Each read, and the create's transaction, is a span named for the method, with the connection it runs
// on acquired in a `pg.connect` span of its own: the pool hands back an idle connection, or opens one
// through the Cloud SQL connector.

const ANALYSIS_COLUMNS = "id, session_id, project_id, inputs, created_at";

export interface AnalysisRow {
  id: string;
  session_id: string;
  project_id: string;
  // The serialized AnalysisInputs; pg hands a bytea back as a Buffer.
  inputs: Buffer;
  created_at: Date;
}

/** The create write: the `analyses` row plus its `session_context` grant row. */
export interface InsertAnalysisInput {
  id: string;
  sessionId: string;
  projectId: string;
  inputs: AnalysisInputs;
  createdBy: string;
  tokenHash: string;
}

export class Sql {
  constructor(private readonly config: SqlConfig) {}

  private async pool() {
    return getPool(this.config);
  }

  private attributes(): Attributes {
    return {
      "db.system.name": "postgresql",
      "db.namespace": this.config.database,
    };
  }

  private async connection(): Promise<PoolClient> {
    return clientSpan("pg.connect", this.attributes(), async () =>
      (await this.pool()).connect(),
    );
  }

  /** Run one statement on a pooled connection, as a span named for `operation`. A connection a statement
   *  failed on is discarded rather than returned to the pool, as `Pool.query` does. */
  private async query<R>(
    operation: string,
    text: string,
    values: unknown[] = [],
  ): Promise<R[]> {
    return clientSpan(
      `pg.${operation}`,
      { ...this.attributes(), "db.query.text": text },
      async () => {
        const client = await this.connection();
        let failure: Error | undefined;
        try {
          const result = await client.query(text, values);
          return result.rows as R[];
        } catch (error) {
          failure = error instanceof Error ? error : new Error(String(error));
          throw error;
        } finally {
          client.release(failure);
        }
      },
    );
  }

  /** Insert the analysis and its session-context grant in one transaction, so a
   *  created session always has a resolvable bearer (or neither row exists).
   *
   *  Returns the stored `created_at`: the column is database-assigned, so the
   *  create response and every later read of the row carry the same instant. */
  async insertAnalysis(input: InsertAnalysisInput): Promise<Date> {
    return clientSpan("pg.insertAnalysis", this.attributes(), () =>
      this.insertAnalysisInTransaction(input),
    );
  }

  private async insertAnalysisInTransaction(
    input: InsertAnalysisInput,
  ): Promise<Date> {
    // Checked out through `connection()`, so the transaction's `pg.connect` span is its child.
    const connections = { connect: () => this.connection() };
    return inTransaction(connections, async (client) => {
      const inserted = await client.query<{ created_at: Date }>(
        `INSERT INTO analyses (id, session_id, project_id, inputs, created_by)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING created_at`,
        [
          input.id,
          input.sessionId,
          input.projectId,
          Buffer.from(toBinary(AnalysisInputsSchema, input.inputs)),
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
      return createdAt;
    });
  }

  /** The analysis row by id. Unknown id → a typed not-found (→ 404). A drifted
   *  column fails loud through the row mapping rather than shaping a
   *  malformed model. */
  async getAnalysis(id: string): Promise<Analysis> {
    const rows = await this.query<AnalysisRow>(
      "getAnalysis",
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
      "listAnalysesIn",
      `SELECT ${ANALYSIS_COLUMNS} FROM analyses
       WHERE project_id = ANY($1::text[]) ORDER BY created_at DESC`,
      [projectIds],
    );
    // A row whose payload will not decode costs its own card, not the listing it sits in — `/` is the
    // entry route, and one corrupt row must not be the whole surface. It degrades to the same unset
    // oneof a scenario this build predates produces, so `lib/scenario.ts` renders it as unrecognised
    // by the case it already has. Opening that Analysis still raises: see `getAnalysis`.
    return rows.map(analysisForListing);
  }

  /** Whether the user is a member of the Project. */
  async isMember(userEmail: string, projectId: string): Promise<boolean> {
    const rows = await this.query<{ one: number }>(
      "isMember",
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
      "projectsOf",
      `SELECT p.id, p.name FROM projects p
       JOIN project_members m ON m.project_id = p.id
       WHERE m.user_email = $1
       ORDER BY p.name`,
      [userEmail],
    );
  }
}

/** An Analysis whose stored inputs did not decode, shaped so a listing can render it: the oneof is
 *  unset, which is the state `lib/scenario.ts` already names as a scenario it cannot read. */
export function analysisForListing(row: AnalysisRow): Analysis {
  try {
    return parseAnalysis(row);
  } catch (e) {
    if (isUndecodableAnalysisError(e)) return unreadableAnalysis(row);
    throw e;
  }
}

function unreadableAnalysis(row: AnalysisRow): Analysis {
  return create(AnalysisSchema, {
    id: row.id,
    sessionId: row.session_id,
    projectId: row.project_id,
    inputs: create(AnalysisInputsSchema, {}),
    createdAt: timestampFromDate(row.created_at),
  });
}

function parseAnalysis(row: AnalysisRow): Analysis {
  // A scenario this build predates parses into an unset oneof — proto keeps the member it does not
  // know as an unknown field. The row is returned as it is; naming it is `lib/scenario.ts`'s job, so
  // one such Analysis costs its own card rather than the listing it appears in.
  // A decode failure carries its Project: the point-access check authorizes against it, so an
  // unreadable row answers a non-member exactly as an unknown id does.
  let inputs: AnalysisInputs;
  try {
    inputs = fromBinary(AnalysisInputsSchema, row.inputs);
  } catch (cause) {
    throw new UndecodableAnalysisError(row.id, row.project_id, { cause });
  }
  return create(AnalysisSchema, {
    id: row.id,
    sessionId: row.session_id,
    projectId: row.project_id,
    inputs,
    // pg hands back timestamptz as a Date; the wire carries a Timestamp.
    createdAt: timestampFromDate(row.created_at),
  });
}
