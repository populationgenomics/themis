"""The OA branch: fetch a paper's full-text XML source and its access terms.

The conversion rule is "if the paper is in PMC / otherwise OA and XML is obtainable
via the litfetch ladder, convert the XML with litdown (`xml-faithful`); otherwise
render the seed Docling json (`pdf-derived`)." This module is the OA side: it walks
the litfetch ladder, and on an XML body returns the bytes (the `convert.convert_jats`
input) together with the provenance (`Source.kind`, `origin_url`) and the
licence/access terms litfetch read from those bytes (mapped by `from_source_metadata`).

Detection is fetch-driven: the gate is "the ladder served an XML body", not a
separate access flag — `extract_source_metadata` returns `access=None` for an
artifact whose licence is read from the bytes, so an access-string predicate is not
a reliable branch driver.

A non-XML body (a PDF the ladder served, which litfetch would OCR) does not take the
OA branch: litcache already holds the seed Docling json, a higher-fidelity source
than OCR, so `fetch_oa_source` returns `None` and the caller renders that instead.
Only an XML body opens the OA branch.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Sequence

import litfetch
from litfetch import artifacts, ids, resolvers, source_metadata

from themis.litcache import identity
from themis.litcache.models import litcache_pb2

# litdown converts both dialects; either body media type is "XML obtainable" for
# the xml-faithful branch.
_XML_MEDIA_TYPES = frozenset({artifacts.JATS_XML, artifacts.ELSEVIER_XML})

# Identity schemes litfetch's `ArticleIds` can fetch against; `pii`/`binhash` have
# no litfetch equivalent, so a paper carrying only those is never OA-fetchable.
_FETCHABLE_SCHEMES = frozenset({'doi', 'pmid', 'pmcid'})

# litfetch's access string is the artifact `open-access` flag or Unpaywall's raw
# `oa_status`. These tokens mark the work openly readable → `free-to-read`;
# `closed` and any unrecognised token do not.
_FREE_TO_READ_TOKENS = frozenset({'open-access', 'gold', 'green', 'hybrid', 'bronze'})

# litfetch's `basis` is `artifact` (read from the fetched bytes) or the asserting
# authority's name (`unpaywall`, …); litcache collapses every authority to
# `asserted`, the only non-artifact basis its schema models.
_ARTIFACT_BASIS = 'artifact'


@dataclasses.dataclass(frozen=True)
class AccessTerms:
    """The licence-bearing fields of a `Source`, derived from litfetch.

    Attributes:
        licence: The raw licence string as litfetch returned it (empty when
            litfetch found none — read-time policy treats empty as the
            conservative unknown, never as permission).
        licence_basis: `LICENCE_BASIS_ARTIFACT` (from the bytes) or
            `LICENCE_BASIS_ASSERTED` (from an authority).
        access: The `Access` oneof — `free_to_read` or `unknown` from litfetch
            alone (`licensed` needs a publisher from bibliographic resolution).
    """

    licence: str
    licence_basis: litcache_pb2.LicenceBasis
    access: litcache_pb2.Access


@dataclasses.dataclass(frozen=True)
class OaSource:
    """A fetched full-text XML source artifact plus its access terms.

    Attributes:
        content: The full-text XML bytes — the `convert.convert_jats` input and
            the `xml` source's stored bytes.
        kind: The manifest `SourceKind` the litfetch fetcher maps to.
        access: The source's licence / basis / access, mapped from the licence
            litfetch read out of these bytes.
        origin_url: The external fetch URL, when litfetch recorded one.
    """

    content: bytes
    kind: litcache_pb2.SourceKind
    access: AccessTerms
    origin_url: str | None


@dataclasses.dataclass(frozen=True)
class SupplementaryFile:
    """A fetched supplementary artifact (figure, dataset, video, …).

    Attributes:
        content: The artifact bytes — the associated-file blob.
        filename: The artifact's original filename; its extension keys the
            content-addressed blob path the writer stores it under.
        media_type: The served media type.
        origin_url: The external fetch URL, when litfetch recorded one.
    """

    content: bytes
    filename: str
    media_type: str
    origin_url: str | None


def _licence_basis(raw_basis: str) -> litcache_pb2.LicenceBasis:
    if raw_basis == _ARTIFACT_BASIS:
        return litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT
    return litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED


def _access(meta: artifacts.SourceMetadata) -> litcache_pb2.Access:
    # An artifact basis means litfetch read the licence from bytes it fetched off
    # its OA ladder (PMC-OA / Europe PMC / Elsevier OA), so the work is
    # free-to-read even when the JATS `license-type` carried no explicit `open`
    # flag and left the access string None.
    if meta.basis == _ARTIFACT_BASIS or meta.access in _FREE_TO_READ_TOKENS:
        return litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead())
    return litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess())


def from_source_metadata(meta: artifacts.SourceMetadata) -> AccessTerms:
    """Map litfetch source metadata to a source's licence / basis / access.

    Args:
        meta: The `SourceMetadata` litfetch returned for the source's body —
            from `extract_source_metadata` (artifact) or `resolve_access`
            (authority).

    Returns:
        The `AccessTerms` to store on the source.

    Raises:
        ValueError: If `meta` is empty (no basis): litfetch resolved no access
            terms, so there is nothing to map. The fully-unresolved case — no
            licence, no basis, and no publisher for the `licensed` variant — is
            bibliographic resolution's to decide, not a value this mapper invents.
    """
    if meta.basis is None:
        raise ValueError('litfetch returned no access terms (empty SourceMetadata): nothing to map')
    return AccessTerms(
        licence=meta.licence or '',
        licence_basis=_licence_basis(meta.basis),
        access=_access(meta),
    )


def default_resolver() -> resolvers.Resolver:
    """The OA-fetch id resolver: litfetch's keyless chain plus Semantic Scholar.

    The ladder's PMC / Europe-PMC fetchers key on `pmcid`, but the seed is
    DOI/PMID-keyed, so the bundle must be enriched to a `pmcid` before they can
    fire. litfetch's `default_resolver` (Europe PMC + NCBI ID Converter) maps
    `pmid -> pmcid` but not `doi -> pmcid`; appending `SemanticScholarResolver`
    (keyless, maps `doi -> PubMedCentral`) closes that gap — litfetch's own
    documented extension point. Without this the OA branch never fires for a
    DOI-keyed paper and every paper falls to the non-OA docling path.

    Semantic Scholar's keyless endpoint is rate-limited; a bulk (Dataflow) run
    should supply an API key or a higher-throughput DOI->PMCID source.
    """
    return resolvers.chain(resolvers.default_resolver(), resolvers.SemanticScholarResolver())


def article_ids(external_ids: Sequence[identity.ExternalId]) -> ids.ArticleIds | None:
    """Build a litfetch `ArticleIds` from a paper's classified identity.

    Args:
        external_ids: The identity ids (`themis.litcache.identity`), any scheme.

    Returns:
        An `ArticleIds` carrying the doi / pmid / pmcid present, or `None` when the
        paper carries no litfetch-fetchable id (only `pii` / `binhash`) — there is
        no OA fetch to attempt.
    """
    fetchable = {eid.scheme: eid.value for eid in external_ids if eid.scheme in _FETCHABLE_SCHEMES}
    if not fetchable:
        return None
    return ids.ArticleIds(doi=fetchable.get('doi'), pmid=fetchable.get('pmid'), pmcid=fetchable.get('pmcid'))


async def fetch_supplementary(
    article_ids: ids.ArticleIds,
    *,
    sources: Sequence[litfetch.FileSource] | None = None,
    session: litfetch.Session | None = None,
) -> list[SupplementaryFile]:
    """Fetch every supplementary artifact litfetch lists for the paper.

    Lists the `SUPPLEMENTARY` file-set across `sources` and downloads each. Unlike
    `fetch_oa_source`, `litfetch.list_files` does no id resolution, so `article_ids`
    must already carry the `pmcid` the PMC source keys on (the caller resolves it for
    the body fetch and reuses it here); a bundle without it lists nothing.

    Args:
        article_ids: The identifier bundle — needs a `pmcid` for the PMC source.
        sources: The file sources to query; defaults to litfetch's own (the PMC OA
            source). Pass `[]` to stay offline.
        session: Optional shared litfetch session (its own HTTP client + pacing);
            `None` opens an ephemeral session per call.

    Returns:
        The fetched supplementary files, in listed order (empty when none are
        listed or served).
    """
    list_files = session.list_files if session is not None else litfetch.list_files
    fetch_file = session.fetch_file if session is not None else litfetch.fetch_file
    files = await list_files(article_ids, sources=sources, kind=artifacts.FileKind.SUPPLEMENTARY)
    fetched: list[SupplementaryFile] = []
    for file in files:
        blob = await fetch_file(file, sources=sources)
        if blob is None:
            continue
        # The writer keys the blob path off the filename extension and records the
        # media type; a supplementary file missing either can't be stored faithfully.
        if file.filename is None or file.media_type is None:
            raise ValueError(f'supplementary file lacks filename/media_type: {file.uri}')
        fetched.append(
            SupplementaryFile(
                content=blob.content, filename=file.filename, media_type=file.media_type, origin_url=file.uri
            )
        )
    return fetched


async def fetch_oa_source(
    article_ids: ids.ArticleIds,
    *,
    resolver: resolvers.Resolver | None = None,
    fetchers: Sequence[litfetch.Fetcher] | None = None,
    session: litfetch.Session | None = None,
) -> OaSource | None:
    """Fetch the paper's full-text XML source from the litfetch ladder.

    Walks the litfetch fetcher ladder for the paper's body. Returns an `OaSource`
    only when the served body is XML (the OA branch); returns `None` when the
    ladder serves nothing or serves a non-XML body (a PDF) — the caller then
    renders the seed Docling json on the non-OA branch.

    Args:
        article_ids: The identifier bundle to retrieve.
        resolver: Optional resolver litfetch invokes once to fill missing ids a
            later fetcher needs; defaults to litfetch's own, which maps
            `pmid→pmcid` but not `doi→pmcid`. A DOI-only bundle therefore needs
            its `pmcid` filled before this call (the pipeline resolves upstream
            via `default_resolver`) — or pass `default_resolver()` here — else
            the ladder finds no OA body and the caller falls to the non-OA path.
        fetchers: The fetcher ladder; defaults to `litfetch.default_fetchers()`.
        session: Optional shared litfetch session (its own HTTP client + pacing);
            `None` opens an ephemeral session per call.

    Returns:
        The `OaSource` (XML bytes + provenance + access terms) when an XML body is
        obtainable, else `None`.

    Raises:
        ValueError: If the served body's source is not a known `SourceKind`, or
            litfetch read no access terms from an XML body it served off its OA
            ladder (both are real anomalies, not silently degraded papers).
    """
    fetch_body = session.fetch_body if session is not None else litfetch.fetch_body
    blob = await fetch_body(
        article_ids,
        resolver=resolver,
        fetchers=fetchers,
    )
    if blob is None or blob.file.media_type not in _XML_MEDIA_TYPES:
        return None
    return OaSource(
        content=blob.content,
        # litfetch's source name is the SourceKind value sans the SOURCE_KIND_ prefix
        # (e.g. `europe_pmc` -> SOURCE_KIND_EUROPE_PMC); Value raises on an unknown source.
        kind=typing.cast(
            'litcache_pb2.SourceKind', litcache_pb2.SourceKind.Value(f'SOURCE_KIND_{blob.file.source.upper()}')
        ),
        access=from_source_metadata(source_metadata.extract_source_metadata(blob)),
        origin_url=blob.file.uri,
    )
