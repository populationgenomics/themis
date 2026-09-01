"""MONDO subclass closure, read from EBI OLS4.

`GeneDisease` resolves the entity a caller names against the terms its curators curate, and curators
routinely curate a *subtype* of the term a presentation names. Deciding that "this curated term is a
kind of the term I asked about" is what MONDO's subclass relation is for, so it is read from the
ontology rather than guessed from labels.

The closure is asked for the CURATED term (its ancestors), never for the requested one (its
descendants): a curated term has a handful of ancestors and a broad requested term can have
thousands of descendants, which OLS4 paginates. The terms come from the reference tables rather than
from a caller, so every failure here is a fault of ours or of OLS4 — none of them is reported as a
malformed request.
"""

from __future__ import annotations

import asyncio
import dataclasses
import urllib.parse
from collections.abc import Mapping, Sequence

import httpx2

from themis.services.evidence import errors

_BASE_URL = 'https://www.ebi.ac.uk/ols4/api'
_OBO_IRI = 'http://purl.obolibrary.org/obo/'
_SOURCE = 'EBI OLS4 (MONDO)'
_PAGE_SIZE = 1000


@dataclasses.dataclass(frozen=True)
class MondoClosureResult:
    """The subclass closure above each queried term.

    Attributes:
        ancestors: Queried MONDO term -> the MONDO terms above it, transitively.
        raw: The same mapping, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The MONDO release OLS4 currently serves.
        query: The terms the closure was read for.
    """

    ancestors: dict[str, tuple[str, ...]]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _term_url(mondo_id: str, suffix: str) -> str:
    """OLS4 addresses a term by its IRI, percent-encoded twice inside the path."""
    iri = urllib.parse.quote(_OBO_IRI + mondo_id.replace(':', '_'), safe='')
    return f'{_BASE_URL}/ontologies/mondo/terms/{urllib.parse.quote(iri, safe="")}{suffix}'


async def _get_json(url: str, *, http_client: httpx2.AsyncClient) -> Mapping[str, object]:
    response = await http_client.get(url, headers={'Accept': 'application/json'})
    if response.is_client_error and response.status_code != httpx2.codes.TOO_MANY_REQUESTS:
        # Not InvalidRequestError: the term asked about is a curated one, so a refusal is a stale
        # reference table or a retired MONDO term, never the caller's request.
        raise ValueError(f'{_SOURCE} rejected {url} ({response.status_code}): {errors.clipped(response.text.strip())}')
    response.raise_for_status()  # 429 / 5xx stay retryable
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f'{_SOURCE} returned {type(payload).__name__} for {url}, expected an object')
    return payload


def _terms(payload: Mapping[str, object], url: str) -> list[Mapping[str, object]]:
    """The terms of one OLS4 collection response, refusing a response it cannot vouch for.

    A short or paginated collection read as complete would flow on as a hierarchy fact — a closure
    missing the very ancestor the caller asked about — so neither is tolerated.
    """
    page = payload.get('page')
    if not isinstance(page, dict):
        raise ValueError(f'{_SOURCE} response for {url} carries no page block')
    pages, elements = page.get('totalPages'), page.get('totalElements')
    if not isinstance(pages, int) or not isinstance(elements, int):
        raise ValueError(f'{_SOURCE} response for {url} states no page count: {page!r}')
    if pages > 1:
        raise ValueError(f'{_SOURCE} paginates {url} over {pages} pages; the closure is truncated')
    embedded = payload.get('_embedded')
    terms = embedded.get('terms', []) if isinstance(embedded, dict) else []
    if not isinstance(terms, list):
        raise ValueError(f'{_SOURCE} response for {url} carries a non-list term collection')
    if len(terms) != elements:
        raise ValueError(f'{_SOURCE} promises {elements} terms for {url} and carries {len(terms)}')
    return terms


def _mondo_ids(terms: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """The MONDO ids of a term collection; OLS4 mixes upper ontologies into a closure."""
    return tuple(
        sorted(
            str(term['obo_id'])
            for term in terms
            if isinstance(term.get('obo_id'), str) and str(term['obo_id']).startswith('MONDO:')
        )
    )


async def _fetch_ancestors(mondo_id: str, *, http_client: httpx2.AsyncClient) -> tuple[str, ...]:
    url = _term_url(mondo_id, f'/ancestors?size={_PAGE_SIZE}')
    ancestors = _mondo_ids(_terms(await _get_json(url, http_client=http_client), url))
    if not ancestors:
        raise ValueError(f'{_SOURCE} places {mondo_id} under no MONDO term; every disease term has at least one')
    return ancestors


async def fetch_mondo_version(*, http_client: httpx2.AsyncClient) -> str:
    """The MONDO release OLS4 currently serves, which dates every closure read from it.

    Raises:
        ValueError: If OLS4 states no version for the ontology, which would leave the closure
            undated and the resolution unreproducible.
    """
    payload = await _get_json(f'{_BASE_URL}/ontologies/mondo', http_client=http_client)
    config = payload.get('config')
    version = config.get('version') if isinstance(config, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f'{_SOURCE} states no version for the MONDO ontology')
    return version


async def fetch_subclass_closure(mondo_ids: Sequence[str], *, http_client: httpx2.AsyncClient) -> MondoClosureResult:
    """The MONDO terms above each of ``mondo_ids``, transitively.

    Args:
        mondo_ids: The curated terms whose ancestry decides whether a requested term subsumes them.
        http_client: The shared async client.

    Returns:
        The closure per queried term, dated by the MONDO release it was read from.

    Raises:
        ValueError: If OLS4 rejects a term, or answers with a truncated collection, an undated
            ontology, or a term it places under nothing.
        httpx2.HTTPStatusError: On a rate-limited or failing OLS4, which a retry can clear.
    """
    version = await fetch_mondo_version(http_client=http_client)
    closures = await asyncio.gather(*(_fetch_ancestors(mondo_id, http_client=http_client) for mondo_id in mondo_ids))
    ancestors = dict(zip(mondo_ids, closures, strict=True))
    return MondoClosureResult(
        ancestors=ancestors,
        raw={term: list(above) for term, above in ancestors.items()},
        source=_SOURCE,
        dataset_versions=(f'MONDO {version}',),
        query=f'ancestors of {list(mondo_ids)}',
    )
