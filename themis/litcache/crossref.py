"""Crossref → `PubmedArticle` mapping for DOI-only papers (no PubMed record).

When a paper has a DOI but no PMID, the efetch path finds nothing, so its
bibliographic metadata comes from Crossref instead. `metadata.pb` stays a
pubmed_proto `PubmedArticle`, so a Crossref `works` response is mapped by hand onto a
`PubmedArticle` — bespoke and lossy: only the fields Crossref reliably carries are
mapped (title, journal, volume/issue, authors, issue date, DOI). PubMed-specific
fields with no Crossref source are left at their honest empty/unspecified values —
the record is publisher-supplied, not MEDLINE-indexed (`status=Publisher`), and
carries no PMID.

The DOI populates the record's `article_id_list` and the manifest `ExternalIds`. The
publisher is returned alongside, for the orchestrator to build the manifest's
`Licensed` access when the paper is not free-to-read.
"""

from __future__ import annotations

import dataclasses
import datetime
import urllib.parse
from collections.abc import Mapping, Sequence

import httpx
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.common import constants
from themis.litcache import efetch
from themis.litcache.models import litcache_pb2

_CROSSREF_URL = 'https://api.crossref.org/works'

# Crossref date fields in preference order: the canonical issue date first.
_DATE_FIELDS = ('issued', 'published', 'published-online', 'published-print')


@dataclasses.dataclass(frozen=True)
class CrossrefResult:
    """The bibliographic outputs of resolving one DOI-only paper via Crossref.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (a mapped `PubmedArticle`).
        external_ids: The cross-ids for the manifest (DOI only — Crossref carries
            no PMID/PMCID).
        publisher: The Crossref `publisher`, for the manifest `Licensed` access;
            `None` if absent.
    """

    metadata: bytes
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None


def _first(value: object) -> str | None:
    """First entry of a Crossref string array (`title`, `container-title`)."""
    if isinstance(value, Sequence) and not isinstance(value, str) and value:
        first = value[0]
        return first if isinstance(first, str) else None
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _pub_date(work: Mapping[str, object], doi: str) -> timestamp_pb2.Timestamp:
    """Build the issue-date `Timestamp` from the first present Crossref date field.

    Crossref dates are `{"date-parts": [[year, month?, day?]]}`; month/day default
    to 1. `FromDatetime` reads the naive datetime as UTC.
    """
    for field in _DATE_FIELDS:
        candidate = work.get(field)
        if not isinstance(candidate, Mapping):
            continue
        parts = candidate.get('date-parts')
        if not (isinstance(parts, Sequence) and parts and isinstance(parts[0], Sequence) and parts[0]):
            continue
        ymd = parts[0]
        # Crossref emits `date-parts: [[null]]` for a record with no known date; a
        # null part is as good as an absent field, so skip to the next candidate
        # rather than let `int(None)` abort the whole fallback.
        if not all(isinstance(v, int) for v in ymd[:3]):
            continue
        year, month, day = ymd[0], ymd[1] if len(ymd) > 1 else 1, ymd[2] if len(ymd) > 2 else 1
        pub_date = timestamp_pb2.Timestamp()
        pub_date.FromDatetime(datetime.datetime(year, month, day))  # noqa: DTZ001 — naive read as UTC
        return pub_date
    raise ValueError(f'Crossref work {doi} has no usable publication date')


def _author(a: Mapping[str, object]) -> pubmed_pb2.Author | None:
    """Map one Crossref author, or None when it names nobody.

    A personal author has `family`/`given`; a consortium or organization is a
    name-only `{"name": "…"}` (DDD, gnomAD, GTEx, ClinGen …), which maps to the
    proto's `collective_name` rather than being dropped.
    """
    family, given = _as_str(a.get('family')), _as_str(a.get('given'))
    if family or given:
        return pubmed_pb2.Author(last_name=family, fore_name=given)
    name = _as_str(a.get('name'))
    if name:
        return pubmed_pb2.Author(collective_name=pubmed_pb2.CollectiveName(value=name))
    return None


