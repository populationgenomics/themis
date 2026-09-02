"""PubMed adapter: the batched PMID → whole-record lookup, riding litcache's efetch path.

The call and the XML→proto conversion are litcache's (``themis.litcache.efetch``) — the same path
that resolves a paper's canonical ``metadata.pb`` — so the record a triage read sees is the record
the store keeps. This module owns the service-side conventions: a single attempt (the caller's retry
helper owns backoff, as for every evidence upstream), faults placed on the shared evidence taxonomy,
and absence stated as an explicit outcome rather than an omission the caller has to notice.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

import httpx2
from pubmed_proto import pubmed_pb2

from themis.litcache import efetch
from themis.services.evidence import errors

_SOURCE = 'PubMed'


class FetchedArticles(NamedTuple):
    """One batch lookup's outcome: every requested PMID lands in exactly one of the three."""

    articles: list[pubmed_pb2.PubmedArticle]  # journal records, whole
    book_articles: list[pubmed_pb2.PubmedBookArticle]  # book records, whole
    pmids_without_record: list[str]  # in request order


async def articles_by_pmid(pmids: Sequence[str], *, http_client: httpx2.AsyncClient) -> FetchedArticles:
    """Resolve a batch of PMIDs to their whole PubMed records, one efetch call for the batch.

    Absence is read off what the answer omits, so every record that came back has to be accounted
    for against a requested PMID: one under a PMID the batch did not ask for would leave a requested
    PMID looking like one the index holds nothing under.

    Args:
        pmids: The PMIDs, already keyed (``pmids.pmid_key``) and distinct.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The records the index holds, whole and in the kind it indexes each PMID under — journal or
        book — and the requested PMIDs it holds nothing under, whether never indexed or since
        deleted (the set's ``DeleteCitation``): the index states the two alike.

    Raises:
        errors.InvalidRequestError: If efetch refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If efetch returns a 429 or a 5xx.
        ValueError: If the answer is not a `PubmedArticleSet`, carries a record or a deletion under
            a PMID the batch did not ask for, answers one PMID twice, or carries a record that does
            not convert (``efetch.parse_set``).
    """
    try:
        xml = await efetch.fetch(pmids, http_client=http_client, attempts=1)
    except httpx2.HTTPStatusError as e:
        errors.raise_for_status(e.response, upstream=_SOURCE, subject=f'efetch of {len(pmids)} PMIDs')
        raise  # unreachable: raise_for_status raises on every non-2xx response
    parsed = efetch.parse_set(xml)
    answered = parsed.articles.keys() | parsed.book_articles.keys()
    if unasked := sorted((answered | parsed.deleted_pmids) - set(pmids)):
        raise ValueError(f'{_SOURCE} efetch answered under PMIDs the batch did not ask for: {unasked}')
    return FetchedArticles(
        articles=list(parsed.articles.values()),
        book_articles=list(parsed.book_articles.values()),
        pmids_without_record=[pmid for pmid in pmids if pmid not in answered],
    )
