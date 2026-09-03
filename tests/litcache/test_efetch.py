"""PubMed efetch → canonical metadata.pb + harvested cross-ids.

The pure parse is exercised against two committed efetch fixtures: the OA paper's journal
record (PMID 29089047 — CC-BY, so its record is redistributable on the public mirror) and a
synthetic book record (`bookshelf/efetch.xml`). The fetch path is driven by an httpx2
`MockTransport` so the offline suite stays deterministic. A live efetch is integration-gated
on `LITCACHE_EFETCH_LIVE_PMID`.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import urllib.parse
from collections.abc import Callable

import httpx2
import pytest
from lxml import etree

from themis.litcache import efetch
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_EFETCH_XML = _FIXTURES / 'oa' / 'efetch.xml'
_BOOK_EFETCH_XML = _FIXTURES / 'bookshelf' / 'efetch.xml'
_PMID = '29089047'
_BOOK_PMID = '30000010'
_BOOKID = 'NBK900001'
# The record's two id lists: the accession sits in the document's own (where NLM states it), the
# `pubmed` id in PubmedBookData's.
_BOOK_ACCESSION_ID = b'<ArticleId IdType="bookaccession">NBK900001</ArticleId>'
_PUBMED_ID = b'<ArticleId IdType="pubmed">30000010</ArticleId>'
_CHAPTER_TITLE = b'<ArticleTitle book="synthetic" part="chapter1">A synthetic chapter</ArticleTitle>'
_DOI = '10.1234/synthetic.chapter'


def _book_record() -> bytes:
    """The synthetic book record alone, out of its set: the tests compose sets of their own around it."""
    record = etree.fromstring(_BOOK_EFETCH_XML.read_bytes()).find('PubmedBookArticle')
    assert record is not None
    return etree.tostring(record, with_tail=False)


_BOOK_XML = _book_record()


def _doi_id(doi: str) -> bytes:
    return b'<ArticleId IdType="doi">' + doi.encode() + b'</ArticleId>'


def _set(*records: bytes) -> bytes:
    return b'<PubmedArticleSet>' + b''.join(records) + b'</PubmedArticleSet>'


def _oa_set_with(*records: bytes) -> bytes:
    """The recorded OA set with `records` appended beside its journal article."""
    return _EFETCH_XML.read_bytes().replace(b'</PubmedArticleSet>', b''.join(records) + b'</PubmedArticleSet>')


def _book_under(pmid: bytes) -> bytes:
    return _BOOK_XML.replace(b'<PMID Version="1">30000010</PMID>', b'<PMID Version="1">' + pmid + b'</PMID>', 1)


def _record(metadata: bytes) -> litcache_pb2.PaperMetadata:
    return litcache_pb2.PaperMetadata.FromString(metadata)


def test_parse_response_validates_and_keys_by_pmid() -> None:
    response = efetch.parse_response(_EFETCH_XML.read_bytes())

    assert set(response.resolved) == {_PMID}
    assert response.precondition_failed == {}
    # the metadata.pb bytes parse straight back to the envelope, the journal record in PubMed's field.
    paper = _record(response.resolved[_PMID].metadata)
    assert paper.pubmed.WhichOneof('kind') == 'article'
    article = paper.pubmed.article
    assert article.medline_citation.pmid.value == _PMID
    title = article.medline_citation.article.article_title.value
    assert 'Whole exome sequencing' in title


def test_cross_ids_harvested_from_own_id_list() -> None:
    resolved = efetch.parse_response(_EFETCH_XML.read_bytes()).resolved
    # DOI + PMCID from PubmedData.ArticleIdList, PMID from MedlineCitation; the
    # reference-list citation ids in the record are not harvested.
    assert resolved[_PMID].external_ids == litcache_pb2.ExternalIds(
        doi='10.1186/s13073-017-0482-5',
        pmid=_PMID,
        pmcid='PMC5664429',
    )


def test_parse_set_keys_each_record_kind_by_the_pmid_it_states() -> None:
    parsed = efetch.parse_set(_oa_set_with(_BOOK_XML))

    assert set(parsed.articles) == {_PMID}
    assert set(parsed.book_articles) == {_BOOK_PMID}
    book = parsed.book_articles[_BOOK_PMID]
    assert book.book_document.pmid.value == _BOOK_PMID
    assert [i.value for i in book.book_document.article_id_list] == [_BOOKID]
    assert book.book_document.article_title.value == 'A synthetic chapter'
    assert book.pubmed_book_data.publication_status == 'ppublish'


def test_parse_response_resolves_a_book_record_in_the_envelopes_book_arm() -> None:
    resolved = efetch.parse_response(_oa_set_with(_BOOK_XML)).resolved

    assert set(resolved) == {_PMID, _BOOK_PMID}
    paper = _record(resolved[_BOOK_PMID].metadata)
    assert paper.pubmed.WhichOneof('kind') == 'book_article'
    assert paper.pubmed.book_article.book_document.article_title.value == 'A synthetic chapter'
    # The accession is the chapter's own id, harvested beside the PMID; a book part has no PMCID.
    assert resolved[_BOOK_PMID].external_ids == litcache_pb2.ExternalIds(pmid=_BOOK_PMID, bookid=_BOOKID)


def test_a_whole_book_record_resolves_without_a_chapter_title() -> None:
    # A record for a book rather than one of its chapters states no ArticleTitle; the book's title is
    # the one the record carries.
    resolved = efetch.parse_response(_set(_BOOK_XML.replace(_CHAPTER_TITLE, b''))).resolved

    document = _record(resolved[_BOOK_PMID].metadata).pubmed.book_article.book_document
    assert not document.HasField('article_title')
    assert document.book.book_title.value == 'A synthetic review series'
    assert resolved[_BOOK_PMID].external_ids.bookid == _BOOKID


@pytest.mark.parametrize(
    'record',
    [
        pytest.param(_BOOK_XML.replace(_PUBMED_ID, _PUBMED_ID + _doi_id(_DOI)), id='in-pubmed-book-data'),
        pytest.param(_BOOK_XML.replace(_BOOK_ACCESSION_ID, _BOOK_ACCESSION_ID + _doi_id(_DOI)), id='in-book-document'),
        pytest.param(
            _BOOK_XML.replace(_PUBMED_ID, _PUBMED_ID + _doi_id(_DOI)).replace(
                _BOOK_ACCESSION_ID, _BOOK_ACCESSION_ID + _doi_id(_DOI)
            ),
            id='in-both',
        ),
    ],
)
def test_a_book_records_doi_is_harvested_from_either_id_list(record: bytes) -> None:
    resolved = efetch.parse_response(_set(record)).resolved
    assert resolved[_BOOK_PMID].external_ids == litcache_pb2.ExternalIds(doi=_DOI, pmid=_BOOK_PMID, bookid=_BOOKID)


@pytest.mark.parametrize(
    ('record', 'message'),
    [
        pytest.param(_BOOK_XML.replace(b'NBK900001', b'nbk900001'), 'not canonical', id='lower-case-accession'),
        pytest.param(_BOOK_XML.replace(b'NBK900001', b'NBX900001'), 'not canonical', id='not-an-accession'),
        pytest.param(_BOOK_XML.replace(_BOOK_ACCESSION_ID, b''), 'no Bookshelf accession', id='no-accession'),
        pytest.param(
            # Only the document's own list is read for the accession; one stated elsewhere is not NLM's.
            _BOOK_XML.replace(_BOOK_ACCESSION_ID, b'').replace(_PUBMED_ID, _PUBMED_ID + _BOOK_ACCESSION_ID),
            'no Bookshelf accession',
            id='accession-only-in-pubmed-book-data',
        ),
        pytest.param(
            _BOOK_XML.replace(_BOOK_ACCESSION_ID, _BOOK_ACCESSION_ID + _BOOK_ACCESSION_ID.replace(b'1<', b'2<')),
            'several Bookshelf accessions',
            id='two-accessions',
        ),
        pytest.param(
            _BOOK_XML.replace(_PUBMED_ID, _PUBMED_ID + _doi_id('10.1234/a')).replace(
                _BOOK_ACCESSION_ID, _BOOK_ACCESSION_ID + _doi_id('10.1234/b')
            ),
            'two DOIs',
            id='dois-disagree-across-lists',
        ),
    ],
)
def test_a_book_record_failing_the_id_precondition_costs_its_paper_alone(record: bytes, message: str) -> None:
    # The journal record fetched beside it resolves, and nothing raises: the fault is that record's.
    response = efetch.parse_response(_oa_set_with(record))

    assert set(response.resolved) == {_PMID}
    assert set(response.precondition_failed) == {_BOOK_PMID}
    assert message in response.precondition_failed[_BOOK_PMID]


def test_a_deletion_notice_names_pmids_nothing_is_indexed_under() -> None:
    # PubMed's trailing `DeleteCitation` names PMIDs whose records it has withdrawn. Nothing is
    # indexed under them — what an absent record means — so they key no record of a third kind.
    xml = _set(_BOOK_XML, b'<DeleteCitation><PMID Version="1">111</PMID><PMID Version="1">222</PMID></DeleteCitation>')
    parsed = efetch.parse_set(xml)

    assert parsed.articles == {}
    assert set(parsed.book_articles) == {_BOOK_PMID}
    assert parsed.deleted_pmids == {'111', '222'}


def test_a_comment_in_the_set_is_not_a_member() -> None:
    parsed = efetch.parse_set(_set(b'<!-- retrieved -->', _BOOK_XML, b'<!-- end -->'))
    assert set(parsed.book_articles) == {_BOOK_PMID}


@pytest.mark.parametrize(
    ('xml', 'message'),
    [
        pytest.param(_set(_BOOK_XML, _BOOK_XML), 'answered twice', id='book-twice'),
        pytest.param(_oa_set_with(_book_under(_PMID.encode())), 'answered twice', id='article-and-book-under-one-pmid'),
        pytest.param(_set(_book_under(b'')), 'not canonical', id='book-stating-no-pmid'),
        pytest.param(_set(_book_under(b'0030000010')), 'not canonical', id='book-under-a-padded-pmid'),
        pytest.param(
            _set(_BOOK_XML, b'<DeleteCitation><PMID Version="1"> 333 </PMID></DeleteCitation>'),
            'not canonical',
            id='deletion-under-a-pmid-with-whitespace',
        ),
        pytest.param(
            _set(
                b'<PubmedBookArticle><BookDocument><PMID Version="1">30000010</PMID></BookDocument></PubmedBookArticle>'
            ),
            r'<PubmedBookArticle> \(PMID 30000010\) does not convert',
            id='truncated-book-names-its-pmid',
        ),
        pytest.param(
            _set(b'<PubmedBookArticle><BookDocument/></PubmedBookArticle>'),
            r'<PubmedBookArticle> \(no PMID stated\) does not convert',
            id='truncated-book-stating-no-pmid',
        ),
        pytest.param(
            _set(b'<PubmedArticle><MedlineCitation><PMID Version="1">111</PMID></MedlineCitation></PubmedArticle>'),
            r'<PubmedArticle> \(PMID 111\) does not convert',
            id='truncated-article-names-its-pmid',
        ),
        pytest.param(
            _set(_BOOK_XML, b'<DeleteCitation><PMID Version="1">30000010</PMID></DeleteCitation>'),
            'answered twice',
            id='record-then-deletion-under-one-pmid',
        ),
        pytest.param(
            _set(b'<DeleteCitation><PMID Version="1">30000010</PMID></DeleteCitation>', _BOOK_XML),
            'answered twice',
            id='deletion-then-record-under-one-pmid',
        ),
        pytest.param(
            _set(_BOOK_XML, b'<DeleteCitation><PMID Version="1"></PMID></DeleteCitation>'),
            'not canonical',
            id='deletion-stating-no-pmid',
        ),
        pytest.param(_set(b'<ErrorList><PMID Version="1">111</PMID></ErrorList>'), 'unexpected', id='not-a-member'),
    ],
)
def test_a_set_that_does_not_read_as_one_record_per_pmid_fails_loud(xml: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        efetch.parse_set(xml)


def test_empty_set_yields_no_record() -> None:
    # efetch returns an empty set for an unknown PMID — the caller's `unknown`.
    assert efetch.parse_response(b'<PubmedArticleSet></PubmedArticleSet>') == efetch.ParsedResponse({}, {})


def test_unexpected_root_fails_loud() -> None:
    with pytest.raises(ValueError, match='PubmedArticleSet'):
        efetch.parse_response(b'<eFetchResult><ERROR>bad id</ERROR></eFetchResult>')


def test_fetch_requires_a_pmid() -> None:
    async def run() -> None:
        async with httpx2.AsyncClient() as client:
            await efetch.fetch([], http_client=client)

    with pytest.raises(ValueError, match='at least one PMID'):
        asyncio.run(run())


def test_fetch_posts_the_id_list_in_the_body() -> None:
    # efetch always POSTs: the id list rides the body (no GET inline-id ceiling), so a
    # batch of any size takes one path and the URL carries no `id=`.
    pmids = [str(i) for i in range(250)]
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen['method'] = request.method
        seen['body'] = request.content.decode()
        seen['query_id'] = request.url.params.get('id', '')
        return httpx2.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            await efetch.fetch(pmids, http_client=client)

    asyncio.run(run())

    assert seen['method'] == 'POST'
    assert 'id=' in seen['body']
    assert seen['query_id'] == ''  # the id list is in the body, not the URL


def test_resolve_drives_efetch_and_parses() -> None:
    body = _EFETCH_XML.read_bytes()
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        # efetch POSTs, so the query rides the form body, not the URL.
        seen.update(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx2.Response(200, content=body)

    async def run() -> efetch.ParsedResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await efetch.resolve([_PMID], http_client=client)

    resolved = asyncio.run(run()).resolved

    assert seen['db'] == 'pubmed'
    assert seen['id'] == _PMID
    assert set(resolved) == {_PMID}
    assert resolved[_PMID].external_ids.pmid == _PMID


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_EFETCH_LIVE_PMID'),
    reason='set LITCACHE_EFETCH_LIVE_PMID to hit live NCBI efetch',
)
def test_live_efetch() -> None:
    pmid = os.environ['LITCACHE_EFETCH_LIVE_PMID']

    async def run() -> efetch.ParsedResponse:
        async with httpx2.AsyncClient(timeout=30.0) as client:
            return await efetch.resolve([pmid], http_client=client)

    resolved = asyncio.run(run()).resolved
    assert pmid in resolved
    assert _record(resolved[pmid].metadata).pubmed.WhichOneof('kind') is not None


def _counting_handler(
    status_sequence: list[int],
) -> tuple[list[int], Callable[[httpx2.Request], httpx2.Response]]:
    """A MockTransport handler returning each status in turn (last repeats); records call count."""
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request  # the response depends only on call count, not the request
        calls.append(1)
        status = status_sequence[min(len(calls) - 1, len(status_sequence) - 1)]
        content = b'<PubmedArticleSet/>' if status == 200 else b''
        return httpx2.Response(status, content=content)

    return calls, handler


def test_fetch_retries_transient_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(efetch, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls, handler = _counting_handler([502, 502, 200])

    async def run() -> bytes:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await efetch.fetch(['1'], http_client=client)

    assert asyncio.run(run()) == b'<PubmedArticleSet/>'
    assert len(calls) == 3  # two transient 502s, then the 200


def test_fetch_gives_up_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(efetch, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls, handler = _counting_handler([503])  # never recovers

    async def run() -> bytes:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await efetch.fetch(['1'], http_client=client)

    with pytest.raises(httpx2.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == efetch._MAX_FETCH_ATTEMPTS


def test_fetch_with_one_attempt_raises_on_the_first_transient_failure() -> None:
    calls, handler = _counting_handler([503])

    async def run() -> bytes:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await efetch.fetch(['1'], http_client=client, attempts=1)

    with pytest.raises(httpx2.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1  # no retry: the caller said its own policy owns backoff


def test_fetch_refuses_non_positive_attempts() -> None:
    async def run() -> bytes:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _r: httpx2.Response(200))) as client:
            return await efetch.fetch(['1'], http_client=client, attempts=0)

    with pytest.raises(ValueError, match='attempts'):
        asyncio.run(run())


def test_fetch_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(efetch, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls, handler = _counting_handler([400])  # deterministic; retrying can't help

    async def run() -> bytes:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await efetch.fetch(['1'], http_client=client)

    with pytest.raises(httpx2.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1
