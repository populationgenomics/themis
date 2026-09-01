"""GTEx v2 median transcript expression: versioned-id guard, pagination, parsing, symbol resolution.

The recorded fixtures are real GTEx v10 responses for BRCA1: a medianTranscriptExpression
response trimmed to a few transcript x tissue rows, and a reference-gene record carrying the
v39 versioned gencodeId. Pagination and error paths use small inline payloads. All requests
are served by an httpx2 `MockTransport`.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable, Sequence

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import gtex

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'gtex.json'
_REFERENCE_GENE_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'gtex_reference_gene.json'

_Handler = Callable[[httpx2.Request], httpx2.Response]


def _fetch(handler: _Handler, gencode_id: str = 'ENSG00000012048.23', tissues: Sequence[str] = ()) -> gtex.GtexResult:
    async def run() -> gtex.GtexResult:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await gtex.fetch_gtex(gencode_id, tissues=tissues, http_client=client)

    return asyncio.run(run())


def _run[T](handler: _Handler, call: Callable[[httpx2.AsyncClient], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _row(transcript: str, tissue: str, median: float) -> dict[str, object]:
    return {'median': median, 'transcriptId': transcript, 'tissueSiteDetailId': tissue,
            'gencodeId': 'ENSG00000012048.23', 'datasetId': 'gtex_v10', 'unit': 'TPM'}  # fmt: skip


def test_happy_path_parses_fixture() -> None:
    body = _FIXTURE.read_bytes()
    result = _fetch(lambda _request: httpx2.Response(200, content=body))
    assert result.transcript_ids == ['ENST00000352993.7', 'ENST00000354071.7']
    assert result.source == 'GTEx'
    assert result.dataset_versions == ('gtex_v10', 'GENCODE v39')
    assert 'gencodeId=ENSG00000012048.23' in result.query
    assert len(result.rows) == 6


def test_unfiltered_summary_is_one_peak_tissue_per_transcript() -> None:
    """No tissue filter: the summary must stay small yet name where each isoform is expressed."""
    body = _FIXTURE.read_bytes()
    result = _fetch(lambda _request: httpx2.Response(200, content=body))
    assert {median.transcript for median in result.medians} == set(result.transcript_ids)
    assert len(result.medians) == len(result.transcript_ids)
    for median in result.medians:
        peers = [row for row in result.rows if row['transcriptId'] == median.transcript]
        assert median.median == max(float(row['median']) for row in peers)  # pyright: ignore[reportArgumentType]
        assert median.tissue in {row['tissueSiteDetailId'] for row in peers}


def test_tissue_filter_is_sent_and_summarises_every_returned_row() -> None:
    seen: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params.get_list('tissueSiteDetailId'))
        rows = [_row('ENST1', 'Liver', 1.0), _row('ENST2', 'Liver', 3.0)]
        return httpx2.Response(200, json={'data': rows, 'paging_info': {'numberOfPages': 1, 'page': 0}})

    result = _fetch(handler, tissues=['Liver'])
    assert seen == [['Liver']]
    assert not result.tissues_without_rows
    assert [(m.transcript, m.tissue, m.median) for m in result.medians] == [
        ('ENST1', 'Liver', 1.0),
        ('ENST2', 'Liver', 3.0),
    ]
    assert 'tissueSiteDetailId=Liver' in result.query


def test_no_tissue_param_is_sent_when_unfiltered() -> None:
    # An empty filter must send no tissueSiteDetailId at all — GTEx 422s an empty value.
    seen: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params.get_list('tissueSiteDetailId'))
        return httpx2.Response(200, content=_FIXTURE.read_bytes())

    _fetch(handler)
    assert seen == [[]]


def test_a_tissue_with_no_row_is_named_rather_than_dropped() -> None:
    # A tissue GTEx accepts but has no row for would otherwise read as "not expressed there"; the
    # other tissues' signal (and the rest of the composed rpc) must survive it.
    def handler(_request: httpx2.Request) -> httpx2.Response:
        rows = [_row('ENST1', 'Liver', 1.0)]
        return httpx2.Response(200, json={'data': rows, 'paging_info': {'numberOfPages': 1, 'page': 0}})

    result = _fetch(handler, tissues=['Liver', 'Nerve_Tibial'])
    assert result.tissues_without_rows == ['Nerve_Tibial']
    assert [m.tissue for m in result.medians] == ['Liver']


def test_a_filter_matching_nothing_is_data_not_a_failure() -> None:
    """The caller composes this with signals GTEx's coverage does not bound — pext among them.

    Raising would take those down for a gene GTEx simply does not measure in the requested tissue,
    which is a fact about the tissue and not a fault; `tissues_without_rows` is where it lands, so
    the absence still cannot read as "not expressed".
    """
    empty = {'data': [], 'paging_info': {'numberOfPages': 1, 'page': 0}}
    result = _fetch(lambda _request: httpx2.Response(200, json=empty), tissues=['Nerve_Tibial'])
    assert result.tissues_without_rows == ['Nerve_Tibial']
    assert not result.medians


def test_an_unfiltered_query_returning_nothing_is_a_bad_id() -> None:
    empty = {'data': [], 'paging_info': {'numberOfPages': 1, 'page': 0}}
    with pytest.raises(ValueError, match='no median transcript expression'):
        _fetch(lambda _request: httpx2.Response(200, json=empty))


def test_rejected_tissue_carries_the_accepted_vocabulary() -> None:
    rejection = {
        'detail': [
            {
                'type': 'enum',
                'loc': ['query', 'tissueSiteDetailId', 0, 'str-enum[TissueSiteDetailId]'],
                'msg': "Input should be 'Liver', 'Lung'",
                'ctx': {'expected': "'Liver', 'Lung'"},
            }
        ]
    }
    with pytest.raises(errors.InvalidRequestError, match="'Liver', 'Lung'"):
        _fetch(lambda _request: httpx2.Response(422, json=rejection), tissues=['Livr'])


def test_unversioned_gencode_id_raises_value_error() -> None:
    with pytest.raises(ValueError, match='versioned gencodeId'):
        _fetch(lambda _request: httpx2.Response(200, json={'data': []}), gencode_id='ENSG00000012048')


def test_pagination_follows_number_of_pages() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        page = request.url.params.get('page')
        if page == '0':
            return httpx2.Response(200, json={'data': [_row('ENST1', 'Liver', 1.0), _row('ENST1', 'Lung', 2.0)],
                                             'paging_info': {'numberOfPages': 2, 'page': 0}})  # fmt: skip
        return httpx2.Response(200, json={'data': [_row('ENST2', 'Liver', 3.0)],
                                         'paging_info': {'numberOfPages': 2, 'page': 1}})  # fmt: skip

    result = _fetch(handler)
    assert len(result.rows) == 3
    assert result.transcript_ids == ['ENST1', 'ENST2']


def test_empty_data_raises_value_error() -> None:
    with pytest.raises(ValueError, match='no median transcript expression'):
        _fetch(lambda _request: httpx2.Response(200, json={'data': [], 'paging_info': {'numberOfPages': 1, 'page': 0}}))


def test_non_2xx_raises_http_status_error() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _fetch(lambda _request: httpx2.Response(500, json={}))


def test_missing_data_list_raises_value_error() -> None:
    with pytest.raises(ValueError, match='no data list'):
        _fetch(lambda _request: httpx2.Response(200, json={'paging_info': {'numberOfPages': 1}}))


def _by_symbol_handler(
    seen: list[tuple[str, str]] | None = None,
    *,
    reference: object | None = None,
) -> _Handler:
    """Route reference-gene to the recorded (or supplied) record and expression to the median fixture."""
    reference_body = reference if reference is not None else None

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if seen is not None:
            seen.append((path, request.url.params.get('gencodeVersion') or ''))
        if path.endswith('/reference/gene'):
            if reference_body is not None:
                return httpx2.Response(200, json=reference_body)
            return httpx2.Response(200, content=_REFERENCE_GENE_FIXTURE.read_bytes())
        if path.endswith('/medianTranscriptExpression'):
            return httpx2.Response(200, content=_FIXTURE.read_bytes())
        raise AssertionError(f'unexpected request path {path!r}')

    return handler


def test_by_symbol_resolves_v39_id_then_fetches_expression() -> None:
    seen: list[tuple[str, str]] = []
    result = _run(
        _by_symbol_handler(seen),
        lambda c: gtex.fetch_gtex_by_symbol('BRCA1', http_client=c),
    )
    # the reference-gene call is pinned to GENCODE v39 (the gtex_v10-aligned release)
    assert ('/api/v2/reference/gene', 'v39') in seen
    # the resolved v39 id feeds the expression query
    assert 'gencodeId=ENSG00000012048.23' in result.query
    assert result.transcript_ids == ['ENST00000352993.7', 'ENST00000354071.7']


def test_by_symbol_missing_gene_is_an_absent_record() -> None:
    empty = {'data': [], 'paging_info': {'numberOfPages': 1, 'page': 0, 'totalNumberOfItems': 0}}
    with pytest.raises(errors.UnknownVariantError, match=r'no .* record for GENCODE v39'):
        _run(
            _by_symbol_handler(reference=empty),
            lambda c: gtex.fetch_gtex_by_symbol('NOTAGENE', http_client=c),
        )


def test_by_symbol_ambiguous_gene_raises_value_error() -> None:
    ambiguous = {
        'data': [
            {'geneSymbolUpper': 'ABC', 'gencodeId': 'ENSG00000000001.1', 'gencodeVersion': 'v39'},
            {'geneSymbolUpper': 'ABC', 'gencodeId': 'ENSG00000000002.1', 'gencodeVersion': 'v39'},
        ],
        'paging_info': {'numberOfPages': 1, 'page': 0, 'totalNumberOfItems': 2},
    }
    with pytest.raises(ValueError, match='multiple GENCODE ids'):
        _run(
            _by_symbol_handler(reference=ambiguous),
            lambda c: gtex.fetch_gtex_by_symbol('ABC', http_client=c),
        )


def test_by_symbol_refuses_a_record_from_another_gencode_release() -> None:
    """The pinned release is what keeps the resolved id inside the expression index.

    GTEx defaults reference/gene to v26, whose ids return zero expression rows — an absence
    indistinguishable from a gene it does not measure.
    """
    drifted = {
        'data': [{'geneSymbolUpper': 'BRCA1', 'gencodeId': 'ENSG00000012048.20', 'gencodeVersion': 'v26'}],
        'paging_info': {'numberOfPages': 1, 'page': 0, 'totalNumberOfItems': 1},
    }
    with pytest.raises(ValueError, match='GENCODE'):
        _run(
            _by_symbol_handler(reference=drifted),
            lambda c: gtex.fetch_gtex_by_symbol('BRCA1', http_client=c),
        )


def test_by_symbol_ignores_non_matching_symbol_records() -> None:
    # reference/gene can return a near-match record; only the exact (case-insensitive) symbol counts.
    mixed = {
        'data': [
            {'geneSymbolUpper': 'BRCA1P1', 'gencodeId': 'ENSG00000267595.1', 'gencodeVersion': 'v26'},
            {'geneSymbolUpper': 'BRCA1', 'gencodeId': 'ENSG00000012048.23', 'gencodeVersion': 'v39'},
        ],
        'paging_info': {'numberOfPages': 1, 'page': 0, 'totalNumberOfItems': 2},
    }
    result = _run(
        _by_symbol_handler(reference=mixed),
        lambda c: gtex.fetch_gtex_by_symbol('brca1', http_client=c),
    )
    assert 'gencodeId=ENSG00000012048.23' in result.query


def test_by_symbol_reference_non_2xx_raises() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _run(
            lambda _request: httpx2.Response(500, json={}),
            lambda c: gtex.fetch_gtex_by_symbol('BRCA1', http_client=c),
        )
