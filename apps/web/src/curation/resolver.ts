// Looking a variant's identity up from a ClinGen allele id, so a manager registering one types the
// identifier and not the identity.
//
// Identity only. The registry also carries dbSNP and ClinVar crosswalks, and neither is requested:
// a route to ClinVar's existing classification, one click from a worksheet header, is exactly the
// anchoring the worksheet is built to avoid (curation-surface.md §Assistance anchors).
//
// Its own port rather than `Variant.Normalize`. That rpc canonicalises the other way — versioned
// RefSeq transcript HGVS in, a CAID out — and every evidence interface gates on a session token,
// which is minted per sandbox session and which a curation, having no session, would have to
// counterfeit.

/** A variant as the ClinGen Allele Registry states it, on its MANE Select transcript. */
export interface ResolvedAllele {
  /** The canonical allele id the registry answered with, which may differ in form from the one
   *  asked for — it is stored rather than the query, so what is recorded is what resolved. */
  clingenAlleleId: string;
  gene: string;
  /** Versioned RefSeq transcript of the MANE Select record, e.g. `NM_000138.5`. */
  transcript: string;
  hgvsC: string;
  /** MANE Select protein HGVS, empty where the registry states none (a non-coding consequence). */
  hgvsP: string;
  /** GRCh38 genomic HGVS on the `NC_` accession, for the manager to confirm against. */
  hgvsG: string;
}

export interface VariantResolver {
  /** Resolve one allele id.
   *
   * @throws ClientInputError If the id is not a ClinGen allele id.
   * @throws AlleleNotResolvedError If the registry holds no usable record for it.
   */
  resolve(clingenAlleleId: string): Promise<ResolvedAllele>;
}

/** Thrown where the registry holds no usable record for an id — no such allele, or one carrying no
 *  MANE Select transcript.
 *
 *  Its own type rather than `ResourceNotFoundError`, whose message the module deliberately masks so a
 *  worksheet's existence is not a side channel. Nothing is concealed here: the registry is public, the
 *  id is the caller's own, and the two cases call for different next steps — correct the id, or
 *  register the identity by hand. A 404 saying only "resource not found" tells a manager neither. */
export class AlleleNotResolvedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AlleleNotResolvedError";
  }
}

export function isAlleleNotResolvedError(
  error: unknown,
): error is AlleleNotResolvedError {
  return error instanceof Error && error.name === "AlleleNotResolvedError";
}

/** Thrown where the registry could not be reached or did not answer in time.
 *
 *  Distinct from a masked internal error, which is what an unreachable upstream would otherwise
 *  become: this one is not the manager's fault and not permanent, and the two things they can do about
 *  it — try again, or type the identity — are only obvious if they are told which happened. */
export class AlleleRegistryUnreachableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "AlleleRegistryUnreachableError";
  }
}

export function isAlleleRegistryUnreachableError(
  error: unknown,
): error is AlleleRegistryUnreachableError {
  return (
    error instanceof Error && error.name === "AlleleRegistryUnreachableError"
  );
}

/** The registry's own id form. `PA…` ids exist and name a protein allele, which names no transcript
 *  HGVS, so only `CA…` is accepted. */
export const CLINGEN_ALLELE_ID = /^CA\d{1,20}$/;
