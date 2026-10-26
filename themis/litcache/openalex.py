"""OpenAlex lookups: a batch of PMIDs or DOIs → the works OpenAlex holds for each, whole.

OpenAlex answers two questions for litcache. For a DOI no PubMed record answers, its work is the paper's record: the
batchable resolver for the papers idconv cannot route, since idconv is PMC-scoped, so a DOI in PubMed-but-not-PMC or not
in PubMed at all (a preprint) never reaches efetch through it. For a paper PubMed or Crossref resolved, OpenAlex's work
of the same paper is stored beside that record, since the paper's authors are read from it
(docs/design/litcache-manifest.md, "The resolving index's record, and OpenAlex's beside it"). Both are one lookup,
`find`: a works filter on `pmid` or `doi`, up to `_MAX_IDS` identifiers in one OR-list, each work returned whole and
stored as JSON text, every key kept (`themis.litcache.upstream_record`). Whole includes every authorship, which a works
list cuts at 100: a work served with 100 is fetched again on its own. A DOI holding `,` or `|` cannot be a filter value,
since the filter grammar splits on both, so it is looked up alone at `/works/doi:{doi}`, which serves its work whole.

Each identifier asked has one of four answers (`Found.work`): one work; none, the caller's "not in OpenAlex", never
invented; more than one, an `AmbiguousWorkError` naming the works, since taking either could give the paper another
work's record; or a work this reader cannot read or store, a `SchemaDriftError` charged to that identifier alone. A
failure to reach OpenAlex, past a few retries, propagates from `find` as httpx2 raises it, and so does a work that
cannot be charged to one identifier, since no answer in its batch can then be trusted. `find` reads of a work only the
identifier it was looked up by. A work found by DOI is also read for the PMID it states (`stated_pmid`), which tells
whether it is the work of a paper PubMed resolved or routes a DOI to PubMed. `read_work` reads the cross-ids (`pmid`,
`pmcid`, bare from their URL forms) and publisher ingest takes from the work of a paper OpenAlex resolved.

Every request carries the caller's API key (`ApiKey`) as the `api_key` query parameter. The key never reaches a log or
an error message: a failure escapes `fetch_page` with the key's value replaced in its message, request and response, and
`httpx2`'s own record of each request (its `HTTP Request:` line) is rewritten the same way before any handler sees it.
"""

from __future__ import annotations

import enum
import json
import logging
import re
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from typing import NamedTuple, override

import httpx2

from themis.common import constants
from themis.litcache import metadata_fields, transient, upstream_record
from themis.litcache.models import litcache_pb2

_WORKS_URL = 'https://api.openalex.org/works'
# A works list serves at most this many authorships of a work; `/works/{id}` serves all of them.
_LISTED_AUTHORSHIPS = 100
# OpenAlex's documented caps: 100 values in one filter OR-list, so a larger batch is chunked, and a `per-page` of 100.
_MAX_IDS = 100
_PER_PAGE = 100
# OpenAlex refuses a request URL over 8190 bytes with a 400. `find` chunks to a shorter URL: 100 SICI DOIs, each
# percent-encoded to about 85 bytes, would run to 8.7 KB.
_MAX_URL_BYTES = 8190
_CHUNK_URL_BYTES = 6000
# OpenAlex decodes the query, then splits `filter` into filters on `,` and a filter's values on `|`; percent-encoding
# either does not keep it in a value.
_FILTER_SEPARATORS = frozenset(',|')
# Tries for a transient failure (`transient`), and the wait before the second: 2s, 4s, 8s, 16s.
_MAX_FETCH_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 2.0
INDEX = upstream_record.Index.OPENALEX

_DOI_PREFIX = 'https://doi.org/'
_WORK_URL = re.compile(r'https://openalex\.org/(W[1-9][0-9]*)')
_PMID_URL = re.compile(r'https://pubmed\.ncbi\.nlm\.nih\.gov/([1-9][0-9]*)/?')
# OpenAlex writes a PMCID's URL with or without its `PMC` prefix.
_PMCID_URL = re.compile(r'https://www\.ncbi\.nlm\.nih\.gov/pmc/articles/(?:PMC)?([1-9][0-9]*)/?')

