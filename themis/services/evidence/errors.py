"""The evidence error taxonomy: what a failed rpc means, as distinct from how it failed.

Held apart from ``backend`` so the upstream adapters can raise these without importing the backend
that composes them. The servicer maps each to its gRPC status; anything else surfaces as ``UNKNOWN``,
which is the honest status for a fault we have not characterised.

``raise_for_status`` places one upstream HTTP status on that taxonomy; ``docs/design/evidence-interfaces.md``
records which transports it is used on, and which are exempt.
"""

from __future__ import annotations

import urllib.parse

import httpx2

# One interpolated part of a message; the whole of one. Both bounds exist because a message becomes a
# gRPC trailer and an over-limit trailer is dropped whole, taking the diagnosis with it.
_MAX_DETAIL = 512
_MAX_MESSAGE = 2048


_CUT_MARKER = '…'


def _wire_cost(text: str) -> int:
    r"""What ``text`` occupies in a ``grpc-message`` trailer, which carries percent-encoded UTF-8.

    ``errors='replace'`` because a JSON body can decode to a lone surrogate — ``"\\ud800"`` is a
    valid escape and yields a ``str`` that will not encode — and a message *about* a bad payload must
    not itself raise on the way out.
    """
    return len(urllib.parse.quote(text, errors='replace'))


def clipped(text: str, limit: int = _MAX_DETAIL) -> str:
    """``text`` bounded to ``limit`` bytes *as the trailer carries them*, cut marker included.

    Encoded bytes, not characters: a request field or an upstream body may be non-ASCII, and one such
    character costs up to twelve bytes once percent-encoded. A character bound passes a message the
    transport then drops for exceeding its metadata limit — which reaches the caller as
    ``RESOURCE_EXHAUSTED`` with the diagnosis gone, and is never retried. That is the outcome the
    bound exists to prevent, so the bound has to be in the units the limit is enforced in.
    """
    if _wire_cost(text) <= limit:
        return text
    budget = limit - _wire_cost(_CUT_MARKER)
    kept: list[str] = []
    used = 0
    for char in text:
        used += _wire_cost(char)
        if used > budget:
            break
        kept.append(char)
    return f'{"".join(kept)}{_CUT_MARKER}'


class _TrailerSafeError(Exception):
    """Base for the taxonomy errors: the message is clipped to fit a gRPC trailer.

    Every one of these messages interpolates a caller- or upstream-supplied value, and neither is
    bounded — a rejected request field is echoed back whole, an accession list runs to hundreds of
    UIDs, a 4xx body can be an HTML page. Clipping on construction is what makes the bound hold
    without every raise site in the service remembering to.
    """

    def __init__(self, message: str) -> None:
        super().__init__(clipped(message, _MAX_MESSAGE))


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


def raise_for_status(response: httpx2.Response, *, upstream: str, subject: str, detail: str | None = None) -> None:
    """Fail a non-2xx upstream response, telling a refusal from a fault.

    A non-429 4xx is the upstream judging the request as issued, so reissuing it unchanged cannot
    change the answer: it becomes ``InvalidRequestError`` (``INVALID_ARGUMENT``) rather than the
    ``UNKNOWN`` a bare ``httpx2.Response.raise_for_status`` yields, which the guest's retry helper
    counts as transient and reissues with backoff. 429 and 5xx stay ``httpx2.HTTPStatusError``: a
    retry can clear those. An upstream that reports "no record" with a 4xx needs its own branch
    ahead of this call — ``UnknownVariantError`` is a settled answer, not a refusal.

    Not every transport qualifies; ``docs/design/evidence-interfaces.md`` names the exemptions and the test.

    Args:
        response: The upstream response to judge; a 2xx returns.
        upstream: The source's label, opening the message.
        subject: What the request was about, already formatted (e.g. ``f'{variant!r}'``).
        detail: The upstream's own explanation of the failure; its body when omitted.

    Raises:
        InvalidRequestError: On a non-429 4xx.
        httpx2.HTTPStatusError: On a 429, a 5xx, or any other non-2xx.
    """
    if response.is_success:
        return
    explained = clipped(detail if detail is not None else response.text.strip())
    named = clipped(subject)
    # 429 is a 4xx about the caller's rate, not about the request, so it stays retryable.
    if response.is_client_error and response.status_code != httpx2.codes.TOO_MANY_REQUESTS:
        raise InvalidRequestError(f'{upstream} rejected {named} ({response.status_code}): {explained}')
    raise httpx2.HTTPStatusError(
        f'{upstream} returned {response.status_code} for {named}: {explained}',
        request=response.request,
        response=response,
    )
