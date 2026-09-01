"""GTEx v2: median transcript expression across tissues, per gene.

Live per-gene query of GTEx's ``medianTranscriptExpression`` (dataset ``gtex_v10``),
following pagination. Returns a small per-transcript summary — the isoform-expression signal
``AssessExonRelevanceResponse`` carries in every response — alongside the verbatim rows, which the
backend surfaces only on request: one gene's full transcript x tissue grid is thousands of rows
(NF1: 35 transcripts x 54 tissues), the largest thing any evidence rpc can return. A ``tissues``
filter narrows the grid at the source, and is what the summary then covers; unfiltered, the
summary is each transcript's peak tissue. The expression path takes a *versioned* hg38 GENCODE id
(e.g. ``ENSG00000012048.23``); an unversioned id is rejected.

``fetch_gtex_by_symbol`` is the entry the exon-relevance backend uses: it resolves an HGNC
symbol to that versioned id via the ``reference/gene`` endpoint, then runs the expression
path. The resolution pins GENCODE ``v39`` — ``gtex_v10`` expression is indexed by v39 ids,
but ``reference/gene`` defaults to v26 (whose id returns zero expression rows).

Both releases are stamped into ``dataset_versions`` (``gtex_v10``, ``GENCODE v39``). The GENCODE one is
what the transcript ids are versioned against, so it is the release any join against another
annotation set is made across — a consumer that cannot see it reads a version mismatch as a
disagreement about the gene. ``reference/gene`` echoes the release it answered on, and a record
naming another one is a fault: the pin is what keeps the resolved id in the expression index.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import httpx2

from themis.services.evidence import errors, hgvs

_BASE_URL = 'https://gtexportal.org/api/v2/expression/medianTranscriptExpression'
_REFERENCE_GENE_URL = 'https://gtexportal.org/api/v2/reference/gene'
_DATASET_ID = 'gtex_v10'
# The GENCODE release ``gtex_v10`` expression is indexed by; reference/gene must be pinned to it.
_GENCODE_VERSION = 'v39'
_DATASET_VERSIONS = (_DATASET_ID, f'GENCODE {_GENCODE_VERSION}')
_SOURCE = 'GTEx'
# GTEx caps a page at 100000 items; one gene's transcript x tissue grid fits a page.
_ITEMS_PER_PAGE = 100000

_TRANSCRIPT_ID = 'transcriptId'
_TISSUE_ID = 'tissueSiteDetailId'
_MEDIAN = 'median'


@dataclasses.dataclass(frozen=True)
class TissueMedian:
    """One transcript's median expression (TPM) in one tissue."""

    transcript: str
    tissue: str
    median: float

    @property
    def base(self) -> str:
        """The transcript accession without its version."""
        return hgvs.accession_base(self.transcript)


@dataclasses.dataclass(frozen=True)
class GtexResult:
    """A gene's GTEx median transcript expression, summarised plus verbatim.

    Attributes:
        transcript_ids: The distinct transcript ids seen, in first-seen order.
        medians: The per-transcript summary — every fetched row when the query was filtered to
            specific tissues, else each transcript's highest-expressing tissue.
        tissues_without_rows: Requested tissues GTEx accepted but returned no row for; the caller
            must surface them, since their absence from ``medians`` otherwise reads as "not
            expressed there".
        rows: The per-transcript per-tissue median rows verbatim, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The GTEx dataset id and the GENCODE release its transcript ids are
            versioned against, e.g. ``("gtex_v10", "GENCODE v39")``.
        query: The exact request URL issued, tissue filter included.
    """

    transcript_ids: list[str]
    medians: list[TissueMedian]
    tissues_without_rows: list[str]
    rows: list[dict[str, object]]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _accepted_tissue_ids(response: httpx2.Response) -> str:
    """The ``tissueSiteDetailId`` vocabulary GTEx names in a rejection body; empty if it names none."""
    try:
        payload = response.json()
    except ValueError:
        return ''
    detail = payload.get('detail') if isinstance(payload, dict) else None
    if not isinstance(detail, list):
        return ''
    for entry in detail:
        if not isinstance(entry, dict) or _TISSUE_ID not in str(entry.get('loc')):
            continue
        context = entry.get('ctx')
        expected = context.get('expected') if isinstance(context, dict) else None
        if isinstance(expected, str):
            return expected
    return ''


def _rejected_query(response: httpx2.Response, tissues: Sequence[str], gencode_id: str) -> errors.InvalidRequestError:
    """The caller-facing error for a query GTEx refused to validate, carrying its accepted values."""
    accepted = _accepted_tissue_ids(response)
    return errors.InvalidRequestError(
        f'GTEx rejected the median-expression query for {gencode_id!r} with tissues {list(tissues)}; '
        f'accepted {_TISSUE_ID} values: {accepted or response.text}'
    )


