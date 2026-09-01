"""gnomAD GraphQL adapter: variant-level frequency data and gene-level constraint signals.

Two calls against the single gnomAD GraphQL endpoint (no auth; ~10 req/IP/min):

- ``fetch_gnomad`` returns one variant's exome/genome/joint frequency block (``faf95`` -> POP_FRQ,
  homozygote/hemizygote counts -> POP_HMZ) and, when a second variant is given, the gnomAD v2
  co-occurrence (biallelic in-trans/cis) assessment. This is a pass-through: the load-bearing
  frequency fields ride in ``raw`` for the model to read; the adapter only fails loud when the
  variant is absent.
- ``fetch_gnomad_gene`` returns the gene-level ExonRelevance signals: LOEUF (``oe_lof_upper``) and
  the pext regions — each with its per-tissue values, since SM18 asks about the disease-relevant
  tissue and a cross-tissue mean answers a different question — with the regional-missense-constraint
  detail kept in ``raw``.

gnomAD answers a bad query with HTTP 200 plus a top-level ``errors`` array and a ``data`` block that
may still carry the field that resolved; the adapter raises only when the load-bearing field
(``variant`` / ``gene``) is itself absent, so a co-occurrence sub-error does not discard good
frequency data. That same 200-with-a-null-field is how it reports an id it could not parse, so
``_require_field`` reads the message to tell an absence from a refusal — for this rpc the absence is
the POP_FRQ evidence, and the two must not collapse.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

import httpx2

from themis.services.evidence import errors

_API_URL = 'https://gnomad.broadinstitute.org/api'
_SOURCE = 'gnomAD GraphQL'

# The message gnomAD refuses an id with, and the ones it states an absence with. Both sets are
# needed, and the refusal is checked first, because one `errors` array covers every root field and
# carries `path: null` on each entry — nothing attributes a message to the field it came from. A
# query that also asks for co-occurrence can therefore carry a refusal of the SECOND id beside a
# genuine absence of the first, and reading that as "no record" settles a caller's typo as a rarity
# finding. A gene symbol gnomAD cannot parse comes back "Gene not found" too, which is honest: there
# the absence is not evidence, only ExonRelevance having no constraint to read.
_REFUSED = frozenset({'Invalid variant ID'})
_NOT_HELD = frozenset({'Variant not found', 'Gene not found'})

# The variant frequency selection: exome/genome/joint AC/AN/AF, the filtering-allele-frequency
# (faf95) POP_FRQ input, per-population homozygote/hemizygote POP_HMZ counts, and the exome age
# distribution. Reused verbatim by every variant query.
_VARIANT_SELECTION = """
    variant_id
    exome {
      ac an af homozygote_count hemizygote_count filters flags
      faf95 { popmax popmax_population }
      populations { id ac an homozygote_count hemizygote_count }
      age_distribution { het { bin_edges bin_freq } hom { bin_edges bin_freq } }
    }
    genome {
      ac an af homozygote_count hemizygote_count filters flags
      faf95 { popmax popmax_population }
      populations { id ac an homozygote_count hemizygote_count }
    }
    joint {
      ac an homozygote_count hemizygote_count
      faf95 { popmax popmax_population }
      populations { id ac an homozygote_count hemizygote_count }
      filters
    }
"""

# Co-occurrence is a gnomAD v2 feature only (dataset hard-wired), so the query pins gnomad_r2_1 on
# that root field regardless of the primary variant's dataset.
_COOCCURRENCE_SELECTION = """
  variant_cooccurrence(variants: [$id, $b], dataset: gnomad_r2_1) {
    variant_ids genotype_counts haplotype_counts p_compound_heterozygous
  }
