"""NCBI LitVar2 adapter (keyless): the variant literature index's four endpoints.

The index keys an entity on whichever identifier its recogniser found in the text — an rsID, a
ClinGen allele id, or a bare change string under a gene — never on a variant, so one variant is
split across entities that share no record. This module reaches each endpoint and nothing more:
``autocomplete_entity_ids`` resolves a query string to candidate entity ids, ``entity_labels`` reads
one entity's own labels, ``search_pmids`` walks its ranked records, and ``gene_entities`` lists a
gene's whole inventory. Which queries to issue, which candidates to keep and how the labels line up
with a request are the caller's (``literature.variants``).

Every parse here is fail-loud on a shape it does not recognise: a skipped row or a defaulted census
would answer with a list silently short of the entity, which is exactly what the counts exist to rule
out.
"""

from __future__ import annotations

import ast
import urllib.parse
from collections.abc import Mapping
from typing import NamedTuple

import httpx2

from themis.services.evidence import errors
from themis.services.evidence.literature import pmids as pmids_mod

_AUTOCOMPLETE_URL = 'https://www.ncbi.nlm.nih.gov/research/litvar2-api/variant/autocomplete/'
_SEARCH_URL = 'https://www.ncbi.nlm.nih.gov/research/litvar2-api/search/'
_ENTITY_URL_TEMPLATE = 'https://www.ncbi.nlm.nih.gov/research/litvar2-api/variant/get/{entity_id}'
_GENE_URL_TEMPLATE = 'https://www.ncbi.nlm.nih.gov/research/litvar2-api/variant/search/gene/{gene}'
_SOURCE = 'NCBI LitVar2'


def _path_segment(value: str, *, subject: str) -> str:
    """``value`` percent-encoded as one path segment of a fixed route, or refused where it would not stay one.

    ``quote(safe='')`` leaves only ``A-Za-z0-9_.-~`` unencoded, so of every value a caller can supply, the
    empty string and the dot segments ``.`` and ``..`` are the ones the URL's path normalisation would
    resolve onto a different route rather than a different record.

    Raises:
        errors.InvalidRequestError: ``value`` is empty, ``.`` or ``..``.
    """
    if value in ('', '.', '..'):
        raise errors.InvalidRequestError(f'{subject} {value!r} is not an identifier {_SOURCE} can be asked about')
    return urllib.parse.quote(value, safe='')


class EntityLabels(NamedTuple):
    """What LitVar2 states about one entity, verbatim and independent of any request.

    ``caids`` holds more than one entry exactly when the entity spans more than one allele at a
    position. ``change`` is empty where the index carries no change string for the entity, which is
    the ordinary shape of an rsID-keyed one.
    """

    id: str
    rsid: str
    caids: tuple[str, ...]
    genes: tuple[str, ...]
    change: str

    def names_an_allele(self) -> bool:
        """Whether the index names an allele for this entity at all.

        A gene-level entity names none — it collects the gene's whole literature under a single id —
        so it is never a variant lookup's answer however loosely autocomplete matched it.
        """
        return bool(self.rsid or self.caids or self.change)


class ListedEntity(NamedTuple):
    """One row of a gene's entity inventory: an id, the keys it carries, and its publication count."""

    id: str
    rsid: str
    caid: str
    total_records: int


async def autocomplete_entity_ids(query: str, *, http_client: httpx2.AsyncClient) -> list[str]:
    """The entity ids autocomplete matches ``query`` to, in the index's own match order.

    Autocomplete matches loosely — on a prefix, and across the gene-level entity as readily as an
    allele-scoped one — so every id it returns is a candidate the caller judges, not an answer.

    Args:
        query: The identifier as the index is asked about it (a ClinGen id, an ``rs``-prefixed rsID,
            or a gene and a bare change).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The matched entity ids; empty where the index matched nothing.

    Raises:
        errors.InvalidRequestError: If LitVar2 refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If LitVar2 returns a 429 or a 5xx.
        ValueError: If the answer is not a list of matches.
    """
    response = await http_client.get(_AUTOCOMPLETE_URL, params={'query': query})
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'autocomplete for {query!r}')
    matches = response.json()
    if not isinstance(matches, list):
        raise ValueError(f'LitVar2 autocomplete answered {type(matches).__name__}, not a list')
    return [
        entity_id
        for match in matches
        if isinstance(entity_id := match.get('_id') if isinstance(match, Mapping) else None, str) and entity_id
    ]


async def entity_labels(entity_id: str, *, http_client: httpx2.AsyncClient) -> EntityLabels | None:
    """The index's account of one entity, or ``None`` where it holds no such entity.

    Autocomplete states a single ``clingen_id``; only this record states every ClinGen allele id an
    entity spans, which is what distinguishes an allele-scoped entity from a position-scoped one.
    LitVar2 answers an unknown id with 400 rather than 404, so that status alone means the index
    holds no such entity — a fact — while any other failure raises.

    Args:
        entity_id: The index's own entity id.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The entity's labels, or ``None`` when the index holds no entity under that id.

    Raises:
        errors.InvalidRequestError: If ``entity_id`` cannot be a path segment (empty, ``.`` or ``..``), or
            LitVar2 refuses the call (a non-400, non-429 4xx).
        httpx2.HTTPStatusError: If LitVar2 returns a 429 or a 5xx.
        ValueError: If the entity record is not a mapping, or states no id of its own.
    """
    url = _ENTITY_URL_TEMPLATE.format(entity_id=_path_segment(entity_id, subject='entity id'))
    response = await http_client.get(url)
    if response.status_code == httpx2.codes.BAD_REQUEST:
        return None
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'entity {entity_id!r}')
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError(f'LitVar2 entity {entity_id!r} answered {type(payload).__name__}')
    resolved_id = _field(payload, '_id')
    if not resolved_id:
        raise ValueError(f'LitVar2 entity {entity_id!r} came back with no id of its own')
    caid = _field(payload, 'clingen_id')
    return EntityLabels(
        id=resolved_id,
        rsid=_field(payload, 'rsid'),
        caids=_strings(payload.get('clingen_ids')) or ((caid,) if caid else ()),
        genes=_strings(payload.get('gene')),
        change=_field(payload, 'hgvs'),
    )


