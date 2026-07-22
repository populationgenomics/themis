"""OpenAlex batch resolution: DOI → cross-ids + a bibliographic record.

The batchable DOI resolver for the papers idconv cannot route — idconv is PMC-scoped,
so a DOI in PubMed-but-not-PMC (subscription journals, the bulk of a DOI-only seed) or
not in PubMed at all (preprints) never reaches efetch through it. OpenAlex's works
endpoint takes up to `_MAX_IDS` DOIs in one filter query and returns each work's
cross-ids (`pmid`, `pmcid`) plus title / date / venue.

Two surfaces over one fetch, matching the litfetch/litcache seam: the cross-ids on
`OpenAlexWork` are the id-resolution half (a batched litfetch `Resolver` eventually
owns this — litfetch is not a bibliographic-metadata client), and `to_pubmed_article`
is the bibliographic half litcache owns. The ladder (`themis.litcache.resolve`) prefers
the id half — routing an OpenAlex-discovered `pmid` back into batched efetch for a
PubMed-native record — and falls to the record half only for the no-`pmid` residual.

Identifiers come back as URLs (`https://doi.org/…`, `https://pubmed.ncbi.nlm.nih.gov/…`);
they are stripped to the bare id. A DOI OpenAlex does not know is absent from the result
— the caller's `unknown`, never invented.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from collections.abc import Mapping, Sequence

import httpx
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.common import constants
from themis.litcache import efetch

_WORKS_URL = 'https://api.openalex.org/works'
# OpenAlex accepts up to 50 values in one filter OR-list; a larger batch is chunked.
_MAX_IDS = 50
# The fields the ladder needs: cross-ids, title, date, venue, and the preprint flag.
_SELECT = 'doi,display_name,publication_date,ids,type,primary_location'

_DOI_PREFIX = 'https://doi.org/'
_PMID_PREFIX = 'https://pubmed.ncbi.nlm.nih.gov/'
_PMCID_PREFIX = 'https://www.ncbi.nlm.nih.gov/pmc/articles/'


@dataclasses.dataclass(frozen=True)
class OpenAlexWork:
    """One work's OpenAlex record — cross-ids plus the fields a `PubmedArticle` needs.

    Attributes:
        doi: The queried DOI (bare, slashes intact).
        pmid: The bare PMID, or `None` (the paper is not in PubMed).
        pmcid: The bare PMCID, or `None` (idconv is the authoritative pmcid source;
            OpenAlex often omits it even for PMC papers).
        title: The work title.
        publication_date: The ISO `YYYY-MM-DD` publication date, or `None`.
        journal: The container/source title, or `None`.
        publisher: The source's host organization, or `None`.
        is_preprint: True when OpenAlex types the work as a preprint.
    """

    doi: str
    pmid: str | None
    pmcid: str | None
    title: str | None
    publication_date: str | None
    journal: str | None
    publisher: str | None
    is_preprint: bool


def _text(value: object) -> str | None:
    """Return `value` when it is a non-empty string, else `None`."""
    return value if isinstance(value, str) and value else None


def _strip(value: object, prefix: str) -> str | None:
    """Return the bare id (prefix stripped) for a string URL id, else `None`."""
    text = _text(value)
    return None if text is None else text.removeprefix(prefix).rstrip('/')


def _work(record: Mapping[str, object]) -> OpenAlexWork | None:
    """Map one OpenAlex work record to an `OpenAlexWork`, or `None` without a DOI."""
    doi = _strip(record.get('doi'), _DOI_PREFIX)
    if doi is None:
        return None
    ids = record.get('ids')
    ids = ids if isinstance(ids, Mapping) else {}
    location = record.get('primary_location')
    source = location.get('source') if isinstance(location, Mapping) else None
    source = source if isinstance(source, Mapping) else {}
    return OpenAlexWork(
        doi=doi,
        pmid=_strip(ids.get('pmid'), _PMID_PREFIX),
        pmcid=_strip(ids.get('pmcid'), _PMCID_PREFIX),
        title=_text(record.get('display_name')),
        publication_date=_text(record.get('publication_date')),
        journal=_text(source.get('display_name')),
        publisher=_text(source.get('host_organization_name')),
        is_preprint=record.get('type') == 'preprint',
    )


async def fetch(dois: Sequence[str], *, http_client: httpx.AsyncClient) -> bytes:
    """Fetch the works page for a batch of DOIs (one OR-filter query).

    Args:
        dois: The DOIs to resolve in one call (at most `_MAX_IDS`).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The raw works-response JSON bytes.

    Raises:
        ValueError: If `dois` is empty or exceeds `_MAX_IDS`.
        httpx.HTTPStatusError: If OpenAlex returns a non-2xx status.
    """
    if not dois:
        raise ValueError('openalex.fetch requires at least one DOI')
    if len(dois) > _MAX_IDS:
        raise ValueError(f'openalex.fetch got {len(dois)} DOIs; the OR-filter caps at {_MAX_IDS} per call')
    response = await http_client.get(
        _WORKS_URL,
        params={
            'filter': 'doi:' + '|'.join(dois),
            'select': _SELECT,
            'per-page': str(_MAX_IDS),
            'mailto': constants.CONTACT_EMAIL,
        },
    )
    response.raise_for_status()
    return response.content


def parse_response(payload: bytes) -> dict[str, OpenAlexWork]:
    """Parse a works-response payload into a DOI → `OpenAlexWork` map.

    A result without a DOI is skipped. A DOI absent from the response (unknown to
    OpenAlex) is simply not in the map — the caller's `unknown`.

    Args:
        payload: The raw works-response JSON bytes.

    Returns:
        A mapping of bare DOI → `OpenAlexWork`.

    Raises:
        ValueError: If the payload is not a works response (`results` absent).
        json.JSONDecodeError: If `payload` is not valid JSON.
    """
    document = json.loads(payload)
    if not isinstance(document, Mapping) or not isinstance(document.get('results'), Sequence):
        raise ValueError('expected an OpenAlex works response with a `results` list')
    works: dict[str, OpenAlexWork] = {}
    for record in document['results']:
        if isinstance(record, Mapping) and (work := _work(record)) is not None:
            works[work.doi] = work
    return works


async def resolve(dois: Sequence[str], *, http_client: httpx.AsyncClient) -> dict[str, OpenAlexWork]:
    """Resolve a batch of DOIs to their OpenAlex works, chunked to the OR-filter cap."""
    works: dict[str, OpenAlexWork] = {}
    for start in range(0, len(dois), _MAX_IDS):
        chunk = dois[start : start + _MAX_IDS]
        works.update(parse_response(await fetch(chunk, http_client=http_client)))
    return works


def _pub_date(work: OpenAlexWork) -> timestamp_pb2.Timestamp:
    """Build the issue-date `Timestamp` from the ISO publication date (read as UTC)."""
    if work.publication_date is None:
        raise ValueError(f'OpenAlex work {work.doi} has no publication date')
    parsed = datetime.datetime.strptime(work.publication_date, '%Y-%m-%d')  # noqa: DTZ007 — naive read as UTC
    return _timestamp(parsed)


def _timestamp(value: datetime.datetime) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(value)
    return stamp


def to_pubmed_article(work: OpenAlexWork) -> bytes:
    """Build the canonical `metadata.pb` bytes for a work with no PubMed record.

    The residual path — a DOI OpenAlex resolved but that carries no `pmid` (a preprint
    or otherwise non-PubMed work), so there is no efetch record to prefer. The status
    is `PUBLISHER` (publisher-supplied, not MEDLINE-indexed). Author names are omitted:
    OpenAlex gives an unstructured `display_name`, not the family/given the proto
    models, so a split would manufacture structure the source does not carry.

    Args:
        work: The resolved OpenAlex work.

    Returns:
        The serialized `PubmedArticle` bytes.

    Raises:
        ValueError: If the work has no title or no usable publication date — a record
            without these is degenerate, not `unknown`.
    """
    if not work.title:
        raise ValueError(f'OpenAlex work {work.doi} has no title')
    article_ids = [pubmed_pb2.ArticleId(id_type=pubmed_pb2.ArticleId.ID_TYPE_DOI, value=work.doi)]
    if work.pmcid is not None:
        article_ids.append(pubmed_pb2.ArticleId(id_type=pubmed_pb2.ArticleId.ID_TYPE_PMC, value=work.pmcid))
    article = pubmed_pb2.PubmedArticle(
        medline_citation=pubmed_pb2.MedlineCitation(
            status=pubmed_pb2.MedlineCitation.STATUS_PUBLISHER,
            pmid=pubmed_pb2.Pmid(version='1'),
            article=pubmed_pb2.Article(
                pub_model=pubmed_pb2.Article.PUB_MODEL_UNSPECIFIED,
                journal=pubmed_pb2.Journal(
                    journal_issue=pubmed_pb2.JournalIssue(
                        cited_medium=pubmed_pb2.JournalIssue.CITED_MEDIUM_UNSPECIFIED,
                        pub_date=_pub_date(work),
                    ),
                    title=work.journal,
                ),
                article_title=pubmed_pb2.ArticleTitle(value=work.title),
            ),
        ),
        pubmed_data=pubmed_pb2.PubmedData(publication_status='', article_id_list=article_ids),
    )
    return efetch.to_canonical_bytes(article)