_API_KEY_PARAM = 'api_key'
_REDACTED = 'REDACTED'
# The key's value as a URL, or a message quoting one, carries it: up to the next parameter, quote or space.
_API_KEY_IN_TEXT = re.compile(rf'({_API_KEY_PARAM}=)[^&\s\'"]*')
# A redacted response holds the body decoded, so the headers that describe its encoding on the wire are dropped.
_WIRE_ENCODING_HEADERS = frozenset({'content-encoding', 'content-length', 'transfer-encoding'})


class ApiKey:
    """An OpenAlex API key: a non-empty token, whose `repr` never shows it."""

    def __init__(self, value: str) -> None:
        """Hold `value`.

        Raises:
            ValueError: If `value` is empty or holds whitespace (a key read with its trailing newline, say).
        """
        if not value or any(c.isspace() for c in value):
            raise ValueError('an OpenAlex API key is a non-empty token with no whitespace')
        self._value = value

    @property
    def value(self) -> str:
        """The key itself, for the request that carries it."""
        return self._value

    @override
    def __repr__(self) -> str:
        return f'openalex.ApiKey({_REDACTED})'


def redact(text: str) -> str:
    """`text` with the value of every `api_key` query parameter it quotes replaced by `REDACTED`."""
    return _API_KEY_IN_TEXT.sub(rf'\g<1>{_REDACTED}', text)


