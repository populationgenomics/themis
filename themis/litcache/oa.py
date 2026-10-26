"""The OA branch: fetch a paper's full-text XML source and its access terms.

The conversion rule is "if the paper is in PMC / otherwise OA — or is a Bookshelf
chapter, which Europe PMC serves under its accession — and XML is obtainable via the
litfetch ladder, convert the XML with litdown (`xml-faithful`); otherwise render the
seed Docling json (`pdf-derived`)." This module is the OA side: it walks
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

A paper with no XML and no PDF of its own — one ingested by id rather than from the
seed — can still have an openly hosted PDF: `fetch_oa_pdf` fetches the one at the
best open-access location Unpaywall lists, for the producer to transcribe.
"""

from __future__ import annotations

import asyncio
import dataclasses
import mimetypes
import posixpath
import re
import urllib.parse
from collections.abc import Sequence

import httpx2
import litfetch
from litfetch import artifacts, ids, resolvers, source_metadata

from themis.common import constants
from themis.litcache import identity
from themis.litcache.models import litcache_pb2

# litdown renders a JATS article, a BITS book part (served as JATS), and Elsevier's XML;
# either body media type is "XML obtainable" for the xml-faithful branch.
_XML_MEDIA_TYPES = frozenset({artifacts.JATS_XML, artifacts.ELSEVIER_XML})

# Identity schemes litfetch's `ArticleIds` can fetch against; `pii`/`binhash` have
# no litfetch equivalent, so a paper carrying only those is never OA-fetchable.
_FETCHABLE_SCHEMES = frozenset({'doi', 'pmid', 'pmcid', 'bookid'})

# litfetch's access string is the artifact `open-access` flag or Unpaywall's raw
# `oa_status`. These tokens mark the work openly readable → `free-to-read`;
# `closed` and any unrecognised token do not.
_FREE_TO_READ_TOKENS = frozenset({'open-access', 'gold', 'green', 'hybrid', 'bronze'})

# litfetch's `basis` is `artifact` (read from the fetched bytes) or the asserting
# authority's name (`unpaywall`, …); litcache collapses every authority to
# `asserted`, the only non-artifact basis its schema models.
_ARTIFACT_BASIS = 'artifact'

# litfetch's OA-ladder source name -> the manifest `SourceKind`. The proto names mirror litfetch's
# (europe_pmc -> SOURCE_KIND_EUROPE_PMC), but the map is explicit rather than derived by string-munging
# the enum name: an unrecognised source is then a loud `OaSourceError`, never a silent map onto a
# litcache-internal kind (SEED / UPLOAD) or a break when a name diverges upstream. A new litfetch source
# is a deliberate line here.
_SOURCE_KINDS: dict[str, litcache_pb2.SourceKind] = {
    'pmc_oa_s3': litcache_pb2.SourceKind.SOURCE_KIND_PMC_OA_S3,
    'europe_pmc': litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC,
    'elsevier_oa': litcache_pb2.SourceKind.SOURCE_KIND_ELSEVIER_OA,
    'biorxiv': litcache_pb2.SourceKind.SOURCE_KIND_BIORXIV,
    'europe_pmc_bookshelf': litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC_BOOKSHELF,
}

_UNPAYWALL_API = 'https://api.unpaywall.org/v2'

# A DOI's shape; anything else is not looked up, so nothing but a DOI reaches the request path.
_DOI_RE = re.compile(r'10\.\d{4,9}/[^\s?#]+')

# Statuses that a later attempt could clear. litfetch's own Unpaywall and download calls report
# these as "nothing found", which the producer would record as a settled NO_FULL_TEXT, so
# `fetch_oa_pdf` makes both requests itself.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

# The copies whose text is the reviewed article; a `submittedVersion` predates peer review.
_REVIEWED_VERSIONS = frozenset({'publishedVersion', 'acceptedVersion'})

# The PDF download's own attempts and backoff, since it streams outside the session's retrying GET.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_SECONDS = 0.5

