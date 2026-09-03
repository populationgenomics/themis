import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { FixtureVariantResolver } from "./fixture-resolver";
import { buildVariantResolver } from "./index";
import { parseAllele, RegistryVariantResolver } from "./registry-resolver";

// The four identity fields, read off a real ClinGen Allele Registry response.
//
// Vendored whole rather than trimmed: what the parser has to do is take four fields and leave a large
// payload alone, and a fixture cut down to the four it reads could not show that.

const CA016924 = JSON.parse(
  readFileSync(
    join(process.cwd(), "fixtures", "allele-registry-CA016924.json"),
    "utf-8",
  ),
);

describe("reading an allele out of the registry's answer", () => {
  test("takes the MANE Select record's gene, transcript and HGVS", () => {
    expect(parseAllele("CA016924", CA016924)).toEqual({
      clingenAlleleId: "CA016924",
      gene: "FBN1",
      transcript: "NM_000138.5",
      hgvsC: "c.7003C>T",
      hgvsP: "NP_000129.3:p.Arg2335Trp",
      hgvsG: "NC_000015.10:g.48427768G>A",
    });
  });

  test("takes the id the registry answered with, not the one asked for", () => {
    // The registry canonicalises; what gets stored has to be what resolved.
    expect(parseAllele("CA99999999", CA016924).clingenAlleleId).toBe(
      "CA016924",
    );
  });

  test("the vendored response carries more than the parser reads", () => {
    // Rules out a fixture reduced to exactly the fields under test.
    expect(Object.keys(CA016924)).toContain("externalRecords");
    expect(CA016924.transcriptAlleles.length).toBeGreaterThan(1);
  });

  test("refuses a record with no MANE Select rather than picking a transcript", () => {
    const noMane = {
      ...CA016924,
      transcriptAlleles: [
        { geneSymbol: "FBN1", hgvs: ["ENST00000316623.10:c.7003C>T"] },
      ],
    };
    expect(() => parseAllele("CA016924", noMane)).toThrow(/no MANE Select/);
  });

  test("refuses a MANE Select record naming no gene", () => {
    const noGene = {
      ...CA016924,
      transcriptAlleles: [
        {
          MANE: {
            maneStatus: "MANE Select",
            nucleotide: { RefSeq: { hgvs: "NM_000138.5:c.7003C>T" } },
          },
        },
      ],
    };
    expect(() => parseAllele("CA016924", noGene)).toThrow(/no gene symbol/);
  });

  test("refuses an unreadable transcript HGVS", () => {
    const bad = {
      ...CA016924,
      transcriptAlleles: [
        {
          geneSymbol: "FBN1",
          MANE: {
            maneStatus: "MANE Select",
            nucleotide: { RefSeq: { hgvs: "NM_000138.5" } },
          },
        },
      ],
    };
    expect(() => parseAllele("CA016924", bad)).toThrow(/unreadable transcript/);
  });

  test("a missing genomic form is left empty, not a refusal", () => {
    // Shown for confirmation only, so its absence must not block a registration.
    const noGenome = { ...CA016924, genomicAlleles: [] };
    expect(parseAllele("CA016924", noGenome).hgvsG).toBe("");
  });
});

describe("the offline resolver", () => {
  const resolver = new FixtureVariantResolver();

  test("answers the seeded alleles", async () => {
    expect((await resolver.resolve("CA016924")).gene).toBe("FBN1");
  });

  test("accepts a lowercase id, since a manager pastes what they have", async () => {
    expect((await resolver.resolve(" ca016924 ")).gene).toBe("FBN1");
  });

  test("refuses an id it was not seeded with, rather than inventing one", async () => {
    // A fixture that answered anything for any id would let the surface look as though it had
    // resolved a variant nobody registered. It says the offline set lacks the id, not that the
    // registry does — the registry very likely holds it.
    expect(resolver.resolve("CA000001")).rejects.toThrow(/offline allele set/);
  });

  test.each(["", "CA", "rs1234", "NM_000138.5:c.7003C>T", "PA123456"])(
    "refuses %p as not an allele id",
    async (id) => {
      expect(resolver.resolve(id)).rejects.toThrow(/not a ClinGen allele id/);
    },
  );
});

describe("which resolver a backend selects", () => {
  test("live picks the registry, fixture picks the offline set", () => {
    expect(buildVariantResolver({ THEMIS_BACKEND: "live" })).toBeInstanceOf(
      RegistryVariantResolver,
    );
    expect(buildVariantResolver({ THEMIS_BACKEND: "fixture" })).toBeInstanceOf(
      FixtureVariantResolver,
    );
  });

  test.each([undefined, "", "prod", "FIXTURE"])(
    "%p is refused rather than defaulting",
    (backend) => {
      // A lost variable must not quietly resolve alleles against the offline set, which would let a
      // manager register an identity the registry never stated.
      expect(() => buildVariantResolver({ THEMIS_BACKEND: backend })).toThrow(
        /THEMIS_BACKEND/,
      );
    },
  );
});