class _RedactingFilter(logging.Filter):
    """Rewrites a record whose message quotes an `api_key` parameter, so no handler formats the key."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


# Installed on import rather than by each entry point, so no process that can send a keyed request can forget it.
logging.getLogger('httpx2').addFilter(_RedactingFilter())


class Work(NamedTuple):
    """One OpenAlex work, whole.

    Attributes:
        text: The work as JSON text (`upstream_record.to_text`), for the source record's field.
        record: The work as OpenAlex served it, parsed, for a reader of a paper OpenAlex resolved (`read_work`).
    """

    text: str
    record: Mapping[str, object]


class WorkFields(NamedTuple):
    """The identifiers and publisher ingest takes from the work of a paper OpenAlex resolved.

    Attributes:
        pmid: The PMID read off its PubMed URL, or `None` when the work states none (not in PubMed).
        pmcid: The PMCID read off its PMC URL, as `PMC…`, or `None` when the work states none; idconv
            is the authoritative PMCID source, and OpenAlex often omits it even for PMC papers.
        publisher: The host organisation of the work's primary source, or `None` when the location,
            its source or the name is null or empty.
    """

    pmid: str | None
    pmcid: str | None
    publisher: str | None


class Scheme(enum.StrEnum):
    """An identifier OpenAlex's works filter looks a paper up by; the value is the filter's name."""

    PMID = 'pmid'
    DOI = 'doi'


class AmbiguousWorkError(Exception):
    """OpenAlex holds more than one work for an identifier, so none of them can be taken as the paper's.

    Attributes:
        identifier: The identifier, as `scheme:value`.
        work_ids: The OpenAlex ids of the works, in the order OpenAlex returned them.
    """

    def __init__(self, identifier: str, work_ids: Sequence[str]) -> None:
        super().__init__(f'OpenAlex holds {len(work_ids)} works for {identifier}: {", ".join(work_ids)}')
        self.identifier = identifier
        self.work_ids = tuple(work_ids)


class Found(NamedTuple):
    """The works OpenAlex holds for a batch of identifiers of one scheme, keyed by `key`.

    No key is in more than one mapping; an identifier OpenAlex holds no work for is in none.

    Attributes:
        scheme: The scheme the identifiers were looked up by.
        works: Each identifier OpenAlex holds exactly one work for, with that work.
        ambiguous: Each identifier OpenAlex holds more than one work for, with their OpenAlex ids.
        drifted: Each identifier whose one work could not be read or stored, with the message naming the field
            (`upstream_record.SchemaDriftError.detail`).
    """

    scheme: Scheme
    works: dict[str, Work]
    ambiguous: dict[str, list[str]]
    drifted: dict[str, str]

    def work(self, identifier: str) -> Work | None:
        """The one work OpenAlex holds for `identifier`, or `None` when it holds none.

        Raises:
            AmbiguousWorkError: If OpenAlex holds more than one work for it.
            upstream_record.SchemaDriftError: If its work could not be read or stored.
        """
        k = key(self.scheme, identifier)
        if k in self.ambiguous:
            raise AmbiguousWorkError(f'{self.scheme}:{identifier}', self.ambiguous[k])
        if k in self.drifted:
            raise upstream_record.SchemaDriftError(INDEX, self.drifted[k])
        return self.works.get(k)

    def holds_none(self, identifier: str) -> bool:
        """Whether OpenAlex holds no work for `identifier`: neither one, nor more, nor one that drifted."""
        k = key(self.scheme, identifier)
        return k not in self.works and k not in self.ambiguous and k not in self.drifted

    def update(self, other: Found) -> None:
        """Add the answers of `other`, a lookup by the same scheme."""
        if other.scheme is not self.scheme:
            raise ValueError(f'cannot merge a {other.scheme} lookup into a {self.scheme} one')
        self.works.update(other.works)
        self.ambiguous.update(other.ambiguous)
        self.drifted.update(other.drifted)


def key(scheme: Scheme, identifier: str) -> str:
    """The form `Found` keys an identifier by: a PMID as it is, a DOI case-folded (DOIs are case-insensitive)."""
    return identifier.casefold() if scheme is Scheme.DOI else identifier


def read_work(work: Mapping[str, object]) -> WorkFields:
    """Read the identifiers and publisher of a paper OpenAlex resolved off its work; every other key is ignored.

    A work held beside another index's record is not read here: of it, only the identifier it was looked up by
    (`find`), the PMID it states when it was found by DOI (`stated_pmid`), and its authors (`metadata_fields.read`)
    are read.

    Raises:
        upstream_record.SchemaDriftError: If a field read has a shape this reader cannot read, or a
            stated `ids.pmid` / `ids.pmcid` is not a PubMed / PMC article URL.
    """
    root = upstream_record.JsonObject.root(INDEX, work)
    ids = root.optional_object_or_null('ids')
    pmid = ids.optional_str_matching('pmid', _PMID_URL, 'a PubMed article URL') if ids is not None else None
    pmcid = ids.optional_str_matching('pmcid', _PMCID_URL, 'a PMC article URL') if ids is not None else None
    location = root.optional_object_or_null('primary_location')
    source = location.optional_object_or_null('source') if location is not None else None
    publisher = source.optional_str_or_null('host_organization_name') if source is not None else None
    return WorkFields(
        pmid=pmid.group(1) if pmid is not None else None,
        pmcid=f'PMC{pmcid.group(1)}' if pmcid is not None else None,
        publisher=publisher or None,
    )


def parse_work(record: Mapping[str, object]) -> Work:
    """Serialise one work as text, reading none of its fields.

    Raises:
        upstream_record.SchemaDriftError: If the work holds a number standard JSON text cannot carry.
    """
    try:
        text = upstream_record.to_text(record)
    except ValueError as e:
        raise upstream_record.SchemaDriftError(INDEX, str(e)) from e
    return Work(text=text, record=record)


def _bare(value: str, prefix: str) -> str:
    return value.removeprefix(prefix).rstrip('/')


async def fetch_page(
    scheme: Scheme,
    identifiers: Sequence[str],
    page: int,
    *,
    http_client: httpx2.AsyncClient,
    api_key: ApiKey,
) -> bytes:
    """Fetch one page of the works a filter on `identifiers` (one OR-list) matches, each work whole.

    Args:
        scheme: The filter the identifiers are values of.
        identifiers: The identifiers, at most `_MAX_IDS`.
        page: The page, from 1.
        http_client: The async HTTP client (caller owns its lifecycle).
        api_key: The key sent as `api_key`.

    Returns:
        The raw works-response JSON bytes.

    Raises:
        ValueError: If `identifiers` is empty or exceeds `_MAX_IDS`, an identifier holds a character the filter
            grammar splits on (`_filterable`), or the request's URL would exceed the length OpenAlex accepts.
        httpx2.HTTPStatusError: If OpenAlex answers with a non-transient status, or a transient one (429, 5xx) past
            the retry budget (`transient.send`); the key is redacted from it (`redact`).
        httpx2.RequestError: If the connection keeps failing past the retry budget; the key is redacted from it.
    """
    if not identifiers:
        raise ValueError('openalex.fetch_page requires at least one identifier')
    if len(identifiers) > _MAX_IDS:
        raise ValueError(f'openalex.fetch_page got {len(identifiers)} identifiers; the OR-filter caps at {_MAX_IDS}')
    unfilterable = [identifier for identifier in identifiers if not _filterable(identifier)]
    if unfilterable:
        raise ValueError(f'openalex.fetch_page cannot carry {unfilterable!r} in a filter, which splits on `,` and `|`')
    query = _query(scheme, identifiers, page)
    length = _url_length(query, api_key)
    if length > _MAX_URL_BYTES:
        raise ValueError(
            f'openalex.fetch_page would send a {length}-byte URL; OpenAlex refuses one over {_MAX_URL_BYTES}'
        )
    return await _get(_WORKS_URL, query, http_client=http_client, api_key=api_key)


def _filterable(identifier: str) -> bool:
    """Whether `identifier` can be a works filter's value: it holds no `,` or `|`, which the filter splits on."""
    return _FILTER_SEPARATORS.isdisjoint(identifier)


