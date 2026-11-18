import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
} from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  AssessmentSchema,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import { ClientInputError } from "@/server/errors";
import {
  type MigratedDatabase,
  openMigratedDatabase,
  SKIP_WITHOUT_POSTGRES,
} from "@/server/migrated-postgres.test-support";
import { MODES, modeFromColumn } from "./mode";
import {
  CLINICAL_CONTEXT_MAX_LENGTH,
  type Entry,
  type NewVariant,
} from "./model";
import { SqlCurationStore } from "./sql";
import { isDuplicateRegistrationError } from "./store";

const FBN1: NewVariant = {
  gene: "FBN1",
  transcript: "NM_000138.5",
  hgvsC: "c.7003C>T",
  clingenAlleleId: "",
  diseaseLabel: "Marfan syndrome",
  mondoId: "MONDO:0007947",
  modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
  clinicalContext: "Tall stature and ectopia lentis.",
};

function draftEntry(workflowId: string, rationale: string): Entry {
  return {
    workflowId,
    assessment: create(AssessmentSchema, {
      kind: {
        case: "workflow",
        value: create(WorkflowAssessmentSchema, { rationale }),
      },
    }),
  };
}

describe.skipIf(SKIP_WITHOUT_POSTGRES)(
  "SqlCurationStore against Postgres",
  () => {
    let db: MigratedDatabase;
    let store: SqlCurationStore;

    beforeAll(async () => {
      db = await openMigratedDatabase();
      store = new SqlCurationStore(db.webPool);
    }, 30_000);

    afterAll(async () => {
      await db?.close();
    });

    beforeEach(async () => {
      await db.adminPool.query(
        "TRUNCATE curation.assessments, curation.submissions, curation.drafts, curation.worksheets, curation.variants",
      );
    });

    test("registers a variant and reads back its Project, clinical context, and mode of inheritance", async () => {
      const created = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );

      const loaded = await store.getVariant(created.id);
      expect(loaded?.projectId).toBe("validation");
      expect(loaded?.clinicalContext).toBe("Tall stature and ectopia lentis.");
      expect(loaded?.modeOfInheritance).toBe(
        ModeOfInheritance.AUTOSOMAL_DOMINANT,
      );
    });

    test("listVariants returns only registrations in the requested Project", async () => {
      const created = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );
      await store.createVariant("acute-care", FBN1, "manager@example.org");

      expect((await store.listVariants("validation")).map((v) => v.id)).toEqual(
        [created.id],
      );
      expect(await store.listVariants("other-project")).toEqual([]);
    });

    test.each(MODES.map((entry) => entry.mode))(
      "round-trips mode of inheritance %p through curation.variants",
      async (mode) => {
        const created = await store.createVariant(
          "validation",
          { ...FBN1, modeOfInheritance: mode },
          "manager@example.org",
        );
        const loaded = await store.getVariant(created.id);
        expect(loaded?.modeOfInheritance).toBe(mode);
      },
    );

    test("modeFromColumn decodes every mode_of_inheritance value the live constraint admits", async () => {
      const { rows } = await db.adminPool.query<{ def: string }>(
        `SELECT pg_get_constraintdef(c.oid) AS def
         FROM pg_constraint c
         JOIN pg_attribute a
           ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
         WHERE c.conrelid = 'curation.variants'::regclass
           AND c.contype = 'c'
           AND cardinality(c.conkey) = 1
           AND a.attname = 'mode_of_inheritance'`,
      );
      expect(rows).toHaveLength(1);
      const admitted = [...(rows[0]?.def.matchAll(/'([^']+)'/g) ?? [])].map(
        (match) => match[1] as string,
      );
      expect(admitted.length).toBeGreaterThan(0);
      expect(admitted.map(modeFromColumn).sort()).toEqual(
        MODES.map((entry) => entry.mode).sort(),
      );
    });

    test.each([
      "UNSPECIFIED",
      "X_LINKED",
      "MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT",
    ])(
      "curation.variants rejects non-admitted mode_of_inheritance %p",
      async (invalidMode) => {
        await expect(
          db.webPool.query(
            `INSERT INTO curation.variants
               (id, project_id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance, clinical_context, created_by)
             VALUES ($1, 'validation', 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', 'MONDO:0007947', $2, 'Tall stature.', 'manager@example.org')`,
            [`var-${invalidMode}`, invalidMode],
          ),
        ).rejects.toMatchObject({
          code: "23514",
          constraint: expect.stringContaining("mode_of_inheritance"),
        });
      },
    );

    test("accepts clinicalContext at CLINICAL_CONTEXT_MAX_LENGTH", async () => {
      const longest = "x".repeat(CLINICAL_CONTEXT_MAX_LENGTH);
      const created = await store.createVariant(
        "validation",
        { ...FBN1, clinicalContext: longest },
        "manager@example.org",
      );
      expect((await store.getVariant(created.id))?.clinicalContext).toBe(
        longest,
      );
    });

    test("rejects clinicalContext past CLINICAL_CONTEXT_MAX_LENGTH", async () => {
      await expect(
        store.createVariant(
          "validation",
          {
            ...FBN1,
            clinicalContext: "x".repeat(CLINICAL_CONTEXT_MAX_LENGTH + 1),
          },
          "manager@example.org",
        ),
      ).rejects.toMatchObject({
        code: "23514",
        constraint: expect.stringContaining("clinical_context"),
      });
    });

    test("refuses a duplicate registration of the same entity in the same Project", async () => {
      await store.createVariant("validation", FBN1, "manager@example.org");
      const error = await store
        .createVariant("validation", FBN1, "manager@example.org")
        .catch((thrown: unknown) => thrown);
      expect(isDuplicateRegistrationError(error)).toBe(true);
    });

    test("admits the same variant in the same Project under another mode of inheritance", async () => {
      const dominant = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );
      const recessive = await store.createVariant(
        "validation",
        {
          ...FBN1,
          modeOfInheritance: ModeOfInheritance.AUTOSOMAL_RECESSIVE,
        },
        "manager@example.org",
      );
      const listed = await store.listVariants("validation");
      expect(listed).toHaveLength(2);
      expect(new Set(listed.map((v) => v.id))).toEqual(
        new Set([dominant.id, recessive.id]),
      );
      expect(listed.map((v) => v.modeOfInheritance).sort()).toEqual(
        [
          ModeOfInheritance.AUTOSOMAL_DOMINANT,
          ModeOfInheritance.AUTOSOMAL_RECESSIVE,
        ].sort(),
      );
    });

    test("submit copies selected drafts into assessments that stay unchanged when drafts are edited", async () => {
      const variant = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );
      const worksheet = await store.assign(
        variant.id,
        "curator@example.org",
        "v1",
        "manager@example.org",
      );
      await store.putDraft(
        worksheet.id,
        draftEntry("POP_FRQ", "Absent from gnomAD v4.1."),
      );
      await store.putDraft(
        worksheet.id,
        draftEntry("CLN_DNV", "Unselected scratch draft."),
      );

      const submission = await store.submit(worksheet.id, "initial", [
        "POP_FRQ",
      ]);

      await store.putDraft(
        worksheet.id,
        draftEntry("POP_FRQ", "Edited after submission."),
      );
      const currentDrafts = await store.drafts(worksheet.id);
      expect(
        currentDrafts.find((entry) => entry.workflowId === "POP_FRQ")
          ?.assessment.kind,
      ).toMatchObject({
        case: "workflow",
        value: { rationale: "Edited after submission." },
      });

      const committed = await store.assessments(submission.id);
      expect(committed).toHaveLength(1);
      expect(committed[0]?.workflowId).toBe("POP_FRQ");
      expect(committed[0]?.assessment.kind).toMatchObject({
        case: "workflow",
        value: { rationale: "Absent from gnomAD v4.1." },
      });
    });

    test("submit with no matching drafts rolls back the submission, leaving withdraw allowed", async () => {
      const variant = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );
      const worksheet = await store.assign(
        variant.id,
        "curator@example.org",
        "v1",
        "manager@example.org",
      );

      await expect(
        store.submit(worksheet.id, "empty", ["POP_FRQ"]),
      ).rejects.toBeInstanceOf(ClientInputError);
      expect(await store.submissions(worksheet.id)).toEqual([]);

      await store.putDraft(worksheet.id, draftEntry("POP_FRQ", "Scratch."));
      await store.withdraw(worksheet.id);
      expect(await store.getWorksheet(worksheet.id)).toBeUndefined();
      expect(await store.drafts(worksheet.id)).toEqual([]);
    });

    test("withdraw refuses a submitted worksheet", async () => {
      const variant = await store.createVariant(
        "validation",
        FBN1,
        "manager@example.org",
      );
      const worksheet = await store.assign(
        variant.id,
        "curator@example.org",
        "v1",
        "manager@example.org",
      );
      await store.putDraft(
        worksheet.id,
        draftEntry("POP_FRQ", "Absent from gnomAD v4.1."),
      );
      await store.submit(worksheet.id, "initial", ["POP_FRQ"]);

      await expect(store.withdraw(worksheet.id)).rejects.toBeInstanceOf(
        ClientInputError,
      );
    });

    test("refuses a drifted row missing its clinical context, naming the row", async () => {
      await db.adminPool.query(
        "ALTER TABLE curation.variants ALTER COLUMN clinical_context DROP NOT NULL",
      );
      try {
        await db.adminPool.query(
          `INSERT INTO curation.variants
             (id, project_id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance, clinical_context, created_by)
           VALUES ('var-1', 'validation', 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT', NULL, 'manager@example.org')`,
        );
        await expect(store.getVariant("var-1")).rejects.toThrow(
          /curation\.variants row var-1 holds no clinical context/,
        );
        await expect(store.listVariants("validation")).rejects.toThrow(
          /curation\.variants row var-1 holds no clinical context/,
        );
      } finally {
        await db.adminPool.query("TRUNCATE curation.variants CASCADE");
        await db.adminPool.query(
          "ALTER TABLE curation.variants ALTER COLUMN clinical_context SET NOT NULL",
        );
      }
    });
  },
);
