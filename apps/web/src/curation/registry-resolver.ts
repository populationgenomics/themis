import { ClientInputError } from "@/server/errors";
import {
  AlleleNotResolvedError,
  AlleleRegistryUnreachableError,
  CLINGEN_ALLELE_ID,
  type ResolvedAllele,
  type VariantResolver,
} from "./resolver";

// The live resolver: the ClinGen Allele Registry's public read endpoint.
//
// Reads are unauthenticated, so this holds no credential. It takes four fields out of a large
// payload and leaves the rest — see `resolver.ts` for why the crosswalks are not among them.
//
// `themis/services/evidence/upstreams/allele_registry.py` parses the same payload for the evidence
// plane, and much more of it. The overlap is these four fields; `registry-resolver.test.ts` pins them
// against a vendored response so a registry schema change fails here rather than mis-registering a
// variant.

const REGISTRY_URL = "https://reg.clinicalgenome.org/allele";

/** How long the registry gets before a manager is told to try again. A registration is interactive
 *  and re-runnable, so waiting longer buys nothing. */
const TIMEOUT_MS = 10_000;

interface RegistryMane {
  maneStatus?: string;
  nucleotide?: { RefSeq?: { hgvs?: string } };
  protein?: { RefSeq?: { hgvs?: string } };
}

interface RegistryTranscriptAllele {
  geneSymbol?: string;
  MANE?: RegistryMane;
}

interface RegistryGenomicAllele {
  referenceGenome?: string;
  hgvs?: string[];
}

interface RegistryAllele {
  "@id"?: string;
  transcriptAlleles?: RegistryTranscriptAllele[];
  genomicAlleles?: RegistryGenomicAllele[];
}

export class RegistryVariantResolver implements VariantResolver {
  constructor(private readonly baseUrl: string = REGISTRY_URL) {}

  async resolve(clingenAlleleId: string): Promise<ResolvedAllele> {
    const id = clingenAlleleId.trim().toUpperCase();
    if (!CLINGEN_ALLELE_ID.test(id)) {
      throw new ClientInputError(
        `${clingenAlleleId} is not a ClinGen allele id: expected the form CA123456`,
      );
    }
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/${id}`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch (cause) {
      throw new AlleleRegistryUnreachableError(
        `the ClinGen Allele Registry did not answer for ${id}; try again, or enter the identity by hand`,
        { cause },
      );
    }
    if (response.status === 404) {
      throw new AlleleNotResolvedError(
        `the ClinGen Allele Registry holds no allele ${id}`,
      );
    }
    if (!response.ok) {
      throw new AlleleRegistryUnreachableError(
        `the ClinGen Allele Registry answered ${response.status} for ${id}; try again, or enter the identity by hand`,
      );
    }
    return parseAllele(id, (await response.json()) as RegistryAllele);
  }
}

/** The four identity fields, or a refusal naming what the registry did not state. Exported for the
 *  test that pins it to a real response. */
export function parseAllele(
  id: string,
  allele: RegistryAllele,
): ResolvedAllele {
  const mane = (allele.transcriptAlleles ?? []).find(
    (t) => t.MANE?.maneStatus === "MANE Select",
  );
  const refseq = mane?.MANE?.nucleotide?.RefSeq?.hgvs;
  if (!mane || !refseq) {
    // No fallback to an arbitrary transcript: both curators of this variant answer whichever one is
    // registered, so a guess here is a guess they cannot see.
    throw new AlleleNotResolvedError(
      `allele ${id} has no MANE Select RefSeq transcript in the registry; enter its identity by hand`,
    );
  }
  const [transcript, hgvsC] = splitHgvs(refseq, id);
  const gene = mane.geneSymbol;
  if (!gene) {
    throw new AlleleNotResolvedError(
      `allele ${id} names no gene symbol in the registry; enter its identity by hand`,
    );
  }
  return {
    clingenAlleleId: idFrom(allele) ?? id,
    gene,
    transcript,
    hgvsC,
    hgvsP: mane.MANE?.protein?.RefSeq?.hgvs ?? "",
    hgvsG: grch38Hgvs(allele),
  };
}

/** `NM_000138.5:c.7003C>T` → the accession and the change. */
function splitHgvs(hgvs: string, id: string): [string, string] {
  const colon = hgvs.indexOf(":");
  if (colon <= 0 || colon === hgvs.length - 1) {
    throw new AlleleNotResolvedError(
      `allele ${id} states an unreadable transcript HGVS (${hgvs})`,
    );
  }
  return [hgvs.slice(0, colon), hgvs.slice(colon + 1)];
}

/** The registry's `@id` is a URL ending in the canonical id. */
function idFrom(allele: RegistryAllele): string | undefined {
  const match = allele["@id"]?.match(/CA\d{1,20}$/);
  return match?.[0];
}

/** The GRCh38 `NC_` form, empty where the registry states none. Shown for confirmation only, so an
 *  absence is not a refusal. */
function grch38Hgvs(allele: RegistryAllele): string {
  const build = (allele.genomicAlleles ?? []).find(
    (g) => g.referenceGenome === "GRCh38",
  );
  return (build?.hgvs ?? []).find((h) => h.startsWith("NC_")) ?? "";
}
