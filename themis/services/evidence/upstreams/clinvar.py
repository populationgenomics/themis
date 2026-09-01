"""NCBI ClinVar adapter (E-utilities): one variation's archive, the gene's pathogenic pool, one span.

Three independent lookups, one per concern, each its own entry point:

- ``fetch_variant_archive`` — the archive ClinVar files under one variation accession (``efetch``
  VCV XML), both whole and as the reading the interface reports: the aggregate classification, its
  review status, and every submission behind it. Context plus the material *_INF eligibility and
  circularity are judged over.
- ``fetch_gene_pool`` — the gene's set of records ClinVar classifies pathogenic (``esearch`` ->
  ``esummary``), filtered to the caller's review-status floor: the pool that feeds the P/LP density
  signal, SM3's pathogenic-variants DAFT, and the pathogenic half of the informative-variant
  candidate set.
- ``fetch_span_records`` — every germline record in one genomic interval of one gene, of every
  classification: the *_INF candidate set at a codon or an exon, whose benign and VUS arms the
  classification-scoped pool cannot hold. The backend projects the caller's c. range onto the
  interval through the transcript's exon table.

The backend composes them per rpc; ``AssessExonRelevance`` takes the pool alone.

The remaps that make the difference between a right and a wrong answer:

- **A variation is fetched by identity, never by its rendering.** ClinVar indexes RENDERINGS, and no
  one HGVS string spans version, notation and shift at once, so a search for the caller's own string
  answers with a different allele or with nothing. The accession the ClinGen Allele Registry's
  crosswalk names is the identity claim; ``efetch`` resolves it, and the archive it returns is taken
  whole. Zero-padding is part of that identity — efetch takes a bare numeric UID with a 200 carrying
  an empty ``<set/>`` — so the accession is held to its shape before the call rather than after.
- **``review_status`` is mapped to the ClinVar gold-star count**, and the caller's floor goes into
  the search term as well as onto the returned records. ClinVar's E-utilities germline classification
  lives under ``germline_classification`` (``description`` / ``review_status`` / ``trait_set``); the
  star map is applied here, the floor is the caller's — the three uses of the pool do not share one,
  and a floor fixed at this layer would narrow a set the caller believes it is choosing. Filtering
  after the cap instead would make the floor unreachable rather than selective: the pool is bounded,
  so a 3-star record ranked below the bound would be dropped before the floor ever saw it, on
  precisely the well-studied genes where such a record settles the question.
- **The pool is pathogenic by the aggregate classification**, on the search term (`clinsig …`
  *properties*, since `[Clinical significance]` is no Entrez field and silently degrades to
  `[All Fields]`) and again on the returned description, term by term through
  ``themis.svcv4.clinvar_classification``. Both gates are needed: the term is a remote index and the
  description is the fact. The gate here is that module's wider one, ``is_pathogenic``; SM3's DAFT
  applies ``is_unqualified_pathogenic`` to the same pool for itself.
- **A span is searched on genomic coordinates and scoped by gene.** ClinVar's Entrez index carries no
  c. coordinate to search on — ``einfo`` lists ``CHRPOS`` (``[Base Position]``, GRCh38) and no
  transcript-relative field — so a positional census runs on the assembly, and the gene clause is
  what keeps a coordinate range from matching the same position on another chromosome. No
  classification clause is applied: the whole point of the span is the records the pool's clause
  excludes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence

import clinvar_proto
import defusedxml.common
import defusedxml.ElementTree
import httpx2

from themis.services.evidence import errors, hgvs, requests
from themis.svcv4 import clinvar_classification, frequency

_EUTILS_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
_SOURCE = 'NCBI ClinVar (E-utilities)'
_DB = 'clinvar'

# Keyless E-utilities allows 3 req/s. Every call sleeps this gap before issuing, which spaces the
# calls within one fetch chain and across two composed ones. It is not a process-wide limiter:
# concurrent rpcs on the same server sleep independently and can still coincide.
_RATE_LIMIT_DELAY_S = 0.34

# One esearch page of UIDs. Entrez documents 10000 as the ceiling for a request that keeps no
# history; the pool is walked in pages so a caller's bound is honoured whatever the server serves.
_ESEARCH_PAGE = 10000

# UIDs per esummary POST. Measured against the live index (July 2026): 500 ClinVar records are
# ~1.1 MB and ~10 s.
_ESUMMARY_BATCH = 500

# The envelope efetch wraps its answer in, whatever the answer is; an empty one carries no child.
_RESULT_SET = 'ClinVarResult-Set'

# The envelope efetch refuses in instead, and the ERROR wording it states for an id that resolves to
# no record: "ID list is empty! In it there are neither IDs nor accessions." Matched on a lowercase
# fragment, so the rest of the sentence can move without the refusal reading back as a bad request.
_EFETCH_ENVELOPE = 'eFetchResult'
_NO_RECORD_ERROR = 'id list is empty'

# ClinVar aggregate review-status phrase -> gold-star count, enumerated including the 0-star ones
# (verified against the live index, July 2026). Nothing falls through to 0: an expert-panel status
# read as 0 stars would take its record out of the *_INF set and the DAFT, so `_review_stars` raises
# on a phrase not listed. This map is also what the pool term's review-status clause is built from,
# and there the two failure modes differ: at a floor above 0 a phrase ClinVar adds matches no clause
# and its records are never fetched, so the fault fires on the queried allele and on a floor-0 pool
# and nowhere else.
_STAR_BY_REVIEW_STATUS = {
    'practice guideline': 4,
    'reviewed by expert panel': 3,
    'criteria provided, multiple submitters, no conflicts': 2,
    'criteria provided, single submitter': 1,
    'criteria provided, conflicting interpretations': 1,  # ClinVar's pre-2024 spelling
    'criteria provided, conflicting classifications': 1,
    'no assertion criteria provided': 0,
    'no classification provided': 0,
    'no classifications from unflagged records': 0,
    'no classification for the single variant': 0,
    # The record carries no germline classification at all — a somatic or oncogenicity-only record,
    # which the P/LP classification filter drops in the same pass.
    '': 0,
}

# The assembly ClinVar's `[Base Position]` index and the esummary spans measured against it are on.
_ASSEMBLY = 'GRCh38'

# How far past each end of a span the search looks for a record long enough to contain it, and so how
# far such a record may overhang it. Sized to the coding indels and delins the informative-variant
# rules are about: past it ClinVar titles a record cytogenetically, and a c. range names no such
# extent anyway. A wider window costs the census its scale — one measured kilobase of SCN2A carries
# 325 records, so padding to catch a copy-number record would fetch hundreds to return three.
_SPAN_PAD = 1000

# ObservedData @Type values carrying a per-zygosity count. Each token is reported verbatim rather
# than mapped onto an adjective, because the unit follows the token: individuals under a -zygote one,
# chromosomes under VariantChromosomes.
_ZYGOSITY_ATTRIBUTE_TYPES = frozenset(
    {'SingleHeterozygote', 'CompoundHeterozygote', 'Homozygote', 'Hemizygote', 'VariantChromosomes', 'NumberMosaic'}
)

# The ObservedData attribute holding the submitter's own total for the block, beside the per-zygosity
# counts. Named for the source attribute: submitters populate it with the number of individuals
# (matching the zygosity counts summed), not an allele count, so the name is all it reliably means.
_VARIANT_ALLELES_ATTRIBUTE = 'VariantAlleles'

# ClinVar's placeholder for "the submitter stated nothing", carried as if it were a value.
_NOT_PROVIDED = frozenset({'not provided', 'none provided'})


@dataclasses.dataclass(frozen=True)
class ClinvarZygosityCountData:
    """One zygosity a submitter reported within a single observation, and how many it reported at it.

    Attributes:
        zygosity: ClinVar's ``ObservedData`` attribute token verbatim.
        count: What that token counts — individuals under a zygosity token, chromosomes under
            ``VariantChromosomes`` — or ``None`` where the submitter stated no count.
    """

    zygosity: str
    count: int | None


@dataclasses.dataclass(frozen=True)
class ClinvarObservationData:
    """One person/family/cohort observation a submitter reported behind its classification.

    Every field is as the submitter stated it; an empty string / list (``None`` for ``variant_alleles``)
    means the submitter stated nothing, never a manufactured default.

    Attributes:
        zygosities: The counts ClinVar files per zygosity token. A cohort can mix them, so this is a
            list, not one zygosity per observation.
        variant_alleles: ClinVar's ``VariantAlleles`` attribute verbatim. Named for the attribute
            because its unit is the submitter's: in practice it holds the number of individuals, so
            it tracks the ``zygosities`` sum rather than an allele count.
    """

    origin: str
    affected_status: str
    zygosities: list[ClinvarZygosityCountData]
    variant_alleles: int | None
    age: str
    sex: str
    collection_method: str
    descriptions: list[str]
    traits: list[str]
    pubmed_ids: list[str]


@dataclasses.dataclass(frozen=True)
class ClinvarSubmissionData:
    """One submitter's assertion (an SCV) on a variant.

    The unit informative-variant eligibility and circularity are judged over: ``assertion_method``,
    ``comment`` and ``observations`` are the evidence; ``submitter`` and ``organization_category``
    are what tells two independent assertions from one lab restating itself.
    """

    scv: str
    submitter: str
    organization_category: str
    classification: str
    review_status: str
    date_evaluated: str
    assertion_method: str
    mode_of_inheritance: str
    comment: str
    conditions: list[str]
    pubmed_ids: list[str]
    erepo_url: str
    observations: list[ClinvarObservationData]


@dataclasses.dataclass(frozen=True)
class ClinvarRecordData:
    """One parsed ClinVar germline record.

    Attributes:
        clinvar_id: The VCV accession.
        hgvs: The record's display HGVS (the esummary ``title`` / the archive ``VariationName``).
        classification: The germline classification description (e.g. ``"Pathogenic"``).
        review_stars: The gold-star review count (0-4).
        review_status: The phrase the stars are derived from; a pool record carries no submissions,
            so this is where its consensus tier is stated.
        conditions: The asserted conditions, for the same-phenotype informative-variant check.
            ClinVar's aggregate carries a trait SET rather than one term, each value verbatim.
        coding_span: ``hgvs`` parsed into c. coordinates, or ``None`` where it names no c. span (a
            copy-number or uncertain-boundary title). Parsed here so that placing the pool against
            an exon table is arithmetic rather than each caller's own regex over display strings,
            and returned as ``None`` rather than skipped so an unplaceable record stays countable.
        submissions: The per-submitter assertions, populated only for a record fetched as the queried
            allele — the gene pool is summarised in bulk, which carries no submission detail.
    """

    clinvar_id: str
    hgvs: str
    classification: str
    review_stars: int
    review_status: str
    conditions: list[str]
    coding_span: hgvs.CodingSpan | None
    submissions: list[ClinvarSubmissionData] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class ClinvarArchive:
    """One variation's ClinVar archive: the record whole, and the reading taken off it.

    Attributes:
        record: The aggregate classification, its review status and conditions, and every germline
            submission — the fields the SVCv4 codes are read from.
        variation_archive: The same archive whole, as clinvar-proto's own generated converter types
            it. It carries what the reading above cannot: ``variation_type`` says what kind of unit
            the record is about (an allele, or a haplotype the allele is one part of), and
            ``record_type`` whether the record is classified in its own right at all.
        source: The upstream label.
        dataset_versions: The E-utilities database queried.
        query: The exact request issued, for replay.
    """

    record: ClinvarRecordData
    variation_archive: clinvar_proto.clinvar_pb2.VariationArchiveType
    source: str
    dataset_versions: tuple[str, ...]
    query: str


@dataclasses.dataclass(frozen=True)
class ClinvarGenePool:
    """The gene's pathogenic records at the requested floor, with the census saying whether they are all of them.

    Attributes:
        records: The records ClinVar classifies pathogenic in aggregate — penetrance qualifiers
            included — and at or above the star floor, out of the ``considered`` fetched.
        total: Every record the gene's term matches — both the ``clinsig`` properties and the review
            status — before the classification and star filters are re-applied to the fetched ones.
        considered: How many of ``total`` were fetched and filtered (the bound).
        source: The upstream label.
        dataset_versions: The E-utilities database queried.
        query: The gene term issued, for replay.
    """

    records: list[ClinvarRecordData]
    total: int
    considered: int
    source: str
    dataset_versions: tuple[str, ...]
    query: str

    @property
    def truncated(self) -> bool:
        """Whether ``records`` is a prefix of the gene's pool rather than the whole of it."""
        return self.considered < self.total