async def _fetch_all_pages(
    gencode_id: str, tissues: Sequence[str], *, http_client: httpx2.AsyncClient
) -> list[dict[str, object]]:
    """Fetch every page of median-expression rows for a versioned gencode id."""
    rows: list[dict[str, object]] = []
    page = 0
    while True:
        params = {
            'gencodeId': gencode_id,
            'datasetId': _DATASET_ID,
            'itemsPerPage': str(_ITEMS_PER_PAGE),
            'page': str(page),
            _TISSUE_ID: list(tissues),
        }
        response = await http_client.get(_BASE_URL, params=params)
        # GTEx validates each param against its own enum, so an unknown tissue comes back as a 422
        # whose body carries the accepted vocabulary.
        if response.status_code == httpx2.codes.UNPROCESSABLE_CONTENT:
            raise _rejected_query(response, tissues, gencode_id)
        errors.raise_for_status(response, upstream=_SOURCE, subject=f'{gencode_id!r}')
        payload = response.json()
        data = payload.get('data')
        if not isinstance(data, list):
            raise ValueError('GTEx response has no data list')
        rows.extend(data)
        paging = payload.get('paging_info')
        number_of_pages = paging.get('numberOfPages') if isinstance(paging, dict) else None
        page += 1
        if not isinstance(number_of_pages, int) or page >= number_of_pages:
            break
    return rows


