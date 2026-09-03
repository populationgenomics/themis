"""OpenAlex batch resolution: DOI → cross-ids + the work record, whole.

The batchable DOI resolver for the papers idconv cannot route — idconv is PMC-scoped, so a DOI in
PubMed-but-not-PMC (subscription journals, the bulk of a DOI-only seed) or not in PubMed at all
(preprints) never reaches efetch through it. OpenAlex's works endpoint takes up to `_MAX_IDS` DOIs
in one filter query and returns each work whole. The record is stored as the envelope's `openalex`
field in OpenAlex's own schema (`openalex.proto`, a mirror of the JSON), loaded strictly
(`themis.litcache.mirror`); the one shape the mirror's header names, `abstract_inverted_index`, an
object whose values are arrays, becomes a `Positions` per word before the parse.

Two surfaces over one fetch, matching the litfetch/litcache seam: the cross-ids a work carries
(`pmid`, `pmcid`, read bare from their URL forms) are the id-resolution half — the ladder
(`themis.litcache.resolve`) prefers them, routing a discovered `pmid` back into batched efetch for
a PubMed-native record — and the record itself is the bibliographic half litcache owns, taken for
the no-`pmid` residual. A DOI OpenAlex does not know is absent from the result — the caller's
`unknown`, never invented; a work whose record does not fit the mirror is charged to its DOI alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import httpx2

from themis.common import constants
from themis.litcache import mirror, paper_metadata
from themis.litcache.models import litcache_pb2, openalex_pb2

_WORKS_URL = 'https://api.openalex.org/works'
# OpenAlex accepts up to 50 values in one filter OR-list; a larger batch is chunked.
_MAX_IDS = 50
INDEX = 'openalex'

_DOI_PREFIX = 'https://doi.org/'
_PMID_PREFIX = 'https://pubmed.ncbi.nlm.nih.gov/'
_PMCID_PREFIX = 'https://www.ncbi.nlm.nih.gov/pmc/articles/'


class ParsedWorks(NamedTuple):
    """A works response read into the mirror, keyed by bare DOI.

    `works` holds every record that fits the mirror; `drifted` holds, for every record that does
    not, the parser's message naming the key (`mirror.SchemaDriftError.detail`). No DOI is in
    both; a DOI the response does not answer is in neither — the caller's `unknown`.
    """

    works: dict[str, openalex_pb2.Work]
    drifted: dict[str, str]


def wrap(record: Mapping[str, object]) -> dict[str, object]:
    """Wrap a work into the shape the mirror declares (see the module docstring); all else passes through."""
    wrapped = dict(record)
    index = wrapped.get('abstract_inverted_index')
    if isinstance(index, Mapping):
        wrapped['abstract_inverted_index'] = {word: {'positions': positions} for word, positions in index.items()}
    return wrapped


def parse_work(record: Mapping[str, object]) -> openalex_pb2.Work:
    """Load one work into the mirror.

    Raises:
        mirror.SchemaDriftError: If the record carries a key the mirror lacks, or a value of a shape
            its field cannot hold.
    """
    return mirror.parse_strict(wrap(record), openalex_pb2.Work(), index=INDEX)


def _bare(value: str, prefix: str) -> str:
    return value.removeprefix(prefix).rstrip('/')


def bare_pmid(work: openalex_pb2.Work) -> str | None:
    """The work's PMID with its URL prefix stripped, or `None` when it states none (not in PubMed)."""
    return _bare(work.ids.pmid, _PMID_PREFIX) or None


def bare_pmcid(work: openalex_pb2.Work) -> str | None:
    """The work's PMCID with its URL prefix stripped, or `None` when it states none.

    idconv is the authoritative pmcid source; OpenAlex often omits it even for PMC papers.
    """
    return _bare(work.ids.pmcid, _PMCID_PREFIX) or None


def publisher(work: openalex_pb2.Work) -> str | None:
    """The host organisation of the work's primary source, or `None` when it states none."""
    return work.primary_location.source.host_organization_name or None


async def fetch(dois: Sequence[str], *, http_client: httpx2.AsyncClient) -> bytes:
    """Fetch the works page for a batch of DOIs (one OR-filter query), each work whole.

    Args:
        dois: The DOIs to resolve in one call (at most `_MAX_IDS`).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The raw works-response JSON bytes.

    Raises:
        ValueError: If `dois` is empty or exceeds `_MAX_IDS`.
        httpx2.HTTPStatusError: If OpenAlex returns a non-2xx status.
    """
    if not dois:
        raise ValueError('openalex.fetch requires at least one DOI')
    if len(dois) > _MAX_IDS:
        raise ValueError(f'openalex.fetch got {len(dois)} DOIs; the OR-filter caps at {_MAX_IDS} per call')
    response = await http_client.get(
        _WORKS_URL,
        params={'filter': 'doi:' + '|'.join(dois), 'per-page': str(_MAX_IDS), 'mailto': constants.CONTACT_EMAIL},
    )
    response.raise_for_status()
    return response.content


def parse_response(payload: bytes) -> ParsedWorks:
    """Read a works-response payload into the mirror, keyed by bare DOI.

    A result stating no DOI is skipped: the batch is keyed by DOI, so nothing could claim it. A
    record that does not fit the mirror is charged to its DOI in `drifted`, and the rest of the
    response reads regardless.

    Raises:
        ValueError: If the payload is not a works response (`results` absent, or a result that is
            not an object).
        json.JSONDecodeError: If `payload` is not valid JSON.
    """
    document = json.loads(payload)
    if not isinstance(document, Mapping) or not isinstance(document.get('results'), list):
        raise ValueError('expected an OpenAlex works response with a `results` list')
    works: dict[str, openalex_pb2.Work] = {}
    drifted: dict[str, str] = {}
    for record in document['results']:
        if not isinstance(record, Mapping):
            raise ValueError('expected every result of an OpenAlex works response to be a work object')
        stated = record.get('doi')
        if not isinstance(stated, str) or not stated:
            continue
        doi = _bare(stated, _DOI_PREFIX)
        try:
            works[doi] = parse_work(record)
        except mirror.SchemaDriftError as e:
            drifted[doi] = e.detail
    return ParsedWorks(works=works, drifted=drifted)


async def resolve(dois: Sequence[str], *, http_client: httpx2.AsyncClient) -> ParsedWorks:
    """Resolve a batch of DOIs to their OpenAlex works, chunked to the OR-filter cap."""
    works: dict[str, openalex_pb2.Work] = {}
    drifted: dict[str, str] = {}
    for start in range(0, len(dois), _MAX_IDS):
        chunk = dois[start : start + _MAX_IDS]
        parsed = parse_response(await fetch(chunk, http_client=http_client))
        works.update(parsed.works)
        drifted.update(parsed.drifted)
    return ParsedWorks(works=works, drifted=drifted)


def to_metadata(work: openalex_pb2.Work) -> bytes:
    """The canonical `metadata.pb` bytes for a paper whose record is OpenAlex's.

    The residual path — a DOI OpenAlex resolved but that carries no `pmid` (a preprint or otherwise
    non-PubMed work), so there is no efetch record to prefer.
    """
    return paper_metadata.to_canonical_bytes(litcache_pb2.PaperMetadata(openalex=work))
