"""The evidence error taxonomy: what a failed rpc means, as distinct from how it failed.

Held apart from ``backend`` so the upstream adapters can raise these without importing the backend
that composes them. The servicer maps each to its gRPC status, and an upstream failure a reissue may
clear to ``UNAVAILABLE`` (``transient_upstream_fault``); anything else surfaces as ``UNKNOWN``, which
is the honest status for a fault we have not characterised.

``raise_for_status`` places one upstream HTTP status on that taxonomy; ``docs/design/evidence-interfaces.md``
records which transports it is used on, and which are exempt.
"""

from __future__ import annotations

import re
import ssl

import httpx2

from themis.common import trailer


class _TrailerSafeError(Exception):
    """Base for the taxonomy errors: the message is clipped to fit a gRPC trailer.

    Every one of these messages interpolates a caller- or upstream-supplied value, and neither is
    bounded — a rejected request field is echoed back whole, an accession list runs to hundreds of
    UIDs, a 4xx body can be an HTML page. Clipping on construction is what makes the bound hold
    without every raise site in the service remembering to.
    """

    def __init__(self, message: str) -> None:
        super().__init__(trailer.clipped(message, trailer.MESSAGE_LIMIT))


class UnknownVariantError(_TrailerSafeError):
    """The source holds no record of the queried variant/gene, and said so.

    A settled answer, not a fault: absence from gnomAD is the POP_FRQ rarity input, and absence from
    MaveDB/SpliceAI means no assay or no score exists. The servicer maps it to gRPC ``NOT_FOUND`` so a
    caller can tell it from an upstream that is down and stop retrying a question already answered.
    Distinct from a malformed payload, which stays a ``ValueError``.

    Only for a source that answered. A source that could not tell the queried variant from a
    malformed one has not said "no record", and must not raise this — the rpcs whose absence *is* the
    evidence (`Gnomad`, `Splice`) are where that distinction is load-bearing.
    """


class InvalidRequestError(_TrailerSafeError):
    """A request field is not a form the rpc accepts.

    A caller-side precondition failure, distinct from an upstream miss: the servicer maps it to gRPC
    ``INVALID_ARGUMENT`` so a malformed request reads differently from an upstream that is down.
    """


class UnresolvedEntityError(_TrailerSafeError):
    """A well-formed request names a disease entity the sources' curations do not settle.

    Raised where `GeneDisease` would otherwise have to choose: several curated entities sit under the
    requested MONDO term, or the gene is curated and neither that term nor a descendant of it is
    (under the requested inheritance). Both are the analyst's question to answer, and answering it
    with the nearest or the strongest curation is what makes a wrong gate level look like a fact.

    The servicer maps it to gRPC ``FAILED_PRECONDITION``: the request is well-formed
    (not ``INVALID_ARGUMENT``) and the sources hold the gene (not ``NOT_FOUND``) — what is missing is
    an entity the caller must restate. The message names every curated entity so the caller can.
    """


class InconsistentSourcesError(_TrailerSafeError):
    """Two sources a lookup composes disagree about whether a record exists.

    One names an entity the other holds nothing under: the registry's crosswalk names a ClinVar
    variation ClinVar answers no archive for, or a transcript alignment names a gene symbol ClinVar
    indexes no record under. Read as an absence, the disagreement becomes the finding — "no ClinVar
    record for this allele", "no informative variant at this codon" — off an answer neither source
    gave, so this is never an `UnknownVariantError`.

    The servicer maps it to gRPC ``FAILED_PRECONDITION``: the request is well-formed
    (not ``INVALID_ARGUMENT``), both sources answered (not an uncharacterised fault), and
    reconciling them is this service's own job rather than anything a reissue can change. The
    message names both sources and what each said, which is what reconciling them starts from.
    """


def first_failure(failures: BaseExceptionGroup[BaseException]) -> BaseException:
    """The first leaf of a task-group failure, so a concurrent leg's status stays its own.

    A group left to escape reaches the servicer as an uncharacterised fault, discarding the
    ``UnknownVariantError`` / ``InvalidRequestError`` distinction the whole taxonomy rests on.
    """
    leaf = failures.exceptions[0]
    return first_failure(leaf) if isinstance(leaf, BaseExceptionGroup) else leaf


