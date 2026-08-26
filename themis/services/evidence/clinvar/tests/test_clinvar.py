"""Behaviour tests for the clinvar servicer over an in-process grpc.aio server.

The fixture backend serves most of them. What one upstream answer becomes is asserted over the live
backend instead, driven by an httpx `MockTransport`, so the status a caller sees is the one the whole
path produces rather than one restated at the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
from collections.abc import AsyncIterator, Callable, Mapping

import grpc
import grpc.aio
import httpx
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, clinvar_pb2, clinvar_pb2_grpc
from themis.services.evidence.clinvar import backend as clinvar_backend
from themis.services.evidence.clinvar import servicer as servicer_mod
from themis.services.evidence.upstreams import clinvar as clinvar_upstream
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500
_VCV = 'VCV001731988'

_UPSTREAM_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
# What efetch answered VCV999999999 with, recorded off the live endpoint.
_NO_RECORD_ENVELOPE = (_UPSTREAM_FIXTURES / 'clinvar_efetch_no_record.xml').read_bytes()
# The exon table the span is projected through, and the release stamp VariantValidator provenance needs.
_TRANSCRIPT = 'NM_001042492.3'
_EXON_TABLE = json.loads((_UPSTREAM_FIXTURES / 'transcript_structure.json').read_bytes())
_VARIANTVALIDATOR_METADATA = {
    'metadata': {
        'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
        'vvdb_version': 'vvdb_2025_3',
        'vvta_version': 'vvta_2025_02',
    }
}


def _describe_request(
    *,
    vcv: str = _VCV,
    gene: str = 'NF1',
    review_status_floor: int = 0,
    max_pool_records: int = _POOL_RECORDS,
) -> clinvar_pb2.DescribeVariantRequest:
    return clinvar_pb2.DescribeVariantRequest(
        vcv=vcv, gene=gene, review_status_floor=review_status_floor, max_pool_records=max_pool_records
    )


def _refused(request: clinvar_pb2.DescribeVariantRequest) -> str:
    """The INVALID_ARGUMENT detail the servicer refuses this request with."""

    async def run() -> clinvar_pb2.DescribeVariantResponse:
        async with _serving(_backend()) as stub:
            return await stub.DescribeVariant(request, metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    return caught.value.details() or ''


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    describe_variant: Mapping[str, clinvar_pb2.DescribeVariantResponse] | None = None,
    search_coding_span: Mapping[str, clinvar_pb2.SearchCodingSpanResponse] | None = None,
) -> clinvar_backend.FixtureBackend:
    return clinvar_backend.FixtureBackend(
        {} if describe_variant is None else describe_variant, {} if search_coding_span is None else search_coding_span
    )


@contextlib.asynccontextmanager
async def _serving(backend: clinvar_backend.ClinVarBackend) -> AsyncIterator[clinvar_pb2_grpc.ClinVarAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        clinvar_pb2_grpc.add_ClinVarServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield clinvar_pb2_grpc.ClinVarStub(channel)


def _failed_describe(handler: Callable[[httpx.Request], httpx.Response]) -> grpc.aio.AioRpcError:
    """Fail `DescribeVariant` over the live backend, with E-utilities answered by `handler`.

    Args:
        handler: Answers every E-utilities request. One is issued: the accession's own archive is
            fetched before the gene pool, and failing it ends the rpc.

    Returns:
        The error the rpc failed with.
    """

    async def run() -> clinvar_pb2.DescribeVariantResponse:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client,
            _serving(clinvar_backend.LiveBackend(http_client)) as stub,
        ):
            return await stub.DescribeVariant(_describe_request(), metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    return caught.value


def _failed_span(handler: Callable[[httpx.Request], httpx.Response]) -> grpc.aio.AioRpcError:
    """Fail `SearchCodingSpan` over the live backend, with both upstreams answered by `handler`.

    Args:
        handler: Answers VariantValidator (the exon table and its release stamp) and every
            E-utilities request the span search then issues.

    Returns:
        The error the rpc failed with.
    """

    async def run() -> clinvar_pb2.SearchCodingSpanResponse:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client,
            _serving(clinvar_backend.LiveBackend(http_client)) as stub,
        ):
            return await stub.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript=_TRANSCRIPT, cds_start=3496, cds_end=3498, max_records=50
                ),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    return caught.value


@pytest.mark.parametrize(
    'vcv',
    [
        '1731988',  # the bare UID efetch answers with an empty result set
        'VCV1731988',
        'CA398989536',
        'rs1597719704',
        'NM_001042492.3:c.3496G>C',
    ],
)
def test_clinvar_rejects_an_identifier_that_is_not_a_padded_accession(vcv: str) -> None:
    """The precondition is the padded accession, and the unpadded forms are why it exists.

    efetch takes a bare UID with a 200 carrying an empty result set, which reads back as ClinVar
    holding no record — the absence a novelty finding rests on. The rest name no ClinVar variation
    at all.
    """
    assert 'variation accession' in _refused(_describe_request(vcv=vcv))


def test_clinvar_rejects_an_absent_gene() -> None:
    """Without the gene clause the pool term answers with ClinVar's whole P/LP set, not the gene's."""
    assert 'HGNC symbol' in _refused(_describe_request(gene=' '))


@pytest.mark.parametrize('floor', [-1, 5])
def test_clinvar_rejects_a_review_status_floor_off_clinvars_star_scale(floor: int) -> None:
    """A floor outside 0-4 filters against a scale ClinVar does not use, silently emptying the pool."""
    assert 'gold-star' in _refused(_describe_request(review_status_floor=floor))


@pytest.mark.parametrize('bound', [0, 2001])
def test_clinvar_refuses_a_pool_bound_it_has_no_default_for(bound: int) -> None:
    """The pool's cost is the caller's to state; an unstated 0 must not read as "everything"."""
    assert 'max_pool_records' in _refused(_describe_request(max_pool_records=bound))


def test_an_unset_accession_asks_for_the_gene_pool_alone() -> None:
    """The crosswalk naming no ClinVar variation is the ordinary case for a novel allele."""
    tables = _backend(describe_variant={':NF1': clinvar_pb2.DescribeVariantResponse(total_in_gene=7310)})

    async def run() -> clinvar_pb2.DescribeVariantResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeVariant(_describe_request(vcv=''), metadata=_GOOD_TOKEN)

    assert asyncio.run(run()).total_in_gene == 7310


def test_the_named_variation_is_part_of_what_is_being_asked() -> None:
    """Keyed on the gene alone, a request naming one variation would answer with another's record."""
    tables = _backend(
        describe_variant={
            'VCV001731988:NF1': clinvar_pb2.DescribeVariantResponse(
                this_variant=clinvar_pb2.ClinVarRecord(clinvar_id='VCV001731988')
            )
        }
    )

    async def run(vcv: str) -> clinvar_pb2.DescribeVariantResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeVariant(_describe_request(vcv=vcv), metadata=_GOOD_TOKEN)

    assert asyncio.run(run('VCV001731988')).this_variant.clinvar_id == 'VCV001731988'
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run('VCV000704508'))
    assert caught.value.code() is grpc.StatusCode.NOT_FOUND