@dataclasses.dataclass(frozen=True)
class ClinvarSpanRecords:
    """Every germline record in one genomic interval of one gene, with the census behind them.

    No classification and no review-status filter is applied at either end: the informative-variant
    rules score the benign and uncertain arms too, so a filtered span would answer the pool's
    question again rather than the trees'.

    Attributes:
        records: The records ClinVar annotates to the gene whose coordinates fall in the interval,
            in the order the search returned them, out of the ``considered`` fetched.
        total: Every record the interval's term matches.
        considered: How many of ``total`` were fetched (the bound).
        source: The upstream label.
        dataset_versions: The E-utilities database queried.
        query: The interval term issued, for replay.
    """

    records: list[ClinvarRecordData]
    total: int
    considered: int
    source: str
    dataset_versions: tuple[str, ...]
    query: str

    @property
    def truncated(self) -> bool:
        """Whether ``records`` is a prefix of the span's records rather than the whole of them."""
        return self.considered < self.total


@dataclasses.dataclass(frozen=True)
class _SearchResult:
    """One esearch answer: the returned UID page and the true total behind it."""

    uids: list[str]
    total: int


def _review_stars(review_status: str) -> int:
    """The gold-star count for a ClinVar aggregate review status.

    Raises:
        ValueError: On a status not in ClinVar's vocabulary. Reading one as 0 stars would file it
            below every floor, which is indistinguishable from ClinVar saying the record is
            unreviewed.
    """
    normalised = review_status.strip().lower()
    try:
        return _STAR_BY_REVIEW_STATUS[normalised]
    except KeyError:
        raise ValueError(f'unknown ClinVar review status {review_status!r}') from None


