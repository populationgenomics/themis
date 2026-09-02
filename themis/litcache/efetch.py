"""PubMed efetch: fetch a PMID batch and parse the `PubmedArticleSet` it answers with.

efetch answers a batch with one `PubmedArticleSet`, each record in the kind PubMed indexes the
PMID under — `PubmedArticle` for a journal record, `PubmedBookArticle` for a book record — and
pubmed_proto's generated converter turns each into its proto record directly:

    efetch XML → xml_converter (XML→proto) → PubmedArticle | PubmedBookArticle, keyed by PMID

A trailing `DeleteCitation` names PMIDs whose records PubMed has withdrawn: nothing is indexed
under them, and the set states so rather than leaving it to be noticed as an omission.
`parse_set` is that parse, both kinds; `parse_response` is the store's view of it — journal
records only, as `metadata.pb` (a serialized `PubmedArticle`) plus the cross-ids
(DOI↔PMID↔PMCID) that fall out of the record's own `pubmed_data.article_id_list`, harvested
into the litcache manifest's `ExternalIds` with no separate id-conversion call. A paper with a
DOI but no PubMed record is resolved from Crossref (`themis.litcache.crossref`) instead.
Batch-first: the id list is POSTed in the request body so one path serves any batch size
(NCBI's GET path caps the inline `id=` list near 200 UIDs; POST has no such ceiling).
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Callable, Container, Sequence
from typing import NamedTuple

import httpx2
import pubmed_proto
from lxml import etree

from themis.common import constants
from themis.litcache.models import litcache_pb2

_EFETCH_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi'
# eutils etiquette: identify the tool + a contact for rate-limit/abuse follow-up.
_TOOL = 'themis-litcache'

# eutils returns transient 5xx/429 and drops connections under load; resolution runs the whole
# corpus through one shard, so an unretried blip would abort the entire run. Retry with
# exponential backoff (2s, 4s, 8s, 16s) before giving up.
_MAX_FETCH_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 2.0

_ArticleId = pubmed_proto.pubmed_pb2.ArticleId

# efetch is NCBI over HTTPS (trusted), but this is a public parser over externally-fetched
# bytes: disable entity resolution + network/DTD loading so hostile XML can't trigger
# entity-expansion DoS or external-entity file disclosure. Comments and processing
# instructions are dropped so a set's members are elements alone.
_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, remove_comments=True, remove_pis=True
)
# A PMID as PubMed states one: digits, no leading zero. The parse reads the index's statement and
# refuses a non-canonical one rather than normalising it.
_CANONICAL_PMID = re.compile(r'[1-9][0-9]*')


@dataclasses.dataclass(frozen=True)
class ResolvedMetadata:
    """The bibliographic outputs of resolving one paper.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (serialized pubmed_proto
            `PubmedArticle`).
        external_ids: The cross-ids harvested from the record, for the manifest.
    """

    metadata: bytes
    external_ids: litcache_pb2.ExternalIds


def _harvest_external_ids(article: pubmed_proto.pubmed_pb2.PubmedArticle) -> litcache_pb2.ExternalIds:
    """Harvest the manifest cross-ids from a record's own id list.

    The PMID is authoritative from `MedlineCitation`; the DOI and PMCID come from
    `PubmedData.ArticleIdList` (the article's own ids — reference-list citation ids
    live under `reference_list`, not here). PII is not a manifest `ExternalIds`
    scheme, so it is dropped.
    """
    doi: str | None = None
    pmcid: str | None = None
    if article.HasField('pubmed_data'):
        for article_id in article.pubmed_data.article_id_list:
            if article_id.id_type == _ArticleId.ID_TYPE_DOI:
                doi = article_id.value
            elif article_id.id_type == _ArticleId.ID_TYPE_PMC:
                pmcid = article_id.value
    return litcache_pb2.ExternalIds(
        doi=doi,
        pmid=article.medline_citation.pmid.value,
        pmcid=pmcid,
    )


def to_canonical_bytes(article: pubmed_proto.pubmed_pb2.PubmedArticle) -> bytes:
    """Serialize a `PubmedArticle` proto to litcache's canonical `metadata.pb` bytes.

    The serialized proto is the at-rest format (docs/design/proto.md): write-once,
    overwritten from a fresh conversion, never read-modify-written.
    """
    return article.SerializeToString()


async def fetch(pmids: Sequence[str], *, http_client: httpx2.AsyncClient, attempts: int = _MAX_FETCH_ATTEMPTS) -> bytes:
    """Fetch the efetch `PubmedArticleSet` XML for a batch of PMIDs.

    Args:
        pmids: The PMIDs to fetch in one efetch call.
        http_client: The async HTTP client (caller owns its lifecycle).
        attempts: Tries for a transient failure (429, 5xx, dropped connection) before it escapes.
            The default suits an unattended batch run; a caller whose own retry policy owns backoff
            — an rpc handler under a deadline — passes 1.

    Returns:
        The raw `PubmedArticleSet` XML bytes.

    Raises:
        ValueError: If `pmids` is empty or `attempts` is not positive.
        httpx2.HTTPStatusError: If efetch returns a non-retryable status (a 4xx
            other than 429), or keeps returning a retryable one past the retry budget.
        httpx2.TransportError: If the connection keeps failing past the retry budget.
    """
    if not pmids:
        raise ValueError('efetch.fetch requires at least one PMID')
    if attempts < 1:
        raise ValueError(f'attempts must be positive, got {attempts}')
    params = {
        'db': 'pubmed',
        'id': ','.join(pmids),
        'retmode': 'xml',
        'tool': _TOOL,
        'email': constants.CONTACT_EMAIL,
    }
    # POST the id list in the body: NCBI caps an inline `id=` list on a GET near 200
    # UIDs, but POST has no such ceiling, so one path serves any batch size.
    for attempt in range(attempts):
        try:
            response = await http_client.post(_EFETCH_URL, data=params)
            response.raise_for_status()
            return response.content
        except (httpx2.TransportError, httpx2.HTTPStatusError) as exc:
            if not _is_retryable(exc) or attempt == attempts - 1:
                raise
        await asyncio.sleep(_RETRY_BASE_DELAY_SECONDS * 2**attempt)
    raise AssertionError('unreachable: the retry loop returns or raises on the final attempt')


def _is_retryable(exc: httpx2.TransportError | httpx2.HTTPStatusError) -> bool:
    """Whether an efetch failure is transient: a transport error, HTTP 429, or 5xx."""
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return True


class ParsedSet(NamedTuple):
    """One efetch `PubmedArticleSet`: its records of each kind, keyed by the PMID each states.

    A PMID keys one record across both kinds — the set is the index's answer for a batch, and one
    identifier names one record in it. Insertion order is the answer's. `deleted_pmids` are the
    PMIDs the set's `DeleteCitation` names: records PubMed has withdrawn, so nothing is indexed
    under them; none keys a record here.
    """

    articles: dict[str, pubmed_proto.pubmed_pb2.PubmedArticle]
    book_articles: dict[str, pubmed_proto.pubmed_pb2.PubmedBookArticle]
    deleted_pmids: frozenset[str]


def parse_set(xml: bytes) -> ParsedSet:
    """Parse an efetch `PubmedArticleSet` into its journal and book records, keyed by PMID.

    Args:
        xml: The raw efetch response bytes (`retmode=xml`).

    Returns:
        Every `PubmedArticle` and `PubmedBookArticle` in the set, under the PMID its own record
        states, and the PMIDs its `DeleteCitation` names. A PMID efetch did not answer is absent
        from all three — the caller's `unknown`, never an invented record.

    Raises:
        ValueError: If the root element is not `PubmedArticleSet` (an unexpected response shape,
            e.g. an eutils error document — not a silent miss); if the set carries a member that is
            neither a record of the two kinds nor the deletion notice; if a record or deletion
            states a PMID that is not canonical (digits, no leading zero), or one the set already
            answered; or if a record does not convert as pubmed_proto's schema states it — a
            truncated or reordered record, named by the PMID it states where it states one.
        lxml.etree.XMLSyntaxError: If `xml` is not well-formed.
    """
    root = etree.fromstring(xml, _PARSER)
    if root.tag != 'PubmedArticleSet':
        raise ValueError(f'expected a PubmedArticleSet, got <{root.tag}>')
    articles: dict[str, pubmed_proto.pubmed_pb2.PubmedArticle] = {}
    book_articles: dict[str, pubmed_proto.pubmed_pb2.PubmedBookArticle] = {}
    deleted: set[str] = set()
    for element in root:
        match element.tag:
            case 'PubmedArticle':
                article = _converted(element, pubmed_proto.xml_converter.PubmedArticle, 'MedlineCitation/PMID')
                pmid = article.medline_citation.pmid.value
                articles[_canonical_unanswered_pmid(pmid, element.tag, articles, book_articles, deleted)] = article
            case 'PubmedBookArticle':
                book = _converted(element, pubmed_proto.xml_converter.PubmedBookArticle, 'BookDocument/PMID')
                pmid = book.book_document.pmid.value
                book_articles[_canonical_unanswered_pmid(pmid, element.tag, articles, book_articles, deleted)] = book
            case 'DeleteCitation':
                for stated in element.findall('PMID'):
                    pmid = stated.text or ''
                    deleted.add(_canonical_unanswered_pmid(pmid, element.tag, articles, book_articles, deleted))
            case _:
                raise ValueError(
                    f'unexpected <{element.tag}> in a PubmedArticleSet; a member is a record of one of two kinds '
                    'or the DeleteCitation notice'
                )
    return ParsedSet(articles=articles, book_articles=book_articles, deleted_pmids=frozenset(deleted))


def _converted[T](element: etree._Element, convert: Callable[[etree._Element], T], pmid_path: str) -> T:
    """One record through the converter; a failure is named by the PMID the raw element states."""
    try:
        return convert(element)
    except (IndexError, KeyError, OverflowError, ValueError) as e:
        stated = (element.findtext(pmid_path) or '').strip()
        where = f'PMID {stated}' if stated else 'no PMID stated'
        raise ValueError(
            f'<{element.tag}> ({where}) does not convert as the pubmed_proto schema states it: {e!r}'
        ) from e


def _canonical_unanswered_pmid(pmid: str, tag: str, *answered: Container[str]) -> str:
    if not _CANONICAL_PMID.fullmatch(pmid):
        raise ValueError(
            f'the index states a PMID that is not canonical (digits, no leading zero) in a {tag}: {pmid!r}'
        )
    if any(pmid in pmids for pmids in answered):
        raise ValueError(f'PMID {pmid} is answered twice in one PubmedArticleSet')
    return pmid


def parse_response(xml: bytes) -> dict[str, ResolvedMetadata]:
    """Parse an efetch `PubmedArticleSet` into per-PMID resolved metadata (see `parse_set`).

    Journal records only: `metadata.pb` is a `PubmedArticle`, so a book record in the set resolves
    nothing here and its PMID is absent from the result, as a deleted PMID is.
    """
    return {
        pmid: ResolvedMetadata(metadata=to_canonical_bytes(article), external_ids=_harvest_external_ids(article))
        for pmid, article in parse_set(xml).articles.items()
    }


async def resolve(pmids: Sequence[str], *, http_client: httpx2.AsyncClient) -> dict[str, ResolvedMetadata]:
    """Resolve a batch of PMIDs to `metadata.pb` + cross-ids via efetch."""
    xml = await fetch(pmids, http_client=http_client)
    return parse_response(xml)
