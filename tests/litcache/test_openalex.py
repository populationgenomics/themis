"""OpenAlex: the batch DOI resolver, and the work record loaded into its mirror and kept as text.

Synthetic works exercise the id half (bare ids off their URL forms, the batch keyed by DOI);
committed OpenAlex responses — the OA paper's and a bioRxiv preprint's (OpenAlex data is CC0) —
exercise the record half. Live OpenAlex is integration-gated: `LITCACHE_OPENALEX_LIVE_DOI`
resolves one DOI, and `LITCACHE_OPENALEX_LIVE_SAMPLE` loads that many random works, each into its
mirror without drift and into text that parses back to the work served — where a declared key
changing shape shows first.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random

import httpx2
import pytest

from themis.common import constants
from themis.litcache import mirror, openalex, paper_metadata

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
    work_a, work_b = parsed.works[_DOI_A].work, parsed.works[_DOI_B].work
    assert work_a.doi == f'https://doi.org/{_DOI_A}'
    assert openalex.bare_pmid(work_a) == '111'
    assert openalex.publisher(work_a) == 'Pub A'
    assert openalex.bare_pmid(work_b) is None
    assert openalex.publisher(work_b) is None
    assert work_b.type == 'preprint'


def test_parse_skips_records_without_a_doi() -> None:
    parsed = openalex.parse_response(_response([{'display_name': 'No DOI', 'ids': {}}]))
    assert parsed == openalex.ParsedWorks(works={}, drifted={})


def test_parse_rejects_a_non_works_payload() -> None:
    with pytest.raises(ValueError, match='results'):
        openalex.parse_response(json.dumps({'meta': {'count': 0}}).encode())


def test_a_record_the_mirror_cannot_hold_is_charged_to_its_doi_alone() -> None:
    parsed = openalex.parse_response(_response([_work(_DOI_A), _work(_DOI_B, publication_year='soon')]))

    assert set(parsed.works) == {_DOI_A}
    assert set(parsed.drifted) == {_DOI_B}
    assert 'publication_year' in parsed.drifted[_DOI_B]


def test_parse_work_names_the_index_in_the_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError) as excinfo:
        openalex.parse_work(_work(_DOI_A, publication_year='soon'))
    assert excinfo.value.index == 'openalex'


def test_a_key_the_mirror_lacks_is_kept_in_the_text_and_fails_nothing() -> None:
    source = _work(_DOI_A, is_zpac=True, primary_location={'source': {'display_name': 'J'}, 'doi': 'x'})
    parsed = openalex.parse_response(_response([source]))

    assert parsed.drifted == {}
    assert mirror.parse_text(parsed.works[_DOI_A].text) == source


def test_wrap_wraps_the_inverted_index() -> None:
    work = openalex.parse_work(_work(_DOI_A, abstract_inverted_index={'The': [0, 5], 'gene': [1]})).work
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
    assert parsed.works[_DOI_A].work.title == 'Title A'


@pytest.mark.parametrize(
    ('fixture', 'doi', 'pmid'),
    [('oa', _DOI, '29089047'), ('preprint', _PREPRINT_DOI, None)],
)
def test_fixture_work_loads_whole(fixture: str, doi: str, pmid: str | None) -> None:
    source = _fixture_work(fixture)
    loaded = openalex.parse_work(source)
    work = loaded.work

    assert mirror.parse_text(loaded.text) == source
    assert work.doi == f'https://doi.org/{doi}'
    assert openalex.bare_pmid(work) == pmid
    assert work.title
    assert len(work.authorships) == len(source['authorships'])  # pyright: ignore[reportArgumentType]
    assert len(work.abstract_inverted_index) == len(source['abstract_inverted_index'])  # pyright: ignore[reportArgumentType]
    assert work.open_access.oa_status


def test_to_metadata_envelopes_the_record_in_both_forms() -> None:
    source = _fixture_work('preprint')
    work = openalex.parse_work(source)
    envelope = paper_metadata.parse(openalex.to_metadata(work))

    assert envelope.openalex == work.work
    assert mirror.parse_text(envelope.openalex_json) == source
    assert not envelope.HasField('pubmed')
    assert paper_metadata.title(envelope) == work.work.title


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
    reason='set LITCACHE_OPENALEX_LIVE_SAMPLE to a sample size to load that many live works',
)
def test_live_openalex_sample_loads() -> None:
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
        assert mirror.parse_text(openalex.parse_work(source).text) == source
