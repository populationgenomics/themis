"""PubMed adapter: the batched whole-record lookup, over a recorded efetch answer.

Driven by an httpx2 `MockTransport`; no test hits the network. The recorded payload is efetch's own
answer for one OA paper (PMID 29089047 — CC-BY, so its record is redistributable on the public
mirror). A requested PMID the answer omits is how PubMed states absence; nothing offline can confirm
that omission is how absence arrives, so the payload is recorded rather than assumed.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest
from pubmed_proto import pubmed_pb2

from themis.services.evidence import errors
from themis.services.evidence.upstreams import pubmed

_EFETCH_XML = (pathlib.Path(__file__).resolve().parent / 'fixtures' / 'pubmed_efetch.xml').read_bytes()
_PMID = '29089047'

_BOOK_PMID = '20301288'
# A synthetic `PubmedBookArticle`, structurally complete in the schema's element order: efetch answers
# a GeneReviews-class PMID with one, and the converter reads a record whole or not at all.
_BOOK_XML = (
    b'<PubmedBookArticle><BookDocument>'
    b'<PMID Version="1">20301288</PMID>'
    b'<ArticleIdList><ArticleId IdType="bookaccession">NBK900001</ArticleId></ArticleIdList>'
    b'<Book>'
    b'<Publisher><PublisherName>A university press</PublisherName>'
    b'<PublisherLocation>A city</PublisherLocation></Publisher>'
    b'<BookTitle book="synthetic">A synthetic review series</BookTitle>'
    b'<PubDate><Year>1993</Year></PubDate>'
    b'</Book>'
    b'<ArticleTitle book="synthetic" part="chapter1">A synthetic chapter</ArticleTitle>'
    b'<AuthorList Type="authors" CompleteYN="Y">'
    b'<Author ValidYN="Y"><LastName>Doe</LastName><ForeName>Jane</ForeName><Initials>J</Initials></Author>'
    b'</AuthorList>'
    b'<PublicationType UI="D016454">Review</PublicationType>'
    b'<Abstract><AbstractText>A synthetic summary.</AbstractText></Abstract>'
    b'<Sections><Section><SectionTitle book="synthetic" part="chapter1.s1">Summary</SectionTitle></Section></Sections>'
    b'<ContributionDate><Year>2010</Year><Month>3</Month><Day>23</Day></ContributionDate>'
    b'<DateRevised><Year>2024</Year><Month>1</Month><Day>4</Day></DateRevised>'
    b'</BookDocument><PubmedBookData>'
    b'<History><PubMedPubDate PubStatus="pubmed"><Year>2010</Year><Month>3</Month><Day>23</Day></PubMedPubDate>'
    b'</History>'
    b'<PublicationStatus>ppublish</PublicationStatus>'
    b'<ArticleIdList>'
    b'<ArticleId IdType="bookaccession">NBK900001</ArticleId>'
    b'<ArticleId IdType="pubmed">20301288</ArticleId>'
    b'</ArticleIdList>'
    b'</PubmedBookData></PubmedBookArticle>'
)
# A set carrying the book record beside the journal article.
_WITH_BOOK_XML = _EFETCH_XML.replace(b'</PubmedArticleSet>', _BOOK_XML + b'</PubmedArticleSet>')
_DELETED_PMID = '333'
# A set whose trailing `DeleteCitation` names a PMID PubMed has withdrawn the record of.
_WITH_DELETION_XML = _EFETCH_XML.replace(
    b'</PubmedArticleSet>',
    b'<DeleteCitation><PMID Version="1">333</PMID></DeleteCitation></PubmedArticleSet>',
)


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def test_a_batch_lands_every_pmid_in_exactly_one_outcome() -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, content=_EFETCH_XML)

    fetched = _run(handler, lambda c: pubmed.articles_by_pmid([_PMID, '999999999'], http_client=c))

    assert len(calls) == 1  # one efetch call for the whole batch: what taking a list is for
    assert [article.medline_citation.pmid.value for article in fetched.articles] == [_PMID]
    assert fetched.articles[0].medline_citation.article.HasField('abstract')  # the record arrives whole
    assert fetched.pmids_without_record == ['999999999']
    assert fetched.book_articles == []


def test_a_book_record_arrives_whole_never_as_absence() -> None:
    # efetch answers a GeneReviews-class PMID as a `PubmedBookArticle`; read as journal records
    # alone, its PMID would land in `pmids_without_record` — the index reported as holding nothing
    # under a PMID it holds a first-class chapter under. The outcome is read off the record's own PMID.
    fetched = _run(
        lambda _r: httpx2.Response(200, content=_WITH_BOOK_XML),
        lambda c: pubmed.articles_by_pmid([_PMID, _BOOK_PMID, '999999999'], http_client=c),
    )

    assert [article.medline_citation.pmid.value for article in fetched.articles] == [_PMID]
    (book,) = fetched.book_articles
    assert book.book_document.pmid.value == _BOOK_PMID
    assert [(i.id_type, i.value) for i in book.book_document.article_id_list] == [
        (pubmed_pb2.ArticleId.ID_TYPE_BOOKACCESSION, 'NBK900001')
    ]
    assert book.book_document.article_title.value == 'A synthetic chapter'
    assert book.book_document.book.book_title.value == 'A synthetic review series'
    assert book.book_document.abstract.abstract_text[0].value == 'A synthetic summary.'
    assert fetched.pmids_without_record == ['999999999']


def test_a_deleted_pmid_is_one_nothing_is_indexed_under() -> None:
    # PubMed's deletion notice states the record is withdrawn. That is what `pmids_without_record`
    # carries — nothing indexed under the PMID — so the notice is neither a fault nor a fourth outcome.
    fetched = _run(
        lambda _r: httpx2.Response(200, content=_WITH_DELETION_XML),
        lambda c: pubmed.articles_by_pmid([_PMID, _DELETED_PMID], http_client=c),
    )

    assert [article.medline_citation.pmid.value for article in fetched.articles] == [_PMID]
    assert fetched.book_articles == []
    assert fetched.pmids_without_record == [_DELETED_PMID]


@pytest.mark.parametrize(
    ('xml', 'pmids'),
    [
        pytest.param(_EFETCH_XML, ['111'], id='journal-record'),
        pytest.param(_WITH_BOOK_XML, [_PMID], id='book-record'),
        pytest.param(_WITH_DELETION_XML, [_PMID], id='deletion-notice'),
    ],
)
def test_an_answer_under_an_unasked_pmid_fails_loud(xml: bytes, pmids: list[str]) -> None:
    # Absence is read off what the answer omits, so a record of either kind, or a deletion, under a
    # PMID nobody asked about is an answer to some other request — one whose omissions say nothing
    # about the PMIDs this batch asked for.
    with pytest.raises(ValueError, match='did not ask'):
        _run(lambda _r: httpx2.Response(200, content=xml), lambda c: pubmed.articles_by_pmid(pmids, http_client=c))


def test_a_refusal_reaches_the_caller_as_an_invalid_request() -> None:
    with pytest.raises(errors.InvalidRequestError):
        _run(lambda _r: httpx2.Response(400, text='no'), lambda c: pubmed.articles_by_pmid(['111'], http_client=c))


def test_a_transient_failure_escapes_after_one_attempt() -> None:
    # The caller's retry helper owns backoff, as for every evidence upstream: one attempt goes out,
    # and the 503 escapes as itself rather than being retried behind the rpc deadline.
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(503, text='busy')

    with pytest.raises(httpx2.HTTPStatusError):
        _run(handler, lambda c: pubmed.articles_by_pmid(['111'], http_client=c))
    assert len(calls) == 1