"""

_GENE_QUERY = """
query Gene($gene: String!) {
  gene(gene_symbol: $gene, reference_genome: GRCh38) {
    gene_id
    symbol
    gnomad_constraint { oe_lof_upper pli mis_z }
    mane_select_transcript { ensembl_id ensembl_version refseq_id refseq_version }
    pext { regions { start stop mean tissues { tissue value } } }
    gnomad_v2_regional_missense_constraint { regions { start stop obs_exp chisq_diff_null } }
  }
}
"""


@dataclasses.dataclass(frozen=True)
class GnomadResult:
    """One variant's gnomAD frequency payload (pass-through) plus its provenance.

    The POP_FRQ / POP_HMZ / co-occurrence fields are not lifted out — gnomAD's schema is
    "subject to change" and the model reads them straight from ``raw`` — so the typed surface is
    just provenance.

    Attributes:
        raw: The GraphQL ``data`` block: ``{"variant": {...}, "variant_cooccurrence": {...}?}``.
        source: The upstream label.
        dataset_versions: The gnomAD dataset queried (e.g. ``"gnomad_r4"``).
        query: The variant id + dataset issued, for replay.
    """

    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


@dataclasses.dataclass(frozen=True)
class PextRegion:
    """One gnomAD pext region: the proportion expressed across transcripts over ``[start, stop]``.

    Attributes:
        start: First genomic coordinate of the region (GRCh38, 1-based inclusive).
        stop: Last genomic coordinate.
        mean: The cross-tissue summary gnomAD publishes for the region.
        tissues: The per-tissue values, keyed by gnomAD's pext tissue id (the GTEx
            ``tissueSiteDetailId`` lower-cased with hyphens as underscores — ``pext_tissue_key``).
            gnomAD carries all 49 GTEx tissues it has pext for on every region. Not the terms of
            ``mean``: the two are separately defined statistics and do not agree (ANO5's first region
            publishes 0.8277 where these average 0.7907), so they are comparable within their own
            kind and not across.
    """

    start: int
    stop: int
    mean: float
    tissues: Mapping[str, float]


@dataclasses.dataclass(frozen=True)
class ManeSelectPair:
    """The MANE Select transcript named in both namespaces, as gnomAD publishes the pair.

    Attributes:
        refseq: The versioned RefSeq accession.
        ensembl: The versioned Ensembl accession.
    """

    refseq: str
    ensembl: str


@dataclasses.dataclass(frozen=True)
class GnomadGeneResult:
    """Gene-level gnomAD constraint signals for ExonRelevance, plus provenance.

    Attributes:
        loeuf: ``gnomad_constraint.oe_lof_upper`` — the LOEUF; ``None`` when the gene carries no
            constraint (unconstrained-in-data), never 0.0.
        mane_select: The MANE Select pair behind the pext regions; ``None`` when gnomAD holds none.
        pext_regions: The per-region proportion-expressed values.
        raw: The GraphQL ``gene`` object (constraint, pext, regional missense constraint).
        source: The upstream label.
        dataset_versions: The gnomAD release the constraint is from.
        query: The gene symbol issued, for replay.
    """

    loeuf: float | None
    mane_select: ManeSelectPair | None
    pext_regions: list[PextRegion]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


async def _post_graphql(query: str, variables: dict[str, str], *, http_client: httpx2.AsyncClient) -> dict[str, object]:
    """POST a GraphQL query and return the decoded response body.

    Not on ``errors.raise_for_status``; the taxonomy note in ``docs/design/evidence-interfaces.md`` says why.

    Raises:
        httpx2.HTTPStatusError: If gnomAD returns a non-2xx status. None of them is a verdict on a
            caller-supplied field: those ride in the GraphQL variables and come back inside a 200.
            Measured, the rate limiter answers 429 (from request 11 in a burst) and a malformed
            query document answers 400 — ours to fix, not the caller's.
    """
    response = await http_client.post(_API_URL, json={'query': query, 'variables': variables})
    response.raise_for_status()
    return response.json()


def _variant_query(*, with_cooccurrence: bool) -> str:
    """The variant GraphQL query, with the v2 co-occurrence root field appended when requested."""
    body = f'  variant(variantId: $id, dataset: $ds) {{{_VARIANT_SELECTION}  }}'
    if with_cooccurrence:
        header = 'query Variant($id: String!, $ds: DatasetId!, $b: String!)'
        return f'{header} {{\n{body}\n{_COOCCURRENCE_SELECTION}}}'
    return f'query Variant($id: String!, $ds: DatasetId!) {{\n{body}\n}}'


def _messages(payload: dict[str, object]) -> list[str]:
    """The ``errors`` array's messages, in order."""
    reported = payload.get('errors')
    if not isinstance(reported, list):
        return []
    return [
        message for entry in reported if isinstance(entry, dict) and isinstance(message := entry.get('message'), str)
    ]


def _require_field(payload: dict[str, object], field: str, *, context: str) -> dict[str, object]:
    """Return ``payload["data"][field]``, raising if the load-bearing field did not resolve.

    gnomAD reports a resolution failure as a 200 with a top-level ``errors`` array and the field left
    null — and it uses that same shape for a variant it does not hold and for one it could not parse.
    Only the message tells them apart, so it is read rather than merely surfaced: ``NOT_FOUND`` from
    this rpc is the POP_FRQ rarity input, so answering it for an id gnomAD never understood would
    score a caller's typo as "absent from gnomAD" — and, being settled, never retry it.
    """
    data = payload.get('data')
    if isinstance(data, dict) and isinstance(data.get(field), dict):
        return data
    reported = _messages(payload)
    if not reported:
        raise ValueError(f'gnomAD returned no {field} for {context} and no reason; the response is malformed')
    detail = '; '.join(reported)
    # Ordered, not quantified: a refusal anywhere in the array wins, because `path: null` means it
    # may belong to any id in the query, and only an array with no refusal at all can settle an
    # absence. A quantifier over the absence set alone gets one of the two live shapes wrong either
    # way — `all` loses the primary absence, `any` answers a typo'd second id as "no record".
    if any(message in _REFUSED for message in reported):
        raise errors.InvalidRequestError(f'gnomAD did not accept the query for {context}: {detail}')
    if any(message in _NOT_HELD for message in reported):
        raise errors.UnknownVariantError(f'gnomAD holds no {field} for {context}: {detail}')
    raise errors.InvalidRequestError(f'gnomAD did not accept the query for {context}: {detail}')