@pytest.mark.parametrize(
    ('status', 'body', 'stated'),
    [
        (400, _NO_RECORD_ENVELOPE, 'ID list is empty'),
        (200, b'<ClinVarResult-Set><set/></ClinVarResult-Set>', 'empty result set'),
    ],
    ids=['a-refusal-naming-an-unresolved-id', 'an-empty-result-set'],
)
def test_an_accession_the_crosswalk_names_and_clinvar_lacks_is_a_disagreement(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, stated: str
) -> None:
    """ClinVar spells one fact two ways, and no caller can act on two codes for one answer.

    A well-formed accession is not a bad request, and an answer ClinVar gave is not a fault to retry
    four times. Nor is it NOT_FOUND: absence is what the novelty finding is scored in, and the
    crosswalk resolved this allele to this variation, so what a caller would be told is absent is
    the very record the other source says exists. FAILED_PRECONDITION says the two disagree, and the
    message carries the accession and which way ClinVar said it.
    """
    monkeypatch.setattr(clinvar_upstream, '_RATE_LIMIT_DELAY_S', 0)

    failure = _failed_describe(lambda _request: httpx.Response(status, content=body))

    assert failure.code() is grpc.StatusCode.FAILED_PRECONDITION
    details = failure.details() or ''
    assert _VCV in details
    assert 'crosswalk' in details
    assert stated in details