def _query(scheme: Scheme, identifiers: Sequence[str], page: int) -> dict[str, str]:
    """The query of a works request for one page of a filter on `identifiers`, but for the key."""
    return {
        'filter': f'{scheme}:' + '|'.join(identifiers),
        'per-page': str(_PER_PAGE),
        'page': str(page),
        'mailto': constants.CONTACT_EMAIL,
    }


def _url_length(query: Mapping[str, str], api_key: ApiKey) -> int:
    """The length of the works URL a request with `query` and the key sends."""
    return len(str(httpx2.URL(_WORKS_URL, params={**query, _API_KEY_PARAM: api_key.value})))


def _encoded_length(value: str) -> int:
    """The length of `value` percent-encoded as a query value; encoding is per character, so lengths add up."""
    return len(str(httpx2.QueryParams({'v': value}))) - len('v=')


def _chunks(scheme: Scheme, identifiers: Sequence[str], api_key: ApiKey) -> Iterator[Sequence[str]]:
    """`identifiers` in runs of at most `_MAX_IDS` whose works URL stays within `_CHUNK_URL_BYTES`.

    An identifier too long to share a URL runs alone; `fetch_page` refuses it if OpenAlex would.
    """
    base = _url_length(_query(scheme, [], 1), api_key)
    separator = _encoded_length('|')
    chunk: list[str] = []
    length = base
    for identifier in identifiers:
        cost = _encoded_length(identifier)
        if chunk and (len(chunk) == _MAX_IDS or length + separator + cost > _CHUNK_URL_BYTES):
            yield chunk
            chunk, length = [], base
        length += cost + (separator if chunk else 0)
        chunk.append(identifier)
    if chunk:
        yield chunk


async def _get(url: str, query: Mapping[str, str], *, http_client: httpx2.AsyncClient, api_key: ApiKey) -> bytes:
    """GET `url` with `query` and the key, retrying a transient failure (`transient.send`).

    Raises:
        httpx2.HTTPStatusError: As `fetch_page`, with the key redacted.
        httpx2.RequestError: As `fetch_page`, with the key redacted.
    """
    sent = await _send(url, query, http_client=http_client, api_key=api_key)
    if isinstance(sent, httpx2.HTTPStatusError | httpx2.RequestError):
        raise sent
    return sent.content


async def _send(
    url: str, query: Mapping[str, str], *, http_client: httpx2.AsyncClient, api_key: ApiKey
) -> httpx2.Response | httpx2.HTTPStatusError | httpx2.RequestError:
    """The response, or the failure with the key redacted, returned rather than raised.

    Raised from here, the failure would carry the original as its `__context__` and this frame, whose `params` hold the
    key, in its traceback; `_get` and `_found_alone` raise it from a frame that holds neither.
    """
    params = {**query, _API_KEY_PARAM: api_key.value}
    try:
        return await transient.send(
            lambda: http_client.get(url, params=params),
            attempts=_MAX_FETCH_ATTEMPTS,
            base_delay=_RETRY_BASE_DELAY_SECONDS,
        )
    except (httpx2.HTTPStatusError, httpx2.RequestError) as e:
        return _redacted(e, api_key)


