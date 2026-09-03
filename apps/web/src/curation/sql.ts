import { fromBinary, toBinary } from "@bufbuild/protobuf";
import type { Pool, PoolClient } from "pg";
import { AssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import { ClientInputError, ResourceNotFoundError } from "@/server/errors";
import { getPool, type SqlConfig } from "@/server/pg";
import type {
  Entry,
  NewVariant,
  Person,
  Role,
  Submission,
  Variant,
  Worksheet,
} from "./model";
import type { CurationStore } from "./store";

// Cloud SQL persistence for the curation schema, over the shared process-wide pool
// (`server/pg.ts`). Every statement names the `curation` schema explicitly rather than relying on a
// search_path the pool does not set.

const VARIANT_COLUMNS =
  "id, gene, transcript, hgvs_c, clingen_allele_id, disease_label, mondo_id, created_by, created_at";
const WORKSHEET_COLUMNS =
  "id, variant_id, curator_email, workflows_version, assigned_by, assigned_at";

interface VariantRow {
  id: string;
  gene: string;
  transcript: string;
  hgvs_c: string;
  clingen_allele_id: string;
  disease_label: string;
  mondo_id: string;
  created_by: string;
  created_at: Date;
}

interface WorksheetRow {
  id: string;
  variant_id: string;
  curator_email: string;
  workflows_version: string;
  assigned_by: string;
  assigned_at: Date;
}

interface EntryRow {
  workflow_id: string;
  assessment: Buffer;
}

export class SqlCurationStore implements CurationStore {
  constructor(private readonly config: SqlConfig) {}

  private async pool(): Promise<Pool> {
    return getPool(this.config);
  }

  private async query<R>(text: string, values: unknown[] = []): Promise<R[]> {
    const pool = await this.pool();
    const result = await pool.query(text, values);
    return result.rows as R[];
  }

  private async transaction<T>(
    body: (client: PoolClient) => Promise<T>,
  ): Promise<T> {
    const pool = await this.pool();
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      const out = await body(client);
      await client.query("COMMIT");
      return out;
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  async roleOf(email: string): Promise<Role | undefined> {
    const rows = await this.query<{ role: string }>(
      "SELECT role FROM curation.roles WHERE email = $1",
      [email],
    );
    const role = rows[0]?.role;
    if (role === undefined) return undefined;
    if (role !== "manager" && role !== "curator") {
      throw new Error(`curation.roles holds an unknown role: ${role}`);
    }
    return role;
  }

  async listPeople(): Promise<Person[]> {
    const rows = await this.query<{
      email: string;
      role: string;
      added_by: string;
      added_at: Date;
    }>(
      "SELECT email, role, added_by, added_at FROM curation.roles ORDER BY email",
    );
    return rows.map((row) => {
      if (row.role !== "manager" && row.role !== "curator") {
        throw new Error(`curation.roles holds an unknown role: ${row.role}`);
      }
      return {
        email: row.email,
        role: row.role,
        addedBy: row.added_by,
        addedAt: row.added_at,
      };
    });
  }

  async addPerson(email: string, role: Role, addedBy: string): Promise<Person> {
    const rows = await this.query<{ added_at: Date }>(
      `INSERT INTO curation.roles (email, role, added_by) VALUES ($1, $2, $3)
       ON CONFLICT (email) DO NOTHING RETURNING added_at`,
      [email, role, addedBy],
    );
    const addedAt = rows[0]?.added_at;
    if (addedAt === undefined) {
      const existing = (await this.listPeople()).find((p) => p.email === email);
      if (existing === undefined) {
        throw new Error(
          `curation.roles lost the row it refused to insert: ${email}`,
        );
      }
      return existing;
    }
    return { email, role, addedBy, addedAt };
  }

  async removePerson(email: string): Promise<void> {
    await this.query("DELETE FROM curation.roles WHERE email = $1", [email]);
  }

  async createVariant(
    variant: NewVariant,
    createdBy: string,
  ): Promise<Variant> {
    const id = crypto.randomUUID();
    const rows = await this.query<{ created_at: Date }>(
      `INSERT INTO curation.variants
         (id, gene, transcript, hgvs_c, clingen_allele_id, disease_label, mondo_id, created_by)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING created_at`,
      [
        id,
        variant.gene,
        variant.transcript,
        variant.hgvsC,
        variant.clingenAlleleId,
        variant.diseaseLabel,
        variant.mondoId,
        createdBy,
      ],
    );
    const createdAt = rows[0]?.created_at;
    if (createdAt === undefined) {
      throw new Error(
        `curation.variants INSERT returned no created_at for ${id}`,
      );
    }
    return { ...variant, id, createdBy, createdAt };
  }

  async listVariants(): Promise<Variant[]> {
    const rows = await this.query<VariantRow>(
      `SELECT ${VARIANT_COLUMNS} FROM curation.variants ORDER BY created_at DESC`,
    );
    return rows.map(parseVariant);
  }

  async getVariant(id: string): Promise<Variant | undefined> {
    const rows = await this.query<VariantRow>(
      `SELECT ${VARIANT_COLUMNS} FROM curation.variants WHERE id = $1`,
      [id],
    );
    const row = rows[0];
    return row === undefined ? undefined : parseVariant(row);
  }

  async assign(
    variantId: string,
    curatorEmail: string,
    workflowsVersion: string,
    assignedBy: string,
  ): Promise<Worksheet> {
    const id = crypto.randomUUID();
    const rows = await this.query<{ assigned_at: Date }>(
      `INSERT INTO curation.worksheets
         (id, variant_id, curator_email, workflows_version, assigned_by)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (variant_id, curator_email) DO NOTHING
       RETURNING assigned_at`,
      [id, variantId, curatorEmail, workflowsVersion, assignedBy],
    );
    const assignedAt = rows[0]?.assigned_at;
    if (assignedAt === undefined) {
      throw new ClientInputError(
        `${curatorEmail} is already assigned to this variant`,
      );
    }
    return {
      id,
      variantId,
      curatorEmail,
      workflowsVersion,
      assignedBy,
      assignedAt,
    };
  }

  async withdraw(worksheetId: string): Promise<void> {
    const submitted = await this.query<{ one: number }>(
      "SELECT 1 AS one FROM curation.submissions WHERE worksheet_id = $1 LIMIT 1",
      [worksheetId],
    );
    if (submitted.length > 0) {
      throw new ClientInputError(
        `worksheet ${worksheetId} has been submitted and cannot be withdrawn`,
      );
    }
    await this.query("DELETE FROM curation.worksheets WHERE id = $1", [
      worksheetId,
    ]);
  }

  async getWorksheet(id: string): Promise<Worksheet | undefined> {
    const rows = await this.query<WorksheetRow>(
      `SELECT ${WORKSHEET_COLUMNS} FROM curation.worksheets WHERE id = $1`,
      [id],
    );
    const row = rows[0];
    return row === undefined ? undefined : parseWorksheet(row);
  }

  async worksheetsOfCurator(email: string): Promise<Worksheet[]> {
    const rows = await this.query<WorksheetRow>(
      `SELECT ${WORKSHEET_COLUMNS} FROM curation.worksheets
       WHERE curator_email = $1 ORDER BY assigned_at DESC`,
      [email],
    );
    return rows.map(parseWorksheet);
  }

  async worksheetsOfVariant(variantId: string): Promise<Worksheet[]> {
    const rows = await this.query<WorksheetRow>(
      `SELECT ${WORKSHEET_COLUMNS} FROM curation.worksheets
       WHERE variant_id = $1 ORDER BY curator_email`,
      [variantId],
    );
    return rows.map(parseWorksheet);
  }

  async drafts(worksheetId: string): Promise<Entry[]> {
    const rows = await this.query<EntryRow>(
      `SELECT workflow_id, assessment FROM curation.drafts
       WHERE worksheet_id = $1 ORDER BY workflow_id`,
      [worksheetId],
    );
    return rows.map(parseEntry);
  }

  async putDraft(worksheetId: string, entry: Entry): Promise<void> {
    await this.query(
      `INSERT INTO curation.drafts (worksheet_id, workflow_id, assessment, updated_at)
       VALUES ($1, $2, $3, now())
       ON CONFLICT (worksheet_id, workflow_id)
       DO UPDATE SET assessment = EXCLUDED.assessment, updated_at = EXCLUDED.updated_at`,
      [
        worksheetId,
        entry.workflowId,
        Buffer.from(toBinary(AssessmentSchema, entry.assessment)),
      ],
    );
  }

  async submissions(worksheetId: string): Promise<Submission[]> {
    const rows = await this.query<{
      id: string;
      worksheet_id: string;
      submitted_at: Date;
      note: string;
    }>(
      `SELECT id, worksheet_id, submitted_at, note FROM curation.submissions
       WHERE worksheet_id = $1 ORDER BY submitted_at`,
      [worksheetId],
    );
    return rows.map((row) => ({
      id: row.id,
      worksheetId: row.worksheet_id,
      submittedAt: row.submitted_at,
      note: row.note,
    }));
  }

  /** One transaction: the submission row, then its assessments copied from the drafts by the
   *  database. The copy never leaves Postgres, so no encode/decode step sits between what the
   *  curator saw and what the submission commits. */
  async submit(worksheetId: string, note: string): Promise<Submission> {
    const id = crypto.randomUUID();
    return this.transaction(async (client) => {
      const inserted = await client.query<{ submitted_at: Date }>(
        `INSERT INTO curation.submissions (id, worksheet_id, note)
         VALUES ($1, $2, $3) RETURNING submitted_at`,
        [id, worksheetId, note],
      );
      const submittedAt = inserted.rows[0]?.submitted_at;
      if (submittedAt === undefined) {
        throw new Error(
          `curation.submissions INSERT returned no submitted_at for ${id}`,
        );
      }
      const copied = await client.query(
        `INSERT INTO curation.assessments (submission_id, workflow_id, assessment)
         SELECT $1, workflow_id, assessment FROM curation.drafts WHERE worksheet_id = $2`,
        [id, worksheetId],
      );
      if (copied.rowCount === 0) {
        throw new ClientInputError(
          "nothing to submit: the worksheet holds no answers",
        );
      }
      return { id, worksheetId, submittedAt, note };
    });
  }

  async assessments(submissionId: string): Promise<Entry[]> {
    const rows = await this.query<EntryRow>(
      `SELECT workflow_id, assessment FROM curation.assessments
       WHERE submission_id = $1 ORDER BY workflow_id`,
      [submissionId],
    );
    if (rows.length === 0) {
      throw new ResourceNotFoundError(`no such submission: ${submissionId}`);
    }
    return rows.map(parseEntry);
  }
}

function parseVariant(row: VariantRow): Variant {
  return {
    id: row.id,
    gene: row.gene,
    transcript: row.transcript,
    hgvsC: row.hgvs_c,
    clingenAlleleId: row.clingen_allele_id,
    diseaseLabel: row.disease_label,
    mondoId: row.mondo_id,
    createdBy: row.created_by,
    createdAt: row.created_at,
  };
}

function parseWorksheet(row: WorksheetRow): Worksheet {
  return {
    id: row.id,
    variantId: row.variant_id,
    curatorEmail: row.curator_email,
    workflowsVersion: row.workflows_version,
    assignedBy: row.assigned_by,
    assignedAt: row.assigned_at,
  };
}

function parseEntry(row: EntryRow): Entry {
  return {
    workflowId: row.workflow_id,
    assessment: fromBinary(AssessmentSchema, row.assessment),
  };
}