def _subject(endpoint: str, params: Mapping[str, str]) -> str:
    """What one E-utilities call was about, for its error message: its search term or its id list."""
    return f'{endpoint} for {params.get("term") or params.get("id") or ""!r}'


async def _spaced_get(endpoint: str, params: Mapping[str, str], *, http_client: httpx2.AsyncClient) -> httpx2.Response:
    """GET one E-utilities endpoint, spaced from the previous call by the keyless rate-limit gap.

    The status is returned unjudged, for a lookup that has to read the body before it is; `_get` is
    the judged form the rest of the module calls.
    """
    await asyncio.sleep(_RATE_LIMIT_DELAY_S)
    return await http_client.get(f'{_EUTILS_URL}/{endpoint}', params=dict(params))


async def _get(endpoint: str, params: Mapping[str, str], *, http_client: httpx2.AsyncClient) -> httpx2.Response:
    """`_spaced_get`, with a non-2xx placed on the evidence taxonomy."""
    response = await _spaced_get(endpoint, params, http_client=http_client)
    errors.raise_for_status(response, upstream=_SOURCE, subject=_subject(endpoint, params))
    return response


async def _post(endpoint: str, params: Mapping[str, str], *, http_client: httpx2.AsyncClient) -> httpx2.Response:
    """POST one E-utilities endpoint, spaced from the previous call by the keyless rate-limit gap."""
    await asyncio.sleep(_RATE_LIMIT_DELAY_S)
    response = await http_client.post(f'{_EUTILS_URL}/{endpoint}', data=dict(params))
    errors.raise_for_status(response, upstream=_SOURCE, subject=_subject(endpoint, params))
    return response


async def _esearch(
    term: str, *, http_client: httpx2.AsyncClient, retmax: int | None = None, retstart: int = 0
) -> _SearchResult:
    """Run one ClinVar esearch and return its UID page plus the total behind it.

    Raises:
        errors.InvalidRequestError: If E-utilities refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If the response carries no ``esearchresult.idlist`` / ``count``.
    """
    params = {'db': _DB, 'retmode': 'json', 'term': term}
    if retmax is not None:
        params['retmax'] = str(retmax)
    if retstart:
        params['retstart'] = str(retstart)
    response = await _get('esearch.fcgi', params, http_client=http_client)
    result = response.json().get('esearchresult')
    if not isinstance(result, dict) or not isinstance(result.get('idlist'), list):
        raise ValueError(f'ClinVar esearch returned no idlist for term {term!r}')
    count = result.get('count')
    if not isinstance(count, str) or not count.isdigit():
        raise ValueError(f'ClinVar esearch returned no count for term {term!r}')
    return _SearchResult(uids=[uid for uid in result['idlist'] if isinstance(uid, str)], total=int(count))


async def _esummary(uids: Sequence[str], *, http_client: httpx2.AsyncClient) -> dict[str, object]:
    """Fetch the esummary ``result`` map for a batch of UIDs (POSTed, so any batch size fits).

    Raises:
        errors.InvalidRequestError: If E-utilities refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If the response carries no ``result`` object.
    """
    params = {'db': _DB, 'retmode': 'json', 'id': ','.join(uids)}
    response = await _post('esummary.fcgi', params, http_client=http_client)
    result = response.json().get('result')
    if not isinstance(result, dict):
        raise ValueError(f'ClinVar esummary returned no result object for ids {list(uids)!r}')
    return result


def _stated_no_record(response: httpx2.Response) -> str | None:
    """ClinVar's own words for "no record under that id" off a refused efetch, or ``None``.

    efetch refuses inside an `_EFETCH_ENVELOPE` whose ``ERROR`` states which refusal it is, and an
    accession ClinVar has never issued is refused with `_NO_RECORD_ERROR` and a 400. Both halves
    decide: the status keeps out a 403 (blocked) and a 429 (throttled), neither of which is about
    the id, and the wording keeps every other refusal on the shared 4xx rule. A 200 is not read
    here because ClinVar spells the same fact differently on one — an empty result set, which
    `_efetch_archive` reads off the parsed envelope.

    A body that is not that envelope, or not XML at all, is left to the shared 4xx rule, which
    refuses it in turn.
    """
    if response.status_code != httpx2.codes.BAD_REQUEST:
        return None
    try:
        root = defusedxml.ElementTree.fromstring(response.content)
    except (ET.ParseError, defusedxml.common.DefusedXmlException):
        return None
    if root.tag != _EFETCH_ENVELOPE:
        return None
    for error in root.findall('ERROR'):
        stated = (error.text or '').strip()
        if _NO_RECORD_ERROR in stated.lower():
            return stated
    return None


