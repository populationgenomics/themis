"""Regenerate `metadata.pb` for committed papers whose record is absent.

The record is the one artifact re-creatable after the manifest commit
(`docs/design/litcache-manifest.md` § Path layout): deleting a paper's `metadata.pb`
and re-deriving it refreshes the bibliographic record while keeping every conversion
the paper holds — where the ingestion pipeline, which keys its skip on the manifest,
would leave the gap in place.

`plan` selects and prepares: a paper is due when its manifest exists and its
`metadata.pb` does not, and each due paper becomes the resolver request its manifest's
identifiers make. `refresh` resolves the requests in chunks and writes each record as
its chunk resolves. Nothing is skipped quietly — a manifest that cannot be read, one
whose `doc_id` disagrees with its directory, one that names no identifier the resolver
takes, a paper the resolver returns nothing for, and a paper it settles with a failure
of its own (a record failing the store's precondition, a record its mirror does not
hold) are each a `Failure` with its reason, and the caller decides what a non-empty
failure list means.

The prefix listed and the objects written are both the writer's layout, so the refresh
cannot read one tree and write another.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence

from google.cloud import storage as gcs
from google.protobuf import message

from themis.litcache import resolve, writer
from themis.litcache.models import litcache_pb2

_LOG = logging.getLogger(__name__)

# Resolves a batch of requests to each settled paper's outcome, keyed by `claim_key`; a
# paper no rung resolves is absent (`resolve.resolve_batch` over its live clients, or a stub).
Resolver = Callable[[Sequence[resolve.ResolveRequest]], Awaitable[Mapping[str, resolve.Outcome]]]

PAPERS_PREFIX = f'{writer.PAPERS_DIR}/'

# Requests per resolver call; each chunk's records are written before the next resolves.
DEFAULT_CHUNK_SIZE = 1000


@dataclasses.dataclass(frozen=True)
class Failure:
    """A paper the refresh could not complete, and why.

    Attributes:
        doc_id: The paper, by its directory name.
        reason: What stopped it — an unreadable manifest, a `doc_id` disagreeing with
            its directory, no resolvable identifier, a resolver miss, or the resolver's
            own failure for the paper: a record failing the store's precondition, or
            one that does not fit its mirror.
    """

    doc_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Plan:
    """The papers a refresh would touch, as the requests it would make.

    Attributes:
        manifests: Every manifest found under `papers/`.
        due: One resolver request per paper whose `metadata.pb` is absent, keyed
            (`claim_key`) by its directory's `doc_id`, in `doc_id` order, capped by
            the caller's limit.
        failures: Due papers a refresh cannot attempt — an unreadable manifest, a
            `doc_id` that disagrees with its directory, no pmid and no doi.
    """

    manifests: int
    due: list[resolve.ResolveRequest]
    failures: list[Failure]


@dataclasses.dataclass(frozen=True)
class RefreshReport:
    """The outcome of one refresh.

    Attributes:
        manifests: Every manifest found under `papers/`.
        refreshed: The `doc_id`s whose `metadata.pb` was written.
        failures: Every paper that was due and was not refreshed, with its reason.
    """

    manifests: int
    refreshed: list[str]
    failures: list[Failure]


def _prepare(doc_id: str, data: bytes) -> resolve.ResolveRequest | Failure:
    """The resolver request for the manifest at `doc_id`'s directory, or why there is none."""
    try:
        manifest = litcache_pb2.Manifest.FromString(data)
    except message.DecodeError as exc:
        return Failure(doc_id=doc_id, reason=f'manifest unreadable: {exc}')
    if manifest.doc_id != doc_id:
        return Failure(doc_id=doc_id, reason=f'manifest doc_id {manifest.doc_id!r} disagrees with its directory')
    ids = manifest.external_ids
    pmid = ids.pmid if ids.HasField('pmid') else None
    doi = ids.doi if ids.HasField('doi') else None
    if pmid is None and doi is None:
        return Failure(doc_id=doc_id, reason='manifest names no pmid and no doi')
    return resolve.ResolveRequest(claim_key=doc_id, pmid=pmid, doi=doi)