# Well above any real paper; a located URL is a third party's, so the read is bounded.
_PDF_MAX_BYTES = 100 * 1024 * 1024

# The PDF header may follow up to 1 KB of leading bytes; an HTML landing page answered with 200 has none.
_PDF_MAGIC = b'%PDF-'
# The version directory in a PMC open-access package key: `PMC1234567.2/`.
_PMC_VERSION = re.compile(r'PMC[0-9]+\.([0-9]+)/')
_PDF_HEADER_WINDOW = 1024


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
class OaPdf:
    """An openly hosted PDF of the paper plus the access terms Unpaywall states for that copy.

    Attributes:
        content: The PDF bytes — the transcriber's input and the `pdf` source's stored bytes.
        access: The licence / basis / access of Unpaywall's best open-access location, the location
            the PDF was fetched from. A PDF carries no terms in its bytes, so the basis is asserted.
        origin_url: The URL the PDF was fetched from.
    """

    content: bytes
    access: AccessTerms
    origin_url: str


class UpstreamUnavailableError(Exception):
    """An upstream gave an answer a later attempt could improve on.

    That is a 429 or 5xx after retries, or an Unpaywall record that does not parse. Raised so the
    caller retries later rather than recording the paper as having no full text.
    """


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
    # its OA ladder (PMC-OA / Europe PMC / Bookshelf / Elsevier OA), so the work
    # is free-to-read even when the JATS `license-type` carried no explicit
    # `open` flag and left the access string None.
    if meta.basis == _ARTIFACT_BASIS or meta.access in _FREE_TO_READ_TOKENS:
        return litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead())
    return litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess())


class OaSourceError(Exception):
    """A served OA body is a real anomaly, not a silently-degraded paper.

    Raised for the documented cases — an unknown source kind, no access terms, or a supplementary file
    with no extension to store it under — so callers can
    tell this permanent-for-this-paper failure apart from a `ValueError` litfetch's own dependencies
    may raise incidentally on a transient upstream blip (a truncated JSON body decodes as a
    `json.JSONDecodeError`, itself a `ValueError`). The producer treats this as permanent; a bare
    `ValueError` it lets propagate to be retried.
    """