def _crosswalk_disagreement(accession: str, answered: str) -> str:
    """What the two sources each said, for an accession ClinVar answers no archive under."""
    return (
        f'ClinVar holds no record under accession {accession!r}, the variation the registry '
        f'crosswalk names for the allele: efetch {answered}. The two sources disagree about the '
        'variation, and the missing archive is about that and not about the allele being absent '
        'from ClinVar'
    )


async def _efetch_archive(accession: str, *, http_client: httpx2.AsyncClient) -> tuple[ET.Element, str]:
    """Fetch the single VCV archive one variation accession names, and the URL that fetched it.

    Raises:
        errors.InconsistentSourcesError: If ClinVar holds no record under the accession, in either
            of the two ways efetch says so — a refusal stating the id resolved to nothing, or a
            success carrying an empty result set. One fact, so one answer: the accession is
            well-formed (`requests.require_vcv_accession`) and came from a crosswalk that resolved
            the allele, so ClinVar answering with no archive is the two sources disagreeing about a
            variation, never the allele being absent from ClinVar.
        errors.InvalidRequestError: If E-utilities refuses the call for any other reason (a non-429
            4xx).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If the response is not a ``ClinVarResult-Set``, or carries more than one archive.
    """
    params = {'db': _DB, 'id': accession, 'rettype': 'vcv', 'retmode': 'xml'}
    response = await _spaced_get('efetch.fcgi', params, http_client=http_client)
    if (stated := _stated_no_record(response)) is not None:
        raise errors.InconsistentSourcesError(
            _crosswalk_disagreement(accession, f'refused it ({response.status_code}) with {errors.clipped(stated)!r}')
        )
    errors.raise_for_status(response, upstream=_SOURCE, subject=_subject('efetch.fcgi', params))
    try:
        root = defusedxml.ElementTree.fromstring(response.content)
    except (ET.ParseError, defusedxml.common.DefusedXmlException) as e:
        raise ValueError(f'ClinVar efetch returned unparsable XML for {accession!r}: {e}') from e
    if root.tag != _RESULT_SET:
        raise ValueError(f'ClinVar efetch answered {accession!r} with a {root.tag!r} envelope, not a {_RESULT_SET!r}')
    archives = root.findall('VariationArchive')
    if not archives:
        raise errors.InconsistentSourcesError(_crosswalk_disagreement(accession, 'answered with an empty result set'))
    if len(archives) > 1:
        raise ValueError(
            f'ClinVar efetch returned {len(archives)} archives for accession {accession!r}; an accession '
            'names one variation and a variation has one archive'
        )
    return archives[0], str(response.request.url)


def _text(elem: ET.Element | None, path: str) -> str:
    """The stripped text at ``path`` under ``elem``, or empty when absent, blank or a placeholder."""
    if elem is None:
        return ''
    found = elem.find(path)
    stripped = (found.text or '').strip() if found is not None else ''
    return '' if stripped.lower() in _NOT_PROVIDED else stripped


def _trait_name(trait: ET.Element) -> str:
    """What ClinVar files one trait under: its preferred display name, else its bare ontology id.

    A Trait can carry an XRef and no Name — VCEP submissions do — and the id is then the whole of
    what identifies the condition, so reading names alone drops the trait without saying it had one.
    """
    for value in trait.findall('Name/ElementValue'):
        if value.get('Type') != 'Alternate' and (text := (value.text or '').strip()):
            return text
    for xref in trait.findall('XRef'):
        database, identifier = xref.get('DB', ''), xref.get('ID', '')
        if database and identifier:
            return f'{database}:{identifier}'
    return ''


def _conditions(container: ET.Element | None, path: str) -> list[str]:
    """The traits under ``path`` as ClinVar states them, one per trait, in document order.

    Verbatim, ClinVar's "not provided" placeholder included: an aggregate whose trait set carries it
    is a record asserted against an unnamed condition, which the same-phenotype check reads as one.
    """
    if container is None:
        return []
    return [name for trait in container.findall(path) if (name := _trait_name(trait))]


def _stated_traits(container: ET.Element | None, path: str) -> list[str]:
    """The traits under ``path`` a submitter named, deduplicated; the placeholder is not a name."""
    if container is None:
        return []
    names: list[str] = []
    for trait in container.findall(path):
        name = _trait_name(trait)
        if name and name.lower() not in _NOT_PROVIDED and name not in names:
            names.append(name)
    return names


def _ages(sample: ET.Element | None) -> str:
    """The sample's ages as submitted, e.g. ``"onset=30years"``; several are joined."""
    if sample is None:
        return ''
    parts = [
        f'{age.get("Type", "")}={value}{age.get("age_unit", "")}'.removeprefix('=')
        for age in sample.findall('Age')
        if (value := (age.text or '').strip())
    ]
    return ', '.join(parts)


@dataclasses.dataclass(frozen=True)
class _ObservedData:
    """The counts and free-text notes carried as ``ObservedData`` attributes of one ``ObservedIn``.

    ``ObservedData`` repeats within the block, so the zygosities and notes accumulate across it: one
    cohort states a count per zygosity, and a literature submitter states one note per family it
    reports. ``variant_alleles`` is the single ``VariantAlleles`` attribute.
    """

    zygosities: list[ClinvarZygosityCountData]
    variant_alleles: int | None
    descriptions: list[str]


def _integer_value(attribute: ET.Element) -> int | None:
    """The attribute's ``integerValue``, or ``None`` when it carries none.

    Raises:
        ValueError: If the value is present but not an integer — a malformed count, which must not
            read back as a count the submitter never stated.
    """
    value = attribute.get('integerValue')
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as e:
        raise ValueError(f'ClinVar {attribute.get("Type")!r} attribute has a non-integer integerValue {value!r}') from e


