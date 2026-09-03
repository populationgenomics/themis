"""Tests for the per-paper ingestion core (`themis.litcache.pipeline`).

Exercises the write half (`ingest_paper`) end-to-end on the synthetic fixtures against
a fake-gcs-server bucket and a throwaway Postgres (the mint is real — the `doc_id` it
claims is what resumability hinges on). Bibliographic resolution is a separate concern
(`test_resolve`): here a `ResolvedPaper` is built via the resolver over a mock transport
and handed to `ingest_paper`, mirroring how the batched stage feeds it. Docker-gated via
the shared `gcs_bucket` and `conn` fixtures: an absent daemon skips rather than hangs.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import json
import pathlib
import posixpath
from collections.abc import Mapping

import httpx2
import litfetch
import pg8000.dbapi
import pytest
from google.cloud import storage as gcs
from litfetch import artifacts, ids

from themis.litcache import crosswalk, pipeline, rebuild, resolve, writer
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).parent.parent / 'fixtures' / 'litcache'
_NONOA = _FIXTURES / 'nonoa'
_OA = _FIXTURES / 'oa'
_BOOKSHELF = _FIXTURES / 'bookshelf'
_OA_EFETCH_XML = (_OA / 'efetch.xml').read_bytes()
_BOOK_EFETCH_XML = (_BOOKSHELF / 'efetch.xml').read_bytes()
_BOOK_PMID = '30000010'
_BOOKID = 'NBK900001'
_NOW = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)
# A DOI bucket key gives the non-OA paper a clean external identity (the fixture
# origin filename is opaque, so without this the paper falls through to a content
# hash — a separate path, not what these tests exercise).
_BUCKET_KEY = '10.9999%2Fsynthetic.nonoa.json'
# An empty fetcher ladder closes the OA branch, so a non-OA test stays offline (the
# DOI key is otherwise fetchable and would hit the live litfetch ladder).
_NO_OA: list[litfetch.Fetcher] = []
# The write half never consumes the resolved metadata on the cross-paper-link path (it
# adopts the canonical, whose manifest exists, and returns before conversion).
_UNUSED_RESOLVED = resolve.ResolvedPaper(metadata=b'', external_ids=litcache_pb2.ExternalIds(), publisher=None)


class _BodyFetcher:
    """A litfetch Fetcher serving one in-memory body Blob, bypassing the network.

    `name` and `requires` default to a keyless fixture rung; a test standing in for a real rung
    passes that rung's, so the ladder gates it as it would the real one. `received` collects the
    id bundles the ladder handed it.
    """

    name: str
    requires: frozenset[str]

    def __init__(
        self,
        blob: artifacts.Blob,
        *,
        name: str = 'fixture',
        requires: frozenset[str] = frozenset(),
        received: list[ids.ArticleIds] | None = None,
    ) -> None:
        self._blob = blob
        self.name = name
        self.requires = requires
        self._received = received

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,  # noqa: ARG002 -- Fetcher protocol arg (keyword-matched)
        http: litfetch.Http,  # noqa: ARG002 -- Fetcher protocol arg (keyword-matched)
    ) -> artifacts.Blob:
        if self._received is not None:
            self._received.append(article_ids)
        return self._blob


def _jats_fetcher() -> _BodyFetcher:
    """A fetcher serving the OA fixture's JATS as a europe_pmc body."""
    file = artifacts.File(kind=artifacts.FileKind.BODY, source='europe_pmc', media_type=artifacts.JATS_XML)
    return _BodyFetcher(artifacts.Blob(file=file, content=(_OA / 'fulltext.xml').read_bytes()))


def _bookshelf_fetcher(received: list[ids.ArticleIds]) -> _BodyFetcher:
    """The Bookshelf rung's double: the synthetic chapter's BITS wrapper, served as the rung serves one."""
    file = artifacts.File(
        kind=artifacts.FileKind.BODY,
        source='europe_pmc_bookshelf',
        media_type=artifacts.JATS_XML,
        uri=f'https://www.ebi.ac.uk/europepmc/webservices/rest/{_BOOKID}/bookXML',
    )
    blob = artifacts.Blob(file=file, content=(_BOOKSHELF / 'chapter.xml').read_bytes())
    return _BodyFetcher(blob, name='europe_pmc_bookshelf', requires=frozenset({'bookid'}), received=received)