def _transcript_ids(rows: list[dict[str, object]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        transcript = row.get(_TRANSCRIPT_ID)
        if not isinstance(transcript, str):
            raise ValueError(f'GTEx row has no {_TRANSCRIPT_ID}: {row!r}')
        seen.setdefault(transcript, None)
    return list(seen)


def _tissue_median(row: dict[str, object]) -> TissueMedian:
    transcript, tissue, median = row.get(_TRANSCRIPT_ID), row.get(_TISSUE_ID), row.get(_MEDIAN)
    if not isinstance(transcript, str) or not isinstance(tissue, str) or not isinstance(median, (int, float)):
        raise ValueError(f'GTEx row is not a {_TRANSCRIPT_ID}/{_TISSUE_ID}/{_MEDIAN} triple: {row!r}')
    return TissueMedian(transcript=transcript, tissue=tissue, median=float(median))


def _summarise(rows: list[dict[str, object]], tissues: Sequence[str]) -> list[TissueMedian]:
    """Every row when the query named its tissues, else each transcript's highest-expressing one."""
    medians = [_tissue_median(row) for row in rows]
    if tissues:
        return medians
    peaks: dict[str, TissueMedian] = {}
    for entry in medians:
        best = peaks.get(entry.transcript)
        if best is None or entry.median > best.median:
            peaks[entry.transcript] = entry
    return list(peaks.values())


def _requested_but_unreturned(rows: list[dict[str, object]], tissues: Sequence[str]) -> list[str]:
    """Requested tissues no row came back for — an absence the caller has to state, not drop."""
    returned = {row.get(_TISSUE_ID) for row in rows}
    return [tissue for tissue in tissues if tissue not in returned]


async def fetch_gtex(gencode_id: str, *, tissues: Sequence[str] = (), http_client: httpx2.AsyncClient) -> GtexResult:
    """Fetch a gene's GTEx median transcript expression.

    Args:
        gencode_id: A versioned hg38 GENCODE gene id (e.g. ``ENSG00000012048.23``).
        tissues: GTEx ``tissueSiteDetailId`` values to restrict the grid to; empty fetches every
            tissue and summarises each transcript at its peak.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The per-transcript summary, the tissues that came back empty, the median-expression rows,
        and the distinct transcript ids.

    Raises:
        errors.InvalidRequestError: If GTEx rejects a requested tissue id (it validates against its
            own enum), naming the ids it accepts.
        ValueError: If ``gencode_id`` is unversioned, if a page lacks a ``data`` list, or if an
            unfiltered query returns no rows (a resolved versioned id always has expression;
            zero rows then signals a bad id, not a normal absence).
        httpx2.HTTPStatusError: If GTEx returns a 429 or a 5xx.
    """
    if '.' not in gencode_id:
        raise ValueError(f'GTEx requires a versioned gencodeId (e.g. ENSG00000012048.23), got {gencode_id!r}')
    rows = await _fetch_all_pages(gencode_id, tissues, http_client=http_client)
    # A tissue filter that returns nothing is a fact about the gene, carried in `tissues_without_rows`
    # rather than raised: the caller composes this with signals that do not share GTEx's coverage —
    # pext is one — and failing here would take those down with it. An UNFILTERED query returning
    # nothing is different: a resolved versioned id always has expression somewhere.
    if not rows and not tissues:
        raise ValueError(f'GTEx returned no median transcript expression for {gencode_id!r}')
    return GtexResult(
        transcript_ids=_transcript_ids(rows),
        medians=_summarise(rows, tissues),
        tissues_without_rows=_requested_but_unreturned(rows, tissues),
        rows=rows,
        source=_SOURCE,
        dataset_versions=_DATASET_VERSIONS,
        query=f'{_BASE_URL}?gencodeId={gencode_id}&datasetId={_DATASET_ID}'
        + ''.join(f'&{_TISSUE_ID}={tissue}' for tissue in tissues),
    )


def _gencode_id_for_symbol(data: list[object], gene_symbol: str) -> str:
    """The single versioned GENCODE id whose reference-gene record matches ``gene_symbol``.

    Raises:
        ValueError: If no record's symbol equals ``gene_symbol`` (case-insensitive), if the records
            carry more than one distinct GENCODE id for it (an ambiguous symbol), or if a matching
            record is stated against another GENCODE release — the id would then be outside the
            expression index and every query on it would come back with no rows.
    """
    target = gene_symbol.upper()
    ids: list[str] = []
    for record in data:
        if not isinstance(record, dict):
            continue
        symbol = record.get('geneSymbolUpper')
        gencode_id = record.get('gencodeId')
        if isinstance(symbol, str) and symbol == target and isinstance(gencode_id, str):
            release = record.get('gencodeVersion')
            if release != _GENCODE_VERSION:
                raise ValueError(
                    f'GTEx reference-gene answered {gene_symbol!r} on GENCODE {release!r}, not the '
                    f'{_GENCODE_VERSION} the query pinned; {gencode_id!r} would index no expression'
                )
            ids.append(gencode_id)
    distinct = list(dict.fromkeys(ids))
    if not distinct:
        raise errors.UnknownVariantError(
            f'GTEx reference-gene has no {gene_symbol!r} record for GENCODE {_GENCODE_VERSION}'
        )
    if len(distinct) > 1:
        raise ValueError(f'GTEx reference-gene has multiple GENCODE ids for {gene_symbol!r}: {distinct}')
    return distinct[0]


async def _resolve_gencode_id(gene_symbol: str, *, http_client: httpx2.AsyncClient) -> str:
    """Resolve an HGNC symbol to its ``gtex_v10`` (GENCODE v39) versioned GENCODE id.

    Raises:
        errors.InvalidRequestError: If GTEx refuses the reference-gene query (a non-429 4xx).
        httpx2.HTTPStatusError: If the reference-gene endpoint returns a 429 or a 5xx.
        errors.UnknownVariantError: If GTEx has no record for the symbol.
        ValueError: If the response has no ``data`` list, the symbol is ambiguous, or a record comes
            back on a GENCODE release other than the pinned one.
    """
    params = {'geneId': gene_symbol, 'gencodeVersion': _GENCODE_VERSION}
    response = await http_client.get(_REFERENCE_GENE_URL, params=params)
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'gene {gene_symbol!r}')
    payload = response.json()
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError(f'GTEx reference-gene response for {gene_symbol!r} has no data list')
    return _gencode_id_for_symbol(data, gene_symbol)


async def fetch_gtex_by_symbol(
    gene_symbol: str, *, tissues: Sequence[str] = (), http_client: httpx2.AsyncClient
) -> GtexResult:
    """Fetch a gene's GTEx median transcript expression, keyed by HGNC symbol.

    Resolves the symbol to its ``gtex_v10`` versioned GENCODE id via the reference-gene endpoint,
    then runs the median-transcript-expression path on it.

    Args:
        gene_symbol: HGNC gene symbol (from Resolve).
        tissues: GTEx ``tissueSiteDetailId`` values to restrict the grid to; empty fetches every
            tissue.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ``GtexResult`` for the symbol's resolved GENCODE id.

    Raises:
        errors.InvalidRequestError: If GTEx rejects a requested tissue id.
        errors.UnknownVariantError: If GTEx has no reference-gene record for the symbol.
        httpx2.HTTPStatusError: If a GTEx call returns a 429 or a 5xx.
        ValueError: If the symbol has no unambiguous reference-gene record, or the resolved id
            yields no expression in any tissue.
    """
    gencode_id = await _resolve_gencode_id(gene_symbol, http_client=http_client)
    return await fetch_gtex(gencode_id, tissues=tissues, http_client=http_client)