def _observed_data(observed_in: ET.Element) -> _ObservedData:
    zygosities: list[ClinvarZygosityCountData] = []
    variant_alleles: int | None = None
    descriptions: list[str] = []
    for attribute in observed_in.findall('ObservedData/Attribute'):
        attribute_type = attribute.get('Type', '')
        if attribute_type in _ZYGOSITY_ATTRIBUTE_TYPES:
            zygosities.append(ClinvarZygosityCountData(attribute_type, _integer_value(attribute)))
        elif attribute_type == _VARIANT_ALLELES_ATTRIBUTE:
            variant_alleles = _integer_value(attribute)
        elif attribute_type == 'Description':
            text = (attribute.text or '').strip()
            if text and text.lower() not in _NOT_PROVIDED:
                descriptions.append(text)
    return _ObservedData(zygosities=zygosities, variant_alleles=variant_alleles, descriptions=descriptions)


def _observation(observed_in: ET.Element) -> ClinvarObservationData:
    """Parse one ``ObservedIn`` block into an observation."""
    sample = observed_in.find('Sample')
    observed = _observed_data(observed_in)
    # A citation sits either directly on the block or on one of its ObservedData entries.
    citations = [*observed_in.findall('Citation'), *observed_in.findall('ObservedData/Citation')]
    return ClinvarObservationData(
        origin=_text(sample, 'Origin'),
        affected_status=_text(sample, 'AffectedStatus'),
        zygosities=observed.zygosities,
        variant_alleles=observed.variant_alleles,
        age=_ages(sample),
        sex=_text(sample, 'Sex'),
        collection_method=_text(observed_in, 'Method/MethodType'),
        descriptions=observed.descriptions,
        traits=_stated_traits(observed_in, 'TraitSet/Trait'),
        pubmed_ids=_pubmed_ids(citations),
    )


def _pubmed_ids(citations: Iterable[ET.Element]) -> list[str]:
    """The PubMed ids across the citations, in document order, deduplicated."""
    pubmed_ids: list[str] = []
    for citation in citations:
        for identifier in citation.findall('ID'):
            pmid = (identifier.text or '').strip()
            if identifier.get('Source') == 'PubMed' and pmid and pmid not in pubmed_ids:
                pubmed_ids.append(pmid)
    return pubmed_ids


def _erepo_url(citations: Iterable[ET.Element]) -> str:
    """The ClinGen Evidence Repository URL among the citations — a VCEP curation's public record."""
    for citation in citations:
        url = _text(citation, 'URL')
        if 'erepo.clinicalgenome' in url:
            return url
    return ''


def _attribute(assertion: ET.Element, attribute_type: str) -> str:
    """The text of the assertion's ``AttributeSet`` entry of ``attribute_type``, or empty."""
    for attribute in assertion.findall('AttributeSet/Attribute'):
        if attribute.get('Type') == attribute_type:
            return (attribute.text or '').strip()
    return ''


def _submission(assertion: ET.Element) -> ClinvarSubmissionData | None:
    """Parse one ``ClinicalAssertion`` (an SCV) into a submission.

    A patient registry submits an observation under "no classification provided"; the empty
    ``classification`` that yields is the submitter's answer, and the observation behind it is
    evidence either way, so the submission is kept.

    Returns:
        The submission, or ``None`` when the assertion is not a germline one — an SCV classifying
        somatic clinical impact or oncogenicity carries a sibling element instead of
        ``GermlineClassification``, and its tumour observations are not germline evidence.

    Raises:
        ValueError: If the assertion has no SCV accession — a structurally wrong assertion, not a
            field gap.
    """
    classification = assertion.find('Classification')
    if classification is None or classification.find('GermlineClassification') is None:
        return None
    accession = assertion.find('ClinVarAccession')
    scv = accession.get('Accession', '') if accession is not None else ''
    if not scv:
        raise ValueError(f'ClinVar clinical assertion has no SCV accession: {assertion.get("ID")!r}')
    citations = [*classification.findall('Citation'), *assertion.findall('Citation')]
    return ClinvarSubmissionData(
        scv=scv,
        submitter=accession.get('SubmitterName', '') if accession is not None else '',
        organization_category=accession.get('OrganizationCategory', '') if accession is not None else '',
        classification=_text(classification, 'GermlineClassification'),
        review_status=_text(classification, 'ReviewStatus'),
        date_evaluated=classification.get('DateLastEvaluated', ''),
        assertion_method=_attribute(assertion, 'AssertionMethod'),
        mode_of_inheritance=_attribute(assertion, 'ModeOfInheritance'),
        comment=_text(classification, 'Comment'),
        conditions=_stated_traits(assertion, 'TraitSet/Trait'),
        pubmed_ids=_pubmed_ids(citations),
        erepo_url=_erepo_url(citations),
        observations=[_observation(observed_in) for observed_in in assertion.findall('ObservedInList/ObservedIn')],
    )


def _record_from_archive(archive: ET.Element) -> ClinvarRecordData:
    """Read a VCV archive into a record: the aggregate classification plus every germline submission.

    An archive ClinVar files only because the allele arrived inside a larger submitted set carries no
    aggregate germline classification, so the classification comes back empty and the review status
    at 0 stars. That is the archive's ``RecordType``, which is on ``variation_archive`` for the
    caller to read; it is not this reading's to guess at.

    Raises:
        ValueError: If the archive has no accession or no name — a structurally wrong archive, not a
            field gap. An unnamed one would read as a record whose HGVS names no c. span, which is a
            different fact and one the caller is asked to act on.
    """
    accession = archive.get('Accession', '')
    if not accession:
        raise ValueError(f'ClinVar archive has no accession: {archive.get("VariationID")!r}')
    name = archive.get('VariationName', '')
    if not name:
        raise ValueError(f'ClinVar archive {accession} has no VariationName')
    germline = archive.find('ClassifiedRecord/Classifications/GermlineClassification')
    submissions = [
        submission
        for assertion in archive.findall('ClassifiedRecord/ClinicalAssertionList/ClinicalAssertion')
        if (submission := _submission(assertion)) is not None
    ]
    return ClinvarRecordData(
        clinvar_id=accession,
        hgvs=name,
        classification=_text(germline, 'Description'),
        review_stars=_review_stars(_text(germline, 'ReviewStatus')),
        review_status=_text(germline, 'ReviewStatus'),
        conditions=_conditions(germline, 'ConditionList/TraitSet/Trait'),
        coding_span=hgvs.coding_span(name),
        submissions=submissions,
    )


