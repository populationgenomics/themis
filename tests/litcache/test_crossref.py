"""Crossref: the work record kept as text, its DOI and publisher read, for DOI-only papers.

The readers run against committed Crossref responses — the OA paper's and a bioRxiv preprint's
(Crossref metadata is CC0, so both are redistributable); neither carries a PMID, so they exercise
the DOI-only path. The fetch path is driven by an httpx2 `MockTransport`. Live Crossref is
integration-gated: `LITCACHE_CROSSREF_LIVE_DOI` resolves one DOI, and `LITCACHE_CROSSREF_LIVE_SAMPLE`
reads that many random works without drift, into text that parses back to the work served —
records the fixtures never saw, where a field Themis reads changing shape shows first.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import pathlib
import re

import httpx2
import pytest

from themis.common import constants
from themis.litcache import crossref, sourced_metadata, upstream_record
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_CROSSREF_JSON = _FIXTURES / 'oa' / 'crossref.json'
_DOI = '10.1186/s13073-017-0482-5'
_PREPRINT_DOI = '10.1101/2024.09.14.613029'
# Markup left in a plain-text field: an element with its start and end tag. A lone tag-shaped run can be text
# (`<x> carriers`, `</CD4>`), and markup the fill does not know still closes what it opens.
_TAG = re.compile(r'<([A-Za-z][\w.:-]*)(?:\s[^<>]*)?>.*?</\1\s*>', re.DOTALL | re.IGNORECASE)
# An ORCID iD as a record states it, captured in its bare form; and the NLM names the type table maps Crossref's onto.
_STATED_ORCID = re.compile(r'\s*(?:https?://orcid\.org/)?([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9Xx])\s*')
_MAPPED_TYPES = frozenset({'Journal Article', 'Preprint'})


def _message(path: pathlib.Path) -> dict[str, object]:
    return json.loads(path.read_bytes())['message']


@pytest.mark.parametrize(('fixture', 'doi'), [('oa', _DOI), ('preprint', _PREPRINT_DOI)])
def test_parse_work_reads_the_doi_and_keeps_the_record_whole(fixture: str, doi: str) -> None:
    source = _message(_FIXTURES / fixture / 'crossref.json')
    work = crossref.parse_work(source)
    assert upstream_record.parse_text(work.text) == source
    assert work.fields.doi == doi


def test_an_empty_doi_is_schema_drift() -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match='DOI is empty'):
        crossref.from_crossref_work({'DOI': '', 'title': ['t']})


def test_a_key_no_reader_reads_is_kept_in_the_text_and_fails_nothing() -> None:
    source = {'DOI': _DOI, 'title': ['T'], 'colour': 'blue', 'author': [{'family': 'F', 'colour': 'red'}]}
    work = crossref.parse_work(source)
    assert upstream_record.parse_text(work.text) == source


def test_a_field_read_in_a_shape_its_reader_cannot_read_is_schema_drift() -> None:
    with pytest.raises(upstream_record.SchemaDriftError) as excinfo:
        crossref.parse_work({'DOI': _DOI, 'publisher': ['not', 'a', 'string']})
    assert excinfo.value.index == 'crossref'
    assert 'publisher' in excinfo.value.detail


def test_from_crossref_work_holds_the_record_and_harvests_the_doi() -> None:
    source = _message(_CROSSREF_JSON)
    result = crossref.from_crossref_work(source)
    sourced = result.sourced
    sourced_metadata.check(sourced)

    assert upstream_record.parse_text(sourced.source.crossref_json) == source
    assert not sourced.source.HasField('pubmed')
    assert 'Whole exome sequencing' in sourced.metadata.title
    assert result.external_ids == litcache_pb2.ExternalIds(doi=_DOI)
    assert result.publisher == 'Springer Science and Business Media LLC'


def test_missing_doi_is_schema_drift() -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match='DOI is missing'):
        crossref.from_crossref_work({'title': ['t']})


def test_a_record_stating_no_title_is_stored_whole() -> None:
    # The record is the index's statement; a title it lacks is an empty title field, not a fault.
    result = crossref.from_crossref_work({'DOI': _DOI, 'publisher': 'P'})
    assert result.sourced.metadata.title == ''
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
    sourced = result.sourced
    assert sourced.source.HasField('crossref_json')
    assert result.external_ids.doi == doi
    assert not _TAG.search(sourced.metadata.title), sourced.metadata.title
    assert not _TAG.search(sourced.metadata.abstract), sourced.metadata.abstract


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_CROSSREF_LIVE_SAMPLE'),
    reason='set LITCACHE_CROSSREF_LIVE_SAMPLE to a sample size to load that many live works',
)
def test_live_crossref_sample_loads() -> None:
    size = int(os.environ['LITCACHE_CROSSREF_LIVE_SAMPLE'])

    async def run() -> list[dict[str, object]]:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                'https://api.crossref.org/works', params={'sample': str(size), 'mailto': constants.CONTACT_EMAIL}
            )
            response.raise_for_status()
            return response.json()['message']['items']

    orcids = 0
    for source in asyncio.run(run()):
        assert upstream_record.parse_text(crossref.parse_work(source).text) == source
        metadata = crossref.from_crossref_work(source).sourced.metadata
        titles = source.get('title')
        if isinstance(titles, list) and any(titles):
            assert metadata.title, source.get('DOI')
        # Every field is plain text: no markup survives, however the index escaped it.
        assert not _TAG.search(metadata.title), (source.get('DOI'), metadata.title)
        assert not _TAG.search(metadata.abstract), (source.get('DOI'), metadata.abstract)
        jats = source.get('abstract')
        if isinstance(jats, str):
            # Nothing but markup is lost: the plain text is not much shorter than the tags crudely removed. The fill
            # drops a heading that only says "Abstract", most of a short one (10.1002/chin.201515351).
            headless = re.sub(r'<([\w:]*title)>\s*abstract\s*:?\s*</\1>', ' ', jats, flags=re.IGNORECASE)
            crude = ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', headless)).split())
            assert len(metadata.abstract) >= 0.8 * len(crude), source.get('DOI')
        _assert_identifiers_hold(source, metadata)
        orcids += sum(1 for author in metadata.authors if author.orcid)
    assert orcids, 'no sampled work carries an ORCID iD the fill read'  # rules out a vacuous pass


def _assert_identifiers_hold(source: dict[str, object], metadata: litcache_pb2.Metadata) -> None:
    """Assert the fill's ORCID iDs and publication type are the ones a live work states; it states no MeSH.

    Each named author's iD is the bare form of their `ORCID`, or "" where none is or it fails its check. A type is one
    the table maps to.
    """
    authors = source.get('author') or []
    assert isinstance(authors, list)
    named = [a for a in authors if any((a.get(key) or '').strip() for key in ('family', 'name', 'given'))]
    assert [author.orcid for author in metadata.authors] == [_bare_orcid(a.get('ORCID')) for a in named], source['DOI']
    assert not metadata.mesh_headings
    assert len(metadata.publication_types) <= 1
    assert set(metadata.publication_types) <= _MAPPED_TYPES, metadata.publication_types


def _bare_orcid(stated: object) -> str:
    """The bare form of a stated ORCID iD whose ISO 7064 MOD 11-2 check digit holds, else ""."""
    match = _STATED_ORCID.fullmatch(stated) if isinstance(stated, str) else None
    if match is None:
        return ''
    orcid = match[1].upper()
    total = 0
    for digit in orcid[:-1].replace('-', ''):
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return orcid if orcid[-1] == '0123456789X'[check] else ''
