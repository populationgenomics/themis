"""ClinGen Allele Registry adapter: canonical CAID + cross-ids + per-transcript projections.

The Allele Registry is the canonical-allele authority the Resolve remap joins on: one HGVS in, a CAID
(`@id`) plus the cross-ids (gnomAD v4/v2, dbSNP, ClinVar) and every transcript's c./p. projection
(with MANE flags) out. This adapter returns those parsed load-bearing fields plus the JSON-LD payload
verbatim for the proto `Struct`; the backend maps them onto `ResolvedVariant` and stamps `retrieved_at`.

It is also the id authority for sources keyed on ClinGen alleles rather than on HGVS text:
`fetch_clingen_allele_ids` returns every registered id one expression resolves to. The registry
registers two kinds — the canonical `CA…` allele of a nucleotide change, and the `PA…` protein allele
each transcript's amino-acid change carries — and a source may hold a record under either, so both
are returned rather than one chosen here.

The same record carries the registry's ClinVar crosswalk, and `parse_allele` types it: the variation
records the allele is named in and ClinVar's own allele records for it. That crosswalk is what makes
"ClinVar holds no record for this allele" statable — ClinVar indexes renderings, so a search for the
caller's own string answers the question about a string and not about the allele. It is bounded by
the registry's last ClinVar ingest, and an empty crosswalk is that release-bounded absence.

The registry exposes no dataset version in its payload, so `dataset_versions` names none — the live
query's as-of time is the backend-stamped `retrieved_at`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence

import httpx2

from themis.rpc import variant_pb2
from themis.services.evidence import errors

_ALLELE_URL = 'https://reg.clinicalgenome.org/allele'
_SOURCE = 'ClinGen Allele Registry'
_MAX_ERROR_DETAIL = 512

# A registered allele id, as the trailing segment of an `@id` IRI. An expression the registry knows
# no allele for answers with a JSON-LD blank node (`_:PA`) in place of one.
_ALLELE_ID = re.compile(r'(?:CA|PA)\d{1,20}')
_BLANK_NODE = '_:'

# MANE Select first, then MANE Plus Clinical, then the rest. A source keyed on allele ids ranks a
# match by which id reached it, so the transcript clinical reporting is anchored on leads.
_MANE_RANK = {'MANE Select': 0, 'MANE Plus Clinical': 1}

# The digit width ClinVar's variation accession fixes: VariationID 704508 is VCV000704508. efetch
# answers an unpadded id with an empty result set, so the width is part of the identifier.
_VCV_DIGITS = 9


class UnregisteredAlleleError(errors.InvalidRequestError):
    """The registry answered and registers no allele for the expression — a verdict, not a fault.

    Its own status is `InvalidRequestError`'s (INVALID_ARGUMENT): the registry read the expression
    and holds no allele under it, so no caller keyed on allele ids can be answered about it. It is a
    distinct class because the verdict is settled where a refusal or an outage is not, and only the
    verdict may be reported as a fact about the variant.
    """


@dataclasses.dataclass(frozen=True)
class AlleleRegistryResult:
    """The Resolve-remap inputs parsed from one Allele Registry allele record.

    Attributes:
        caid: The canonical ClinGen allele id (`CA…`) — the join key across sources.
        gnomad_v4_id: The GRCh38 gnomAD v4 id (`chrom-pos-ref-alt`), or `None` if the allele has
            no gnomAD v4 record.
        gnomad_v2_id: The GRCh37 gnomAD v2 id, or `None` if absent.
        transcripts: One projection per transcript allele carrying an HGVS, MANE flags included.
        canonical_refseq_hgvs: The RefSeq c. HGVS of the MANE Select transcript (else MANE Plus
            Clinical), or `None` if the allele has neither.
        gene: The HGNC gene symbol (from the MANE Select transcript if present, else the first
            transcript that carries one); empty for an intergenic allele.
        clinvar_variations: The ClinVar variation records the crosswalk names for the allele, each
            with the accession form `clinvar.DescribeVariant` takes. Empty is a settled fact rather
            than a gap: the registry resolved the allele and its last ClinVar ingest names no
            variation for it.
        clinvar_alleles: ClinVar's own allele records for it. A separate list because the two are
            different ClinVar entity levels and the registry states no key between them.
        raw: The JSON-LD allele payload verbatim, for the proto `Struct`.
        source: Provenance source label.
        dataset_versions: Empty — the registry carries no version (see module docstring).
        query: The exact request URL issued, for replay.
    """

    caid: str
    gnomad_v4_id: str | None
    gnomad_v2_id: str | None
    transcripts: list[variant_pb2.TranscriptProjection]
    canonical_refseq_hgvs: str | None
    gene: str
    clinvar_variations: list[variant_pb2.ClinVarVariation]
    clinvar_alleles: list[variant_pb2.ClinVarAllele]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


@dataclasses.dataclass(frozen=True)
class ClinGenAlleleIds:
    """Every ClinGen allele id one expression registers, ordered for a lookup keyed on them.

    Attributes:
        allele_ids: The id the expression names itself, then the protein allele ids its transcripts
            carry, MANE first. A nucleotide expression therefore leads with its canonical `CA…`
            allele and still reaches sources that only hold the protein change; a protein expression
            registers a `PA…` allele alone. Never empty. The order decides which id is credited when
            several reach one record downstream, not which record a source answers from.
        source: The upstream label.
        dataset_versions: Empty — the registry carries no version (see module docstring).
        query: The exact request URL issued, for replay.
    """

    allele_ids: list[str]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _caid(payload: Mapping[str, object]) -> str:
    """The record's canonical allele id.

    Raises:
        UnregisteredAlleleError: If the `@id` is a blank node — the registry answered and registers
            no allele for the expression. Read as a fault it would be retried; read as a record it
            would carry an empty ClinVar crosswalk, which is the absence a novelty finding rests on.
        ValueError: If there is no `@id`, or it resolves to neither.
    """
    at_id = payload.get('@id')
    if not isinstance(at_id, str):
        raise ValueError('Allele Registry payload has no @id (canonical allele id)')
    if at_id.startswith(_BLANK_NODE):
        raise UnregisteredAlleleError('the Allele Registry registers no canonical allele for this expression')
    caid = at_id.rsplit('/', 1)[-1]
    if not caid.startswith('CA'):
        raise ValueError(f'Allele Registry @id {at_id!r} does not resolve to a CAID')
    return caid


def _external_records(payload: Mapping[str, object]) -> Mapping[str, object]:
    """The record's cross-source ids; absent is legitimate and answers empty.

    Raises:
        ValueError: If the key is present in a shape this cannot read. Coerced to an empty object it
            would answer with an empty ClinVar crosswalk and no gnomAD id, and the first of those is
            the release-bounded absence a novelty finding rests on.
    """
    external = payload.get('externalRecords')
    if external is None:
        return {}
    if not isinstance(external, Mapping):
        raise ValueError('Allele Registry externalRecords is not an object')
    return external


def _external_id(records: Mapping[str, object], key: str) -> str | None:
    """First `id` under an `externalRecords` key (`gnomAD_4`, `gnomAD_2`), or `None` when absent."""
    entries = records.get(key)
    if not (isinstance(entries, Sequence) and not isinstance(entries, str) and entries):
        return None
    first = entries[0]
    value = first.get('id') if isinstance(first, Mapping) else None
    return value if isinstance(value, str) else None


def _transcript_projection(entry: Mapping[str, object]) -> variant_pb2.TranscriptProjection | None:
    """Project one `transcriptAlleles` entry, or `None` when it carries no HGVS to project."""
    hgvs = entry.get('hgvs')
    if not (isinstance(hgvs, Sequence) and not isinstance(hgvs, str) and hgvs):
        return None
    hgvs_c = hgvs[0]
    if not isinstance(hgvs_c, str):
        return None
    protein = entry.get('proteinEffect')
    hgvs_p = protein.get('hgvs') if isinstance(protein, Mapping) else None
    mane = entry.get('MANE')
    status = mane.get('maneStatus') if isinstance(mane, Mapping) else None
    return variant_pb2.TranscriptProjection(
        transcript=hgvs_c.split(':', 1)[0],
        hgvs_c=hgvs_c,
        hgvs_p=hgvs_p if isinstance(hgvs_p, str) else '',
        mane_select=status == 'MANE Select',
        mane_plus_clinical=status == 'MANE Plus Clinical',
        sources=[_SOURCE],
    )


def _refseq_hgvs_with_status(transcript_alleles: Sequence[object], status: str) -> str | None:
    """The RefSeq c. HGVS of the transcript with this `maneStatus`, from the registry's MANE pairing.

    A MANE transcript appears as two `transcriptAlleles` entries — the Ensembl accession and the
    RefSeq one — and both carry the same `MANE.nucleotide` Ensembl/RefSeq pair, so the first entry
    with the status answers this regardless of which namespace it is keyed on.
    """
    for entry in transcript_alleles:
        if not isinstance(entry, Mapping):
            continue
        mane = entry.get('MANE')
        if not (isinstance(mane, Mapping) and mane.get('maneStatus') == status):
            continue
        nucleotide = mane.get('nucleotide')
        refseq = nucleotide.get('RefSeq') if isinstance(nucleotide, Mapping) else None
        hgvs = refseq.get('hgvs') if isinstance(refseq, Mapping) else None
        if isinstance(hgvs, str) and hgvs:
            return hgvs
    return None


def _canonical_refseq_hgvs(transcript_alleles: Sequence[object]) -> str | None:
    """The RefSeq c. HGVS to canonicalise on: MANE Select, else MANE Plus Clinical."""
    return _refseq_hgvs_with_status(transcript_alleles, 'MANE Select') or _refseq_hgvs_with_status(
        transcript_alleles, 'MANE Plus Clinical'
    )


def _gene(transcript_alleles: Sequence[object]) -> str:
    """The HGNC symbol: the MANE Select transcript's, else the first transcript that carries one."""
    fallback = ''
    for entry in transcript_alleles:
        if not isinstance(entry, Mapping):
            continue
        symbol = entry.get('geneSymbol')
        if not isinstance(symbol, str):
            continue
        mane = entry.get('MANE')
        if isinstance(mane, Mapping) and mane.get('maneStatus') == 'MANE Select':
            return symbol
        if not fallback:
            fallback = symbol
    return fallback