class UpstreamStatusError(httpx2.HTTPStatusError):
    """A 429 or 5xx, or another non-2xx no rule places, from an upstream named by its label.

    The message names the source, the request's subject, the status and the upstream's stated
    reason (``explanation``), and never the URL: it is what ``transient_upstream_fault`` reports for
    a 429 or 5xx.
    """

    def __init__(self, message: str, *, request: httpx2.Request, response: httpx2.Response, upstream: str) -> None:
        super().__init__(message, request=request, response=response)
        self.upstream = upstream


# What an HTML page opens with, once leading whitespace is dropped. Sniffed as well as read off the
# content type, because an error page served by a proxy or a framework's default handler often
# declares none, or `text/plain`.
_HTML_OPENINGS = ('<!doctype html', '<html', '<head', '<body')

# Our egress address, echoed in an error body. NCBI's rate-limit answer names the caller under
# `api-key` when no key is sent (`{"error":"API rate limit exceeded","api-key":"<our address>",...}`),
# and would name the key itself if one were: the field is withheld whole, and any other address a
# body quotes is masked.
_API_KEY_FIELD = re.compile(r'("api[-_]?key"\s*:\s*)"[^"]*"', re.IGNORECASE)
# An address stands on its own: never inside a word, a version string or a dotted identifier. The
# lookarounds are what keep `std::bad_alloc`, `Bio::EnsEMBL::Variation`, `NC_012920.1:3242::G` and
# `v1.2.3.4` unmasked; an enzyme's EC number is the one bare dotted quad that is not an address. A
# colon may precede an IPv4 address, as a label's does (`IP:10.0.0.1`).
_V4 = r'(?:\d{1,3}\.){3}\d{1,3}'
_H = r'[0-9A-Fa-f]{1,4}'
_IPV4 = re.compile(rf'(?<![\w.])(?<!EC ){_V4}(?!\.?\w)')
# Only the forms with a `::` or all eight groups, so a clock time is never taken for one; an
# IPv4-mapped tail (`::ffff:10.0.0.1`) is read as part of the address rather than left half-masked.
_IPV6 = re.compile(
    rf'(?<![\w:.])(?:(?:{_H}(?::{_H})*)?::(?:{_H}:){{0,5}}{_V4}'
    rf'|(?:{_H}:){{6}}{_V4}'
    rf'|(?:{_H}:){{7}}{_H}'
    rf'|(?:{_H}:){{1,7}}:(?:{_H}(?::{_H}){{0,6}})?'
    rf'|::{_H}(?::{_H}){{0,6}})(?![\w:])'
)


def _is_html(response: httpx2.Response, text: str) -> bool:
    if response.headers.get('content-type', '').lower().startswith('text/html'):
        return True
    return text.lstrip()[:16].lower().startswith(_HTML_OPENINGS)


def _scrubbed(text: str) -> str:
    """``text`` with any key field withheld and any network address masked."""
    text = _API_KEY_FIELD.sub(r'\1"<withheld>"', text)
    text = _IPV6.sub('<address>', text)
    return _IPV4.sub('<address>', text)


def explanation(response: httpx2.Response, detail: str | None = None) -> str:
    """The upstream's reason for a failed response, as a message may quote it.

    ``detail`` where the adapter read one out of the body's envelope, else the body. An HTML page is
    named rather than quoted, whichever the text came from: it states nothing a caller acts on and
    costs the trailer budget. A key field and any network address are withheld, since an upstream
    can echo our egress address or credential back, and the message reaches the sandbox agent.
    """
    body = response.text
    text = detail if detail is not None else body.strip()
    if _is_html(response, body) or _is_html(response, text):
        return 'an HTML error page'
    return trailer.clipped(_scrubbed(text))


