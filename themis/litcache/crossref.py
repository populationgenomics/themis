"""Crossref: a DOI's work record, whole, for a paper PubMed does not index.

When a paper has a DOI but no PMID, the efetch path finds nothing, so its bibliographic record
comes from Crossref's `works` endpoint and is stored as the envelope's `crossref` field, in
Crossref's own schema (`crossref.proto`, a mirror of the JSON) and loaded strictly
(`themis.litcache.mirror`). Two shapes the mirror's header names are wrapped before the parse:
`date-parts`, an array of arrays, becomes one `DateParts` per inner array, less the trailing null
Crossref states an unknown date with; `relation`, an object whose values are arrays, becomes a
`RelationList` per key. A null element of any other array, which proto3-JSON cannot hold, is
dropped.

The DOI populates the manifest `ExternalIds`. The publisher is returned alongside, for the
orchestrator to build the manifest's `Licensed` access when the paper is not free-to-read.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Mapping

import httpx2

from themis.common import constants
from themis.litcache import mirror, paper_metadata
from themis.litcache.models import crossref_pb2, litcache_pb2

_CROSSREF_URL = 'https://api.crossref.org/works'
INDEX = 'crossref'


@dataclasses.dataclass(frozen=True)
class CrossrefResult:
    """The bibliographic outputs of resolving one DOI-only paper via Crossref.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (a `PaperMetadata` envelope with the work in
            its `crossref` field).
        external_ids: The cross-ids for the manifest (DOI only — Crossref carries no PMID/PMCID).
        publisher: The Crossref `publisher`, for the manifest `Licensed` access; `None` if absent.
    """

    metadata: bytes
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None


def wrap(document: Mapping[str, object]) -> dict[str, object]:
    """Wrap a `works` message object into the shapes the mirror declares (see the module docstring).

    Anything else passes through untouched, so a shape the mirror does not hold fails the strict
    parse rather than being coerced here.
    """
    wrapped = _wrap_dates(document)
    relation = wrapped.get('relation')
    if isinstance(relation, Mapping):
        wrapped['relation'] = {kind: {'items': items} for kind, items in relation.items()}
    return wrapped


def _wrap_dates(document: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _wrap_date_parts(value) if key == 'date-parts' else _wrap_value(value) for key, value in document.items()
    }


def _wrap_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _wrap_dates(value)
    if isinstance(value, list):
        return [_wrap_value(item) for item in value if item is not None]
    return value


def _wrap_date_parts(value: object) -> object:
    if not isinstance(value, list):
        return value
    return [{'parts': _without_trailing_nulls(inner)} if isinstance(inner, list) else inner for inner in value]


def _without_trailing_nulls(parts: list[object]) -> list[object]:
    # `[[null]]` is Crossref's unknown date; a null between stated parts is not a date, and stays
    # for the strict parse to charge as drift rather than shifting the parts after it.
    end = len(parts)
    while end and parts[end - 1] is None:
        end -= 1
    return parts[:end]


def parse_work(document: Mapping[str, object]) -> crossref_pb2.Work:
    """Load a `works` message object into the mirror.

    Raises:
        mirror.SchemaDriftError: If the record carries a key the mirror lacks, or a value of a shape
            its field cannot hold.
    """
    return mirror.parse_strict(wrap(document), crossref_pb2.Work(), index=INDEX)


def from_crossref_work(document: Mapping[str, object]) -> CrossrefResult:
    """Resolve a `works` message object to `metadata.pb` bytes + cross-ids + publisher.

    Args:
        document: The `message` object of a Crossref `works` response.

    Returns:
        The `CrossrefResult`.

    Raises:
        mirror.SchemaDriftError: If the record does not fit the mirror.
        ValueError: If the work states no DOI — the record Crossref answers a DOI with names it.
    """
    work = parse_work(document)
    if not work.doi:
        raise ValueError('Crossref work states no DOI')
    return CrossrefResult(
        metadata=paper_metadata.to_canonical_bytes(litcache_pb2.PaperMetadata(crossref=work)),
        external_ids=litcache_pb2.ExternalIds(doi=work.doi),
        publisher=work.publisher if work.HasField('publisher') else None,
    )


async def fetch_crossref(doi: str, *, http_client: httpx2.AsyncClient) -> Mapping[str, object]:
    """Fetch a Crossref `works` message for one DOI.

    Args:
        doi: The DOI to resolve (raw, slashes intact).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The `message` object of the Crossref response.

    Raises:
        httpx2.HTTPStatusError: If Crossref returns a non-2xx status (e.g. 404 for
            an unknown DOI — the caller's `unknown`).
    """
    # DOIs contain '/' (kept) and can carry reserved chars (?, #, ;) that would otherwise
    # reparse the path — percent-encode the suffix so the request means what it says.
    quoted = urllib.parse.quote(doi, safe='/')
    response = await http_client.get(f'{_CROSSREF_URL}/{quoted}', params={'mailto': constants.CONTACT_EMAIL})
    response.raise_for_status()
    return response.json()['message']


async def resolve(doi: str, *, http_client: httpx2.AsyncClient) -> CrossrefResult:
    """Resolve one DOI to `metadata.pb` + cross-ids + publisher via Crossref."""
    return from_crossref_work(await fetch_crossref(doi, http_client=http_client))