def _transport(body: bytes) -> httpx2.MockTransport:
    """A transport serving one body for any request (metadata resolution: Crossref or efetch)."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body)

    return httpx2.MockTransport(handler)


# The non-OA fixture's DOI has no real Crossref record; a synthetic works message
# stands in so metadata resolution (Crossref rung) produces a record the mirror holds.
_NONOA_CROSSREF = json.dumps(
    {
        'message': {
            'DOI': '10.9999/synthetic.nonoa',
            'title': ['Synthetic Non-OA Fixture'],
            'issued': {'date-parts': [[2020, 1, 1]]},
            'publisher': 'Synthetic Press',
        }
    }
).encode('utf-8')


def _seed() -> pipeline.SeedObject:
    return pipeline.SeedObject(
        bucket_key=_BUCKET_KEY,
        docling_json=(_NONOA / 'docling.json').read_bytes(),
        pdf=(_NONOA / 'source.pdf').read_bytes(),
    )


def _licence() -> pipeline.LicenceFacts:
    # Non-OA fallback: litfetch access authorities not wired, so the licence is unknown.
    return pipeline.LicenceFacts(
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    )


def _resolve_paper(*, pmid: str | None, doi: str | None, transport: httpx2.MockTransport) -> resolve.ResolvedPaper:
    """Resolve a `ResolvedPaper` over a mock transport (the input the write half takes)."""

    async def run() -> resolve.ResolvedPaper:
        async with httpx2.AsyncClient(transport=transport) as client:
            return await resolve.resolve_metadata(pmid=pmid, doi=doi, http_client=client)

    return asyncio.run(run())


def _ingest_via_transport(
    bucket: gcs.Bucket,
    conn: pg8000.dbapi.Connection,
    seed: pipeline.SeedObject,
    licence: pipeline.LicenceFacts,
    *,
    fetchers: list[litfetch.Fetcher],
    transport: httpx2.MockTransport,
    file_sources: list[litfetch.FileSource] | None = None,
) -> pipeline.IngestResult:
    """Extract identity, resolve metadata over `transport`, and run the write half."""
    ident = pipeline.extract_identity(seed)
    by_scheme = {eid.scheme: eid.value for eid in ident.external_ids}
    resolved = _resolve_paper(pmid=by_scheme.get('pmid'), doi=by_scheme.get('doi'), transport=transport)
    return pipeline.ingest_paper(
        bucket,
        functools.partial(crosswalk.mint, conn),
        seed,
        ident,
        resolved,
        licence,
        now=_NOW,
        fetchers=fetchers,
        file_sources=file_sources,
    )


def _ingest_link(bucket: gcs.Bucket, conn: pg8000.dbapi.Connection, seed: pipeline.SeedObject) -> pipeline.IngestResult:
    """Ingest a seed whose identity links existing works — resolution is never reached."""
    ident = pipeline.extract_identity(seed)
    return pipeline.ingest_paper(
        bucket,
        functools.partial(crosswalk.mint, conn),
        seed,
        ident,
        _UNUSED_RESOLVED,
        _licence(),
        now=_NOW,
        fetchers=_NO_OA,
    )


def _load_manifest(bucket: gcs.Bucket, doc_id: str) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())


def _doc_id_for(conn: pg8000.dbapi.Connection, external_id: str) -> str | None:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT doc_id FROM litcache.crosswalk WHERE external_id = %s', (external_id,))
        row = cur.fetchone()
    return row[0] if row is not None else None


def _keys_under(conn: pg8000.dbapi.Connection, scheme: str) -> list[str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id FROM litcache.crosswalk WHERE external_id LIKE %s', (f'{scheme}:%',))
        return [row[0] for row in cur.fetchall()]


def _journal_seed() -> pipeline.SeedObject:
    """A seed keyed by the OA paper's PMID; efetch (the recorded set) answers its journal record."""
    return pipeline.SeedObject(
        bucket_key='29089047.json',
        docling_json=(_NONOA / 'docling.json').read_bytes(),
        pdf=(_NONOA / 'source.pdf').read_bytes(),
    )