async def fetch_gnomad(
    gnomad_id: str,
    dataset: str,
    *,
    http_client: httpx2.AsyncClient,
    cooccurrence_with: str | None = None,
) -> GnomadResult:
    """Fetch one variant's gnomAD frequency block (and optional co-occurrence).

    Args:
        gnomad_id: The gnomAD variant id (chrom-pos-ref-alt); its build must match ``dataset``.
        dataset: ``"gnomad_r4"`` (GRCh38) or ``"gnomad_r2_1"`` (GRCh37 / co-occurrence).
        http_client: The async HTTP client (caller owns its lifecycle).
        cooccurrence_with: A second gnomAD v2 variant id to assess biallelic in-trans/cis against;
            both ids must exist in gnomAD v2.

    Returns:
        The ``GnomadResult``: the ``data`` block (variant, plus ``variant_cooccurrence`` when
        requested) and provenance.

    Raises:
        httpx2.HTTPStatusError: If gnomAD returns a non-2xx status.
        errors.UnknownVariantError: If gnomAD holds no record of the variant in ``dataset``.
        errors.InvalidRequestError: If gnomAD could not parse ``gnomad_id`` / ``cooccurrence_with``.
        ValueError: If the variant did not resolve and gnomAD gave no reason.
    """
    variables = {'id': gnomad_id, 'ds': dataset}
    # $b is only referenced by the co-occurrence field; declare it only when that field is present.
    if cooccurrence_with is not None:
        variables['b'] = cooccurrence_with
    query = _variant_query(with_cooccurrence=cooccurrence_with is not None)

    payload = await _post_graphql(query, variables, http_client=http_client)
    context = f'{gnomad_id!r} in {dataset}'
    data = _require_field(payload, 'variant', context=context)
    replay = f'variant(variantId: "{gnomad_id}", dataset: {dataset})'
    if cooccurrence_with is not None:
        replay += f' + variant_cooccurrence([{gnomad_id}, {cooccurrence_with}], gnomad_r2_1)'
    return GnomadResult(raw=data, source=_SOURCE, dataset_versions=(dataset,), query=replay)


def pext_tissue_key(gtex_tissue_site_detail_id: str) -> str:
    """The gnomAD pext column name for a GTEx ``tissueSiteDetailId``.

    gnomAD keys pext by the GTEx id lower-cased with hyphens as underscores
    (``Cells_EBV-transformed_lymphocytes`` -> ``cells_ebv_transformed_lymphocytes``). Verified
    against the live vocabularies: the mapping is total over gnomAD's 49 columns, and the 5 GTEx
    tissues it leaves unmatched (Bladder, Cervix_Ectocervix, Cervix_Endocervix, Fallopian_Tube,
    Kidney_Medulla) are ones gnomAD holds no pext for.
    """
    return gtex_tissue_site_detail_id.lower().replace('-', '_')


def _pext_tissues(region: dict[str, object]) -> dict[str, float]:
    """One region's per-tissue pext values.

    Raises:
        ValueError: If an entry is not a ``tissue``/``value`` pair — a tissue silently dropped here
            would come back as "gnomAD carries no pext for that tissue".
    """
    tissues = region.get('tissues')
    if not isinstance(tissues, list) or not tissues:
        # An empty list passes the cross-region vocabulary check and then reports every requested
        # tissue as one gnomAD holds no pext for, over regions that do publish a mean.
        raise ValueError(f'gnomAD pext region {region.get("start")}-{region.get("stop")} carries no tissues')
    values: dict[str, float] = {}
    for entry in tissues:
        if not isinstance(entry, dict) or not isinstance(name := entry.get('tissue'), str):
            raise ValueError(f'gnomAD pext tissue entry is not a tissue/value pair: {entry!r}')
        if not isinstance(value := entry.get('value'), (int, float)):
            raise ValueError(f'gnomAD pext tissue {name!r} carries no numeric value: {entry!r}')
        values[name] = float(value)
    return values


