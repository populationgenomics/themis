"""The `metadata.pb` envelope: its constraints on the way to bytes and back, and the derived title."""

from __future__ import annotations

import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache import paper_metadata
from themis.litcache.models import crossref_pb2, litcache_pb2, openalex_pb2


def _article(title: str) -> litcache_pb2.PubmedRecord:
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.article.article_title.value = title
    return litcache_pb2.PubmedRecord(article=article)


def _book(*, chapter_title: str | None, book_title: str) -> litcache_pb2.PubmedRecord:
    book = pubmed_pb2.PubmedBookArticle()
    book.book_document.book.book_title.value = book_title
    if chapter_title is not None:
        book.book_document.article_title.value = chapter_title
    return litcache_pb2.PubmedRecord(book_article=book)


@pytest.mark.parametrize(
    'envelope',
    [
        litcache_pb2.PaperMetadata(pubmed=_article('T')),
        litcache_pb2.PaperMetadata(crossref=crossref_pb2.Work(doi='10.1/x')),
        litcache_pb2.PaperMetadata(openalex=openalex_pb2.Work(id='W1')),
        litcache_pb2.PaperMetadata(pubmed=_article('T'), openalex=openalex_pb2.Work(id='W1')),
    ],
)
def test_bytes_round_trip_for_each_index(envelope: litcache_pb2.PaperMetadata) -> None:
    assert paper_metadata.parse(paper_metadata.to_canonical_bytes(envelope)) == envelope


def test_an_envelope_with_no_record_fails_both_ways() -> None:
    with pytest.raises(ValueError, match='no record set'):
        paper_metadata.to_canonical_bytes(litcache_pb2.PaperMetadata())
    with pytest.raises(ValueError, match='no record set'):
        paper_metadata.parse(litcache_pb2.PaperMetadata().SerializeToString())


def test_a_pubmed_record_in_neither_kind_fails() -> None:
    with pytest.raises(ValueError, match='neither of its kinds'):
        paper_metadata.to_canonical_bytes(litcache_pb2.PaperMetadata(pubmed=litcache_pb2.PubmedRecord()))


def test_bytes_that_are_not_an_envelope_fail() -> None:
    with pytest.raises(ValueError, match='not a valid PaperMetadata'):
        paper_metadata.parse(b'\xff\xff\xff')


def test_title_prefers_pubmed_then_crossref_then_openalex() -> None:
    crossref = crossref_pb2.Work(title=['Crossref title'])
    openalex = openalex_pb2.Work(title='OpenAlex title')
    assert paper_metadata.title(litcache_pb2.PaperMetadata(pubmed=_article('PubMed title'), crossref=crossref)) == (
        'PubMed title'
    )
    assert paper_metadata.title(litcache_pb2.PaperMetadata(crossref=crossref, openalex=openalex)) == 'Crossref title'
    assert paper_metadata.title(litcache_pb2.PaperMetadata(openalex=openalex)) == 'OpenAlex title'


def test_title_falls_through_a_record_that_states_none() -> None:
    envelope = litcache_pb2.PaperMetadata(
        pubmed=_article(''), crossref=crossref_pb2.Work(title=['', 'Second title']), openalex=openalex_pb2.Work()
    )
    assert paper_metadata.title(envelope) == 'Second title'


def test_title_of_a_book_record_is_the_chapters_or_else_the_books() -> None:
    chapter = litcache_pb2.PaperMetadata(pubmed=_book(chapter_title='A chapter', book_title='A book'))
    whole = litcache_pb2.PaperMetadata(pubmed=_book(chapter_title=None, book_title='A book'))
    assert paper_metadata.title(chapter) == 'A chapter'
    assert paper_metadata.title(whole) == 'A book'


def test_title_is_empty_when_no_record_states_one() -> None:
    assert paper_metadata.title(litcache_pb2.PaperMetadata(openalex=openalex_pb2.Work(id='W1'))) == ''


def test_title_fails_on_an_envelope_with_no_record() -> None:
    with pytest.raises(ValueError, match='no record set'):
        paper_metadata.title(litcache_pb2.PaperMetadata())
