"""Europe PMC adapter (REST, keyless): ranked keyword search.

``search`` maps a keyword query's ``core`` results to ``Record``s in the index's own relevance
order, alongside the index's own count of everything the query matched. The index covers what
PubMed cannot: open-access full text and preprint deposits, so a hit may carry no PMID at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

import httpx2

from themis.services.evidence import errors

_SEARCH_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
_SOURCE = 'Europe PMC'


class Record(NamedTuple):
    """One bibliographic record as the index states it, each field empty where it states none.

    ``authors`` holds one entry per author in the index's own order — a group author included, which
    the index names as a body rather than a person.
    """

    pmid: str
    title: str
    authors: tuple[str, ...]
    journal: str
    year: str
    doi: str
    abstract: str
    pmcid: str


class SearchHits(NamedTuple):
    """One search page's records, and the index's count of every record the query matched.

    ``total_matched`` above ``len(records)`` means the page is the top-ranked prefix of a longer
    match — the page size bound it, not the index.
    """

    records: list[Record]
    total_matched: int


async def search(query: str, max_results: int, *, http_client: httpx2.AsyncClient) -> SearchHits:
    """Search the index by keyword and return the matching records, most-relevant first.

    Args:
        query: The keyword query, in Europe PMC's own search syntax.
        max_results: The page size asked of the index; the caller's clamped budget.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The page's records in the index's relevance order — empty where it matched nothing — and the
        index's own hit count for the query. A search reaches every source the index holds, so a
        record it carries under no PubMed id — a preprint, a PMC-only deposit — comes back with an
        empty ``pmid`` rather than the index's own id for it.

    Raises:
        errors.InvalidRequestError: If Europe PMC refuses the call — a non-429 4xx, or the refusal
            document it delivers with HTTP 200 for a query it will not run.
        httpx2.HTTPStatusError: If Europe PMC returns a 429 or a 5xx.
        ValueError: If the answer states no hit count or carries no readable result list.
    """
    response = await http_client.get(
        _SEARCH_URL,
        params={'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': str(max_results)},
    )
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'search {query!r}')
    payload = response.json()
    _raise_on_refusal(payload, query=query)
    return SearchHits(
        records=[_record(result) for result in _results(payload, query=query)],
        total_matched=_hit_count(payload, query=query),
    )


def _raise_on_refusal(payload: object, *, query: str) -> None:
    """Fail a refusal the index delivers with HTTP 200: an error body in place of a result page.

    Europe PMC answers an empty or over-long query with status 200 and an ``errMsg`` document, so the
    status-based judgement never sees it. Reissued unchanged it cannot come back different, which is
    the same verdict a 4xx carries.

    Raises:
        errors.InvalidRequestError: The answer is a refusal document.
    """
    if isinstance(payload, Mapping) and 'hitCount' not in payload and ('errMsg' in payload or 'errCode' in payload):
        explained = payload.get('errMsg', f'error code {payload.get("errCode")}')
        raise errors.InvalidRequestError(
            f'{_SOURCE} rejected search {errors.clipped(query)!r}: {errors.clipped(str(explained))}'
        )


def _results(payload: object, *, query: str) -> list[Mapping[str, object]]:
    """The page's result entries, or a fault: a shape this cannot read is never an empty page.

    Raises:
        ValueError: The answer carries no result list, or an entry that is not an object — read as
            empty, either would tell a caller the index matched nothing.
    """
    result_list = payload.get('resultList') if isinstance(payload, Mapping) else None
    results = result_list.get('result') if isinstance(result_list, Mapping) else None
    if not isinstance(results, list) or not all(isinstance(entry, Mapping) for entry in results):
        raise ValueError(f'Europe PMC answered {query!r} without a readable result list')
    return results


def _hit_count(payload: object, *, query: str) -> int:
    """The index's own count of every record matching ``query``.

    Raises:
        ValueError: The answer states no hit count. Reading the page length as the count would state
            that the query matched exactly what came back, which is what a census exists to deny.
    """
    hits = payload.get('hitCount') if isinstance(payload, Mapping) else None
    if not isinstance(hits, int) or isinstance(hits, bool):
        raise ValueError(f'Europe PMC answered {query!r} with no hit count')
    return hits


def _record(result: Mapping[str, object]) -> Record:
    """One result as a ``Record``.

    The PMID is read off ``pmid`` alone, never ``id``: a result the index carries under no PubMed id
    states a preprint's or a PMC deposit's own id there, and a record carrying that as its ``pmid``
    would send a caller to PubMed for a record PubMed does not hold.
    """
    return Record(
        pmid=_field(result, 'pmid'),
        title=_field(result, 'title'),
        authors=_authors(result),
        journal=_journal(result),
        year=_year(result),
        doi=_field(result, 'doi'),
        abstract=_field(result, 'abstractText'),
        pmcid=_field(result, 'pmcid'),
    )


def _authors(result: Mapping[str, object]) -> tuple[str, ...]:
    """The record's authors, one per entry in the index's own order, empty where it names none.

    Read off ``authorList`` rather than the pre-joined ``authorString``: splitting that back apart
    invents boundaries, since a name may carry the comma the join uses. A group author states a
    ``collectiveName`` and no ``fullName`` — reading only the latter drops it from the byline.
    """
    author_list = result.get('authorList')
    authors = author_list.get('author') if isinstance(author_list, Mapping) else None
    if not isinstance(authors, list):
        return ()
    named = (
        _field(author, 'fullName') or _field(author, 'collectiveName')
        for author in authors
        if isinstance(author, Mapping)
    )
    return tuple(name for name in named if name)


def _field(result: Mapping[str, object], key: str) -> str:
    value = result.get(key)
    return value if isinstance(value, str) else ''


def _journal(result: Mapping[str, object]) -> str:
    """The containing work: the journal for a journal record, the book for a Bookshelf chapter."""
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
