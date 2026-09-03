"""Crossref: the work record loaded whole into its mirror, for DOI-only papers.

The loader runs against committed Crossref responses — the OA paper's and a bioRxiv preprint's
(Crossref metadata is CC0, so both are redistributable); neither carries a PMID, so they exercise
the DOI-only path. The fetch path is driven by an httpx2 `MockTransport`. Live Crossref is
integration-gated: `LITCACHE_CROSSREF_LIVE_DOI` resolves one DOI, and `LITCACHE_CROSSREF_LIVE_SAMPLE`
runs the round-trip gate over that many random works — records the fixtures never saw, which is
where the mirror's lag behind the index shows first.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from collections.abc import Mapping

import httpx2
import pytest

from themis.common import constants
from themis.litcache import crossref, mirror, paper_metadata
from themis.litcache.models import crossref_pb2, litcache_pb2
from themis.testing import mirror_roundtrip

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_CROSSREF_JSON = _FIXTURES / 'oa' / 'crossref.json'
_DOI = '10.1186/s13073-017-0482-5'
_PREPRINT_DOI = '10.1101/2024.09.14.613029'


def _message(path: pathlib.Path) -> dict[str, object]:
    return json.loads(path.read_bytes())['message']


def _unwrap(node: object) -> object:
    """The inverse of `crossref.wrap`, over the proto3-JSON the mirror serialises to."""
    if isinstance(node, Mapping):
        unwrapped: dict[str, object] = {}
        for key, value in node.items():
            if key == 'date-parts' and isinstance(value, list):
                unwrapped[key] = [inner.get('parts', []) for inner in value]
            elif key == 'relation' and isinstance(value, Mapping):
                unwrapped[key] = {kind: items.get('items', []) for kind, items in value.items()}
            elif key == 'timestamp' and isinstance(value, str):
                unwrapped[key] = int(value)  # int64 serialises as a JSON string
            else:
                unwrapped[key] = _unwrap(value)
        return unwrapped
    if isinstance(node, list):
        return [_unwrap(item) for item in node]
    return node


def _unwrap_document(document: dict[str, object]) -> dict[str, object]:
    unwrapped = _unwrap(document)
    assert isinstance(unwrapped, dict)
    return unwrapped


@pytest.mark.parametrize(
    ('fixture', 'doi', 'kind'),
    [('oa', _DOI, 'journal-article'), ('preprint', _PREPRINT_DOI, 'posted-content')],
)
def test_parse_work_loads_the_record_whole(fixture: str, doi: str, kind: str) -> None:
    source = _message(_FIXTURES / fixture / 'crossref.json')
    work = crossref.parse_work(source)

    assert work.doi == doi
    assert work.type == kind
    assert work.title[0]
    assert work.author[0].family
    assert len(work.reference) == len(source['reference'])  # pyright: ignore[reportArgumentType]
    assert work.is_referenced_by_count == source['is-referenced-by-count']
    assert work.issued.date_parts[0].parts[0] >= 2017


@pytest.mark.parametrize('fixture', ['oa', 'preprint'])
def test_round_trip_is_lossless(fixture: str) -> None:
    source = _message(_FIXTURES / fixture / 'crossref.json')
    mirror_roundtrip.assert_lossless(source, crossref.parse_work(source), _unwrap_document)


def test_wrap_drops_the_unknown_date_null_and_wraps_relations() -> None:
    # Crossref emits `date-parts: [[null]]` for a record with no known date, and `relation` as a
    # map from relation type to an array — neither a shape a proto field holds as published.
    work = crossref.parse_work(
        {
            'DOI': _DOI,
            'issued': {'date-parts': [[None]]},
            'published': {'date-parts': [[2019, 5]]},
            'relation': {'is-preprint-of': [{'id-type': 'doi', 'id': '10.1/final', 'asserted-by': 'subject'}]},
        }
    )
    assert list(work.issued.date_parts[0].parts) == []
    assert list(work.published.date_parts[0].parts) == [2019, 5]
    assert work.relation['is-preprint-of'].items[0].id == '10.1/final'


def test_a_null_array_element_is_dropped() -> None:
    # Crossref emits `role: [null]` on some dissertation contributors; proto3-JSON holds no null
    # element, and the record carries nothing in it.
    work = crossref.parse_work({'DOI': _DOI, 'contributor': [{'sequence': 'additional', 'role': [None]}]})
    assert work.contributor[0].sequence == 'additional'
    assert len(work.contributor[0].role) == 0


def test_a_null_between_stated_date_parts_is_schema_drift() -> None:
    # `[2019, null, 5]` is not a date; dropping the null would read the day as the month.
    with pytest.raises(mirror.SchemaDriftError, match='date-parts'):
        crossref.parse_work({'DOI': _DOI, 'issued': {'date-parts': [[2019, None, 5]]}})


def test_an_empty_doi_fails_loud() -> None:
    with pytest.raises(ValueError, match='no DOI'):
        crossref.from_crossref_work({'DOI': '', 'title': ['t']})


def test_a_key_the_mirror_lacks_is_schema_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError) as excinfo:
        crossref.parse_work({'DOI': _DOI, 'title': ['T'], 'colour': 'blue'})
    assert excinfo.value.index == 'crossref'
    assert 'colour' in excinfo.value.detail
    assert '\n' not in excinfo.value.detail  # the key, not the list of every field the mirror declares


def test_a_value_of_a_shape_the_field_cannot_hold_is_schema_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError, match='title'):
        crossref.parse_work({'DOI': _DOI, 'title': 'not an array'})


def test_from_crossref_work_envelopes_the_record_and_harvests_the_doi() -> None:
    result = crossref.from_crossref_work(_message(_CROSSREF_JSON))
    envelope = paper_metadata.parse(result.metadata)

    assert envelope.HasField('crossref')
    assert not envelope.HasField('pubmed')
    assert envelope.crossref.doi == _DOI
    assert 'Whole exome sequencing' in paper_metadata.title(envelope)
    assert result.external_ids == litcache_pb2.ExternalIds(doi=_DOI)
    assert result.publisher == 'Springer Science and Business Media LLC'


def test_missing_doi_fails_loud() -> None:
    with pytest.raises(ValueError, match='no DOI'):
        crossref.from_crossref_work({'title': ['t']})


def test_a_record_stating_no_title_is_stored_whole() -> None:
    # The record is the index's statement; a title it lacks is a title the summary lacks, not a
    # fault in the record.
    result = crossref.from_crossref_work({'DOI': _DOI, 'publisher': 'P'})
    assert paper_metadata.title(paper_metadata.parse(result.metadata)) == ''
    assert result.publisher == 'P'


def test_resolve_drives_crossref() -> None:
    body = _CROSSREF_JSON.read_bytes()
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen['path'] = request.url.path
        seen['mailto'] = request.url.params.get('mailto')
        return httpx2.Response(200, content=body)

    async def run() -> crossref.CrossrefResult:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await crossref.resolve(_DOI, http_client=client)

    result = asyncio.run(run())

    assert seen['path'] == f'/works/{_DOI}'
    assert seen['mailto']
    assert result.external_ids.doi == _DOI


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_CROSSREF_LIVE_DOI'),
    reason='set LITCACHE_CROSSREF_LIVE_DOI to hit live Crossref',
)
def test_live_crossref() -> None:
    doi = os.environ['LITCACHE_CROSSREF_LIVE_DOI']

    async def run() -> crossref.CrossrefResult:
        async with httpx2.AsyncClient(timeout=30.0) as client:
            return await crossref.resolve(doi, http_client=client)

    result = asyncio.run(run())
    assert paper_metadata.parse(result.metadata).HasField('crossref')
    assert result.external_ids.doi == doi


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_CROSSREF_LIVE_SAMPLE'),
    reason='set LITCACHE_CROSSREF_LIVE_SAMPLE to a sample size to run the round-trip gate over live works',
)
def test_live_crossref_sample_round_trips() -> None:
    size = int(os.environ['LITCACHE_CROSSREF_LIVE_SAMPLE'])

    async def run() -> list[dict[str, object]]:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                'https://api.crossref.org/works', params={'sample': str(size), 'mailto': constants.CONTACT_EMAIL}
            )
            response.raise_for_status()
            return response.json()['message']['items']

    for source in asyncio.run(run()):
        work: crossref_pb2.Work = crossref.parse_work(source)
        mirror_roundtrip.assert_lossless(source, work, _unwrap_document)
