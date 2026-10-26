"""Crossref: a DOI's work record, whole, for a paper PubMed does not index.

When a paper has a DOI but no PMID, the efetch path finds nothing, so its bibliographic record
comes from Crossref's `works` endpoint and is stored in the source record of `metadata.pb` as JSON
text in `crossref_json`, every key kept (`themis.litcache.upstream_record`). `read_work` reads what
ingest takes from it: the DOI, which populates the manifest `ExternalIds`, and the publisher,
returned for the orchestrator to build the manifest's `Licensed` access when the paper is not
free-to-read.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Mapping
from typing import NamedTuple

import httpx2

from themis.common import constants
from themis.litcache import metadata_fields, upstream_record
from themis.litcache.models import litcache_pb2

_CROSSREF_URL = 'https://api.crossref.org/works'
INDEX = upstream_record.Index.CROSSREF


@dataclasses.dataclass(frozen=True)
class CrossrefResult:
    """The bibliographic outputs of resolving one DOI-only paper via Crossref.

    Attributes:
        sourced: The work in its source record's `crossref_json` field, and the metadata filled from it
            alone.
        external_ids: The cross-ids for the manifest (DOI only — Crossref carries no PMID/PMCID).
        publisher: The Crossref `publisher`, for the manifest `Licensed` access; `None` if absent.
    """

    sourced: litcache_pb2.SourcedMetadata
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None


class WorkFields(NamedTuple):
    """What ingest takes from a Crossref work.

    Attributes:
        doi: The work's DOI, as Crossref states it.
        publisher: The `publisher` as stated, or `None` when the work leaves it out, states it as
            null or states it empty.
    """

    doi: str
    publisher: str | None


type Work = upstream_record.Record[WorkFields]


def read_work(work: Mapping[str, object]) -> WorkFields:
    """Read what ingest takes from a Crossref work (the `message` object of a works response).

    Every other key is ignored.

    Raises:
        upstream_record.SchemaDriftError: If `DOI` is missing or empty, or `DOI` or `publisher` has a
            shape this reader cannot read.
    """
    root = upstream_record.JsonObject.root(INDEX, work)
    doi = root.required_str('DOI')
    if not doi:
        raise root.drift('DOI', 'is empty, where the DOI Crossref answers a DOI with is read')
    return WorkFields(doi=doi, publisher=root.optional_str_or_null('publisher') or None)


def parse_work(document: Mapping[str, object]) -> Work:
    """Read a `works` message object's DOI and publisher, and serialise it as text.

    Raises:
        upstream_record.SchemaDriftError: If `read_work` cannot read a field it needs, or the work
            holds a number standard JSON text cannot carry.
    """
    return upstream_record.load(INDEX, document, read_work)


def from_crossref_work(document: Mapping[str, object]) -> CrossrefResult:
    """Resolve a `works` message object to its `SourcedMetadata` + cross-ids + publisher.

    Args:
        document: The `message` object of a Crossref `works` response.

    Returns:
        The `CrossrefResult`.

    Raises:
        upstream_record.SchemaDriftError: If the work cannot be read or stored (`parse_work`), or a
            metadata field cannot be read from it (`metadata_fields.fill`).
    """
    work = parse_work(document)
    source = litcache_pb2.MetadataSource(crossref_json=work.text)
    return CrossrefResult(
        sourced=litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source),
        external_ids=litcache_pb2.ExternalIds(doi=work.fields.doi),
        publisher=work.fields.publisher,
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
    """Resolve one DOI to its `SourcedMetadata` + cross-ids + publisher via Crossref."""
    return from_crossref_work(await fetch_crossref(doi, http_client=http_client))