def test_a_refusal_worded_otherwise_is_still_invalid_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ClinVar's own "no record" wording is an absence; the rest of the 4xx taxonomy is unmoved."""
    monkeypatch.setattr(clinvar_upstream, '_RATE_LIMIT_DELAY_S', 0)
    body = b'<eFetchResult><ERROR>Invalid db name specified: clnvar</ERROR></eFetchResult>'

    failure = _failed_describe(lambda _request: httpx.Response(400, content=body))

    assert failure.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert 'Invalid db name' in (failure.details() or '')


def test_a_symbol_the_exon_table_names_and_clinvar_does_not_index_is_a_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other spelling of the same disagreement, and the one whose absence would be scored.

    An empty span *is* the finding the *_INF rules ask for — "no informative variant at this codon"
    — so a symbol ClinVar files nothing under has to fail rather than answer, and it cannot fail as
    NOT_FOUND either: that is the vocabulary the empty census is already reported in.
    """
    monkeypatch.setattr(clinvar_upstream, '_RATE_LIMIT_DELAY_S', 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/hello/':
            return httpx.Response(200, json=_VARIANTVALIDATOR_METADATA)
        if request.url.path.endswith('/esearch.fcgi'):  # the span, then the gene-only probe
            return httpx.Response(200, json={'esearchresult': {'idlist': [], 'count': '0'}})
        return httpx.Response(200, json=_EXON_TABLE)

    failure = _failed_span(handler)

    assert failure.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert 'indexes no record under gene' in (failure.details() or '')


def test_clinvar_span_is_keyed_by_the_c_range_it_was_asked_about() -> None:
    """Two spans of one transcript are two questions; a transcript-only key answers the wrong one."""
    tables = _backend(
        search_coding_span={
            'NM_001040142.2:1108:1110': clinvar_pb2.SearchCodingSpanResponse(
                gene='SCN2A', records=[clinvar_pb2.ClinVarRecord(clinvar_id='VCV000207049')]
            ),
            'NM_001040142.2:1111:1113': clinvar_pb2.SearchCodingSpanResponse(gene='SCN2A'),
        }
    )

    async def run(start: int, end: int) -> clinvar_pb2.SearchCodingSpanResponse:
        async with _serving(tables) as stub:
            return await stub.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript='NM_001040142.2', cds_start=start, cds_end=end, max_records=50
                ),
                metadata=_GOOD_TOKEN,
            )

    assert [r.clinvar_id for r in asyncio.run(run(1108, 1110)).records] == ['VCV000207049']
    assert not asyncio.run(run(1111, 1113)).records


@pytest.mark.parametrize(
    ('transcript', 'start', 'end', 'max_records'),
    [
        # c. numbering has no 0, so this is where an unset endpoint is caught rather than searched.
        ('NM_001040142.2', 0, 1110, 50),
        ('NM_001040142.2', 1108, 0, 50),
        # Descending: read as a span it would search nothing and answer "no informative variant".
        ('NM_001040142.2', 1110, 1108, 50),
        ('NM_001040142.2', 1108, 1110, 0),
        ('NM_001040142.2', 1108, 1110, 2001),
        # The exon table is accession-version-specific, and an ENST has no RefSeq record.
        ('NM_001040142', 1108, 1110, 50),
        ('ENST00000283256.10', 1108, 1110, 50),
        ('NM_001040142.2:c.1108T>G', 1108, 1110, 50),
    ],
)
def test_clinvar_span_refuses_a_request_that_would_search_the_wrong_thing(
    transcript: str, start: int, end: int, max_records: int
) -> None:
    async def run() -> clinvar_pb2.SearchCodingSpanResponse:
        async with _serving(_backend()) as stub:
            return await stub.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript=transcript, cds_start=start, cds_end=end, max_records=max_records
                ),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_the_clinvar_span_fixture_refuses_an_unseeded_span() -> None:
    # An unseeded span answering with an empty census reads as "no informative variant at this
    # codon", which is the finding the rpc exists to make statable.
    tables = _backend(search_coding_span={'NM_001040142.2:1108:1110': clinvar_pb2.SearchCodingSpanResponse()})

    async def run() -> clinvar_pb2.SearchCodingSpanResponse:
        async with _serving(tables) as stub:
            return await stub.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript='NM_001040142.2', cds_start=1200, cds_end=1202, max_records=50
                ),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.NOT_FOUND