async def search_pmids(entity_id: str, limit: int, *, http_client: httpx2.AsyncClient) -> tuple[list[str], int]:
    """An entity's ranked PMIDs up to ``limit``, and the count the index states for it whole.

    The count is what makes a returned list legible as a prefix, and it rides on the same pages the
    walk already fetches. A page missing either it or the pagination bound would leave the walk
    reporting a truncated list as the whole of the entity, so it raises instead.

    The PMIDs come back keyed (``pmids.pmid_key``), since they go on to key the record lookup as well
    as its query; one the index states in any other form is a change in that shape rather than a row
    to skip, and raises.

    Args:
        entity_id: The index's own entity id.
        limit: The most PMIDs to walk to; the walk stops at the first page that reaches it.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ranked PMIDs the walk reached, and the whole count the index states for the entity.

    Raises:
        errors.InvalidRequestError: If LitVar2 refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If LitVar2 returns a 429 or a 5xx.
        ValueError: A page is not a mapping carrying an integer ``count`` and ``total_pages`` — the
            shape the walk's own bound and the census both read — or it states a PMID that is not one.
    """
    found: list[str] = []
    page = 1
    while True:
        response = await http_client.get(
            _SEARCH_URL, params={'variant': entity_id, 'sort': 'score desc', 'page': str(page)}
        )
        errors.raise_for_status(response, upstream=_SOURCE, subject=f'search page {page} of {entity_id!r}')
        payload = response.json()
        total, total_pages = _search_census(payload, entity_id)
        for result in _search_results(payload):
            stated = result.get('pmid')
            if stated is not None:
                try:
                    found.append(pmids_mod.pmid_key(str(stated)))
                except ValueError as e:
                    raise ValueError(f'LitVar2 search page for {entity_id!r}: {e}') from e
                if len(found) >= limit:
                    return found, total
        if page >= total_pages:
            return found, total
        page += 1


async def gene_entities(gene: str, *, http_client: httpx2.AsyncClient) -> list[ListedEntity]:
    """The rows of LitVar2's per-gene listing, one per line.

    The endpoint answers in Python ``repr`` syntax — single-quoted keys and values — one record per
    line, not JSON, so a JSON parser fails on the first line and each is read as a Python literal
    instead. A line that is not a mapping carrying a string ``_id`` and an integer ``pmids_count`` is
    a change in that shape rather than a row to skip, and raises: skipping would answer a listing
    silently short of the gene's entities, which is exactly what this listing exists to rule out.

    Args:
        gene: HGNC symbol.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The gene's rows in the index's own order; empty where it lists none.

    Raises:
        errors.InvalidRequestError: If ``gene`` cannot be a path segment (empty, ``.`` or ``..``), or LitVar2
            refuses the call (a non-429 4xx).
        httpx2.HTTPStatusError: If LitVar2 returns a 429 or a 5xx.
        ValueError: A line does not parse, or does not carry the two fields a row is.
    """
    response = await http_client.get(_GENE_URL_TEMPLATE.format(gene=_path_segment(gene, subject='gene')))
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'gene listing for {gene!r}')
    return _listed_entities(response.text)


def _listed_entities(payload: str) -> list[ListedEntity]:
    entities: list[ListedEntity] = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            record = ast.literal_eval(line)
        except (SyntaxError, ValueError) as e:
            raise ValueError(f'LitVar2 gene listing line is not a Python literal: {line[:120]!r}') from e
        if not isinstance(record, Mapping):
            raise ValueError(f'LitVar2 gene listing line is a {type(record).__name__}, not a mapping')
        entity_id = record.get('_id')
        total = record.get('pmids_count')
        if not isinstance(entity_id, str) or not entity_id or not isinstance(total, int):
            raise ValueError(f'LitVar2 gene listing row lacks an id or a count: {line[:120]!r}')
        entities.append(
            ListedEntity(
                id=entity_id, rsid=_field(record, 'rsid'), caid=_field(record, 'clingen_id'), total_records=total
            )
        )
    return entities


def _search_census(payload: object, entity_id: str) -> tuple[int, int]:
    """A search page's whole-entity count and page bound (fail-loud on either being absent)."""
    total = payload.get('count') if isinstance(payload, Mapping) else None
    total_pages = payload.get('total_pages') if isinstance(payload, Mapping) else None
    if not isinstance(total, int) or not isinstance(total_pages, int):
        raise ValueError(f'LitVar2 search page for {entity_id!r} states no count or page bound')
    return total, total_pages


def _search_results(payload: object) -> list[Mapping[str, object]]:
    results = payload.get('results') if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        raise ValueError(f'LitVar2 search page carries no results list, but {type(results).__name__}')
    return [entry for entry in results if isinstance(entry, Mapping)]


def _field(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    return value if isinstance(value, str) else ''


def _strings(value: object) -> tuple[str, ...]:
    """The non-empty strings in a LitVar2 list field, empty where it carries none."""
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str) and entry)