def plan(bucket: gcs.Bucket, *, limit: int | None = None) -> Plan:
    """Find the committed papers with no `metadata.pb` and prepare their requests.

    One listing of `papers/` decides presence for every paper — no per-paper probe —
    and only the due manifests are downloaded.

    Args:
        bucket: The cache bucket.
        limit: Cap the requests at the first `limit` due papers in `doc_id` order that
            yield one; a paper that fails preparation is reported and does not consume
            the cap. `None` takes them all.

    Returns:
        The `Plan`.

    Raises:
        ValueError: If `limit` is not positive.
        google.api_core.exceptions.NotFound: If a due manifest vanishes between the
            listing and its download.
    """
    if limit is not None and limit <= 0:
        raise ValueError(f'limit must be positive, got {limit}')
    with_manifest: set[str] = set()
    with_metadata: set[str] = set()
    for blob in bucket.list_blobs(prefix=PAPERS_PREFIX):
        parts = blob.name[len(PAPERS_PREFIX) :].split('/')
        if len(parts) != 2:  # exact `{doc_id}/<file>` depth, not a nested artifact
            continue
        doc_id, name = parts
        if name == writer.MANIFEST_NAME:
            with_manifest.add(doc_id)
        elif name == writer.METADATA_NAME:
            with_metadata.add(doc_id)

    due: list[resolve.ResolveRequest] = []
    failures: list[Failure] = []
    for doc_id in sorted(with_manifest - with_metadata):
        if limit is not None and len(due) == limit:
            break
        prepared = _prepare(doc_id, bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())
        if isinstance(prepared, Failure):
            failures.append(prepared)
        else:
            due.append(prepared)
    return Plan(manifests=len(with_manifest), due=due, failures=failures)


def _chunks(items: Sequence[resolve.ResolveRequest], size: int) -> Iterator[Sequence[resolve.ResolveRequest]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def refresh(
    bucket: gcs.Bucket,
    resolver: Resolver,
    *,
    limit: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> RefreshReport:
    """Resolve and write `metadata.pb` for every committed paper that lacks one.

    Requests are keyed by `doc_id`, so two papers sharing an identifier each receive
    a record. A paper present in the plan and absent from the resolver's result is a
    failure, never a silent skip; so is one the resolver settles with a failure of its
    own — a record failing the store's precondition, or one its mirror does not hold —
    carrying that failure's reason. The due papers are resolved in chunks and each chunk
    written before the next is resolved: a resolver transport error propagates, the
    records written so far stay (each is valid on its own), and a re-run resumes with
    the papers still lacking one.

    Args:
        bucket: The cache bucket.
        resolver: Resolves a batch of requests (`resolve.resolve_batch` over its live
            clients).
        limit: Cap the papers refreshed at the first `limit` due in `doc_id` order.
        chunk_size: Requests per resolver call.

    Returns:
        The `RefreshReport`; `failures` empty means every due paper was refreshed.

    Raises:
        ValueError: If `limit` or `chunk_size` is not positive, or the resolver returns
            bytes that are not a `PaperMetadata` envelope meeting its constraints (a
            resolver defect, not a per-paper condition).
        Exception: Whatever the resolver raises on transport failure propagates.
    """
    if chunk_size <= 0:
        raise ValueError(f'chunk_size must be positive, got {chunk_size}')
    found = plan(bucket, limit=limit)
    failures = list(found.failures)
    refreshed: list[str] = []
    for chunk in _chunks(found.due, chunk_size):
        outcomes = await resolver(chunk)
        for request in chunk:
            outcome = outcomes.get(request.claim_key)
            if isinstance(outcome, resolve.ResolvedPaper):
                writer.write_metadata(bucket, request.claim_key, outcome.metadata)
                refreshed.append(request.claim_key)
            else:
                failures.append(Failure(doc_id=request.claim_key, reason=_failure_reason(request, outcome)))
    _LOG.info('refreshed %d of %d due paper(s); %d failure(s)', len(refreshed), len(found.due), len(failures))
    return RefreshReport(manifests=found.manifests, refreshed=refreshed, failures=failures)


def _failure_reason(
    request: resolve.ResolveRequest, outcome: resolve.RecordPreconditionFailure | resolve.SchemaDriftFailure | None
) -> str:
    """Why the paper was not refreshed, worded as ingestion's dead letters word the same outcome."""
    match outcome:
        case None:
            return f'metadata unresolved (pmid={request.pmid!r}, doi={request.doi!r})'
        case resolve.RecordPreconditionFailure(reason=reason):
            return f'precondition failed: {reason}'
        case resolve.SchemaDriftFailure(reason=reason):
            return f'schema drift: {reason}'


def render_failures(failures: Iterable[Failure]) -> str:
    """One line per failure, for the operator."""
    return '\n'.join(f'  {f.doc_id}: {f.reason}' for f in failures)