def raise_for_status(response: httpx2.Response, *, upstream: str, subject: str, detail: str | None = None) -> None:
    """Fail a non-2xx upstream response, telling a refusal from a fault.

    A non-429 4xx is the upstream judging the request as issued, so reissuing it unchanged cannot
    change the answer: it becomes ``InvalidRequestError`` (``INVALID_ARGUMENT``) rather than the
    ``UNKNOWN`` a bare ``httpx2.Response.raise_for_status`` yields, which the guest's retry helper
    counts as transient and reissues with backoff. 429 and 5xx become ``UpstreamStatusError``, which
    the servicer reports as ``UNAVAILABLE``: a retry can clear those. An upstream that reports "no
    record" with a 4xx needs its own branch ahead of this call — ``UnknownVariantError`` is a settled
    answer, not a refusal.

    Not every transport qualifies; ``docs/design/evidence-interfaces.md`` names the exemptions and the test.

    Args:
        response: The upstream response to judge; a 2xx returns.
        upstream: The source's label, opening the message.
        subject: What the request was about, already formatted (e.g. ``f'{variant!r}'``).
        detail: The upstream's own explanation of the failure, read out of its envelope; the body
            when omitted. Either way it passes through ``explanation``.

    Raises:
        InvalidRequestError: On a non-429 4xx.
        UpstreamStatusError: On a 429, a 5xx, or any other non-2xx.
    """
    if response.is_success:
        return
    explained = explanation(response, detail)
    named = trailer.clipped(subject)
    # 429 is a 4xx about the caller's rate, not about the request, so it stays retryable.
    if response.is_client_error and response.status_code != httpx2.codes.TOO_MANY_REQUESTS:
        raise InvalidRequestError(f'{upstream} rejected {named} ({response.status_code}): {explained}')
    raise UpstreamStatusError(
        f'{upstream} returned {response.status_code} for {named}: {explained}',
        request=response.request,
        response=response,
        upstream=upstream,
    )


def _retryable_status(status: int) -> bool:
    return status == httpx2.codes.TOO_MANY_REQUESTS or status >= httpx2.codes.INTERNAL_SERVER_ERROR


def _host(error: httpx2.HTTPError) -> str:
    """The host the failed request was for; an error raised with no request names none.

    Only a public upstream's host can appear: every request goes through `destinations.admitting_client`,
    which refuses any host it does not admit before the request is sent.
    """
    try:
        return error.request.url.host
    except RuntimeError:  # httpx raises this for a `.request` never set, not an AttributeError
        return 'an upstream'


def upstream_of(error: httpx2.HTTPError) -> str:
    """The upstream a failed request was for: its label where ``raise_for_status`` raised, else its host."""
    return error.upstream if isinstance(error, UpstreamStatusError) else _host(error)


def _is_tls_verification(error: BaseException) -> bool:
    """Whether a failed connection was refused by certificate verification, anywhere down its chain."""
    seen: set[int] = set()
    link: BaseException | None = error
    while link is not None and id(link) not in seen:
        if isinstance(link, ssl.SSLCertVerificationError):
            return True
        seen.add(id(link))
        link = link.__cause__ or link.__context__
    return False


def transient_upstream_fault(error: BaseException) -> str | None:
    """What an upstream's failure is reported as where a reissue may clear it, or None for any other error.

    The message states what happened, not what it means: the status the upstream returned or the
    way the connection failed, and that such a failure is usually transient. Reported as ``UNAVAILABLE``:
    a 429 or 5xx; a connect, read or write timeout; and a connection that could not be made or broke
    off. Everything else stays an uncharacterised fault of ours (``UNKNOWN``): any other status (a
    3xx, a 4xx on an exempt transport), a malformed request line or an unsupported scheme, a
    ``PoolTimeout`` (this service's own connections are all in use; the upstream was never asked),
    and a connection refused by TLS verification, which either our trust store or the upstream's
    certificate causes and no reissue clears.

    The upstream is named by its label where ``raise_for_status`` raised, else by host, and never by
    URL: a URL repeats the whole query, including parameters this service adds to it (the dbNSFP
    column list on a VEP call), and a message is a bounded trailer the sandbox agent reads.
    """
    if isinstance(error, UpstreamStatusError):
        status = error.response.status_code
        return f'{error}; {_status_reading(status)}' if _retryable_status(status) else None
    if isinstance(error, httpx2.HTTPStatusError):
        status = error.response.status_code
        return f'{_host(error)} returned {status}; {_status_reading(status)}' if _retryable_status(status) else None
    if isinstance(error, httpx2.PoolTimeout):
        return None
    if isinstance(error, httpx2.TimeoutException):
        return f'{_host(error)} did not answer in time ({type(error).__name__}); a timeout is usually transient'
    if isinstance(error, (httpx2.NetworkError, httpx2.RemoteProtocolError)) and not _is_tls_verification(error):
        failed = f'the connection to {_host(error)} failed ({type(error).__name__})'
        return f'{failed}; a failed connection is usually transient'
    return None


def _status_reading(status: int) -> str:
    if status == httpx2.codes.TOO_MANY_REQUESTS:
        return 'a 429 is the upstream limiting its callers, which a later reissue usually clears'
    return 'a 5xx is usually transient'
