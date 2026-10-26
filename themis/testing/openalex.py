"""A fake of OpenAlex's works endpoint, answering `pmid` and `doi` filters from works held in memory.

It answers as the live endpoint does for the requests `themis.litcache.openalex.find` makes. It parses `filter` as
OpenAlex does: filters separated by `,`, each `key:values` with the values separated by `|`, so a value holding either
character is split. It answers the works that, for every filter, state one of its values as their `ids.pmid` or `doi`
(a DOI matched case-insensitively), paged by `per-page` and `page`, with `meta.count` the number matched across every
page, and each work's authorships cut at 100 as a list cuts them. `/works/{id}` answers with that work whole, and
`/works/doi:{doi}` with the work stating that DOI whole, or a 404. A filter on any other key, or one with no `:`, is
refused with a 400, so a test cannot pass by asking something the fake does not model, and so is a URL longer than
OpenAlex accepts. A request carrying an `api_key` other than `API_KEY` is refused with a 401; one carrying none is
answered, as OpenAlex answers a keyless request (litfetch's own OpenAlex lookup sends no key).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import httpx2

from themis.litcache import openalex

HOST = 'api.openalex.org'
_PMID_URL = 'https://pubmed.ncbi.nlm.nih.gov/'
_DOI_URL = 'https://doi.org/'
_OPENALEX_URL = 'https://openalex.org/'
# The only key the fake answers; a test hands it to the code under test.
API_KEY = openalex.ApiKey('fake-openalex-key')
# OpenAlex answers a longer request URL with a 400.
MAX_URL_BYTES = 8190
# A works list serves at most this many authorships of a work; `/works/{id}` serves all of them.
LISTED_AUTHORSHIPS = 100
_BY_DOI = '/works/doi:'


def authorships(count: int) -> list[dict[str, object]]:
    """`count` authorships, each naming a distinct author with no ORCID iD."""
    return [{'author': {'display_name': f'Author {n}', 'orcid': None}} for n in range(count)]


def work(*, work_id: str, pmid: str | None = None, doi: str | None = None, **fields: object) -> dict[str, object]:
    """An OpenAlex work stating `pmid` and `doi` in their URL forms, with `fields` as its other keys.

    Args:
        work_id: The work's OpenAlex id, bare (`W123`).
        pmid: The PMID it states, or `None`.
        doi: The DOI it states, or `None`.
        **fields: Further keys; `title` defaults to the work id.
    """
    record: dict[str, object] = {
        'id': f'{_OPENALEX_URL}{work_id}',
        'doi': f'{_DOI_URL}{doi}' if doi is not None else None,
        'title': work_id,
        'ids': {'openalex': f'{_OPENALEX_URL}{work_id}'} | ({'pmid': f'{_PMID_URL}{pmid}'} if pmid else {}),
    }
    record.update(fields)
    return record


def _states(record: Mapping[str, object], scheme: str, value: str) -> bool:
    """Whether `record` matches a `scheme` filter on `value`: a DOI filter matches its own DOI or any location's."""
    if scheme == 'doi':
        locations = record.get('locations')
        listed = locations if isinstance(locations, list) else []
        located = [location.get('doi') for location in listed if isinstance(location, Mapping)]
        return any(
            isinstance(stated, str) and stated.removeprefix(_DOI_URL).casefold() == value.casefold()
            for stated in [record.get('doi'), *located]
        )
    ids = record.get('ids')
    return isinstance(ids, Mapping) and ids.get('pmid') == f'{_PMID_URL}{value}'


def answer(works: Sequence[Mapping[str, object]], request: httpx2.Request) -> httpx2.Response:
    """Answer a works request from `works`, as OpenAlex would.

    Raises:
        ValueError: If the request is not to OpenAlex's works endpoint.
    """
    if request.url.host != HOST or not (request.url.path == '/works' or request.url.path.startswith('/works/')):
        raise ValueError(f'not an OpenAlex works request: {request.url}')
    if len(str(request.url)) > MAX_URL_BYTES:
        return httpx2.Response(400, json={'error': 'Request URL too long'})
    if request.url.params.get('api_key', API_KEY.value) != API_KEY.value:
        return httpx2.Response(401, json={'error': 'the fake answers its API_KEY or no key, not another'})
    if request.url.path.startswith(_BY_DOI):
        return _answer_doi(works, request.url.path.removeprefix(_BY_DOI))
    if request.url.path != '/works':
        return _answer_one(works, request.url.path.removeprefix('/works/'))
    filters: list[tuple[str, list[str]]] = []
    for clause in request.url.params.get('filter', '').split(','):
        scheme, colon, values = clause.partition(':')
        if not colon or scheme not in ('pmid', 'doi'):
            return httpx2.Response(400, json={'error': f'the fake answers pmid and doi filters, not {clause!r}'})
        filters.append((scheme, values.split('|')))
    matched = [
        record
        for record in works
        if all(any(_states(record, scheme, value) for value in wanted) for scheme, wanted in filters)
    ]
    per_page = int(request.url.params.get('per-page', '25'))
    page = int(request.url.params.get('page', '1'))
    results = [_listed(record) for record in matched[(page - 1) * per_page : page * per_page]]
    return httpx2.Response(200, content=json.dumps({'meta': {'count': len(matched)}, 'results': results}).encode())


def _listed(record: Mapping[str, object]) -> Mapping[str, object]:
    """`record` as a works list serves it: its authorships cut at `LISTED_AUTHORSHIPS`."""
    authorships = record.get('authorships')
    if isinstance(authorships, list) and len(authorships) > LISTED_AUTHORSHIPS:
        return {**record, 'authorships': authorships[:LISTED_AUTHORSHIPS]}
    return record


def _answer_doi(works: Sequence[Mapping[str, object]], doi: str) -> httpx2.Response:
    """Answer `/works/doi:{doi}` with the work stating `doi` whole, or a 404.

    Raises:
        ValueError: If more than one work states `doi`, which this endpoint cannot answer.
    """
    stating = [record for record in works if _states(record, 'doi', doi)]
    if len(stating) > 1:
        raise ValueError(f'the fake holds {len(stating)} works stating doi {doi!r}; /works/doi: answers one')
    if not stating:
        return httpx2.Response(404, json={'error': f'no work with doi {doi}'})
    return httpx2.Response(200, content=json.dumps(stating[0]).encode())


def _answer_one(works: Sequence[Mapping[str, object]], work_id: str) -> httpx2.Response:
    """Answer `/works/{work_id}` with that work whole, or a 404."""
    for record in works:
        if record.get('id') == f'{_OPENALEX_URL}{work_id}':
            return httpx2.Response(200, content=json.dumps(record).encode())
    return httpx2.Response(404, json={'error': f'no work {work_id}'})