def _parse_summary_record(record: Mapping[str, object]) -> ClinvarRecordData:
    """Parse one esummary record object into a ``ClinvarRecordData``.

    Raises:
        ValueError: If the record has no accession — a structurally wrong esummary object, not a
            benign field gap (an absent classification maps to empty / 0 stars).
    """
    accession = record.get('accession')
    if not isinstance(accession, str) or not accession:
        raise ValueError(f'ClinVar esummary record has no accession: {record.get("uid")!r}')
    germline = record.get('germline_classification')
    germline = germline if isinstance(germline, dict) else {}
    description = germline.get('description')
    review_status = germline.get('review_status')
    title = record.get('title')
    name = title if isinstance(title, str) else ''
    return ClinvarRecordData(
        clinvar_id=accession,
        hgvs=name,
        classification=description if isinstance(description, str) else '',
        review_stars=_review_stars(review_status if isinstance(review_status, str) else ''),
        review_status=review_status if isinstance(review_status, str) else '',
        conditions=_summary_conditions(germline),
        coding_span=hgvs.coding_span(name),
    )


def _summary_conditions(germline: Mapping[str, object]) -> list[str]:
    traits = germline.get('trait_set')
    if not isinstance(traits, list):
        return []
    return [t['trait_name'] for t in traits if isinstance(t, dict) and isinstance(t.get('trait_name'), str)]


def _records_from_summary(result: Mapping[str, object]) -> list[ClinvarRecordData]:
    uids = result.get('uids')
    if not isinstance(uids, list):
        raise ValueError('ClinVar esummary result has no uids list')
    records = []
    for uid in uids:
        record = result.get(uid)
        if isinstance(record, dict):
            records.append(_parse_summary_record(record))
    return records


def _assembly_span(record: Mapping[str, object]) -> tuple[int, int] | None:
    """The record's widest span on the assembly ``[Base Position]`` indexes, or ``None`` if it states none.

    An esummary carries one ``variation_loc`` per assembly per allele; the widest is taken because a
    record covering several alleles overlaps a span if any of them does.
    """
    alleles = record.get('variation_set')
    bounds: list[int] = []
    for allele in alleles if isinstance(alleles, list) else []:
        locations = allele.get('variation_loc') if isinstance(allele, Mapping) else None
        for location in locations if isinstance(locations, list) else []:
            if not isinstance(location, Mapping) or location.get('assembly_name') != _ASSEMBLY:
                continue
            start, stop = location.get('start'), location.get('stop')
            if isinstance(start, str) and start.isdigit() and isinstance(stop, str) and stop.isdigit():
                bounds += [int(start), int(stop)]
    return (min(bounds), max(bounds)) if bounds else None


def _overlapping_records(result: Mapping[str, object], start: int, end: int) -> list[ClinvarRecordData]:
    """The summarised records whose own span meets ``[start, end]``.

    The search term reaches every record with an ENDPOINT in the span plus the long records near it
    (see ``_span_term``), so the ones the second clause pulled in have to be measured against the
    span rather than assumed to meet it. A record stating no span on this assembly is kept: it was
    matched at these coordinates, and dropping it would take a record out of the census on the
    strength of a field it did not fill.
    """
    uids = result.get('uids')
    if not isinstance(uids, list):
        raise ValueError('ClinVar esummary result has no uids list')
    records = []
    for uid in uids:
        record = result.get(uid)
        if not isinstance(record, dict):
            continue
        span = _assembly_span(record)
        if span is None or (span[0] <= end and span[1] >= start):
            records.append(_parse_summary_record(record))
    return records


def _clinsig_property(term: str) -> str:
    """The Entrez property indexing one ClinVar germline classification term.

    Entrez spells the property as the term with its comma dropped, so "Pathogenic, low penetrance"
    is indexed as ``clinsig pathogenic low penetrance``.

    `[Clinical significance]` is no ClinVar search field at all (einfo lists none), and Entrez does
    not refuse an unknown one: it translates `"pathogenic"[Clinical significance]` to `[All Fields]`,
    which matched 1452 MYH7 records, 60% of a 200-record page "Uncertain significance".
    """
    return f'"clinsig {term.replace(",", "")}"[Properties]'


def _review_status_clause(review_status_floor: int) -> str:
    """The term clause admitting only review statuses worth ``review_status_floor`` stars or more.

    Derived from the same star map the returned records are graded with, for the reason the
    ``clinsig`` clause is derived from the classification vocabulary: a clause narrower than the
    filter reads back as ClinVar holding no such record. `[Review status]` is a real ClinVar search
    field (unlike `[Clinical significance]`), matched on the exact status phrase; an unrecognised
    phrase contributes nothing to the OR, so ClinVar's retired spellings can stay in the map.

    Returns:
        The parenthesised clause, or empty at a floor of 0 — where every status qualifies, including
        the one a record with no germline classification carries, which has no phrase to search on.
    """
    if review_status_floor <= 0:
        return ''
    admitted = sorted(
        status for status, stars in _STAR_BY_REVIEW_STATUS.items() if status and stars >= review_status_floor
    )
    return ' AND ({})'.format(' OR '.join(f'"{status}"[Review status]' for status in admitted))


def _gene_term(gene: str, review_status_floor: int) -> str:
    """The gene's pathogenic term at a review-status floor, as clauses over ClinVar's own indexes.

    Both clause lists are derived from the vocabularies their filters apply rather than kept beside
    them, so adding a term cannot leave the search narrower than the filter — which would read back
    as ClinVar holding no such record for the gene. That closes the drift, not the spelling:
    `_clinsig_property` transforms a term into a property name and nothing checks the result is one
    Entrez indexes, so a term whose property is spelled otherwise matches nothing.

    Raises:
        errors.InvalidRequestError: If ``gene`` is empty — the term would drop the gene clause and
            return ClinVar's whole P/LP set as if it were the gene's.
    """
    if not gene.strip():
        raise errors.InvalidRequestError('ClinVar takes an HGNC symbol for the gene pool; got an empty gene')
    clinsig = ' OR '.join(sorted(_clinsig_property(t) for t in clinvar_classification.PATHOGENIC_TERMS))
    return f'{gene}[gene] AND ({clinsig}){_review_status_clause(review_status_floor)}'


