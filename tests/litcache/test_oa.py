"""Tests for `themis.litcache.oa` — the fetch-driven OA branch.

Drives the litfetch fetcher ladder with in-memory doubles (no network) to prove the
branch's outcomes: an XML body opens the OA branch (XML bytes + provenance + access
terms returned, convertible by `convert.convert_jats`) — a journal article's JATS or a
Bookshelf chapter's BITS wrapper alike; a served non-XML body and an empty ladder both
close it (`None` → the caller renders the seed Docling json on the non-OA path). The
served body's `source` is a real fetcher name, so it maps to a `SourceKind`.
"""

from __future__ import annotations

import asyncio
import datetime
import pathlib
from collections.abc import Mapping

import litfetch
import pytest
from litfetch import artifacts, ids
from litfetch import source_metadata as sm

from themis.litcache import convert, identity, oa
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_OA_JATS = _FIXTURES / 'oa' / 'fulltext.xml'
_BOOKSHELF_XML = _FIXTURES / 'bookshelf' / 'chapter.xml'
_ARTICLE = ids.ArticleIds(pmcid='PMC5664429')
_CHAPTER = ids.ArticleIds(bookid='NBK900001')
_CREATED_AT = datetime.datetime(2026, 6, 25, 12, 0, tzinfo=datetime.UTC)
# GeneReviews states its terms as the URL of its usage notice, flagged open-access — not a CC licence.
_GENEREVIEWS_TERMS = 'https://www.ncbi.nlm.nih.gov/books/NBK138602/'


class _BodyFetcher:
    """A Fetcher serving one in-memory body Blob, bypassing the network.

    `name` and `requires` default to a keyless fixture rung; a test standing in for a real rung
    passes that rung's, so the ladder gates it as it would the real one.
    """

    name: str
    requires: frozenset[str]

    def __init__(
        self, blob: artifacts.Blob | None, *, name: str = 'fixture', requires: frozenset[str] = frozenset()
    ) -> None:
        self._blob = blob
        self.name = name
        self.requires = requires

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> artifacts.Blob | None:
        del article_ids, credentials, http  # litfetch's Fetcher signature; this double ignores them
        return self._blob


def _bookshelf_fetcher() -> _BodyFetcher:
    """The Bookshelf rung's double: the synthetic chapter, served as litfetch's rung serves one."""
    blob = _body_blob(artifacts.JATS_XML, _BOOKSHELF_XML.read_bytes(), source='europe_pmc_bookshelf')
    return _BodyFetcher(blob, name='europe_pmc_bookshelf', requires=frozenset({'bookid'}))


def _body_blob(media_type: str, content: bytes, *, source: str = 'europe_pmc') -> artifacts.Blob:
    file = artifacts.File(kind=artifacts.FileKind.BODY, source=source, media_type=media_type)
    return artifacts.Blob(file=file, content=content)


def _fetch(fetcher: _BodyFetcher) -> oa.OaSource | None:
    return asyncio.run(oa.fetch_oa_source(_ARTICLE, fetchers=[fetcher]))


def test_xml_body_opens_oa_branch() -> None:
    xml = _OA_JATS.read_bytes()
    result = _fetch(_BodyFetcher(_body_blob(artifacts.JATS_XML, xml)))
    assert result is not None
    assert result.content == xml
    # The served body's source maps to a SourceKind for the manifest.
    assert result.kind == litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC
    # litfetch read CC-BY off the bytes (basis=artifact) → free-to-read.
    assert result.access.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert result.access.access.WhichOneof('kind') == 'free_to_read'
    # The bytes are exactly what the xml→litdown conversion consumes.
    conversion = convert.convert_jats(result.content, from_source='xml', from_revision='0', created_at=_CREATED_AT)
    assert conversion.rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN


def test_bookshelf_rung_opens_oa_branch_for_a_chapter() -> None:
    result = asyncio.run(oa.fetch_oa_source(_CHAPTER, fetchers=[_bookshelf_fetcher()]))
    assert result is not None
    assert result.kind == litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC_BOOKSHELF
    # The chapter states its terms as a usage-terms URL: recorded verbatim, read off the bytes.
    assert result.access.licence == 'https://example.test/books/NBK900000/terms/'
    assert result.access.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert result.access.access.WhichOneof('kind') == 'free_to_read'
    # The BITS wrapper is what the xml→litdown conversion consumes.
    conversion = convert.convert_jats(result.content, from_source='xml', from_revision='0', created_at=_CREATED_AT)
    assert conversion.markdown.startswith('# A synthetic chapter')


def test_bookshelf_rung_does_not_fire_without_an_accession() -> None:
    # The rung requires a bookid; a bundle without one walks past it, so a PMCID-only paper is never
    # served a chapter's body.
    assert asyncio.run(oa.fetch_oa_source(_ARTICLE, fetchers=[_bookshelf_fetcher()])) is None


def test_pdf_body_closes_oa_branch() -> None:
    # A PDF body is not "XML obtainable"; the seed Docling json is higher-fidelity
    # than OCR, so the gate declines → caller takes the non-OA path.
    assert _fetch(_BodyFetcher(_body_blob(artifacts.PDF, b'%PDF-1.7 ...'))) is None


def test_no_body_closes_oa_branch() -> None:
    assert _fetch(_BodyFetcher(None)) is None


def test_empty_ladder_closes_oa_branch() -> None:
    assert asyncio.run(oa.fetch_oa_source(_ARTICLE, fetchers=[])) is None


def test_unknown_source_kind_fails_loud() -> None:
    # litfetch serving an XML body under a source name with no SOURCE_KIND_ mapping is a real anomaly:
    # fetch_oa_source retypes the SourceKind.Value ValueError as OaSourceError so the producer treats it
    # as permanent, not as a transient ValueError to retry forever.
    xml = b'<article><body>Full text.</body></article>'
    with pytest.raises(oa.OaSourceError, match='unknown source kind'):
        _fetch(_BodyFetcher(_body_blob(artifacts.JATS_XML, xml, source='not_a_real_source')))


class _SuppSource:
    """A FileSource serving in-memory files, bypassing the network."""

    name = 'fixture'

    def __init__(self, files: list[tuple[artifacts.File, bytes]]) -> None:
        self._files = files

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> tuple[artifacts.File, ...]:
        del article_ids, credentials, http  # litfetch's FileSource signature; this double ignores them
        return tuple(f for f, _ in self._files)

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> artifacts.Blob | None:
        del credentials, http  # litfetch's FileSource signature; this double ignores them
        for f, data in self._files:
            if f.uri == file.uri:
                return artifacts.Blob(file=f, content=data)
        return None


def _file(kind: artifacts.FileKind, filename: str, media_type: str) -> artifacts.File:
    return artifacts.File(
        kind=kind, source='fixture', media_type=media_type, uri=f'https://example.test/{filename}', filename=filename
    )


def test_fetch_supplementary_lists_and_downloads() -> None:
    body = _file(artifacts.FileKind.BODY, 'body.xml', artifacts.JATS_XML)
    fig = _file(artifacts.FileKind.SUPPLEMENTARY, 'fig1.png', 'image/png')
    data = _file(artifacts.FileKind.SUPPLEMENTARY, 'data.csv', 'text/csv')
    source = _SuppSource([(body, b'<xml/>'), (fig, b'PNG'), (data, b'a,b\n1,2\n')])

    out = asyncio.run(oa.fetch_supplementary(ids.ArticleIds(pmcid='PMC9'), sources=[source]))

    # Only the SUPPLEMENTARY files (not the body) come back, with bytes + provenance.
    assert [(s.filename, s.media_type, s.content) for s in out] == [
        ('fig1.png', 'image/png', b'PNG'),
        ('data.csv', 'text/csv', b'a,b\n1,2\n'),
    ]
    assert out[0].origin_url == 'https://example.test/fig1.png'


