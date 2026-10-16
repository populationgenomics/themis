import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { create } from "@bufbuild/protobuf";
import { createValidator } from "@bufbuild/protovalidate";
import { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import { VariantClassificationInputsSchema } from "@/gen/themis/workbench/models/workbench_pb";
import { isClientInputError } from "@/server/errors";
import { FixtureCurationStore } from "./fixture";
import { toCurationErrorResponse } from "./http";
import { CLINICAL_CONTEXT_MAX_LENGTH, clinicalContextLength } from "./model";
import { parseRegistration } from "./registration";
import { REGISTRATION_KEY } from "./sql";
import { isDuplicateRegistrationError } from "./store";

// A registration states the whole question, the entity and the clinical context, and states it
// once.

const BODY = {
  gene: "FBN1",
  transcript: "NM_000138.5",
  hgvsC: "c.7003C>T",
  diseaseLabel: "Marfan syndrome",
  mondoId: "MONDO:0007947",
  modeOfInheritance: "MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT",
  clinicalContext: "Tall stature, arachnodactyly and aortic root dilation.",
};

function refusal(body: Record<string, unknown>): string {
  try {
    parseRegistration(body);
  } catch (error) {
    if (isClientInputError(error)) return error.message;
    throw error;
  }
  throw new Error("the registration was accepted");
}

describe("a register request", () => {
  test("reads the mode as the member it names", () => {
    expect(parseRegistration(BODY).modeOfInheritance).toBe(
      ModeOfInheritance.AUTOSOMAL_DOMINANT,
    );
  });

  test.each([
    [{ mondoId: undefined }, /mondoId is required/],
    [{ mondoId: "" }, /mondoId is required/],
    [{ mondoId: "Marfan syndrome" }, /not a MONDO id/],
    [{ mondoId: "MONDO:7947" }, /not a MONDO id/],
    [{ modeOfInheritance: undefined }, /modeOfInheritance is required/],
    [
      { modeOfInheritance: "MODE_OF_INHERITANCE_UNSPECIFIED" },
      /not a mode of inheritance/,
    ],
    [{ modeOfInheritance: "X_LINKED" }, /not a mode of inheritance/],
    [{ clinicalContext: undefined }, /clinicalContext is required/],
    [{ clinicalContext: " \n\t " }, /clinicalContext is required/],
    [
      { clinicalContext: "x".repeat(CLINICAL_CONTEXT_MAX_LENGTH + 1) },
      /a registration states at most 10000/,
    ],
  ])("%p is refused", (change, message) => {
    expect(refusal({ ...BODY, ...change })).toMatch(message);
  });

  test("keeps the clinical context as written, without the whitespace around it", () => {
    expect(
      parseRegistration({
        ...BODY,
        clinicalContext: "\n Line one.\nLine two. ",
      }).clinicalContext,
    ).toBe("Line one.\nLine two.");
  });
});

describe("a clinical context's length", () => {
  test("leaves out the whitespace around the prose", () => {
    expect(clinicalContextLength("\n  ab c \t")).toBe(4);
  });

  test("puts the maximum within the bound and one more over it", () => {
    for (const [extra, over] of [
      [0, false],
      [1, true],
    ] as const) {
      const raw = ` ${"x".repeat(CLINICAL_CONTEXT_MAX_LENGTH + extra)} `;
      expect(clinicalContextLength(raw) > CLINICAL_CONTEXT_MAX_LENGTH).toBe(
        over,
      );
    }
  });
});

describe("the longest clinical context a registration takes", () => {
  const validator = createValidator();
  const run = (length: number) =>
    validator.validate(
      VariantClassificationInputsSchema,
      create(VariantClassificationInputsSchema, {
        transcript: "NM_000138.5",
        hgvsC: "c.7003C>T",
        clinicalContext: "x".repeat(length),
      }),
    ).kind;

  test("is the bound the classification scenario sets on a run's clinical context", () => {
    expect(run(CLINICAL_CONTEXT_MAX_LENGTH)).toBe("valid");
    expect(run(CLINICAL_CONTEXT_MAX_LENGTH + 1)).toBe("invalid");
  });

  test("is accepted by the register request", () => {
    expect(
      parseRegistration({
        ...BODY,
        clinicalContext: "x".repeat(CLINICAL_CONTEXT_MAX_LENGTH),
      }).clinicalContext,
    ).toHaveLength(CLINICAL_CONTEXT_MAX_LENGTH);
  });

  test("is the bound the latest migration to bound the column declares", () => {
    // A context the surface accepts and the database refuses would fail the register as a 500.
    const migrations = join(
      import.meta.dir,
      "../../../../themis/migrate/migrations",
    );
    const bounds = readdirSync(migrations)
      .sort()
      .flatMap((name) => [
        ...readFileSync(join(migrations, name), "utf-8").matchAll(
          /char_length\(clinical_context\) <= (\d+)/g,
        ),
      ])
      .map((match) => Number(match[1]));
    expect(bounds.at(-1)).toBe(CLINICAL_CONTEXT_MAX_LENGTH);
  });
});

describe("a second registration of one entity", () => {
  const variant = parseRegistration(BODY);

  test("is refused as a conflict that names the entity", async () => {
    const store = new FixtureCurationStore();
    await store.createVariant(variant, "manager@example.org");
    const error = await store
      .createVariant(variant, "manager@example.org")
      .then(
        () => undefined,
        (e: unknown) => e,
      );
    expect(isDuplicateRegistrationError(error)).toBe(true);
    const response = toCurationErrorResponse(error);
    expect(response.status).toBe(409);
    expect((await response.json()).error.message).toBe(
      "NM_000138.5:c.7003C>T is already registered against MONDO:0007947 as autosomal dominant",
    );
  });

  test("is refused in SQL by a constraint a migration declares", () => {
    // The SQL store names the constraint in its ON CONFLICT; a rename there or in a migration would
    // turn every registration into a 500.
    const migrations = join(
      import.meta.dir,
      "../../../../themis/migrate/migrations",
    );
    expect(
      readdirSync(migrations).some((name) =>
        readFileSync(join(migrations, name), "utf-8").includes(
          `ADD CONSTRAINT ${REGISTRATION_KEY}`,
        ),
      ),
    ).toBe(true);
  });

  test("under another mode is a registration of its own", async () => {
    const store = new FixtureCurationStore();
    await store.createVariant(variant, "manager@example.org");
    const recessive = await store.createVariant(
      { ...variant, modeOfInheritance: ModeOfInheritance.AUTOSOMAL_RECESSIVE },
      "manager@example.org",
    );
    expect(recessive.modeOfInheritance).toBe(
      ModeOfInheritance.AUTOSOMAL_RECESSIVE,
    );
  });
});