async def fetch_variant_archive(vcv: str, *, http_client: httpx2.AsyncClient) -> ClinvarArchive:
    """Fetch the ClinVar archive one variation accession names.

    Keyed on the accession rather than on an HGVS string because ClinVar indexes renderings: FOXG1
    ``NM_005249.5:c.234_236del`` is indexed under the ``NM_005249.4`` spelling and titled in repeat
    notation, so a search for the caller's own expression answers with nothing or with a different
    allele of the same codon. The ClinGen Allele Registry's crosswalk resolves the identity; this
    resolves what that identity names.

    Args:
        vcv: The zero-padded variation accession (``VCV000704508``), as ``Variant.Normalize``'s
            ``clinvar_variations`` states it.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ``ClinvarArchive``: the archive whole, and the reading the interface reports off it.

    Raises:
        errors.InconsistentSourcesError: If ClinVar holds no record under the accession the
            crosswalk named, whichever of its two spellings efetch answers with (see
            `_efetch_archive`).
        errors.InvalidRequestError: If ``vcv`` is not a zero-padded VCV accession, or if E-utilities
            refuses the call for any other reason (a non-429 4xx).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If efetch answers with more than one archive, or the archive is structurally
            malformed.
    """
    requests.require_vcv_accession('ClinVar', vcv)
    archive, query = await _efetch_archive(vcv, http_client=http_client)
    return ClinvarArchive(
        record=_record_from_archive(archive),
        variation_archive=clinvar_proto.xml_converter.VariationArchiveType(archive),
        source=_SOURCE,
        dataset_versions=(_DB,),
        query=query,
    )


async def _walk_uids(term: str, limit: int, *, http_client: httpx2.AsyncClient) -> _SearchResult:
    """Walk the term's UIDs up to ``limit``, in esearch pages.

    Entrez's default ordering is by descending variation id and is stable across pages, so a page
    boundary neither repeats nor skips a record. No ``sort`` is sent: ClinVar accepts only
    `relevance` and `title` (every other schema comes back "Unknown sort schema … ignored"), and
    neither ranks by review status or classification, so a sort would reorder the prefix without
    improving what falls inside it. What makes a bounded prefix answer the question is the term:
    the pool's review-status clause, the span's coordinate range.
    """
    uids: list[str] = []
    total = 0
    while len(uids) < limit:
        page = await _esearch(
            term, http_client=http_client, retmax=min(_ESEARCH_PAGE, limit - len(uids)), retstart=len(uids)
        )
        total = page.total
        uids.extend(page.uids)
        if not page.uids or len(uids) >= page.total:
            break
    return _SearchResult(uids=uids, total=total)


async def _summarised(uids: Sequence[str], *, http_client: httpx2.AsyncClient) -> list[ClinvarRecordData]:
    """Every UID's esummary record, fetched in batches sized to the measured payload cost."""
    records: list[ClinvarRecordData] = []
    for batch in itertools.batched(uids, _ESUMMARY_BATCH, strict=False):
        records.extend(_records_from_summary(await _esummary(batch, http_client=http_client)))
    return records


def _require_record_bound(limit: int, *, subject: str) -> None:
    if limit < 1:
        raise ValueError(f'the {subject} needs a positive record bound, got {limit}')


async def fetch_gene_pool(
    gene: str, *, http_client: httpx2.AsyncClient, review_status_floor: int, limit: int
) -> ClinvarGenePool:
    """Fetch the gene's pathogenic-classification pool at a review-status floor, and its census.

    Args:
        gene: HGNC symbol whose classified-variant set feeds the informative-variant pool + density.
        http_client: The async HTTP client (caller owns its lifecycle).
        review_status_floor: Minimum ClinVar gold stars a record needs to enter the pool; 0 keeps
            every record. Required, and passed through from the caller rather than fixed here:
            what review status "known pathogenic" demands is a curation policy that differs per use
            (see ``themis.svcv4.frequency`` on the frequency case), and a floor applied invisibly at
            this layer would silently narrow one the caller thinks it is choosing. It scopes the
            search itself, so raising it is what makes a well-reviewed record beyond ``limit``
            reachable rather than merely selecting among the records the bound already admitted.
        limit: Max records to summarise and filter. Required: it is the pool's whole cost, ~1.1 MB
            and ~10 s per 500 records against the live index, so no layer here can pick one for a
            caller that must answer within a deadline. ``total`` still reports every record the term
            matches, so a bounded pool says so.

    Returns:
        The ``ClinvarGenePool``: the records ClinVar classifies pathogenic in any of its aggregate
        spellings, at or above the floor, the census behind them, and provenance.
        This is ``clinvar_classification``'s wider gate; SM3's DAFT applies the narrower one to the
        same pool itself (``themis.svcv4.frequency.known_pathogenic``).

    Raises:
        errors.InvalidRequestError: If ``gene`` is empty.
        errors.InvalidRequestError: If E-utilities refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If ``review_status_floor`` is outside ClinVar's gold-star range, ``limit`` is not
            positive, an esearch/esummary response is structurally malformed, or a record carries a
            classification term outside ClinVar's germline vocabulary.
    """
    if not 0 <= review_status_floor <= frequency.MAX_REVIEW_STARS:
        raise ValueError(f'review status floor must be 0-{frequency.MAX_REVIEW_STARS} stars, got {review_status_floor}')
    _require_record_bound(limit, subject='gene pool')
    term = _gene_term(gene, review_status_floor)
    search = await _walk_uids(term, limit, http_client=http_client)
    summarised = await _summarised(search.uids, http_client=http_client)
    records = [
        r
        for r in summarised
        if clinvar_classification.is_pathogenic(r.classification) and r.review_stars >= review_status_floor
    ]
    return ClinvarGenePool(
        records=records,
        total=search.total,
        considered=len(search.uids),
        source=_SOURCE,
        dataset_versions=(_DB,),
        query=term,
    )