def _author_list(authors: object) -> pubmed_pb2.AuthorList | None:
    """Map Crossref `author[]` onto an `AuthorList`, preserving collective authors."""
    if not isinstance(authors, Sequence) or not authors:
        return None
    mapped: list[pubmed_pb2.Author] = []
    for a in authors:
        if isinstance(a, Mapping) and (author := _author(a)) is not None:
            mapped.append(author)
    if not mapped:
        return None
    return pubmed_pb2.AuthorList(type=pubmed_pb2.AuthorList.TYPE_AUTHORS, author=mapped)


def from_crossref_work(work: Mapping[str, object]) -> CrossrefResult:
    """Map a Crossref `works` message onto a `PubmedArticle` + cross-ids + publisher.

    Args:
        work: The `message` object of a Crossref `works` response.

    Returns:
        The `CrossrefResult`.

    Raises:
        ValueError: If the work has no DOI, no title, or no usable issue date —
            a bibliographic record without these is degenerate, not `unknown`.
    """
    doi = work.get('DOI')
    if not isinstance(doi, str):
        raise ValueError('Crossref work has no DOI')
    title = _first(work.get('title'))
    if not title:  # absent or empty-string title — both degenerate, not `unknown`
        raise ValueError(f'Crossref work {doi} has no title')

    article = pubmed_pb2.PubmedArticle(
        medline_citation=pubmed_pb2.MedlineCitation(
            # Publisher-supplied metadata, not MEDLINE-indexed; no PMID exists.
            status=pubmed_pb2.MedlineCitation.STATUS_PUBLISHER,
            pmid=pubmed_pb2.Pmid(version='1'),
            article=pubmed_pb2.Article(
                pub_model=pubmed_pb2.Article.PUB_MODEL_UNSPECIFIED,
                journal=pubmed_pb2.Journal(
                    journal_issue=pubmed_pb2.JournalIssue(
                        cited_medium=pubmed_pb2.JournalIssue.CITED_MEDIUM_UNSPECIFIED,
                        pub_date=_pub_date(work, doi),
                        volume=_as_str(work.get('volume')),
                        issue=_as_str(work.get('issue')),
                    ),
                    title=_first(work.get('container-title')),
                ),
                article_title=pubmed_pb2.ArticleTitle(value=title),
                author_list=_author_list(work.get('author')),
            ),
        ),
        pubmed_data=pubmed_pb2.PubmedData(
            # Crossref carries no NLM PublicationStatus; honest empty, not invented.
            publication_status='',
            article_id_list=[pubmed_pb2.ArticleId(id_type=pubmed_pb2.ArticleId.ID_TYPE_DOI, value=doi)],
        ),
    )
    return CrossrefResult(
        metadata=efetch.to_canonical_bytes(article),
        external_ids=litcache_pb2.ExternalIds(doi=doi),
        publisher=_as_str(work.get('publisher')),
    )


async def fetch_crossref(doi: str, *, http_client: httpx.AsyncClient) -> Mapping[str, object]:
    """Fetch a Crossref `works` message for one DOI.

    Args:
        doi: The DOI to resolve (raw, slashes intact).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The `message` object of the Crossref response.

    Raises:
        httpx.HTTPStatusError: If Crossref returns a non-2xx status (e.g. 404 for
            an unknown DOI — the caller's `unknown`).
    """
    # DOIs contain '/' (kept) and can carry reserved chars (?, #, ;) that would otherwise
    # reparse the path — percent-encode the suffix so the request means what it says.
    quoted = urllib.parse.quote(doi, safe='/')
    response = await http_client.get(f'{_CROSSREF_URL}/{quoted}', params={'mailto': constants.CONTACT_EMAIL})
    response.raise_for_status()
    return response.json()['message']


async def resolve(doi: str, *, http_client: httpx.AsyncClient) -> CrossrefResult:
    """Resolve one DOI to `metadata.pb` + cross-ids + publisher via Crossref."""
    return from_crossref_work(await fetch_crossref(doi, http_client=http_client))