def from_source_metadata(meta: artifacts.SourceMetadata) -> AccessTerms:
    """Map litfetch source metadata to a source's licence / basis / access.

    Args:
        meta: The `SourceMetadata` litfetch returned for the source's body —
            from `extract_source_metadata` (artifact) or `resolve_access`
            (authority).

    Returns:
        The `AccessTerms` to store on the source.

    Raises:
        OaSourceError: If `meta` is empty (no basis): litfetch resolved no access
            terms, so there is nothing to map. The fully-unresolved case — no
            licence, no basis, and no publisher for the `licensed` variant — is
            bibliographic resolution's to decide, not a value this mapper invents.
    """
    if meta.basis is None:
        raise OaSourceError('litfetch returned no access terms (empty SourceMetadata): nothing to map')
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
    both `pmid -> pmcid` and `doi -> pmcid`; appending `SemanticScholarResolver`
    (keyless) adds coverage for the papers those two miss. This is for a caller
    resolving inline; the batch pipeline pre-resolves the `pmcid` and fetches
    with `resolver=None`, so it does not use this.

    Semantic Scholar's keyless endpoint is rate-limited; a bulk (Dataflow) run
    should supply an API key or a higher-throughput DOI->PMCID source.
    """
    return resolvers.chain(resolvers.default_resolver(), resolvers.SemanticScholarResolver())


def article_ids(external_ids: Sequence[identity.ExternalId]) -> ids.ArticleIds | None:
    """Build a litfetch `ArticleIds` from a paper's classified identity.

    Args:
        external_ids: The identity ids (`themis.litcache.identity`), any scheme.

    Returns:
        An `ArticleIds` carrying the doi / pmid / pmcid / bookid present, or `None`
        when the paper carries no litfetch-fetchable id (only `pii` / `binhash`) —
        there is no OA fetch to attempt.
    """
    fetchable = {eid.scheme: eid.value for eid in external_ids if eid.scheme in _FETCHABLE_SCHEMES}
    if not fetchable:
        return None
    return ids.ArticleIds(
        doi=fetchable.get('doi'),
        pmid=fetchable.get('pmid'),
        pmcid=fetchable.get('pmcid'),
        bookid=fetchable.get('bookid'),
    )


def article_ids_for_fetch(
    external_ids: Sequence[identity.ExternalId], resolved: litcache_pb2.ExternalIds
) -> ids.ArticleIds | None:
    """Build the OA-fetch id bundle from identity plus the resolved cross-ids.

    The PMC fetchers key on `pmcid` and the Bookshelf fetcher on `bookid`; `article_ids`
    alone rarely carries either (the seed is DOI/PMID). Resolution supplies them — efetch
    harvests them from the PubMed record, idconv maps a `pmcid` from a DOI — so the fetch
    bundle is the identity's fetchable ids unioned with the resolved ids, identity taking
    precedence. This is why the OA fetch needs no live resolver: the ids the fetchers key
    on are already resolved.

    Args:
        external_ids: The paper's identity ids (any scheme).
        resolved: The cross-ids from `resolve.ResolvedPaper` (efetch-harvested or
            idconv-mapped); unset fields are the empty string.

    Returns:
        An `ArticleIds` carrying every doi / pmid / pmcid / bookid known, or `None` when
        none is (no OA fetch to attempt).
    """
    fetchable = {eid.scheme: eid.value for eid in external_ids if eid.scheme in _FETCHABLE_SCHEMES}
    doi = fetchable.get('doi') or resolved.doi or None
    pmid = fetchable.get('pmid') or resolved.pmid or None
    pmcid = fetchable.get('pmcid') or resolved.pmcid or None
    bookid = fetchable.get('bookid') or resolved.bookid or None
    if doi is None and pmid is None and pmcid is None and bookid is None:
        return None
    return ids.ArticleIds(doi=doi, pmid=pmid, pmcid=pmcid, bookid=bookid)


async def fetch_supplementary(
    article_ids: ids.ArticleIds,
    *,
    sources: Sequence[litfetch.FileSource] | None = None,
    session: litfetch.Session | None = None,
    skip_unfetchable: bool,
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
        skip_unfetchable: Whether a listed file litfetch could not download is left out. litfetch
            reports a failed download as no file, so leaving it out records the set as complete.

    Returns:
        The fetched supplementary files, in listed order (empty when none are
        listed or served).

    Raises:
        OaSourceError: A listed file has no extension to store it under.
        UpstreamUnavailableError: A listed file could not be downloaded, and `skip_unfetchable` is false.
    """
    list_files = session.list_files if session is not None else litfetch.list_files
    fetch_file = session.fetch_file if session is not None else litfetch.fetch_file
    files = await list_files(article_ids, sources=sources, kind=artifacts.FileKind.SUPPLEMENTARY)
    fetched: list[SupplementaryFile] = []
    for file in files:
        blob = await fetch_file(file, sources=sources)
        if blob is None:
            if not skip_unfetchable:
                raise UpstreamUnavailableError(f'listed supplementary file {file.uri} could not be downloaded')
            continue
        # The writer keys the blob path off the filename extension, so a filename with no
        # extension can't be placed — the one genuinely unstorable case. The filename is
        # recoverable from the URI basename when litfetch didn't parse one. An unmapped
        # extension is still storable: the bytes are opaque, so record octet-stream rather
        # than dropping the whole paper over one odd supplementary attachment.
        uri_path = urllib.parse.urlparse(file.uri).path if file.uri else ''
        filename = file.filename or posixpath.basename(uri_path)
        if not filename or not posixpath.splitext(filename)[1]:
            raise OaSourceError(f'supplementary file has no storable extension: {file.uri}')
        media_type = file.media_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        fetched.append(
            SupplementaryFile(content=blob.content, filename=filename, media_type=media_type, origin_url=file.uri)
        )
    return fetched


