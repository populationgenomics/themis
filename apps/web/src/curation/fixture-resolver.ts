import { ClientInputError } from "@/server/errors";
import {
  AlleleNotResolvedError,
  CLINGEN_ALLELE_ID,
  type ResolvedAllele,
  type VariantResolver,
} from "./resolver";

// The offline resolver, so registering a variant is exercisable and screenshottable with no network.
//
// Seeded with the three alleles the offline surface needs: the two the fixture store already holds,
// and one that is not registered there, so the retrieve-review-add path ends in a new variant rather
// than a duplicate. Every field is the registry's own answer for that allele, read from it rather
// than invented — a fixture stating a coordinate the registry does not would teach a reader a wrong
// one, and the FBN1 record is what `registry-resolver.test.ts` parses.

const SEEDED: ResolvedAllele[] = [
  {
    clingenAlleleId: "CA016924",
    gene: "FBN1",
    transcript: "NM_000138.5",
    hgvsC: "c.7003C>T",
    hgvsP: "NP_000129.3:p.Arg2335Trp",
    hgvsG: "NC_000015.10:g.48427768G>A",
  },
  {
    clingenAlleleId: "CA011552",
    gene: "MYH7",
    transcript: "NM_000257.4",
    hgvsC: "c.1988G>A",
    hgvsP: "NP_000248.2:p.Arg663His",
    hgvsG: "NC_000014.9:g.23426833C>T",
  },
  {
    clingenAlleleId: "CA118639",
    gene: "CFTR",
    transcript: "NM_000492.4",
    hgvsC: "c.1521_1523del",
    hgvsP: "NP_000483.3:p.Phe508del",
    hgvsG: "NC_000007.14:g.117559592_117559594del",
  },
];

export class FixtureVariantResolver implements VariantResolver {
  private readonly alleles = new Map(
    SEEDED.map((allele) => [allele.clingenAlleleId, allele]),
  );

  async resolve(clingenAlleleId: string): Promise<ResolvedAllele> {
    const id = clingenAlleleId.trim().toUpperCase();
    if (!CLINGEN_ALLELE_ID.test(id)) {
      throw new ClientInputError(
        `${clingenAlleleId} is not a ClinGen allele id: expected the form CA123456`,
      );
    }
    const allele = this.alleles.get(id);
    if (!allele) {
      // Unseeded is a refusal, not a stand-in: a fixture that answered anything for any id would let
      // the surface look as though it had resolved a variant nobody registered.
      throw new AlleleNotResolvedError(
        `the offline allele set holds no ${id}; the real registry may well hold it`,
      );
    }
    return allele;
  }
}