def _redacted(
    error: httpx2.HTTPStatusError | httpx2.RequestError, api_key: ApiKey
) -> httpx2.HTTPStatusError | httpx2.RequestError:
    """The same failure, with the key redacted from its message and from the request and response it carries.

    A response's headers and body are redacted too: a redirect's `Location`, or an error body, can echo the query.
    """
    message = _scrubbed(str(error), api_key)
    if isinstance(error, httpx2.HTTPStatusError):
        request = _redacted_request(error.request, api_key)
        headers = [
            (k, _scrubbed(v, api_key))
            for k, v in error.response.headers.multi_items()
            if k.lower() not in _WIRE_ENCODING_HEADERS
        ]
        content = error.response.content.replace(api_key.value.encode(), _REDACTED.encode())
        response = httpx2.Response(error.response.status_code, headers=headers, content=content, request=request)
        return httpx2.HTTPStatusError(message, request=request, response=response)
    try:
        request = _redacted_request(error.request, api_key)
    except RuntimeError:  # httpx raises this for a `.request` never set, not an AttributeError
        request = None
    return type(error)(message, request=request)


def _scrubbed(text: str, api_key: ApiKey) -> str:
    """`text` with every `api_key` parameter and every other occurrence of the key's value replaced."""
    return redact(text).replace(api_key.value, _REDACTED)


def _redacted_request(request: httpx2.Request, api_key: ApiKey) -> httpx2.Request:
    return httpx2.Request(request.method, _scrubbed(str(request.url), api_key))


class Page(NamedTuple):
    """One page of a works response.

    Attributes:
        matched: How many works the filter matches across every page (`meta.count`).
        results: This page's works.
    """

    matched: int
    results: list[Mapping[str, object]]


def parse_page(payload: bytes) -> Page:
    """Read one page of a works response.

    Raises:
        ValueError: If the payload is not a works response (`meta.count` not a count, `results` absent, or a result
            that is not an object).
        json.JSONDecodeError: If `payload` is not valid JSON.
    """
    document = json.loads(payload)
    if not isinstance(document, Mapping) or not isinstance(document.get('results'), list):
        raise ValueError('expected an OpenAlex works response with a `results` list')
    meta = document.get('meta')
    count = meta.get('count') if isinstance(meta, Mapping) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError('expected an OpenAlex works response whose `meta.count` is a count')
    results = document['results']
    if not all(isinstance(result, Mapping) for result in results):
        raise ValueError('expected every result of an OpenAlex works response to be a work object')
    return Page(matched=count, results=results)


def read_results(scheme: Scheme, identifiers: Sequence[str], results: Sequence[Mapping[str, object]]) -> Found:
    """Sort the works a filter on `identifiers` matched by the identifier each states.

    Raises:
        ValueError: If a work states none of `identifiers` (OpenAlex matched it by a value this reader does not key it
            by), so no identifier's answer can be trusted.
        upstream_record.SchemaDriftError: If a work's identifier cannot be read, so it cannot be charged to one.
    """
    asked = {key(scheme, identifier) for identifier in identifiers}
    matched: dict[str, list[Mapping[str, object]]] = {}
    for work in results:
        stated = _stated_key(scheme, work)
        if stated not in asked:
            raise ValueError(f'an OpenAlex work matched by {scheme} states {stated!r}, none of the identifiers asked')
        matched.setdefault(stated, []).append(work)
    found = Found(scheme=scheme, works={}, ambiguous={}, drifted={})
    for k, works in matched.items():
        try:
            if len(works) > 1:
                found.ambiguous[k] = [upstream_record.JsonObject.root(INDEX, w).required_str('id') for w in works]
            else:
                found.works[k] = parse_work(works[0])
        except upstream_record.SchemaDriftError as e:
            found.drifted[k] = e.detail
    return found