def test_ingests_a_nonoa_paper_end_to_end(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    result = _ingest_via_transport(
        gcs_bucket, conn, _seed(), _licence(), fetchers=_NO_OA, transport=_transport(_NONOA_CROSSREF)
    )

    assert result.written is True
    assert result.minted is True

    paper_dir = posixpath.join('papers', result.doc_id)
    manifest = _load_manifest(gcs_bucket, result.doc_id)
    assert manifest.doc_id == result.doc_id
    assert manifest.claim_key == 'doi:10.9999/synthetic.nonoa'
    assert manifest.external_ids.doi == '10.9999/synthetic.nonoa'

    # Non-OA branch: one rendering from the pdf source, via docling.
    assert [s.handle for s in manifest.sources] == ['pdf']
    rendering = next(iter(manifest.renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_DOCLING
    assert rendering.from_source == 'pdf'
    pdf_revision = manifest.sources[0].revisions[0]
    # The pdf is the source of truth, so its revision carries the char-addressability
    # probe; the fixture pdf has a text layer.
    assert pdf_revision.has_text_layer is True

    pdf_path = f'sources/pdf/{pdf_revision.hash}.pdf'
    assert (
        gcs_bucket.blob(posixpath.join(paper_dir, pdf_path)).download_as_bytes() == (_NONOA / 'source.pdf').read_bytes()
    )
    # metadata.pb is the resolved Crossref record, whole, in the envelope's crossref field — not a
    # caller input.
    metadata_bytes = gcs_bucket.blob(posixpath.join(paper_dir, 'metadata.pb')).download_as_bytes()
    paper = litcache_pb2.PaperMetadata.FromString(metadata_bytes)
    assert paper.HasField('crossref')
    assert not paper.HasField('pubmed')
    assert paper.crossref.title[0] == 'Synthetic Non-OA Fixture'
    assert _doc_id_for(conn, 'doi:10.9999/synthetic.nonoa') == result.doc_id


def test_re_run_is_idempotent_and_skips(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    transport = _transport(_NONOA_CROSSREF)
    first = _ingest_via_transport(gcs_bucket, conn, _seed(), _licence(), fetchers=_NO_OA, transport=transport)
    committed = gcs_bucket.blob(writer.manifest_path(first.doc_id)).download_as_bytes()

    second = _ingest_via_transport(gcs_bucket, conn, _seed(), _licence(), fetchers=_NO_OA, transport=transport)

    assert second.written is False
    assert second.minted is False  # the incumbent doc_id is adopted
    assert second.doc_id == first.doc_id
    assert gcs_bucket.blob(writer.manifest_path(first.doc_id)).download_as_bytes() == committed


def test_resumes_after_a_crash_reusing_the_claimed_doc_id(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_blob = gcs_bucket.blob
    failing = {'on': True}

    def guarded_blob(name: str) -> gcs.Blob:
        blob = real_blob(name)
        if name.endswith('manifest.pb') and failing['on']:

            def boom(*_args: object, **_kwargs: object) -> None:
                raise OSError('simulated crash before commit')

            monkeypatch.setattr(blob, 'upload_from_string', boom)
        return blob

    monkeypatch.setattr(gcs_bucket, 'blob', guarded_blob)

    transport = _transport(_NONOA_CROSSREF)
    with pytest.raises(OSError, match='simulated crash'):
        _ingest_via_transport(gcs_bucket, conn, _seed(), _licence(), fetchers=_NO_OA, transport=transport)

    # The mint claimed a doc_id; the manifest never committed.
    claimed = _doc_id_for(conn, 'doi:10.9999/synthetic.nonoa')
    assert claimed is not None
    assert not real_blob(writer.manifest_path(claimed)).exists()

    # The re-run reuses the claimed doc_id and completes the commit.
    failing['on'] = False
    result = _ingest_via_transport(gcs_bucket, conn, _seed(), _licence(), fetchers=_NO_OA, transport=transport)

    assert result.doc_id == claimed
    assert result.written is True
    assert real_blob(writer.manifest_path(claimed)).exists()


def test_ingests_an_oa_paper_via_the_xml_branch(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # The OA fixture: a fetcher serves its real JATS, opening the xml-faithful
    # branch. The injected facts' licence is the non-OA fallback and must be
    # overridden by the licence litfetch reads off the fetched bytes.
    seed = pipeline.SeedObject(
        bucket_key='10.1186%2Fs13073-017-0482-5.json',
        docling_json=(_OA / 'docling.json').read_bytes(),
        pdf=(_OA / 'source.pdf').read_bytes(),
    )
    licence = pipeline.LicenceFacts(
        licence='SHOULD-BE-OVERRIDDEN',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    )
    # Metadata resolves via Crossref (the OA paper's identity carries a DOI, no PMID).
    result = _ingest_via_transport(
        gcs_bucket,
        conn,
        seed,
        licence,
        fetchers=[_jats_fetcher()],
        transport=_transport((_OA / 'crossref.json').read_bytes()),
        file_sources=[],
    )

    assert result.written is True
    # No file sources supplied, so no supplementary artifacts were fetched.
    assert len(result.manifest.files) == 0
    # Both source lineages retained: the seed pdf and the fetched xml.
    sources = {s.handle: s for s in result.manifest.sources}
    assert set(sources) == {'pdf', 'xml'}
    xml_revision = sources['xml'].revisions[0]
    assert xml_revision.kind == litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC
    assert sources['xml'].media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_XML
    # The pdf is retained but unprobed — the XML is the source of truth.
    assert not sources['pdf'].revisions[0].HasField('has_text_layer')

    # One rendering from the xml source, via litdown.
    rendering = next(iter(result.manifest.renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert rendering.from_source == 'xml'

    # Licence/access are per-source. The xml lineage carries the fetched artifact's
    # terms (CC-BY → free-to-read), overriding the non-OA fallback; the retained seed
    # pdf asserts the same licence (it carries none of its own).
    assert sources['xml'].licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert sources['xml'].access.WhichOneof('kind') == 'free_to_read'
    assert 'creativecommons.org/licenses/by/4.0' in sources['xml'].licence
    assert sources['pdf'].licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED

    xml_path = f'sources/xml/{xml_revision.hash}.xml'
    assert (
        gcs_bucket.blob(posixpath.join('papers', result.doc_id, xml_path)).download_as_bytes()
        == (_OA / 'fulltext.xml').read_bytes()
    )


def test_ingests_a_bookshelf_chapter_via_the_xml_branch(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # A seed keyed by a chapter's PMID: PubMed answers a book record, and the accession it carries is
    # what the Bookshelf rung fetches the chapter's XML under.
    seed = pipeline.SeedObject(
        bucket_key=f'{_BOOK_PMID}.json',
        docling_json=(_NONOA / 'docling.json').read_bytes(),
        pdf=(_NONOA / 'source.pdf').read_bytes(),
    )
    received: list[ids.ArticleIds] = []
    result = _ingest_via_transport(
        gcs_bucket,
        conn,
        seed,
        _licence(),
        fetchers=[_bookshelf_fetcher(received)],
        transport=_transport(_BOOK_EFETCH_XML),
        file_sources=[],
    )

    assert result.written is True
    manifest = result.manifest
    assert manifest.claim_key == f'pmid:{_BOOK_PMID}'
    # The accession is minted beside the PMID and recorded, so the manifest and the crosswalk agree.
    assert manifest.external_ids == litcache_pb2.ExternalIds(pmid=_BOOK_PMID, bookid=_BOOKID)
    assert _doc_id_for(conn, f'pmid:{_BOOK_PMID}') == result.doc_id
    assert _doc_id_for(conn, f'bookid:{_BOOKID}') == result.doc_id

    # metadata.pb is PubMed's book record, in the book arm of the envelope's pubmed field.
    metadata_bytes = gcs_bucket.blob(posixpath.join('papers', result.doc_id, 'metadata.pb')).download_as_bytes()
    paper = litcache_pb2.PaperMetadata.FromString(metadata_bytes)
    assert paper.pubmed.WhichOneof('kind') == 'book_article'
    assert paper.pubmed.book_article.book_document.article_title.value == 'A synthetic chapter'

    # The fetch bundle carried the resolved accession, which is what let the Bookshelf rung fire.
    assert received == [ids.ArticleIds(pmid=_BOOK_PMID, bookid=_BOOKID)]

    # The chapter's XML is a lineage of its own kind, under the terms its bytes state.
    sources = {s.handle: s for s in manifest.sources}
    assert set(sources) == {'pdf', 'xml'}
    assert sources['xml'].revisions[0].kind == litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC_BOOKSHELF
    assert sources['xml'].licence == 'https://example.test/books/NBK900000/terms/'
    assert sources['xml'].licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    assert sources['xml'].access.WhichOneof('kind') == 'free_to_read'

    # The BITS wrapper rendered through litdown.
    rendering_hash, rendering = next(iter(manifest.renderings.items()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert rendering.from_source == 'xml'
    markdown_path = posixpath.join('papers', result.doc_id, 'renderings', f'{rendering_hash}.md')
    assert gcs_bucket.blob(markdown_path).download_as_bytes().decode('utf-8').startswith('# A synthetic chapter')

    # The crosswalk reconstructs the accession row from the manifest alone.
    rebuild.rebuild(conn, gcs_bucket)
    assert _doc_id_for(conn, f'bookid:{_BOOKID}') == result.doc_id


def test_a_journal_record_mints_no_accession(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    result = _ingest_via_transport(
        gcs_bucket, conn, _journal_seed(), _licence(), fetchers=_NO_OA, transport=_transport(_OA_EFETCH_XML)
    )

    assert result.written is True
    # Identity's PMID alone: efetch's harvested DOI/PMCID are not minted, and there is no accession.
    assert result.manifest.external_ids == litcache_pb2.ExternalIds(pmid='29089047')
    assert _keys_under(conn, 'bookid') == []


def test_an_accession_resolved_for_a_committed_paper_is_not_minted(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # A committed paper is skipped before the write path, so a resolution that now carries an accession
    # leaves no crosswalk row its manifest does not record — the rows must stay what `rebuild` would
    # reconstruct from the manifests.
    seed = _journal_seed()
    first = _ingest_via_transport(
        gcs_bucket, conn, seed, _licence(), fetchers=_NO_OA, transport=_transport(_OA_EFETCH_XML)
    )
    committed = gcs_bucket.blob(writer.manifest_path(first.doc_id)).download_as_bytes()
    with_accession = resolve.ResolvedPaper(
        metadata=b'', external_ids=litcache_pb2.ExternalIds(pmid='29089047', bookid=_BOOKID), publisher=None
    )

    second = pipeline.ingest_paper(
        gcs_bucket,
        functools.partial(crosswalk.mint, conn),
        seed,
        pipeline.extract_identity(seed),
        with_accession,
        _licence(),
        now=_NOW,
        fetchers=_NO_OA,
    )

    assert second.written is False
    assert second.doc_id == first.doc_id
    assert _keys_under(conn, 'bookid') == []
    assert gcs_bucket.blob(writer.manifest_path(first.doc_id)).download_as_bytes() == committed


def test_an_accession_claimed_by_another_paper_fails_loud(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # A second deposit of one chapter under a different identity would leave the accession's row on one
    # paper and its manifest on another; that is the deferred equivalence path, refused before any write.
    book_seed = pipeline.SeedObject(
        bucket_key=f'{_BOOK_PMID}.json',
        docling_json=(_NONOA / 'docling.json').read_bytes(),
        pdf=(_NONOA / 'source.pdf').read_bytes(),
    )
    chapter = _ingest_via_transport(
        gcs_bucket, conn, book_seed, _licence(), fetchers=_NO_OA, transport=_transport(_BOOK_EFETCH_XML)
    )
    assert _doc_id_for(conn, f'bookid:{_BOOKID}') == chapter.doc_id
    other_seed = _doi_seed('10.5555%2Fsynthetic.chapter.json')
    other_resolution = resolve.ResolvedPaper(
        metadata=b'',
        external_ids=litcache_pb2.ExternalIds(doi='10.5555/synthetic.chapter', pmid=_BOOK_PMID, bookid=_BOOKID),
        publisher=None,
    )

    with pytest.raises(ValueError, match='already claimed by another paper'):
        pipeline.ingest_paper(
            gcs_bucket,
            functools.partial(crosswalk.mint, conn),
            other_seed,
            pipeline.extract_identity(other_seed),
            other_resolution,
            _licence(),
            now=_NOW,
            fetchers=_NO_OA,
        )
    # The chapter's row and manifest are untouched.
    assert _doc_id_for(conn, f'bookid:{_BOOKID}') == chapter.doc_id
    assert _load_manifest(gcs_bucket, chapter.doc_id).external_ids.bookid == _BOOKID


# --- equivalence (cross-paper link) ---------------------------------------------


def _crossref_echo_transport() -> httpx2.MockTransport:
    """Resolve any DOI to a minimal valid Crossref work echoing the requested DOI."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        doi = request.url.path.removeprefix('/works/')
        message = {'DOI': doi, 'title': ['Synthetic'], 'issued': {'date-parts': [[2020, 1, 1]]}, 'publisher': 'X'}
        return httpx2.Response(200, content=json.dumps({'message': message}).encode('utf-8'))

    return httpx2.MockTransport(handler)


def _doi_seed(doi_key: str) -> pipeline.SeedObject:
    """A normal non-OA seed identified solely by a DOI bucket key (reuses nonoa bytes)."""
    return pipeline.SeedObject(
        bucket_key=doi_key,
        docling_json=(_NONOA / 'docling.json').read_bytes(),
        pdf=(_NONOA / 'source.pdf').read_bytes(),
    )


def test_extract_identity_ignores_pdf_doi_when_key_names_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit key DOI is authoritative; the pdf's embedded DOI is not even consulted
    # (publisher metadata must never override what the deposit explicitly declares).
    def _must_not_be_called(_pdf: bytes) -> str | None:
        raise AssertionError('pdf DOI consulted despite an explicit id')

    monkeypatch.setattr(pipeline.pdf, 'doi_from_metadata', _must_not_be_called)
    seed = pipeline.SeedObject(
        bucket_key='10.5555%2Fexplicit.aaa.json',
        docling_json=json.dumps({'origin': {'filename': 'source.pdf', 'binary_hash': 1}}).encode(),
        pdf=b'',
    )
    ident = pipeline.extract_identity(seed)
    assert ident.claim_key == 'doi:10.5555/explicit.aaa'
    assert not ident.content_addressed


def test_extract_identity_falls_back_to_pdf_doi_when_id_poor(monkeypatch: pytest.MonkeyPatch) -> None:
    # No id in key or origin: the pdf's embedded DOI rescues the deposit from a
    # binhash-only identity (the empirically useful fallback).
    monkeypatch.setattr(pipeline.pdf, 'doi_from_metadata', lambda _pdf: '10.9999/from-pdf')
    seed = pipeline.SeedObject(
        bucket_key='opaque-deposit.pdf',
        docling_json=json.dumps({'origin': {'filename': 'opaque-deposit.pdf', 'binary_hash': 7}}).encode(),
        pdf=b'',
    )
    ident = pipeline.extract_identity(seed)
    assert ident.claim_key == 'doi:10.9999/from-pdf'
    assert not ident.content_addressed


def _bridging_seed(doi_key: str, origin_doi: str) -> pipeline.SeedObject:
    """A seed whose two ids (bucket-key DOI + docling-origin DOI) bridge two works.

    Only its identity is exercised — a cross-paper link skips conversion — so the
    docling json need only carry the origin, and the pdf is never read.
    """
    docling = json.dumps({'origin': {'filename': origin_doi, 'binary_hash': 1}}).encode('utf-8')
    return pipeline.SeedObject(bucket_key=doi_key, docling_json=docling, pdf=b'')


def _ingest_doi_paper(bucket: gcs.Bucket, conn: pg8000.dbapi.Connection, doi_key: str) -> pipeline.IngestResult:
    return _ingest_via_transport(
        bucket, conn, _doi_seed(doi_key), _licence(), fetchers=_NO_OA, transport=_crossref_echo_transport()
    )


def test_cross_paper_link_writes_equivalence_edges(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    a = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.aaa.json')
    b = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.bbb.json')
    assert a.written
    assert b.written
    assert a.doc_id != b.doc_id

    # A third seed whose ids (aaa via bucket key, bbb via origin) bridge a and b.
    bridge = _bridging_seed('10.5555%2Fsynthetic.aaa.json', '10.5555/synthetic.bbb.pdf')
    linked = _ingest_link(gcs_bucket, conn, bridge)

    canonical = min(a.doc_id, b.doc_id)
    # The bridge adopts the canonical and writes no new content (the work is cached).
    assert linked.written is False
    assert linked.minted is False
    assert linked.doc_id == canonical

    # Both involved manifests now carry the edge to each other + the canonical.
    ma, mb = _load_manifest(gcs_bucket, a.doc_id), _load_manifest(gcs_bucket, b.doc_id)
    assert ma.equivalence.canonical_doc_id == canonical
    assert mb.equivalence.canonical_doc_id == canonical
    assert list(ma.equivalence.edges) == [b.doc_id]
    assert list(mb.equivalence.edges) == [a.doc_id]

    # The manifests stay rebuild-consistent: the table reconstructs without error.
    result = rebuild.rebuild(conn, gcs_bucket)
    assert result.canonical_doc_ids == {a.doc_id: canonical, b.doc_id: canonical}


def test_cross_paper_link_is_idempotent(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    a = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.aaa.json')
    b = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.bbb.json')
    bridge = _bridging_seed('10.5555%2Fsynthetic.aaa.json', '10.5555/synthetic.bbb.pdf')

    first = _ingest_link(gcs_bucket, conn, bridge)
    snapshot = gcs_bucket.blob(writer.manifest_path(a.doc_id)).download_as_bytes()
    second = _ingest_link(gcs_bucket, conn, bridge)

    assert second.doc_id == first.doc_id == min(a.doc_id, b.doc_id)
    # Re-linking recomputes the same class — no drift in the involved manifests.
    assert gcs_bucket.blob(writer.manifest_path(a.doc_id)).download_as_bytes() == snapshot


def test_equivalence_closure_is_transitive(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    a = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.aaa.json')
    c = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.ccc.json')
    # First link a—c.
    _ingest_link(gcs_bucket, conn, _bridging_seed('10.5555%2Fsynthetic.aaa.json', '10.5555/synthetic.ccc.pdf'))
    b = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.bbb.json')
    # Linking a—b must pull c into the class via a's existing edge.
    _ingest_link(gcs_bucket, conn, _bridging_seed('10.5555%2Fsynthetic.aaa.json', '10.5555/synthetic.bbb.pdf'))

    klass = {a.doc_id, b.doc_id, c.doc_id}
    canonical = min(klass)
    for doc_id in klass:
        manifest = _load_manifest(gcs_bucket, doc_id)
        assert manifest.equivalence.canonical_doc_id == canonical
        assert set(manifest.equivalence.edges) == klass - {doc_id}


def test_cross_paper_link_to_an_orphan_incumbent_fails_loud(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    a = _ingest_doi_paper(gcs_bucket, conn, '10.5555%2Fsynthetic.aaa.json')
    # An orphan: a claimed crosswalk row whose paper never committed a manifest.
    crosswalk.mint(conn, ['doi:10.5555/synthetic.bbb'])
    bridge = _bridging_seed('10.5555%2Fsynthetic.aaa.json', '10.5555/synthetic.bbb.pdf')

    with pytest.raises(ValueError, match='orphan incumbent'):
        _ingest_link(gcs_bucket, conn, bridge)
    # a's manifest is untouched by the failed link.
    assert list(_load_manifest(gcs_bucket, a.doc_id).equivalence.edges) == []