async def fetch_supplementary_resolving(
    article_ids: ids.ArticleIds, *, resolver: resolvers.Resolver
) -> list[SupplementaryFile]:
    """The paper's supplementary files from its PubMed Central open-access package, one per name.

    The package is listed by `pmcid`, which a paper keyed by DOI or PMID does not carry; `resolver` maps one
    to it first, inside the same session. Every listed file must download, so a set returned is complete. The
    package holds each version of a revised article, so a name listed under several versions is taken from
    the latest.

    Args:
        article_ids: The paper's ids.
        resolver: Fills a missing `pmcid`.

    Returns:
        The files, one per name; empty when the package lists none, or none could be found to list.

    Raises:
        OaSourceError: A listed file has no extension to store it under.
        UpstreamUnavailableError: A listed file could not be downloaded.
    """
    async with litfetch.Session() as session:
        if article_ids.pmcid is None:
            article_ids = await resolver(article_ids, session)
        files = await fetch_supplementary(
            article_ids, sources=(litfetch.PmcOaFetcher(),), session=session, skip_unfetchable=False
        )
    latest: dict[str, tuple[int, SupplementaryFile]] = {}
    for file in files:
        version = _pmc_version(file.origin_url)
        if file.filename not in latest or version >= latest[file.filename][0]:
            latest[file.filename] = (version, file)
    return [file for _, file in latest.values()]