def stated_pmid(work: Work) -> str | None:
    """The PMID `work` states, bare, or `None` when it states none (not in PubMed).

    Raises:
        upstream_record.SchemaDriftError: If a stated `ids.pmid` is not a PubMed article URL.
    """
    return _stated_key(Scheme.PMID, work.record)


def _stated_key(scheme: Scheme, work: Mapping[str, object]) -> str | None:
    root = upstream_record.JsonObject.root(INDEX, work)
    if scheme is Scheme.DOI:
        doi = root.optional_str_or_null('doi')
        return key(scheme, _bare(doi, _DOI_PREFIX)) if doi else None
    ids = root.optional_object_or_null('ids')
    pmid = ids.optional_str_matching('pmid', _PMID_URL, 'a PubMed article URL') if ids is not None else None
    return pmid.group(1) if pmid is not None else None


async def find(
    scheme: Scheme, identifiers: Sequence[str], *, http_client: httpx2.AsyncClient, api_key: ApiKey
) -> Found:
    """Look up the works OpenAlex holds for each of `identifiers`, chunked to the OR-filter cap and a URL length, paged.

    A works list serves at most 100 authorships of a work, so a work it serves 100 of is fetched again from
    `/works/{id}`, which serves them all; every work is returned whole. A DOI the filter grammar would split (one
    holding `,` or `|`) is looked up alone at `/works/doi:{doi}`, whose 404 means OpenAlex holds no work for it. The
    pages of one filter must serve `meta.count` distinct works. A work served on two pages (OpenAlex's order shifted
    between them) leaves another unserved, so the lookup fails rather than miss it. A failure that cannot be charged to
    one identifier fails the whole lookup too: a work stating none of the identifiers asked, or one whose `id` or
    identifier cannot be read.

    Args:
        scheme: The filter the identifiers are values of.
        identifiers: The identifiers to look up.
        http_client: The async HTTP client (caller owns its lifecycle).
        api_key: The key every request carries.

    Raises:
        httpx2.HTTPError: If OpenAlex cannot be reached or answers a request with a non-2xx status past the retry
            budget (`fetch_page`), but for a DOI looked up alone that it holds no work for.
        ValueError: If a page is not a works response, the pages serve fewer distinct works than `meta.count` (the
            order shifted between pages), a work states none of the identifiers asked (`read_results`), an
            identifier alone makes a URL longer than OpenAlex accepts (`fetch_page`), a PMID holds a character the
            filter grammar splits on (`fetch_page`), or OpenAlex answers a work's id, or a DOI looked up alone, with
            something other than a work stating it.
        upstream_record.SchemaDriftError: If a work's `id`, or the identifier it is looked up by, cannot be read.
    """
    found = Found(scheme=scheme, works={}, ambiguous={}, drifted={})
    batched: list[str] = []
    for identifier in identifiers:
        if scheme is Scheme.DOI and not _filterable(identifier):
            found.update(await _found_alone(identifier, http_client=http_client, api_key=api_key))
        else:
            batched.append(identifier)
    for chunk in _chunks(scheme, batched, api_key):
        served = await _served(scheme, chunk, http_client=http_client, api_key=api_key)
        whole = [await _whole(work, http_client=http_client, api_key=api_key) for work in served]
        found.update(read_results(scheme, chunk, whole))
    return found


async def _served(
    scheme: Scheme, identifiers: Sequence[str], *, http_client: httpx2.AsyncClient, api_key: ApiKey
) -> list[Mapping[str, object]]:
    """The distinct works every page of a filter on `identifiers` serves, as the works list serves them.

    Raises:
        httpx2.HTTPError: As `fetch_page`.
        ValueError: If a page is not a works response, or the pages serve fewer distinct works than `meta.count`.
        upstream_record.SchemaDriftError: If a work's `id` cannot be read.
    """
    results: dict[str, Mapping[str, object]] = {}
    page = 1
    while True:
        answered = parse_page(await fetch_page(scheme, identifiers, page, http_client=http_client, api_key=api_key))
        for work in answered.results:
            results.setdefault(upstream_record.JsonObject.root(INDEX, work).required_str('id'), work)
        if not answered.results or page * _PER_PAGE >= answered.matched:
            break
        page += 1
    if len(results) != answered.matched:
        raise ValueError(
            f'OpenAlex counts {answered.matched} works for a {scheme} filter but served {len(results)} distinct'
        )
    return list(results.values())