def _pext_region(region: object) -> PextRegion:
    """One pext region.

    Raises:
        ValueError: If the region is not an object carrying a span, a mean and its tissues — a
            dropped region silently shifts both an exon's weighted mean and its covered span.
    """
    if not isinstance(region, dict):
        raise ValueError(f'gnomAD pext region is not an object: {region!r}')
    try:
        return PextRegion(
            start=int(region['start']),
            stop=int(region['stop']),
            mean=float(region['mean']),
            tissues=_pext_tissues(region),
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f'gnomAD pext region is missing a span or mean: {region!r}') from e


def _pext_regions(gene: dict[str, object]) -> list[PextRegion]:
    """The gene's pext regions.

    Raises:
        ValueError: If the payload is malformed, or if the regions disagree on which tissues they
            carry — the per-exon values are weighted across the regions covering an exon, so a tissue
            missing from some of them would be averaged over the wrong span. Only a null ``pext``
            reads as "gnomAD holds none for this gene"; a shape that cannot be parsed is a fault, and
            a dropped region would silently shift both the weighted mean and its covered span.
    """
    pext = gene.get('pext')
    if pext is None:
        return []
    if not isinstance(pext, dict):
        raise ValueError(f'gnomAD pext for {gene.get("symbol")!r} is neither an object nor null: {pext!r}')
    regions = pext.get('regions')
    if not isinstance(regions, list):
        raise ValueError(f'gnomAD pext for {gene.get("symbol")!r} carries no regions list')
    parsed = [_pext_region(region) for region in regions]
    vocabularies = {frozenset(region.tissues) for region in parsed}
    if len(vocabularies) > 1:
        raise ValueError(f'gnomAD pext regions carry {len(vocabularies)} different tissue vocabularies')
    return parsed


def _loeuf(gene: dict[str, object]) -> float | None:
    constraint = gene.get('gnomad_constraint')
    if not isinstance(constraint, dict):
        return None
    value = constraint.get('oe_lof_upper')
    return float(value) if isinstance(value, (int, float)) else None


def _mane_select(gene: dict[str, object]) -> ManeSelectPair | None:
    """The MANE Select pair, or ``None`` when gnomAD holds no MANE Select for the gene.

    A half-stated pair is `None` too, not a pair with an empty half: it joins like a whole one
    wherever it is read. It stays visible in ``raw``, and it is not raised on — every other signal
    this gene query carries is independent of it.

    Raises:
        ValueError: If the block is neither an object nor null, which is a response-shape fault.
    """
    mane = gene.get('mane_select_transcript')
    if mane is None:
        return None
    if not isinstance(mane, dict):
        raise ValueError(f'gnomAD mane_select_transcript for {gene.get("symbol")!r} is not an object: {mane!r}')
    parts = {key: mane.get(key) for key in ('refseq_id', 'refseq_version', 'ensembl_id', 'ensembl_version')}
    if not all(isinstance(value, str) and value for value in parts.values()):
        return None
    return ManeSelectPair(
        refseq=f'{parts["refseq_id"]}.{parts["refseq_version"]}',
        ensembl=f'{parts["ensembl_id"]}.{parts["ensembl_version"]}',
    )


async def fetch_gnomad_gene(gene_symbol: str, *, http_client: httpx2.AsyncClient) -> GnomadGeneResult:
    """Fetch gene-level gnomAD constraint signals (LOEUF + pext) for ExonRelevance.

    Args:
        gene_symbol: HGNC gene symbol.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ``GnomadGeneResult``: LOEUF, the MANE Select pair the pext regions are computed against,
        the regions themselves, the raw ``gene`` object, and provenance.

    Raises:
        httpx2.HTTPStatusError: If gnomAD returns a non-2xx status.
        errors.UnknownVariantError: If gnomAD holds no record of the gene.
        errors.InvalidRequestError: If gnomAD refused the query.
        ValueError: If the gene did not resolve and gnomAD gave no reason, or is not an object.
    """
    payload = await _post_graphql(_GENE_QUERY, {'gene': gene_symbol}, http_client=http_client)
    data = _require_field(payload, 'gene', context=f'{gene_symbol!r}')
    gene = data['gene']
    if not isinstance(gene, dict):
        raise ValueError(f'gnomAD gene payload for {gene_symbol!r} is not an object')
    return GnomadGeneResult(
        loeuf=_loeuf(gene),
        mane_select=_mane_select(gene),
        pext_regions=_pext_regions(gene),
        raw=gene,
        source=_SOURCE,
        dataset_versions=('gnomad_r4',),
        query=f'gene(gene_symbol: "{gene_symbol}", reference_genome: GRCh38)',
    )