def _pmc_version(uri: str | None) -> int:
    """The article version a PMC package key names (`…/PMC123.2/…`); 0 for one that names none."""
    match = _PMC_VERSION.search(uri or '')
    return int(match.group(1)) if match else 0


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
            later fetcher needs. `None` (the default) runs no resolver at all —
            litfetch skips any fetcher whose required ids the bundle lacks. A
            DOI-only bundle therefore needs its `pmcid` filled before this call
            (the pipeline resolves upstream) — or pass `default_resolver()` here
            — else the ladder finds no OA body and the caller falls to the
            non-OA path.
        fetchers: The fetcher ladder; defaults to `litfetch.default_fetchers()`.
        session: Optional shared litfetch session (its own HTTP client + pacing);
            `None` opens an ephemeral session per call.

    Returns:
        The `OaSource` (XML bytes + provenance + access terms) when an XML body is
        obtainable, else `None`.

    Raises:
        OaSourceError: If the served body's source is not a known `SourceKind`, or
            litfetch read no access terms from an XML body it served off its OA
            ladder (both are real anomalies, not silently degraded papers). A
            transient upstream failure surfaces as litfetch's own error, not this.
    """
    fetch_body = session.fetch_body if session is not None else litfetch.fetch_body
    blob = await fetch_body(
        article_ids,
        resolver=resolver,
        fetchers=fetchers,
    )
    if blob is None or blob.file.media_type not in _XML_MEDIA_TYPES:
        return None
    kind = _SOURCE_KINDS.get(blob.file.source)
    if kind is None:
        raise OaSourceError(f'litfetch served an unknown source kind {blob.file.source!r}')
    return OaSource(
        content=blob.content,
        kind=kind,
        access=from_source_metadata(source_metadata.extract_source_metadata(blob)),
        origin_url=blob.file.uri,
    )


async def _unpaywall_record(doi: str, session: litfetch.Session) -> dict[str, object] | None:
    """The Unpaywall record for `doi`, or `None` when Unpaywall holds none.

    A 200 whose body is not a JSON object is treated as transient, like a 5xx: Unpaywall serves a
    maintenance page that way.
    """
    response = await session.get(
        f'{_UNPAYWALL_API}/{urllib.parse.quote(doi, safe="/")}', params={'email': constants.CONTACT_EMAIL}
    )
    if response.status_code in _TRANSIENT_STATUS:
        raise UpstreamUnavailableError(f'Unpaywall answered {response.status_code} for {doi}')
    if response.status_code != 200:
        return None
    try:
        record = response.json()
    except ValueError as e:
        raise UpstreamUnavailableError(f'Unpaywall answered a body that is not JSON for {doi}') from e
    if not isinstance(record, dict):
        raise UpstreamUnavailableError(f'Unpaywall answered a non-object record for {doi}')
    return record


async def _stream_pdf(url: str, session: litfetch.Session) -> bytes | None:
    """One attempt at the PDF at `url`; `None` when the host refuses it or serves something else."""
    async with session.client.stream('GET', url, follow_redirects=True) as response:
        # A bot challenge answers 403 or 503 alike, and no retry gets past it.
        if response.headers.get('cf-mitigated') == 'challenge':
            return None
        if response.status_code in _TRANSIENT_STATUS:
            raise UpstreamUnavailableError(f'{url} answered {response.status_code}')
        if response.status_code != 200:
            return None
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content += chunk
            if len(content) > _PDF_MAX_BYTES:
                raise OaSourceError(f'{url} serves more than {_PDF_MAX_BYTES} bytes')
    if _PDF_MAGIC not in content[:_PDF_HEADER_WINDOW]:
        return None
    return bytes(content)


async def _download_pdf(url: str, session: litfetch.Session) -> bytes | None:
    """The PDF at `url`, retrying a transient failure; `None` when this copy cannot be had.

    A redirect loop, an undecodable body, or a URL httpx2 cannot request is a property of the
    location, not of the moment, so it is a settled absence rather than a retry.
    """
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        try:
            return await _stream_pdf(url, session)
        except (httpx2.TooManyRedirects, httpx2.DecodingError, httpx2.InvalidURL, httpx2.UnsupportedProtocol):
            return None
        except (UpstreamUnavailableError, httpx2.TransportError):
            if attempt == _DOWNLOAD_ATTEMPTS - 1:
                raise
        await asyncio.sleep(_DOWNLOAD_BACKOFF_SECONDS * 2**attempt)
    raise AssertionError('unreachable: the last attempt returns or raises')


async def fetch_oa_pdf(article_ids: ids.ArticleIds, *, session: litfetch.Session | None = None) -> OaPdf | None:
    """Fetch the PDF at the paper's best open-access location, with the terms Unpaywall states for it.

    Only Unpaywall's best location is taken, because its terms describe that copy and no other, and
    only when that copy is the reviewed article (published or accepted). A paper Unpaywall does not
    report open (`closed`, or no status) is not fetched.

    Args:
        article_ids: The identifier bundle. Unpaywall keys on the DOI, so a bundle without a
            well-formed one returns `None`.
        session: The litfetch session to request on; `None` opens one identified by the project's
            contact address.

    Returns:
        The `OaPdf`, or `None` when the paper has no DOI, Unpaywall has no record or reports it
        closed, lists no PDF or only a submitted version, or the host refuses the PDF or serves
        something else.

    Raises:
        UpstreamUnavailableError: If Unpaywall or the PDF's host still answers 429 or a 5xx after
            retries, or Unpaywall's record is unreadable. Transient; the caller retries.
        httpx2.TransportError: If a request still fails at the transport layer after retries.
            Transient.
        OaSourceError: If the PDF exceeds the size bound. Permanent for this copy.
    """
    if article_ids.doi is None or not _DOI_RE.fullmatch(article_ids.doi):
        return None
    if session is None:
        async with litfetch.Session(contact=constants.CONTACT_EMAIL) as owned:
            return await fetch_oa_pdf(article_ids, session=owned)
    record = await _unpaywall_record(article_ids.doi, session)
    if record is None:
        return None
    oa_status = record.get('oa_status')
    best = record.get('best_oa_location')
    if oa_status not in _FREE_TO_READ_TOKENS or not isinstance(best, dict):
        return None
    if best.get('version') not in _REVIEWED_VERSIONS:
        return None
    url = best.get('url_for_pdf')
    if not isinstance(url, str) or not url:
        return None
    content = await _download_pdf(url, session)
    if content is None:
        return None
    licence = best.get('license')
    terms = artifacts.SourceMetadata(
        licence=licence if isinstance(licence, str) else None, access=str(oa_status), basis='unpaywall'
    )
    return OaPdf(content=content, access=from_source_metadata(terms), origin_url=url)