async def _found_alone(doi: str, *, http_client: httpx2.AsyncClient, api_key: ApiKey) -> Found:
    """The answer for `doi` from `/works/doi:{doi}`, which serves its one work whole, or a 404 when OpenAlex holds none.

    Raises:
        httpx2.HTTPError: As `fetch_page`, but for a 404.
        ValueError: If OpenAlex answers with something other than a work stating `doi` (`read_results`).
        upstream_record.SchemaDriftError: If the work's DOI cannot be read.
    """
    # Every `/` encoded, so the DOI stays one path segment: httpx2 resolves a `.` or `..` segment before sending.
    url = f'{_WORKS_URL}/doi:{urllib.parse.quote(doi, safe="")}'
    sent = await _send(url, {'mailto': constants.CONTACT_EMAIL}, http_client=http_client, api_key=api_key)
    if isinstance(sent, httpx2.HTTPStatusError) and sent.response.status_code == httpx2.codes.NOT_FOUND:
        return read_results(Scheme.DOI, [doi], [])
    if isinstance(sent, httpx2.HTTPStatusError | httpx2.RequestError):
        raise sent
    answer = json.loads(sent.content)
    if not isinstance(answer, Mapping):
        raise ValueError(f'OpenAlex answered doi:{doi} with something other than a work')
    return read_results(Scheme.DOI, [doi], [answer])


async def _whole(
    work: Mapping[str, object], *, http_client: httpx2.AsyncClient, api_key: ApiKey
) -> Mapping[str, object]:
    """`work` as the works list served it, or as `/works/{id}` serves it when the list may have cut its authorships.

    Raises:
        httpx2.HTTPError: As `fetch_page`.
        ValueError: If OpenAlex answers the work's id with something other than that work.
        upstream_record.SchemaDriftError: If the work's `id` is not an OpenAlex work URL.
    """
    authorships = work.get('authorships')
    if not isinstance(authorships, list) or len(authorships) < _LISTED_AUTHORSHIPS:
        return work
    root = upstream_record.JsonObject.root(INDEX, work)
    work_url = root.required_str('id')
    work_id = _WORK_URL.fullmatch(work_url)
    if work_id is None:
        raise root.drift('id', f'is {work_url!r}, where an OpenAlex work URL is read')
    answer = json.loads(
        await _get(
            f'{_WORKS_URL}/{work_id.group(1)}',
            {'mailto': constants.CONTACT_EMAIL},
            http_client=http_client,
            api_key=api_key,
        )
    )
    if not isinstance(answer, Mapping) or answer.get('id') != work_url:
        raise ValueError(f'OpenAlex answered {work_url} with something other than that work')
    return answer


def sourced(work: Work) -> litcache_pb2.SourcedMetadata:
    """The `SourcedMetadata` of a paper OpenAlex resolved: its work the only record, the metadata filled from it.

    Raises:
        upstream_record.SchemaDriftError: If a metadata field cannot be read from the record (`metadata_fields.fill`).
    """
    source = litcache_pb2.MetadataSource(openalex_json=work.text)
    return litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)


def paired(resolved: litcache_pb2.SourcedMetadata, work: Work) -> litcache_pb2.SourcedMetadata:
    """Store OpenAlex's work of a paper beside the record of the index that resolved it, and fill the metadata again.

    The filled metadata's authors are then OpenAlex's (`metadata_fields.read`).

    Raises:
        ValueError: If `resolved` already holds OpenAlex's record.
        upstream_record.SchemaDriftError: If a metadata field cannot be read from the records (`metadata_fields.fill`).
    """
    if resolved.source.HasField('openalex_json'):
        raise ValueError("the source record already holds OpenAlex's record")
    source = litcache_pb2.MetadataSource()
    source.CopyFrom(resolved.source)
    source.openalex_json = work.text
    return litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
