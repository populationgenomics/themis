"""Europe PMC adapter (REST, keyless): ranked keyword search and the batched record lookup.

Two calls against the one search endpoint. ``search`` maps a keyword query's ``core`` results to
``ArticleRecord``s in the index's own relevance order. ``records_by_pmid`` resolves a batch of PMIDs
to their records in one query per chunk — the endpoint takes a disjunction of ``EXT_ID`` terms — and
is where a PMID's *absence* is read off what the answer omits, so a page that came back short of what
it reported is a fault rather than a shorter answer.

``FetchedArticle.of`` is the classification the bibliographic lookup ends in: a record carrying an
abstract, a record the index carries none under, and no record at all. Every outcome is a fact about
the record; a lookup that could not complete raises instead.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import httpx

from themis.services.evidence import errors

_SEARCH_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
_SOURCE = 'Europe PMC'

# PMIDs per record query: the endpoint takes a disjunction of `EXT_ID` terms, and this bounds the
# query string a longer list is split across.
_MAX_IDS_PER_QUERY = 50


class ArticleRecord(NamedTuple):
    pmid: str
    title: str
    authors: str
    journal: str
    year: str
    doi: str
    abstract: str


class Unavailable(enum.Enum):
    """Why a bibliographic lookup yields no abstract for a record — a fact, never a fault.

    Both members carry the same operational answer for the caller, that repeating the request for
    this PMID will not produce the text; they differ in what the caller learns about the record.
    Depositing the paper in the corpus answers neither. A lookup the adapter cannot complete raises
    rather than answering with a member, since a fault says nothing either way about the record.
    """

    NO_RECORD = enum.auto()
    NO_ABSTRACT = enum.auto()


class FetchedArticle(NamedTuple):
    """What one PMID's bibliographic lookup found: the record, and why it falls short of a whole hit.

    Build one through ``of``, which is where the three states a lookup reaches are classified: a
    record carrying an abstract (``unavailable`` ``None``), a record the index carries none under
    (``NO_ABSTRACT``, the bibliography still present), and no record at all (``NO_RECORD``, no
    ``record``).
    """

    pmid: str
    record: ArticleRecord | None
    unavailable: Unavailable | None

    @classmethod
    def of(cls, pmid: str, record: ArticleRecord | None) -> FetchedArticle:
        """The outcome for one PMID, classified from what the index held for it.

        Args:
            pmid: The PMID as the lookup keyed it (``pmids.pmid_key``).
            record: The bibliographic record, or ``None`` where the index holds none.

        Returns:
            The outcome, carrying an ``Unavailable`` unless the record states an abstract.
        """
        if record is None:
            return cls(pmid=pmid, record=None, unavailable=Unavailable.NO_RECORD)
        if not record.abstract.strip():
            return cls(pmid=pmid, record=record, unavailable=Unavailable.NO_ABSTRACT)
        return cls(pmid=pmid, record=record, unavailable=None)


async def search(query: str, max_results: int, *, http_client: httpx.AsyncClient) -> list[ArticleRecord]:
    """Search the index by keyword and return the matching records, most-relevant first.

    Args:
        query: The keyword query, in Europe PMC's own search syntax.
        max_results: The page size asked of the index; the caller's clamped budget.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The page's records in the index's relevance order; empty where it matched nothing.

    Raises:
        errors.InvalidRequestError: If Europe PMC refuses the call (a non-429 4xx).
        httpx.HTTPStatusError: If Europe PMC returns a 429 or a 5xx.
    """
    response = await http_client.get(
        _SEARCH_URL,
        params={'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': str(max_results)},
    )
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'search {query!r}')
    return [_article_record(result) for result in _results(response.json())]


async def records_by_pmid(pmids: Sequence[str], *, http_client: httpx.AsyncClient) -> dict[str, ArticleRecord]:
    """The record for each PMID the index holds one for, keyed by the PMID the record carries.

    One query per chunk of ids rather than one per id: Europe PMC resolves a disjunction of
    ``EXT_ID`` terms in a single request. ``pageSize`` covers the whole chunk.

    Absence is read off this mapping, so every record that came back has to be accounted for against
    the PMID it was asked under: one that names no PMID, or one naming a PMID the query did not,
    would leave a requested PMID looking like one the index holds nothing for.

    Args:
        pmids: The PMIDs, already keyed (``pmids.pmid_key``) and distinct.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The records, keyed by PMID; a PMID the index holds no record for is left out.

    Raises:
        errors.InvalidRequestError: If Europe PMC refuses the call (a non-429 4xx).
        httpx.HTTPStatusError: If Europe PMC returns a 429 or a 5xx.
        ValueError: If a page carried fewer records than it reported, or carried a record this query
            did not ask for.
    """
    records: dict[str, ArticleRecord] = {}
    for start in range(0, len(pmids), _MAX_IDS_PER_QUERY):
        chunk = pmids[start : start + _MAX_IDS_PER_QUERY]
        requested = set(chunk)
        query = f'({" OR ".join(f"EXT_ID:{pmid}" for pmid in chunk)}) AND SRC:MED'
        response = await http_client.get(
            _SEARCH_URL,
            params={'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': str(len(chunk))},
        )
        errors.raise_for_status(response, upstream=_SOURCE, subject=f'records for {list(chunk)!r}')
        payload = response.json()
        results = _results(payload)
        _whole_page(payload, results, query=query)
        for result in results:
            pmid = _field(result, 'pmid') or _field(result, 'id')
            if pmid not in requested:
                raise ValueError(
                    f'Europe PMC answered {query!r} with a record under {pmid!r}, which it did not ask about'
                )
            records[pmid] = _article_record(result)
    return records


def _results(payload: object) -> list[Mapping[str, object]]:
    result_list = payload.get('resultList') if isinstance(payload, Mapping) else None
    results = result_list.get('result') if isinstance(result_list, Mapping) else None
    if not isinstance(results, list):
        return []
    return [entry for entry in results if isinstance(entry, Mapping)]


def _whole_page(payload: object, results: Sequence[Mapping[str, object]], *, query: str) -> None:
    """Fail loud unless a record query's one page carried every hit it reported.

    Raises:
        ValueError: The answer states no hit count, or states more hits than it carried records — a
            cut page, whose missing records no caller can distinguish from records the index holds
            none of.
    """
    hits = payload.get('hitCount') if isinstance(payload, Mapping) else None
    if not isinstance(hits, int) or isinstance(hits, bool):
        raise ValueError(f'Europe PMC answered {query!r} with no hit count')
    if hits > len(results):
        raise ValueError(
            f'Europe PMC answered {query!r} with {len(results)} of {hits} records; the rest would '
            'read as records that do not exist'
        )


def _article_record(result: Mapping[str, object]) -> ArticleRecord:
    return ArticleRecord(
        pmid=_field(result, 'pmid') or _field(result, 'id'),
        title=_field(result, 'title'),
        authors=_field(result, 'authorString'),
        journal=_journal(result),
        year=_year(result),
        doi=_field(result, 'doi'),
        abstract=_field(result, 'abstractText'),
    )


def _field(result: Mapping[str, object], key: str) -> str:
    value = result.get(key)
    return value if isinstance(value, str) else ''


def _journal(result: Mapping[str, object]) -> str:
    """The containing work: the journal for an article, the book for a Bookshelf chapter."""
    top_level = _field(result, 'journalTitle')
    if top_level:
        return top_level
    journal_info = result.get('journalInfo')
    journal = journal_info.get('journal') if isinstance(journal_info, Mapping) else None
    title = journal.get('title') if isinstance(journal, Mapping) else None
    if isinstance(title, str) and title:
        return title
    comprising = _book_details(result).get('comprisingTitle')
    return comprising if isinstance(comprising, str) else ''


def _year(result: Mapping[str, object]) -> str:
    """The year to cite the record by.

    ``pubYear`` dates the containing work, which for a Bookshelf chapter is the year the series
    opened rather than the year this chapter says anything — a chapter revised last month cites to
    the 1990s. A book part carries its own publication date, so read the year off that instead.
    """
    if _book_details(result):
        first_published = _field(result, 'firstPublicationDate')
        if first_published:
            return first_published[:4]
    return _field(result, 'pubYear')


def _book_details(result: Mapping[str, object]) -> Mapping[str, object]:
    """The record's book metadata, empty for a record that is not part of a book."""
    details = result.get('bookOrReportDetails')
    return details if isinstance(details, Mapping) else {}
