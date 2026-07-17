"""PubMed efetch: fetch a PMID batch and parse the `PubmedArticleSet` into `metadata.pb` + cross-ids.

`metadata.pb` is a serialized pubmed_proto `PubmedArticle`. efetch returns `PubmedArticle`
XML, which pubmed_proto's generated converter turns into the proto record directly:

    efetch XML → xml_converter (XML→proto) → PubmedArticle proto → SerializeToString

The cross-ids (DOI↔PMID↔PMCID) fall out of the record's own `pubmed_data.article_id_list`
— no separate id-conversion call — and are harvested into the litcache manifest's
`ExternalIds`. A paper with a DOI but no PubMed record is resolved from Crossref
(`themis.litcache.crossref`) instead. Batch-first: efetch serves a whole PMID batch in one
`PubmedArticleSet` — but over HTTP GET, so the batch is bounded (`_MAX_GET_IDS`); NCBI's
guidance is to POST the id list above ~200 UIDs, which this GET path does not implement.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import httpx
import pubmed_proto
from lxml import etree

from themis.common import constants
from themis.litcache.models import litcache_pb2

_EFETCH_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi'
# eutils etiquette: identify the tool + a contact for rate-limit/abuse follow-up.
_TOOL = 'themis-litcache'

# NCBI's ceiling for an inline `id=` list on a GET; a larger batch must POST via the
# history server (unimplemented). Fail loud rather than send a truncated/over-long URL.
_MAX_GET_IDS = 200

_ArticleId = pubmed_proto.pubmed_pb2.ArticleId

# efetch is NCBI over HTTPS (trusted), but this is a public parser over externally-fetched
# bytes: disable entity resolution + network/DTD loading so hostile XML can't trigger
# entity-expansion DoS or external-entity file disclosure.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


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


async def fetch(pmids: Sequence[str], *, http_client: httpx.AsyncClient) -> bytes:
    """Fetch the efetch `PubmedArticleSet` XML for a batch of PMIDs.

    Args:
        pmids: The PMIDs to fetch in one efetch call.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The raw `PubmedArticleSet` XML bytes.

    Raises:
        ValueError: If `pmids` is empty, or exceeds `_MAX_GET_IDS` (the GET path's
            inline-id ceiling — a larger batch needs the unimplemented POST path).
        httpx.HTTPStatusError: If efetch returns a non-2xx status.
    """
    if not pmids:
        raise ValueError('efetch.fetch requires at least one PMID')
    if len(pmids) > _MAX_GET_IDS:
        raise ValueError(
            f'efetch.fetch got {len(pmids)} PMIDs; the GET path caps at {_MAX_GET_IDS} (POST unimplemented)'
        )
    response = await http_client.get(
        _EFETCH_URL,
        params={
            'db': 'pubmed',
            'id': ','.join(pmids),
            'retmode': 'xml',
            'tool': _TOOL,
            'email': constants.CONTACT_EMAIL,
        },
    )
    response.raise_for_status()
    return response.content


def parse_response(xml: bytes) -> dict[str, ResolvedMetadata]:
    """Parse an efetch `PubmedArticleSet` into per-PMID resolved metadata.

    Args:
        xml: The raw efetch response bytes (`retmode=xml`).

    Returns:
        A mapping of PMID → `ResolvedMetadata` for each `PubmedArticle` in the set.
        A PMID that efetch did not return is simply absent — the caller's
        `unknown`, never an invented record.

    Raises:
        ValueError: If the root element is not `PubmedArticleSet` (an unexpected
            response shape, e.g. an eutils error document — not a silent miss).
        lxml.etree.XMLSyntaxError: If `xml` is not well-formed.
    """
    root = etree.fromstring(xml, _PARSER)
    if root.tag != 'PubmedArticleSet':
        raise ValueError(f'expected a PubmedArticleSet, got <{root.tag}>')
    resolved: dict[str, ResolvedMetadata] = {}
    for element in root.findall('PubmedArticle'):
        article = pubmed_proto.xml_converter.PubmedArticle(element)
        pmid = article.medline_citation.pmid.value
        if not pmid:
            raise ValueError('PubmedArticle has no PMID value')
        resolved[pmid] = ResolvedMetadata(
            metadata=to_canonical_bytes(article),
            external_ids=_harvest_external_ids(article),
        )
    return resolved


async def resolve(pmids: Sequence[str], *, http_client: httpx.AsyncClient) -> dict[str, ResolvedMetadata]:
    """Resolve a batch of PMIDs to `metadata.pb` + cross-ids via efetch."""
    xml = await fetch(pmids, http_client=http_client)
    return parse_response(xml)