def test_fetch_supplementary_empty_when_no_sources() -> None:
    assert asyncio.run(oa.fetch_supplementary(ids.ArticleIds(pmcid='PMC9'), sources=[])) == []


def test_fetch_supplementary_derives_filename_and_media_type_from_uri() -> None:
    # litfetch listed this real PMC OA file with neither filename nor media type; both are
    # recoverable from the URI (basename + extension) so the writer can still store it.
    supp = artifacts.File(
        kind=artifacts.FileKind.SUPPLEMENTARY,
        source='fixture',
        media_type=None,
        uri='https://pmc-oa-opendata.s3.amazonaws.com/PMC10366952.1/jamaneurol-e232363-s002.xlsx',
        filename=None,
    )
    source = _SuppSource([(supp, b'PK\x03\x04')])

    out = asyncio.run(oa.fetch_supplementary(ids.ArticleIds(pmcid='PMC9'), sources=[source]))

    assert [(s.filename, s.media_type) for s in out] == [
        ('jamaneurol-e232363-s002.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    ]


def test_fetch_supplementary_falls_back_to_octet_stream_for_unmapped_extension() -> None:
    # A real extension mimetypes does not know (.R) is still storable — the writer keys the
    # path off the extension; the media type falls back to octet-stream rather than dropping
    # the whole paper over one odd supplementary attachment.
    supp = artifacts.File(
        kind=artifacts.FileKind.SUPPLEMENTARY,
        source='fixture',
        media_type=None,
        uri='https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/ANA-99-1277-s002.R',
        filename=None,
    )
    source = _SuppSource([(supp, b'library(x)')])

    out = asyncio.run(oa.fetch_supplementary(ids.ArticleIds(pmcid='PMC9'), sources=[source]))

    assert [(s.filename, s.media_type) for s in out] == [('ANA-99-1277-s002.R', 'application/octet-stream')]


def test_fetch_supplementary_raises_when_uri_has_no_extension() -> None:
    # No extension at all: the writer can't key a blob path, so it is genuinely unstorable.
    supp = artifacts.File(
        kind=artifacts.FileKind.SUPPLEMENTARY,
        source='fixture',
        media_type=None,
        uri='https://example.test/supplement',
        filename=None,
    )
    source = _SuppSource([(supp, b'data')])
    with pytest.raises(ValueError, match='no storable extension'):
        asyncio.run(oa.fetch_supplementary(ids.ArticleIds(pmcid='PMC9'), sources=[source]))


def test_article_ids_maps_fetchable_schemes() -> None:
    external_ids = (
        identity.ExternalId(scheme='doi', value='10.1/x'),
        identity.ExternalId(scheme='pmid', value='123'),
        identity.ExternalId(scheme='pmcid', value='PMC9'),
        identity.ExternalId(scheme='bookid', value='NBK900001'),
    )
    aids = oa.article_ids(external_ids)
    assert aids == ids.ArticleIds(doi='10.1/x', pmid='123', pmcid='PMC9', bookid='NBK900001')


def test_article_ids_is_none_without_a_fetchable_id() -> None:
    # A content-addressed paper (only binhash) has nothing litfetch can fetch.
    assert oa.article_ids((identity.ExternalId(scheme='binhash', value='abc'),)) is None


def test_article_ids_for_fetch_takes_the_resolved_ids_the_fetchers_key_on() -> None:
    # A PMID-keyed seed: the pmcid and the bookid are the resolver's, and the fetch bundle carries them.
    resolved = litcache_pb2.ExternalIds(pmid='30000010', bookid='NBK900001')
    aids = oa.article_ids_for_fetch((identity.ExternalId(scheme='pmid', value='30000010'),), resolved)
    assert aids == ids.ArticleIds(pmid='30000010', bookid='NBK900001')


def test_article_ids_for_fetch_is_none_when_nothing_is_fetchable() -> None:
    aids = oa.article_ids_for_fetch((identity.ExternalId(scheme='pii', value='S1'),), litcache_pb2.ExternalIds())
    assert aids is None


def _access_kind(terms: oa.AccessTerms) -> str | None:
    return terms.access.WhichOneof('kind')


def test_artifact_basis_maps_verbatim_and_free_to_read() -> None:
    meta = artifacts.SourceMetadata(licence='CC-BY-4.0', access='open-access', basis='artifact')
    terms = oa.from_source_metadata(meta)
    assert terms.licence == 'CC-BY-4.0'
    assert terms.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert _access_kind(terms) == 'free_to_read'


def test_a_usage_terms_url_read_off_a_chapter_is_recorded_verbatim_and_free_to_read() -> None:
    # The GeneReviews shape: litfetch read a usage-terms URL (not a CC licence) off the chapter's bytes,
    # flagged open-access. The raw string is what the source records, with the basis it was read under.
    meta = artifacts.SourceMetadata(licence=_GENEREVIEWS_TERMS, access='open-access', basis='artifact')
    terms = oa.from_source_metadata(meta)
    assert terms.licence == _GENEREVIEWS_TERMS
    assert terms.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert _access_kind(terms) == 'free_to_read'


def test_artifact_basis_is_free_to_read_even_without_open_access_token() -> None:
    # litfetch leaves `access` None when the JATS license-type carried no 'open'
    # flag; an artifact basis still means OA-ladder bytes → free-to-read.
    meta = artifacts.SourceMetadata(
        licence='http://creativecommons.org/licenses/by/4.0/', access=None, basis='artifact'
    )
    assert _access_kind(oa.from_source_metadata(meta)) == 'free_to_read'


def test_unpaywall_basis_maps_to_asserted() -> None:
    meta = artifacts.SourceMetadata(licence='cc-by', access='gold', basis='unpaywall')
    terms = oa.from_source_metadata(meta)
    assert terms.licence == 'cc-by'
    assert terms.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED
    assert _access_kind(terms) == 'free_to_read'


@pytest.mark.parametrize('token', ['open-access', 'gold', 'green', 'hybrid', 'bronze'])
def test_open_access_tokens_are_free_to_read(token: str) -> None:
    meta = artifacts.SourceMetadata(licence=None, access=token, basis='unpaywall')
    assert _access_kind(oa.from_source_metadata(meta)) == 'free_to_read'


def test_closed_status_is_unknown_access() -> None:
    # Unpaywall `closed` is not openly readable; with no publisher litfetch can
    # supply, the licensed variant is unreachable here, so access is unknown.
    meta = artifacts.SourceMetadata(licence=None, access='closed', basis='unpaywall')
    terms = oa.from_source_metadata(meta)
    assert _access_kind(terms) == 'unknown'
    assert terms.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED


def test_missing_licence_becomes_empty_string() -> None:
    # A bronze paper Unpaywall has no licence string for: openly readable, but no
    # machine-readable redistribution licence — empty, not invented.
    meta = artifacts.SourceMetadata(licence=None, access='bronze', basis='unpaywall')
    terms = oa.from_source_metadata(meta)
    assert terms.licence == ''
    assert _access_kind(terms) == 'free_to_read'


def test_unrecognised_access_token_is_unknown() -> None:
    meta = artifacts.SourceMetadata(licence='cc-by', access='something-new', basis='unpaywall')
    assert _access_kind(oa.from_source_metadata(meta)) == 'unknown'


def test_empty_source_metadata_fails_loud() -> None:
    with pytest.raises(oa.OaSourceError, match='no access terms'):
        oa.from_source_metadata(artifacts.SourceMetadata())


def test_real_oa_fixture_is_free_to_read_artifact() -> None:
    file = artifacts.File(kind=artifacts.FileKind.BODY, source='fixture', media_type=artifacts.JATS_XML)
    meta = sm.extract_source_metadata(artifacts.Blob(file=file, content=_OA_JATS.read_bytes()))
    terms = oa.from_source_metadata(meta)
    assert terms.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert _access_kind(terms) == 'free_to_read'
    assert 'creativecommons.org/licenses/by/4.0' in terms.licence
