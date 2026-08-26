"""Conditional refresh of the raw GenCC / ClinGen dumps.

Each raw file (GenCC submissions TSV, ClinGen validity + dosage CSVs) is fetched with a conditional
GET: the previously stored ETag rides in ``If-None-Match``; a 304 leaves the stored dump and its
sidecar untouched, a 200 rewrites both. An upstream that returns no ETag is re-downloaded every run
(fine at the weekly cadence) and stores no sidecar. The bytes are written verbatim — the server
parses them with the same GenCC/ClinGen parsers, so the download is the parse contract. A fresh
download (a 200) is round-tripped through that parser before the write, so a format regression fails
the job loudly rather than poisoning the bucket.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import httpx

from themis.services.evidence.gene_disease.refresh import _http, object_store

_ETAG_SUFFIX = '.etag'


@dataclasses.dataclass(frozen=True)
class RefreshOutcome:
    """What a conditional refresh did, for logging and tests.

    Attributes:
        object_name: The dump object the refresh targeted.
        changed: True if the dump was (re)written (a 200), False on a 304 skip.
        etag_stored: True if an upstream ETag is now stored alongside the dump.
    """

    object_name: str
    changed: bool
    etag_stored: bool


def etag_name(object_name: str) -> str:
    """The sidecar object name holding ``object_name``'s stored ETag."""
    return f'{object_name}{_ETAG_SUFFIX}'


async def refresh_file(
    client: httpx.AsyncClient,
    store: object_store.ReferenceObjectStore,
    *,
    url: str,
    object_name: str,
    validate: Callable[[bytes], object],
) -> RefreshOutcome:
    """Conditionally refresh one raw dump from ``url`` into ``object_name``.

    Args:
        client: The caller-owned async client the request rides.
        store: The reference-bucket object store.
        url: The upstream download URL.
        object_name: The bucket object the raw bytes are written to.
        validate: The server loader the fresh bytes are round-tripped through before the write; only
            a 200's new bytes are checked (a 304 leaves the already-validated stored dump untouched).

    Returns:
        What the refresh did (written vs 304-skipped, and whether an ETag was stored).

    Raises:
        httpx.HTTPStatusError: If the upstream returns a status other than 200 or 304.
        ValueError: If the freshly downloaded bytes fail to parse through ``validate``.
    """
    stored_etag = await store.read(etag_name(object_name))
    headers = {'If-None-Match': stored_etag.decode('utf-8')} if stored_etag else None
    response = await _http.request_with_retry(client, 'GET', url, headers=headers)
    if response.status_code == httpx.codes.NOT_MODIFIED:
        return RefreshOutcome(object_name=object_name, changed=False, etag_stored=bool(stored_etag))
    response.raise_for_status()
    try:
        validate(response.content)
    except ValueError as e:
        raise ValueError(f'{object_name}: refreshed bytes did not parse through the server loader') from e
    await store.write(object_name, response.content)
    upstream_etag = response.headers.get('ETag')
    if upstream_etag:
        await store.write(etag_name(object_name), upstream_etag.encode('utf-8'))
    return RefreshOutcome(object_name=object_name, changed=True, etag_stored=upstream_etag is not None)
