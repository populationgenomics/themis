import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
} from "bun:test";
import { create } from "@bufbuild/protobuf";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import { AnalysisInputsSchema } from "@/models/workbench";
import {
  ResourceNotFoundError,
  UndecodableAnalysisError,
} from "@/server/errors";
import {
  type MigratedDatabase,
  openMigratedDatabase,
  SKIP_WITHOUT_POSTGRES,
} from "@/server/migrated-postgres.test-support";
import { Sql } from "./sql";

const VALID_INPUTS = create(AnalysisInputsSchema, {
  scenario: {
    case: "variantClassification",
    value: {
      transcript: "NM_000138.5",
      hgvsC: "c.7003C>T",
      clinicalContext: "Tall stature.",
    },
  },
});

describe.skipIf(SKIP_WITHOUT_POSTGRES)("Sql against Postgres", () => {
  let db: MigratedDatabase;
  let sql: Sql;

  beforeAll(async () => {
    db = await openMigratedDatabase();
    sql = new Sql(db.webPool);
  }, 30_000);

  afterAll(async () => {
    await db?.close();
  });

  beforeEach(async () => {
    await db.adminPool.query("TRUNCATE session_context, analyses");
  });

  test("insertAnalysis writes the analysis and its session_context grant, and getAnalysis reads it back", async () => {
    const tokenHash = "a".repeat(64);
    const createdAt = await sql.insertAnalysis({
      id: "an_1",
      sessionId: "sesn_1",
      projectId: "proj_a",
      inputs: VALID_INPUTS,
      createdBy: "curator@example.org",
      tokenHash,
    });

    const loaded = await sql.getAnalysis("an_1");
    expect(loaded.id).toBe("an_1");
    expect(loaded.sessionId).toBe("sesn_1");
    expect(loaded.projectId).toBe("proj_a");
    expect(loaded.inputs?.scenario.case).toBe("variantClassification");
    expect(loaded.createdAt && timestampDate(loaded.createdAt).getTime()).toBe(
      createdAt.getTime(),
    );

    const grants = await db.adminPool.query<{
      project_id: string;
      analysis_id: string;
    }>(
      "SELECT project_id, analysis_id FROM session_context WHERE token_hash = $1",
      [tokenHash],
    );
    expect(grants.rows).toEqual([
      { project_id: "proj_a", analysis_id: "an_1" },
    ]);
  });

  test("insertAnalysis rolls back the analyses row when the session_context grant fails", async () => {
    const sharedHash = "c".repeat(64);
    await sql.insertAnalysis({
      id: "an_first",
      sessionId: "sesn_first",
      projectId: "proj_a",
      inputs: VALID_INPUTS,
      createdBy: "curator@example.org",
      tokenHash: sharedHash,
    });

    await expect(
      sql.insertAnalysis({
        id: "an_rolled_back",
        sessionId: "sesn_rolled_back",
        projectId: "proj_a",
        inputs: VALID_INPUTS,
        createdBy: "curator@example.org",
        tokenHash: sharedHash,
      }),
    ).rejects.toThrow();

    await expect(sql.getAnalysis("an_rolled_back")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("getAnalysis raises ResourceNotFoundError for an unknown id", async () => {
    await expect(sql.getAnalysis("an_missing")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("listAnalysesIn returns only rows in the requested Projects", async () => {
    await sql.insertAnalysis({
      id: "an_proj_a",
      sessionId: "sesn_a",
      projectId: "proj_a",
      inputs: VALID_INPUTS,
      createdBy: "curator@example.org",
      tokenHash: "d".repeat(64),
    });
    await sql.insertAnalysis({
      id: "an_proj_b",
      sessionId: "sesn_b",
      projectId: "proj_b",
      inputs: VALID_INPUTS,
      createdBy: "curator@example.org",
      tokenHash: "e".repeat(64),
    });

    const listed = await sql.listAnalysesIn(["proj_a"]);
    expect(listed.map((analysis) => analysis.id)).toEqual(["an_proj_a"]);
  });

  test("listAnalysesIn renders an undecodable row as an unreadable scenario while keeping decodable rows", async () => {
    await sql.insertAnalysis({
      id: "an_valid",
      sessionId: "sesn_valid",
      projectId: "proj_a",
      inputs: VALID_INPUTS,
      createdBy: "curator@example.org",
      tokenHash: "b".repeat(64),
    });
    await db.adminPool.query(
      `INSERT INTO analyses (id, session_id, project_id, inputs, created_by)
       VALUES ($1, $2, $3, $4, $5)`,
      [
        "an_corrupt",
        "sesn_corrupt",
        "proj_a",
        Buffer.from([0xff, 0xff, 0xff]),
        "curator@example.org",
      ],
    );

    const listed = await sql.listAnalysesIn(["proj_a"]);
    const corrupt = listed.find((analysis) => analysis.id === "an_corrupt");
    const valid = listed.find((analysis) => analysis.id === "an_valid");
    expect(corrupt?.projectId).toBe("proj_a");
    // `inputs` itself must be present (an empty AnalysisInputs message) so `requireInputs` on `/`
    // renders the unreadable-scenario card instead of throwing on missing `inputs`.
    expect(corrupt?.inputs).toBeDefined();
    expect(corrupt?.inputs?.scenario.case).toBeUndefined();
    expect(valid?.inputs?.scenario.case).toBe("variantClassification");
  });

  test("getAnalysis raises UndecodableAnalysisError naming the row's Project when inputs do not decode", async () => {
    await db.adminPool.query(
      `INSERT INTO analyses (id, session_id, project_id, inputs, created_by)
       VALUES ($1, $2, $3, $4, $5)`,
      [
        "an_corrupt",
        "sesn_corrupt",
        "proj_a",
        Buffer.from([0xff, 0xff, 0xff]),
        "curator@example.org",
      ],
    );

    const error = await sql
      .getAnalysis("an_corrupt")
      .catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(UndecodableAnalysisError);
    expect((error as UndecodableAnalysisError).message).toContain("an_corrupt");
    expect((error as UndecodableAnalysisError).projectId).toBe("proj_a");
  });

  test("listAnalysesIn short-circuits when given no Projects", async () => {
    expect(await sql.listAnalysesIn([])).toEqual([]);
  });
});