def _span_term(gene: str, start: int, end: int) -> str:
    """The term matching every record of ``gene`` whose own span meets ``[start, end]``.

    ``[Base Position]`` (Entrez ``CHRPOS``) is ClinVar's only positional index and carries no
    chromosome, so an unscoped range matches the same coordinates on every chromosome; the gene
    clause is what confines it, and it is also what the *_INF rules are asked within. Deliberately
    carries no classification and no review-status clause: the benign and uncertain records are the
    ones this lookup exists to return.

    Two clauses, because the index holds a record's two ENDPOINTS and not the bases between them
    (measured: a 6-nt deletion answers a search at its first and last base and not at either base in
    between). A coordinate range alone therefore misses exactly the record that CONTAINS the span —
    a deletion beginning before a codon and ending after it, which is an informative variant at that
    codon — and misses it as an empty census. So a second clause searches a window ``_SPAN_PAD``
    wider at each end, for records between the span's own length — the shortest a containing record
    can be — and that length plus the two pads, the longest one that still fits the window. Both
    bounds are measured from the span, so an exon-wide interval states the same range a codon does;
    ``_overlapping_records`` then measures every hit against the span, since the wider clause also
    reaches long records that miss it. The residual is a containing record overhanging the span by
    more than the two pads together: a copy-number record, whose extent no c. range expresses and
    whose per-exon reading is ``AssessExonRelevance``'s.

    Raises:
        errors.InvalidRequestError: If ``gene`` is empty — the term would drop the gene clause and
            answer with every chromosome's records at those coordinates.
        ValueError: If the interval is not ascending or is not 1-based.
    """
    if not gene.strip():
        raise errors.InvalidRequestError('ClinVar takes an HGNC symbol to scope a span; got an empty gene')
    if start < 1 or end < start:
        raise ValueError(f'a ClinVar span takes an ascending 1-based interval, got {start}-{end}')
    span_length = end - start + 1
    containing = (
        f'{start - _SPAN_PAD}:{end + _SPAN_PAD}[Base Position] '
        f'AND {span_length}:{span_length + 2 * _SPAN_PAD}[Length of the variant]'
    )
    return f'{gene}[gene] AND ({start}:{end}[Base Position] OR ({containing}))'


async def _require_indexed_gene(gene: str, *, http_client: httpx2.AsyncClient) -> None:
    """Confirm ClinVar indexes the symbol at all, for a span search that matched nothing.

    An empty span is a *finding* — "no informative variant at this codon" — so a symbol ClinVar
    files nothing under must not answer as one, because a search scoped by it is empty whatever the
    coordinates. The symbol is the exon table's, not the caller's, so what this catches is the two
    annotation sources disagreeing about the gene's name outright. It does not catch the narrower
    disagreements: a symbol ClinVar indexes under a different locus, or a record ClinVar annotates
    only to the neighbouring gene, both leave the probe satisfied.

    Issued only on the empty path, where one more request buys the distinction.

    Raises:
        errors.InconsistentSourcesError: If the symbol matches no ClinVar record at all. Neither the
            request nor either source is at fault in a way a caller can act on — the two disagree,
            and reconciling them is this service's job — so it fails the lookup rather than reaching
            a caller as an absence it would read as the finding.
    """
    indexed = await _esearch(f'{gene}[gene]', http_client=http_client, retmax=0)
    if not indexed.total:
        raise errors.InconsistentSourcesError(
            f'ClinVar indexes no record under gene {gene!r}, the symbol the transcript alignment '
            'names, so a span scoped by it is empty whatever the coordinates; the two sources '
            'disagree about the gene, and the empty result is about that and not about the span'
        )


async def fetch_span_records(
    gene: str, start: int, end: int, *, http_client: httpx2.AsyncClient, limit: int
) -> ClinvarSpanRecords:
    """Fetch every germline record ClinVar holds for ``gene`` in one genomic interval.

    Args:
        gene: HGNC symbol the interval is read within; it scopes the search (see ``_span_term``).
        start: First genomic coordinate of the interval, 1-based inclusive, on the assembly
            ``[Base Position]`` indexes (GRCh38).
        end: Last genomic coordinate, inclusive; not before ``start``.
        http_client: The async HTTP client (caller owns its lifecycle).
        limit: Max records to summarise. Required, for the reason ``fetch_gene_pool``'s is — the
            summaries are the lookup's whole cost — though a codon or an exon holds tens of records
            rather than thousands, so here the bound guards rather than selects.

    Returns:
        The ``ClinvarSpanRecords``: every record whose own span meets the interval, unfiltered by
        classification and by review status, with the census behind them and provenance. A record
        ClinVar carries with no germline classification at all comes back with an empty
        ``classification`` rather than being dropped: it is in the span, and it is not an informative
        variant.

    Raises:
        errors.InvalidRequestError: If ``gene`` is empty, or if E-utilities refuses the call (a
            non-429 4xx).
        errors.InconsistentSourcesError: If the span matched nothing and ClinVar indexes no record
            under the symbol at all (see ``_require_indexed_gene``).
        httpx2.HTTPStatusError: If E-utilities returns a 429 or a 5xx.
        ValueError: If the interval is not an ascending 1-based one, ``limit`` is not positive, an
            esearch/esummary response is structurally malformed, or a record carries a review status
            outside ClinVar's vocabulary.
    """
    _require_record_bound(limit, subject='span census')
    term = _span_term(gene, start, end)
    search = await _walk_uids(term, limit, http_client=http_client)
    if not search.total:
        await _require_indexed_gene(gene, http_client=http_client)
    records: list[ClinvarRecordData] = []
    for batch in itertools.batched(search.uids, _ESUMMARY_BATCH, strict=False):
        records.extend(_overlapping_records(await _esummary(batch, http_client=http_client), start, end))
    return ClinvarSpanRecords(
        records=records,
        total=search.total,
        considered=len(search.uids),
        source=_SOURCE,
        dataset_versions=(_DB,),
        query=term,
    )