def _external_entries(external: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """The `externalRecords` list under `key`; absent is legitimate and answers empty.

    Raises:
        ValueError: If the key is present in a shape this cannot read. Coercing one to nothing would
            report ClinVar as holding no record off a payload that never said so — and that absence
            is what a novelty finding rests on.
    """
    entries = external.get(key)
    if entries is None:
        return []
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        raise ValueError(f'Allele Registry {key} is not a list')
    parsed: list[Mapping[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError(f'Allele Registry {key} carries a non-object entry')
        parsed.append(entry)
    return parsed


def _integer_field(entry: Mapping[str, object], key: str, *, container: str) -> int:
    """One entry's integer id.

    Raises:
        ValueError: If it is absent or not an integer (`bool` included, which `int` admits).
    """
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f'Allele Registry {container} entry has no integer {key}: {value!r}')
    return value


def _vcv(variation_id: int) -> str:
    """ClinVar's accession form of a VariationID.

    Raises:
        ValueError: If the id does not fit the accession's fixed width. The rpcs that take a VCV hold
            it to that width, so an over-wide one would reach a caller as a request they cannot make.
    """
    if not 0 < variation_id < 10**_VCV_DIGITS:
        raise ValueError(f'ClinVar VariationID {variation_id} does not fit a {_VCV_DIGITS}-digit VCV accession')
    return f'VCV{variation_id:0{_VCV_DIGITS}d}'


def _rcv_accessions(entry: Mapping[str, object]) -> list[str]:
    """One crosswalk entry's RCV accessions, one per condition ClinVar aggregates the variation under.

    Raises:
        ValueError: If `RCV` is present in a shape this cannot read.
    """
    rcv = entry.get('RCV')
    if rcv is None:
        return []
    if not isinstance(rcv, Sequence) or isinstance(rcv, str):
        raise ValueError('Allele Registry ClinVarVariations RCV is not a list')
    accessions: list[str] = []
    for accession in rcv:
        if not isinstance(accession, str):
            raise ValueError(f'Allele Registry ClinVarVariations RCV carries a non-string entry: {accession!r}')
        accessions.append(accession)
    return accessions


def _clinvar_variations(external: Mapping[str, object]) -> list[variant_pb2.ClinVarVariation]:
    """The ClinVar variation records the crosswalk names, in the registry's own order, deduplicated.

    Raises:
        ValueError: If the crosswalk is in a shape this cannot read (see `_external_entries`).
    """
    variations: list[variant_pb2.ClinVarVariation] = []
    seen: set[int] = set()
    for entry in _external_entries(external, 'ClinVarVariations'):
        variation_id = _integer_field(entry, 'variationId', container='ClinVarVariations')
        if variation_id in seen:
            continue
        seen.add(variation_id)
        variations.append(
            variant_pb2.ClinVarVariation(variation_id=variation_id, vcv=_vcv(variation_id), rcv=_rcv_accessions(entry))
        )
    return variations


def _clinvar_alleles(external: Mapping[str, object]) -> list[variant_pb2.ClinVarAllele]:
    """ClinVar's own allele records for the canonical allele, in the registry's order.

    Raises:
        ValueError: If an entry carries no integer `alleleId` or no `preferredName` — the name is
            what ClinVar indexes the allele under, and an empty one would read as ClinVar naming it
            nothing.
    """
    alleles: list[variant_pb2.ClinVarAllele] = []
    for entry in _external_entries(external, 'ClinVarAlleles'):
        preferred_name = entry.get('preferredName')
        if not isinstance(preferred_name, str) or not preferred_name:
            raise ValueError(f'Allele Registry ClinVarAlleles entry has no preferredName: {preferred_name!r}')
        alleles.append(
            variant_pb2.ClinVarAllele(
                allele_id=_integer_field(entry, 'alleleId', container='ClinVarAlleles'),
                preferred_name=preferred_name,
            )
        )
    return alleles


def _error_detail(response: httpx2.Response) -> str:
    """The registry's own `errorType`/`description`/`message` for a failed query, else the raw body.

    Truncated: this rides in an exception message that becomes a gRPC trailer, and a trailer over
    the transport's header limit is dropped for a size error that names nothing.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        parts = [str(value) for key in ('errorType', 'description', 'message') if (value := body.get(key)) is not None]
    else:
        parts = []
    return ' — '.join(parts)[:_MAX_ERROR_DETAIL] if parts else response.text.strip()[:_MAX_ERROR_DETAIL]


def parse_allele(payload: Mapping[str, object], *, query: str) -> AlleleRegistryResult:
    """Parse an Allele Registry allele record into the Resolve-remap inputs.

    Args:
        payload: The JSON-LD allele object returned by the registry.
        query: The exact request URL issued, carried into provenance for replay.

    Returns:
        The parsed `AlleleRegistryResult`.

    Raises:
        UnregisteredAlleleError: If the registry registers no allele for the expression.
        ValueError: If the payload has no `@id` resolving to a CAID (a degenerate record, not an
            absence), or its cross-source records are in a shape this cannot read.
    """
    external = _external_records(payload)
    transcript_alleles = payload.get('transcriptAlleles')
    if not (isinstance(transcript_alleles, Sequence) and not isinstance(transcript_alleles, str)):
        transcript_alleles = []
    projections = [
        projection
        for entry in transcript_alleles
        if isinstance(entry, Mapping) and (projection := _transcript_projection(entry)) is not None
    ]
    return AlleleRegistryResult(
        caid=_caid(payload),
        gnomad_v4_id=_external_id(external, 'gnomAD_4'),
        gnomad_v2_id=_external_id(external, 'gnomAD_2'),
        transcripts=projections,
        canonical_refseq_hgvs=_canonical_refseq_hgvs(transcript_alleles),
        gene=_gene(transcript_alleles),
        clinvar_variations=_clinvar_variations(external),
        clinvar_alleles=_clinvar_alleles(external),
        raw=dict(payload),
        source=_SOURCE,
        dataset_versions=(),
        query=query,
    )


def _allele_id(at_id: object) -> str | None:
    """The `CA…`/`PA…` id an `@id` IRI names, or `None` for the blank node meaning "no allele here".

    Raises:
        ValueError: If `@id` is neither. Returning `None` for an unreadable one would drop a protein
            allele from the lookup that follows, which reads back as "no assay covers this variant".
    """
    if not isinstance(at_id, str):
        raise ValueError(f'Allele Registry @id is {type(at_id).__name__}, not an IRI')
    if at_id.startswith(_BLANK_NODE):
        return None
    identifier = at_id.rsplit('/', 1)[-1]
    if _ALLELE_ID.fullmatch(identifier) is None:
        raise ValueError(f'Allele Registry @id {at_id!r} names neither a registered allele nor a blank node')
    return identifier


def _transcript_alleles(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The record's transcript alleles; absent is legitimate (a protein record carries amino-acid ones).

    Raises:
        ValueError: If the key is present in a shape this cannot read. Coercing it to nothing would
            silently drop every protein allele the lookup keyed on them needs.
    """
    transcript_alleles = payload.get('transcriptAlleles')
    if transcript_alleles is None:
        return []
    if not isinstance(transcript_alleles, Sequence) or isinstance(transcript_alleles, str):
        raise ValueError('Allele Registry transcriptAlleles is not a list')
    if not all(isinstance(entry, Mapping) for entry in transcript_alleles):
        raise ValueError('Allele Registry transcriptAlleles carries a non-object entry')
    return list(transcript_alleles)


def _mane_rank(entry: Mapping[str, object]) -> int:
    mane = entry.get('MANE')
    status = mane.get('maneStatus') if isinstance(mane, Mapping) else None
    return _MANE_RANK.get(status, len(_MANE_RANK)) if isinstance(status, str) else len(_MANE_RANK)


def parse_allele_ids(payload: Mapping[str, object]) -> list[str]:
    """The registered ClinGen allele ids in one allele record, in lookup order (see `ClinGenAlleleIds`).

    Args:
        payload: The JSON-LD allele object returned by the registry.

    Returns:
        The record's own id first, then each transcript allele's protein id, MANE first, deduplicated
        (one protein allele spans every version of its transcript).

    Raises:
        ValueError: If the record is not the shape the registry documents — no `@id`, an `@id` naming
            neither an allele nor a blank node, or `transcriptAlleles` in a shape this cannot read.
            Each would otherwise drop protein alleles from the lookup that follows, silently.
        UnregisteredAlleleError: If the record carries no registered allele id — the registry
            answered, with a blank node. A settled verdict on the expression, so it must not read as
            a fault a caller retries, nor as the source holding no record of a variant it was never
            asked about.
    """
    if '@id' not in payload:
        raise ValueError('Allele Registry payload has no @id')
    ids: list[str] = []
    own = _allele_id(payload['@id'])
    if own is not None:
        ids.append(own)
    for entry in sorted(_transcript_alleles(payload), key=_mane_rank):
        protein = _allele_id(entry['@id']) if '@id' in entry else None
        if protein is not None and protein not in ids:
            ids.append(protein)
    if not ids:
        raise UnregisteredAlleleError('the Allele Registry registers no ClinGen allele for this expression')
    return ids


async def _fetch_allele(hgvs: str, *, http_client: httpx2.AsyncClient) -> tuple[dict[str, object], str]:
    """One registry allele record and the request URL that produced it."""
    response = await http_client.get(_ALLELE_URL, params={'hgvs': hgvs})
    if not response.is_success:
        errors.raise_for_status(response, upstream=_SOURCE, subject=f'{hgvs!r}', detail=_error_detail(response))
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f'Allele Registry returned a non-object payload for {hgvs!r}')
    return payload, str(response.request.url)


async def fetch_allele_registry(hgvs: str, *, http_client: httpx2.AsyncClient) -> AlleleRegistryResult:
    """Resolve one HGVS to its canonical allele via the ClinGen Allele Registry.

    Args:
        hgvs: The HGVS expression to resolve (any transcript / genomic form the registry accepts).
        http_client: The async HTTP client (caller owns its lifecycle and timeouts).

    Returns:
        The parsed `AlleleRegistryResult`.

    Raises:
        errors.InvalidRequestError: If the registry judges the HGVS unacceptable.
        httpx2.HTTPStatusError: For any non-2xx that is not that. The registry answers an out-of-range
            position with a 500, so the status alone does not say whether a retry could succeed — the
            detail it returns does, and rides in the message.
        ValueError: If the response is not a JSON object or carries no CAID.
    """
    payload, query = await _fetch_allele(hgvs, http_client=http_client)
    return parse_allele(payload, query=query)


async def fetch_clingen_allele_ids(hgvs: str, *, http_client: httpx2.AsyncClient) -> ClinGenAlleleIds:
    """Resolve one HGVS to the ClinGen allele ids a source keyed on them can be asked about.

    Args:
        hgvs: The coding or protein HGVS to register (see `ClinGenAlleleIds.allele_ids` for what each
            form yields).
        http_client: The async HTTP client (caller owns its lifecycle and timeouts).

    Returns:
        The `ClinGenAlleleIds`, never empty.

    Raises:
        errors.InvalidRequestError: If the registry judges the HGVS unacceptable — including the
            predicted-consequence parentheses, which it parses as an amino acid.
        UnregisteredAlleleError: If the registry registers no allele for it (a subclass of the above,
            so a caller that does not care which reads them alike).
        httpx2.HTTPStatusError: For a 429 or a 5xx.
        ValueError: If the response is not a JSON object.
    """
    payload, query = await _fetch_allele(hgvs, http_client=http_client)
    return ClinGenAlleleIds(allele_ids=parse_allele_ids(payload), source=_SOURCE, dataset_versions=(), query=query)
