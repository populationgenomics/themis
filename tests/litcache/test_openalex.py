"""OpenAlex: the batch DOI resolver, and the work record loaded whole into its mirror.

Synthetic works exercise the id half (bare ids off their URL forms, the batch keyed by DOI);
committed OpenAlex responses — the OA paper's and a bioRxiv preprint's (OpenAlex data is CC0) —
exercise the record half and the round-trip gate. Live OpenAlex is integration-gated:
`LITCACHE_OPENALEX_LIVE_DOI` resolves one DOI, and `LITCACHE_OPENALEX_LIVE_SAMPLE` runs the
round-trip gate over that many random works — where the mirror's lag behind the index shows first.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
from collections.abc import Mapping

import httpx2
import pytest

from themis.common import constants
from themis.litcache import mirror, openalex, paper_metadata
from themis.litcache.models import openalex_pb2
from themis.testing import mirror_roundtrip

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_DOI = '10.1186/s13073-017-0482-5'
_PREPRINT_DOI = '10.1101/2024.09.14.613029'
_DOI_A = '10.1/a'
_DOI_B = '10.1/b'


def _work(doi: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        'doi': f'https://doi.org/{doi}',
        'title': 'Title A',
        'display_name': 'Title A',
        'publication_date': '2020-01-15',
        'ids': {'pmid': 'https://pubmed.ncbi.nlm.nih.gov/111'},
        'type': 'article',
        'primary_location': {'source': {'display_name': 'Journal A', 'host_organization_name': 'Pub A'}},
    }
    record.update(overrides)
    return record


def _response(records: list[dict[str, object]]) -> bytes:
    return json.dumps({'meta': {'count': len(records)}, 'results': records}).encode()


def _fixture_work(fixture: str) -> dict[str, object]:
    return json.loads((_FIXTURES / fixture / 'openalex.json').read_bytes())['results'][0]


def _unwrap(document: dict[str, object]) -> dict[str, object]:
    """The inverse of `openalex.wrap`, over the proto3-JSON the mirror serialises to."""
    unwrapped = dict(document)
    index = unwrapped.get('abstract_inverted_index')
    if isinstance(index, Mapping):
        unwrapped['abstract_inverted_index'] = {word: entry.get('positions', []) for word, entry in index.items()}
    return unwrapped


def test_parse_response_reads_bare_ids_off_their_url_forms() -> None:
    records = [
        _work(_DOI_A),
        _work(
            _DOI_B,
            title='Preprint B',
            ids={},
            type='preprint',
            primary_location={'source': {'display_name': 'medRxiv'}},
        ),
    ]
    parsed = openalex.parse_response(_response(records))

    assert set(parsed.works) == {_DOI_A, _DOI_B}
    assert parsed.drifted == {}
    assert parsed.works[_DOI_A].doi == f'https://doi.org/{_DOI_A}'
    assert openalex.bare_pmid(parsed.works[_DOI_A]) == '111'
    assert openalex.publisher(parsed.works[_DOI_A]) == 'Pub A'
    assert openalex.bare_pmid(parsed.works[_DOI_B]) is None
    assert openalex.publisher(parsed.works[_DOI_B]) is None
    assert parsed.works[_DOI_B].type == 'preprint'


def test_parse_skips_records_without_a_doi() -> None:
    parsed = openalex.parse_response(_response([{'display_name': 'No DOI', 'ids': {}}]))
    assert parsed == openalex.ParsedWorks(works={}, drifted={})


def test_parse_rejects_a_non_works_payload() -> None:
    with pytest.raises(ValueError, match='results'):
        openalex.parse_response(json.dumps({'meta': {'count': 0}}).encode())


def test_a_record_that_does_not_fit_the_mirror_is_charged_to_its_doi_alone() -> None:
    parsed = openalex.parse_response(_response([_work(_DOI_A), _work(_DOI_B, is_zpac=True)]))

    assert set(parsed.works) == {_DOI_A}
    assert set(parsed.drifted) == {_DOI_B}
    assert 'is_zpac' in parsed.drifted[_DOI_B]


def test_parse_work_names_the_index_in_the_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError) as excinfo:
        openalex.parse_work(_work(_DOI_A, is_zpac=True))
    assert excinfo.value.index == 'openalex'


def test_wrap_wraps_the_inverted_index() -> None:
    work = openalex.parse_work(_work(_DOI_A, abstract_inverted_index={'The': [0, 5], 'gene': [1]}))
    assert list(work.abstract_inverted_index['The'].positions) == [0, 5]
    assert list(work.abstract_inverted_index['gene'].positions) == [1]


def test_fetch_rejects_a_batch_over_the_cap() -> None:
    async def run() -> None:
        async with httpx2.AsyncClient() as client:
            await openalex.fetch([f'10.1/{i}' for i in range(51)], http_client=client)

    with pytest.raises(ValueError, match='caps at 50'):
        asyncio.run(run())


def test_resolve_drives_fetch_with_a_doi_filter() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(request.url.params)
        return httpx2.Response(200, content=_response([_work(_DOI_A)]))

    async def run() -> openalex.ParsedWorks:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await openalex.resolve([_DOI_A], http_client=client)

    parsed = asyncio.run(run())
    assert seen['filter'] == f'doi:{_DOI_A}'
    assert parsed.works[_DOI_A].title == 'Title A'


@pytest.mark.parametrize(
    ('fixture', 'doi', 'pmid'),
    [('oa', _DOI, '29089047'), ('preprint', _PREPRINT_DOI, None)],
)
def test_fixture_work_loads_whole(fixture: str, doi: str, pmid: str | None) -> None:
    source = _fixture_work(fixture)
    work = openalex.parse_work(source)

    assert work.doi == f'https://doi.org/{doi}'
    assert openalex.bare_pmid(work) == pmid
    assert work.title
    assert len(work.authorships) == len(source['authorships'])  # pyright: ignore[reportArgumentType]
    assert len(work.abstract_inverted_index) == len(source['abstract_inverted_index'])  # pyright: ignore[reportArgumentType]
    assert work.open_access.oa_status


@pytest.mark.parametrize('fixture', ['oa', 'preprint'])
def test_round_trip_is_lossless(fixture: str) -> None:
    source = _fixture_work(fixture)
    mirror_roundtrip.assert_lossless(source, openalex.parse_work(source), _unwrap)


def test_to_metadata_envelopes_the_record() -> None:
    work = openalex.parse_work(_fixture_work('preprint'))
    envelope = paper_metadata.parse(openalex.to_metadata(work))

    assert envelope.HasField('openalex')
    assert not envelope.HasField('pubmed')
    assert paper_metadata.title(envelope) == work.title


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_DOI'),
    reason='set LITCACHE_OPENALEX_LIVE_DOI to hit the live OpenAlex works API',
)
def test_live_openalex() -> None:
    doi = os.environ['LITCACHE_OPENALEX_LIVE_DOI']

    async def run() -> openalex.ParsedWorks:
        async with httpx2.AsyncClient(timeout=30.0) as client:
            return await openalex.resolve([doi], http_client=client)

    parsed = asyncio.run(run())
    assert parsed.drifted == {}
    assert doi in parsed.works


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_SAMPLE'),
    reason='set LITCACHE_OPENALEX_LIVE_SAMPLE to a sample size to run the round-trip gate over live works',
)
def test_live_openalex_sample_round_trips() -> None:
    size = int(os.environ['LITCACHE_OPENALEX_LIVE_SAMPLE'])

    async def run() -> list[dict[str, object]]:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                'https://api.openalex.org/works',
                params={
                    'sample': str(size),
                    'seed': str(random.randint(1, 10**6)),  # noqa: S311 — a sampling seed, not a secret
                    'per-page': str(size),
                    'mailto': constants.CONTACT_EMAIL,
                },
            )
            response.raise_for_status()
            return response.json()['results']

    for source in asyncio.run(run()):
        work: openalex_pb2.Work = openalex.parse_work(source)
        mirror_roundtrip.assert_lossless(source, work, _unwrap)
