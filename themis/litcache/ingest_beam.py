"""Beam/Dataflow wrapper around the ingestion core (`themis.litcache.pipeline`).

`pipeline` is the runtime-agnostic core, split into a local identity half
(`extract_identity`) and a write half (`ingest_paper`); this is the runtime that
drives them, with a batched bibliographic-resolution stage between so the NCBI rate
domain (efetch / idconv) is hit in bulk, not once per paper. It reads the flat seed
prefix (`ingest/`), pairs each paper's `<id>.json` + `<id>.pdf`, and runs a three-stage
graph, talking to GCS (`google.cloud.storage.Bucket`) and Cloud SQL (the crosswalk
mint) from inside the workers:

1. `_ExtractIdentityFn` — per paper, local: read the seed and classify identity, emit
   `(claim_key, _PaperWork)`. The seed bytes are not carried forward (a pdf is MBs); the
   write stage re-reads them, so only the `SeedRef` + `Identity` are shuffled. An
   unreadable seed (e.g. a truncated docling json) is dead-lettered by its seed key
   (counted `paper_failed`) rather than aborting the run.
2. `_ResolveBatchFn` — resolution, batched: every paper's identifier is keyed to one
   shard so `GroupByKey` funnels the whole seed to one worker, which resolves it in bulk
   (`resolve.resolve_batch`, which chunks the ids into per-call batches internally). One
   key ⇒ no fan-out ⇒ the global NCBI request rate is bounded regardless of the job's
   worker count; batching keeps the call count tiny (⌈ids/200⌉), so serial resolution is
   cheap. Emits `(claim_key, ResolvedPaper)`.
3. `_WritePaperFn` — per paper, the write half: `CoGroupByKey` joins each paper's work
   with its resolution on `claim_key`; the worker re-reads the seed and runs
   `ingest_paper`. Per-paper failure is isolated, not fatal: a paper with no resolution
   (counted `paper_unresolved`) and a paper whose write half raises (counted
   `paper_failed`, the exception recorded as the reason) are both dead-lettered under the
   run's own prefix (`dead_letter_paths`) and the run carries on — one bad paper never
   aborts a full-seed pass. `report_run` consolidates that run's records into one text
   file, so a re-run against the same bucket reports its own failures, not the history.

Backends reach the workers as factories, not instances: the driver and each worker live
in different processes, so a `Bucket` / Postgres connection / HTTP transport is built
where it is used. Factories are picklable; the instances they build are not shipped.

Seed pairing is driver-side (`pair_seed_keys`): the key set is finite and a single GCS
list is cheap. A seed object missing its counterpart (a `.json` with no `.pdf`, or the
reverse) is logged and excluded, a data-quality signal the diagnostics report surfaces,
never a per-paper crash.

Per-stage counts are emitted as Beam metrics under `METRICS_NAMESPACE` (`papers_seen`,
`doc_id_minted` / `doc_id_adopted`, `paper_written` / `paper_skipped`).

`report_run` is the post-run step (call after `wait_until_finish`): it builds and logs
the diagnostics report. Deleting the transient seed prefix is a separate manual operator
step (`themis.litcache.report.teardown_seed`), taken only once the report confirms a
clean ingestion — never a side effect of the run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import functools
import json
import logging
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import override

import apache_beam as beam
import httpx2
import litfetch
import pg8000.exceptions
from apache_beam import pvalue
from apache_beam.metrics import metric
from apache_beam.options import pipeline_options
from apache_beam.runners import runner
from apache_beam.utils import shared
from google.cloud import storage as gcs

from themis.common import constants
from themis.litcache import crosswalk, identity, pipeline, report, resolve

_LOG = logging.getLogger(__name__)

# Beam-metrics namespace for the per-stage ingestion counters.
METRICS_NAMESPACE = 'litcache.ingest'

# The per-stage counters the DoFns emit, snapshotted into the diagnostics report.
_COUNTER_NAMES = (
    'papers_seen',
    'doc_id_minted',
    'doc_id_adopted',
    'paper_written',
    'paper_skipped',
    'paper_unresolved',
    'paper_failed',
)

# Papers that could not be ingested — unresolved metadata, or a write-half failure — are
# recorded here (one JSON blob per paper, keyed by the url-encoded key, carrying the reason)
# instead of failing the run. `report_run` consolidates them into a text summary.
_DEAD_LETTER_ROOT = 'diagnostics/dead_letters/'

_JSON_SUFFIX = '.json'
_PDF_SUFFIX = '.pdf'

# A seed pdf larger than this is read-whole-into-memory + parsed on the worker, which OOMs or
# stalls a modest machine; such a seed is dead-lettered by size rather than pulled. The corpus
# has a handful of 200-450 MB outliers; legitimate papers sit well under this.
_MAX_SEED_PDF_BYTES = 100 * 1024 * 1024

# One shard key funnels every resolve request through a single GroupByKey group, so one
# worker resolves the whole seed serially — bounding the global NCBI request rate
# independent of the job's worker count. `resolve.resolve_batch` chunks the collected
# requests into per-call id batches internally.
#
# The single group sets this stage's operating envelope, and it is the run's one
# unisolated step:
#   - the whole seed's metadata is resident on one worker (twice at peak — the returned
#     mapping plus `resolve_batch`'s own copy), so seed size is bounded by worker memory,
#     not by the job's worker count;
#   - a group has no partial progress to check point, so a transport failure that outlives
#     `resolve_batch`'s retry budget aborts the bundle and, past Beam's retry limit, the
#     job — discarding every already-resolved paper. Unlike the identity and write stages,
#     resolution has no dead-letter path.
# Sharding to a small fixed fan-out would scope a failure to one slice and cap peak
# memory, at the cost of multiplying the NCBI request rate by the fan-out.
_RESOLVE_SHARD_KEY = 0

# Picklable per-worker builders for a backend; the instance they return is bound to the
# worker process, never shipped through the pipeline.
BucketFactory = Callable[[], gcs.Bucket]
ConnFactory = Callable[[], crosswalk.Connection]
FetchersFactory = Callable[[], Sequence[litfetch.Fetcher]]
FileSourcesFactory = Callable[[], Sequence[litfetch.FileSource]]
TransportFactory = Callable[[], httpx2.AsyncBaseTransport]


@dataclasses.dataclass(frozen=True)
class SeedRef:
    """A paired seed object: the two storage keys to read and the identity key.

    Attributes:
        bucket_key: The json object's name relative to the seed prefix (the
            `ingest/` prefix stripped), the identity input `determine_identity`
            decodes and classifies.
        json_key: The full storage key of the Docling json (identity origin and
            non-OA markdown source).
        pdf_key: The full storage key of the pdf (retained source + char probe).
    """

    bucket_key: str
    json_key: str
    pdf_key: str


@dataclasses.dataclass(frozen=True)
class SeedPairing:
    """The outcome of pairing the seed prefix.

    Attributes:
        refs: The papers with both a json and a pdf, ready to ingest.
        unpaired: Storage keys with no counterpart (a json without a pdf, or the
            reverse) — excluded from ingestion, surfaced for diagnostics.
    """

    refs: list[SeedRef]
    unpaired: list[str]


@dataclasses.dataclass(frozen=True)
class _PaperWork:
    """A paper in flight between the identity and write stages.

    Carries only what the write stage needs beyond the batch-resolved metadata: the
    `SeedRef` to re-read the bytes, and the classified identity (mint keys, ids). The
    seed bytes themselves are not carried — the write stage re-reads them from GCS.
    """

    ref: SeedRef
    ident: identity.Identity


def pair_seed_keys(keys: Iterable[str], *, prefix: str = 'ingest/') -> SeedPairing:
    """Pair `<id>.json` + `<id>.pdf` seed objects by their shared stem.

    Args:
        keys: The storage keys under the seed prefix (full keys including `prefix`,
            e.g. as `Bucket.list_blobs` names them).
        prefix: The seed prefix to strip from each key before pairing.

    Returns:
        The `SeedPairing`: paired `SeedRef`s (in stem order) and the keys left
        unpaired (a `.json`/`.pdf` with no counterpart, or an unknown extension).
    """
    jsons: dict[str, str] = {}
    pdfs: dict[str, str] = {}
    unpaired: list[str] = []
    for key in keys:
        if not key.startswith(prefix):
            continue
        name = key[len(prefix) :]
        if name.endswith(_JSON_SUFFIX):
            jsons[name[: -len(_JSON_SUFFIX)]] = key
        elif name.endswith(_PDF_SUFFIX):
            pdfs[name[: -len(_PDF_SUFFIX)]] = key
        elif name:  # an object that is neither (the bare prefix "directory" is skipped)
            unpaired.append(key)

    refs: list[SeedRef] = []
    for stem in sorted(jsons.keys() | pdfs.keys()):
        json_key = jsons.get(stem)
        pdf_key = pdfs.get(stem)
        if json_key is None or pdf_key is None:
            unpaired.append(json_key or pdf_key or '')
            continue
        refs.append(SeedRef(bucket_key=f'{stem}{_JSON_SUFFIX}', json_key=json_key, pdf_key=pdf_key))
    return SeedPairing(refs=refs, unpaired=unpaired)


@dataclasses.dataclass(frozen=True)
class IngestionRun:
    """A submitted run and the diagnostics paths belonging to it.

    `report_run` reads the paths from here instead of re-deriving them from a timestamp the
    caller carries by hand: two `datetime.now()` calls would otherwise leave the report
    reading an empty prefix and declaring a run clean that dead-lettered every paper.

    Attributes:
        result: The pipeline result (await with `.wait_until_finish()`).
        dead_letter_prefix: Where this run's per-paper records were written.
        dead_letter_summary: Where `report_run` consolidates them.
    """

    result: runner.PipelineResult
    dead_letter_prefix: str
    dead_letter_summary: str

    @classmethod
    def stamped(cls, result: runner.PipelineResult, now: datetime.datetime) -> IngestionRun:
        """Bind `result` to the diagnostics paths for the run stamped `now`."""
        prefix, summary = dead_letter_paths(now)
        return cls(result=result, dead_letter_prefix=prefix, dead_letter_summary=summary)


def dead_letter_paths(now: datetime.datetime) -> tuple[str, str]:
    """The record prefix and summary key for the run stamped `now`.

    Records are scoped to one pass. A bucket accumulates passes, so an unscoped prefix
    would make every later run's report describe the bucket's whole history: a paper
    whose failure was fixed would keep failing the dead-letter gate, and the gate is the
    documented precondition for `report.teardown_seed`.

    Args:
        now: The run's timestamp — the same one threaded through `build_pipeline`.

    Returns:
        `(records_prefix, summary_path)`. The summary sits beside the prefix rather than
        inside it, so consolidating never lists its own output.
    """
    run_id = f'{now:%Y%m%dT%H%M%SZ}'
    return f'{_DEAD_LETTER_ROOT}{run_id}/', f'{_DEAD_LETTER_ROOT}{run_id}.jsonl'


def _write_dead_letter(
    bucket: gcs.Bucket, *, prefix: str, key: str, reason: str, pmid: str | None = None, doi: str | None = None
) -> None:
    """Record a paper that could not be ingested under the run's dead-letter prefix.

    Args:
        bucket: The cache bucket to write the record to.
        prefix: The run's record prefix, from `dead_letter_paths`.
        key: The paper's identifier — its `claim_key` once identity is known, or the seed
            object key when extraction itself failed; url-encoded, it names the record blob.
        reason: Why it was dead-lettered — the exception (`type: message`) or
            `'metadata unresolved'`.
        pmid: The paper's PMID, when identity was classified.
        doi: The paper's DOI, when identity was classified.
    """
    record = {'key': key, 'pmid': pmid, 'doi': doi, 'reason': reason}
    name = prefix + urllib.parse.quote(key, safe='') + '.json'
    bucket.blob(name).upload_from_string(json.dumps(record), content_type='application/json')


class _ExtractIdentityFn(beam.DoFn):
    """Read a seed pair and classify its identity — the local, network-free stage.

    Builds the bucket per worker; emits `(claim_key, _PaperWork)` without the seed
    bytes (the write stage re-reads them). An unreadable seed is dead-lettered rather
    than crashing the stage. Counts `papers_seen` and `paper_failed`.
    """

    def __init__(self, *, bucket_factory: BucketFactory, dead_letter_prefix: str) -> None:
        self._bucket_factory = bucket_factory
        self._dead_letter_prefix = dead_letter_prefix
        self._seen = metric.Metrics.counter(METRICS_NAMESPACE, 'papers_seen')
        self._failed = metric.Metrics.counter(METRICS_NAMESPACE, 'paper_failed')

    @override
    def setup(self) -> None:
        self._bucket = self._bucket_factory()

    @override
    def process(self, ref: SeedRef) -> Iterator[tuple[str, _PaperWork]]:
        self._seen.inc()
        # A pathologically large seed pdf (hundreds of MB) is read whole into memory and parsed
        # for metadata; on a modest worker that OOMs or stalls the parser — a hang, not a
        # catchable error, so the exception handler below can't reach it. Skip it by metadata
        # (the size, no download) before pulling the bytes.
        pdf_blob = self._bucket.get_blob(ref.pdf_key)
        if pdf_blob is not None and pdf_blob.size is not None and pdf_blob.size > _MAX_SEED_PDF_BYTES:
            _LOG.warning(
                'seed pdf %s is %d bytes (> %d): dead-lettering', ref.pdf_key, pdf_blob.size, _MAX_SEED_PDF_BYTES
            )
            _write_dead_letter(
                self._bucket,
                prefix=self._dead_letter_prefix,
                key=ref.bucket_key,
                reason=f'seed pdf too large: {pdf_blob.size} bytes',
            )
            self._failed.inc()
            return
        # The downloads stay outside the handler: a GCS fault is transient, and letting it
        # raise keeps Beam's bundle retries. Dead-lettering is for a seed that will not parse
        # however many times it is read.
        seed = pipeline.SeedObject(
            bucket_key=ref.bucket_key,
            docling_json=self._bucket.blob(ref.json_key).download_as_bytes(),
            pdf=(pdf_blob or self._bucket.blob(ref.pdf_key)).download_as_bytes(),
        )
        try:
            ident = pipeline.extract_identity(seed)
        except Exception as exc:  # noqa: BLE001 — isolate one unreadable seed; the reason is recorded, not swallowed
            _LOG.warning('identity extraction failed for %s, dead-lettering: %r', ref.bucket_key, exc)
            _write_dead_letter(
                self._bucket,
                prefix=self._dead_letter_prefix,
                key=ref.bucket_key,
                reason=f'{type(exc).__name__}: {exc}',
            )
            self._failed.inc()
            return
        yield ident.claim_key, _PaperWork(ref=ref, ident=ident)


def _to_resolve_request(keyed_work: tuple[str, _PaperWork]) -> tuple[int, resolve.ResolveRequest]:
    """Key a paper's identifiers to the single resolution shard for batching."""
    claim_key, work = keyed_work
    by_scheme = {eid.scheme: eid.value for eid in work.ident.external_ids}
    request = resolve.ResolveRequest(claim_key=claim_key, pmid=by_scheme.get('pmid'), doi=by_scheme.get('doi'))
    return _RESOLVE_SHARD_KEY, request


class _ResolveBatchFn(beam.DoFn):
    """Resolve the collected papers' bibliographic metadata in bulk.

    Runs `resolve.resolve_batch` over the `GroupByKey` group (it chunks the ids into
    per-call batches), emitting `(claim_key, ResolvedPaper)` for the papers that
    resolved (an unresolved paper is absent — the write stage fails it loud). The HTTP
    transport is built per worker.
    """

    def __init__(self, *, transport_factory: TransportFactory | None) -> None:
        self._transport_factory = transport_factory

    @override
    def setup(self) -> None:
        self._transport = self._transport_factory() if self._transport_factory is not None else None

    @override
    def process(
        self, batch: tuple[int, Iterable[resolve.ResolveRequest]]
    ) -> Iterator[tuple[str, resolve.ResolvedPaper]]:
        _shard, requests = batch
        resolved = asyncio.run(self._resolve(list(requests)))
        yield from resolved.items()

    async def _resolve(self, requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
        # The batched id-resolver runs on a litfetch Session; a test transport is
        # injected via its client_factory, else litfetch builds its live default client.
        client_factory = (
            functools.partial(httpx2.AsyncClient, transport=self._transport) if self._transport is not None else None
        )
        async with (
            httpx2.AsyncClient(transport=self._transport) as client,
            litfetch.Session(client_factory=client_factory, contact=constants.CONTACT_EMAIL) as session,
        ):
            return await resolve.resolve_batch(requests, http_client=client, session=session)


class _MintConnection:
    """One crosswalk connection plus a mutex, shared across a worker process.

    pg8000 is not thread-safe, and Runner v2 runs several `_WritePaperFn` instances (one per
    bundle processor) concurrently on a worker, so a per-instance connection multiplies the
    count and exhausts the shared Cloud SQL instance. Held via `beam.utils.shared.Shared`,
    this is one connection per worker *process*, with the (brief) mint transaction serialized
    on it — dwarfed by the per-paper OA fetch, so the serialization costs ~nothing.

    The connection is dialed on the first mint and re-dialed after one that leaves it
    unusable. `Shared` holds this object for the process lifetime, so a connection kept past
    the point the server closed it would fail every remaining paper on the worker.
    """

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._conn_factory = conn_factory
        self._conn: crosswalk.Connection | None = None
        self._lock = threading.Lock()

    def mint(self, external_ids: Iterable[str]) -> crosswalk.MintResult:
        """Mint under the mutex, leaving the connection usable for the next caller."""
        with self._lock:
            if self._conn is None:
                self._conn = self._conn_factory()
            try:
                return crosswalk.mint(self._conn, external_ids)
            except pg8000.exceptions.InterfaceError:
                # The session is gone (socket closed, server restarted), so there is no
                # transaction to roll back and every later mint on it fails identically.
                self._discard()
                raise
            except Exception:
                self._rollback()
                raise

    def _rollback(self) -> None:
        # Postgres rejects every statement on an aborted transaction until it is rolled back.
        # A rollback that itself fails means the session is unusable, not just the transaction.
        conn = self._conn
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort; the mint's error propagates
            _LOG.warning('crosswalk rollback failed, discarding the connection: %r', exc)
            self._discard()

    def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001 — the connection is being dropped either way
            _LOG.warning('closing the discarded crosswalk connection failed: %r', exc)


class _WritePaperFn(beam.DoFn):
    """Join a paper's identity with its resolution and run the write half.

    The crosswalk connection is one per worker process (`_MintConnection` via
    `beam.utils.shared.Shared`), opened lazily on the first mint — a bundle of only
    unresolved papers never opens one. Counts `doc_id_minted`/`doc_id_adopted`,
    `paper_written`/`paper_skipped`, `paper_unresolved`/`paper_failed`.
    """

    def __init__(
        self,
        *,
        bucket_factory: BucketFactory,
        conn_factory: ConnFactory,
        shared_conn: shared.Shared,
        dead_letter_prefix: str,
        licence: pipeline.LicenceFacts,
        now: datetime.datetime,
        fetchers_factory: FetchersFactory | None,
        file_sources_factory: FileSourcesFactory | None,
    ) -> None:
        self._bucket_factory = bucket_factory
        self._conn_factory = conn_factory
        self._shared_conn = shared_conn
        self._dead_letter_prefix = dead_letter_prefix
        self._licence = licence
        self._now = now
        self._fetchers_factory = fetchers_factory
        self._file_sources_factory = file_sources_factory
        self._minted = metric.Metrics.counter(METRICS_NAMESPACE, 'doc_id_minted')
        self._adopted = metric.Metrics.counter(METRICS_NAMESPACE, 'doc_id_adopted')
        self._written = metric.Metrics.counter(METRICS_NAMESPACE, 'paper_written')
        self._skipped = metric.Metrics.counter(METRICS_NAMESPACE, 'paper_skipped')
        self._unresolved = metric.Metrics.counter(METRICS_NAMESPACE, 'paper_unresolved')
        self._failed = metric.Metrics.counter(METRICS_NAMESPACE, 'paper_failed')

    @override
    def setup(self) -> None:
        self._bucket = self._bucket_factory()
        self._fetchers = self._fetchers_factory() if self._fetchers_factory is not None else None
        self._file_sources = self._file_sources_factory() if self._file_sources_factory is not None else None
        # `Shared` caches by weak reference, keeping the object alive only through a
        # single-slot keepalive that the next `acquire` of a *different* token displaces.
        # Holding the strong reference here is what makes one dial per process not depend
        # on this being the process's only token. Constructing it does not dial.
        self._mint_conn = self._shared_conn.acquire(lambda: _MintConnection(self._conn_factory))

    def _mint(self, external_ids: Iterable[str]) -> crosswalk.MintResult:
        return self._mint_conn.mint(external_ids)

    def _dead_letter(self, ident: identity.Identity, *, reason: str) -> None:
        """Record an identified paper that could not be written, keyed by its `claim_key`."""
        by_scheme = {eid.scheme: eid.value for eid in ident.external_ids}
        _write_dead_letter(
            self._bucket,
            prefix=self._dead_letter_prefix,
            key=ident.claim_key,
            reason=reason,
            pmid=by_scheme.get('pmid'),
            doi=by_scheme.get('doi'),
        )

    @override
    def process(self, joined: tuple[str, dict[str, list[object]]]) -> Iterator[str]:
        _claim_key, grouped = joined
        works = [work for work in grouped['work'] if isinstance(work, _PaperWork)]
        resolutions = [r for r in grouped['resolved'] if isinstance(r, resolve.ResolvedPaper)]
        resolved = resolutions[0] if resolutions else None
        for work in works:
            if resolved is None:
                # No metadata resolvable: dead-letter the paper (record it for review)
                # and carry on, rather than failing the bundle and aborting the run.
                self._dead_letter(work.ident, reason='metadata unresolved')
                self._unresolved.inc()
                continue
            # Outside the handler, as in the identity stage: a failed seed read is transient
            # and worth Beam's retries, not a permanent dead-letter.
            seed = pipeline.SeedObject(
                bucket_key=work.ref.bucket_key,
                docling_json=self._bucket.blob(work.ref.json_key).download_as_bytes(),
                pdf=self._bucket.blob(work.ref.pdf_key).download_as_bytes(),
            )
            try:
                result = pipeline.ingest_paper(
                    self._bucket,
                    self._mint,
                    seed,
                    work.ident,
                    resolved,
                    self._licence,
                    now=self._now,
                    fetchers=self._fetchers,
                    file_sources=self._file_sources,
                )
            except Exception as exc:  # noqa: BLE001 — isolate one bad paper; the reason is recorded, not swallowed
                _LOG.warning('write half failed for %s, dead-lettering: %r', work.ident.claim_key, exc)
                self._dead_letter(work.ident, reason=f'{type(exc).__name__}: {exc}')
                self._failed.inc()
                continue
            (self._minted if result.minted else self._adopted).inc()
            (self._written if result.written else self._skipped).inc()
            yield result.doc_id


def build_pipeline(
    root: beam.Pipeline,
    *,
    bucket_factory: BucketFactory,
    conn_factory: ConnFactory,
    licence: pipeline.LicenceFacts,
    now: datetime.datetime,
    seed_prefix: str = 'ingest/',
    limit: int | None = None,
    fetchers_factory: FetchersFactory | None = None,
    file_sources_factory: FileSourcesFactory | None = None,
    transport_factory: TransportFactory | None = None,
) -> pvalue.PCollection[str]:
    """Attach the seed-ingestion transform to `root`, returning the doc_id output.

    The driver lists and pairs the seed prefix (via `bucket_factory()`), emits the
    pairs, and runs the three-stage graph: classify identity, batch-resolve
    bibliographic metadata, then join and write. Unpaired seed objects are logged and
    excluded.

    Args:
        root: The Beam pipeline to attach the transform to.
        bucket_factory: Builds the cache `Bucket` (the same bucket holds `ingest/`
            and `papers/`); called in the driver to list and in each worker.
        conn_factory: Builds a Postgres connection for the crosswalk mint, once per
            write-stage worker.
        licence: The non-OA-branch licence fallback (the OA branch reads its own from
            the fetched bytes).
        now: Timezone-aware capture/rendering timestamp (the job's logical time).
        seed_prefix: The flat seed prefix to ingest.
        limit: Ingest only the first `limit` paired seed objects (in stem order) for
            a bounded run; `None` ingests every pair. Deterministic across runs.
        fetchers_factory: Builds the litfetch OA-XML ladder per worker; `None` uses
            litfetch's default (live) ladder.
        file_sources_factory: Builds the litfetch supplementary file sources per
            worker; `None` uses litfetch's default (live) PMC OA source.
        transport_factory: Builds the HTTP transport for batched metadata resolution
            per worker; `None` uses httpx2's default (live) transport.

    Returns:
        The `PCollection` of ingested `doc_id`s.

    Raises:
        ValueError: If `limit` is not positive.
    """
    if limit is not None and limit <= 0:
        raise ValueError(f'limit must be positive, got {limit}')
    pairing = pair_seed_keys(
        (blob.name for blob in bucket_factory().list_blobs(prefix=seed_prefix)), prefix=seed_prefix
    )
    if pairing.unpaired:
        _LOG.warning('%d unpaired seed object(s) excluded from ingestion: %s', len(pairing.unpaired), pairing.unpaired)
    refs = pairing.refs
    if limit is not None and len(refs) > limit:
        _LOG.warning('limit=%d: ingesting the first %d of %d paired seed objects', limit, limit, len(refs))
        refs = refs[:limit]
    dead_letter_prefix, _ = dead_letter_paths(now)

    works = (
        root
        | 'CreateSeedRefs' >> beam.Create(refs)
        | 'FanOut' >> beam.Reshuffle()
        | 'ExtractIdentity'
        >> beam.ParDo(_ExtractIdentityFn(bucket_factory=bucket_factory, dead_letter_prefix=dead_letter_prefix))
    )
    resolved = (
        works
        | 'ToResolveRequests' >> beam.Map(_to_resolve_request)
        | 'CollectForResolve' >> beam.GroupByKey()
        | 'ResolveMetadata' >> beam.ParDo(_ResolveBatchFn(transport_factory=transport_factory))
    )
    return (
        {'work': works, 'resolved': resolved}
        | 'JoinResolution' >> beam.CoGroupByKey()
        | 'WritePaper'
        >> beam.ParDo(
            _WritePaperFn(
                bucket_factory=bucket_factory,
                conn_factory=conn_factory,
                shared_conn=shared.Shared(),
                dead_letter_prefix=dead_letter_prefix,
                licence=licence,
                now=now,
                fetchers_factory=fetchers_factory,
                file_sources_factory=file_sources_factory,
            )
        )
    )


def run_ingestion(
    *,
    bucket_factory: BucketFactory,
    conn_factory: ConnFactory,
    licence: pipeline.LicenceFacts,
    now: datetime.datetime,
    options: pipeline_options.PipelineOptions,
    seed_prefix: str = 'ingest/',
    limit: int | None = None,
) -> IngestionRun:
    """Run seed ingestion over GCS + Cloud SQL (the production entrypoint).

    The fetcher ladder, file sources, and HTTP transport are left to their live
    defaults. The OA branch needs no live id resolver: batched resolution supplies the
    `pmcid` (efetch-harvested or idconv-mapped). `options` carries the runner +
    Dataflow configuration; `bucket_factory` and `conn_factory` carry the backend
    wiring (a real bucket + Cloud SQL, or a fake-gcs-server bucket + throwaway Postgres
    for a local run).

    Args:
        bucket_factory: Builds the cache `Bucket` (holding `ingest/` and `papers/`).
        conn_factory: Builds a crosswalk-database connection per worker.
        licence: The non-OA-branch licence fallback.
        now: Timezone-aware capture/rendering timestamp.
        options: Beam pipeline options (runner, project, region, …).
        seed_prefix: The flat seed prefix to ingest.
        limit: Ingest only the first `limit` paired seed objects (for a bounded run,
            e.g. on the DirectRunner); `None` ingests every pair.

    Returns:
        The `IngestionRun` — the pipeline result plus this run's diagnostics paths, which
        `report_run` needs.
    """
    root = beam.Pipeline(options=options)
    build_pipeline(
        root,
        bucket_factory=bucket_factory,
        conn_factory=conn_factory,
        licence=licence,
        now=now,
        seed_prefix=seed_prefix,
        limit=limit,
    )
    return IngestionRun.stamped(root.run(), now)


def read_counter(result: runner.PipelineResult, name: str) -> int:
    """Total a `METRICS_NAMESPACE` counter across the steps reporting it (0 if unreported)."""
    query = result.metrics().query(metric.MetricsFilter().with_namespace(METRICS_NAMESPACE).with_name(name))
    # Beam keys a metric result by (step, name), so a name two transforms emit — `paper_failed`,
    # from the identity and write stages — comes back as one result per step.
    return sum(counter.result for counter in query['counters'])


def report_run(
    run: IngestionRun,
    bucket_factory: BucketFactory,
    *,
    seed_prefix: str = 'ingest/',
) -> report.IngestReport:
    """Build and log the post-run diagnostics report.

    Call after the run reaches a terminal state (`result.wait_until_finish()`).
    The unpaired-seed list is re-derived here (the seed is still intact — teardown
    is a separate manual step, `themis.litcache.report.teardown_seed`), the counters
    snapshotted from the run's metrics, and the `no_text_layer` papers scanned from
    the committed manifests.

    Args:
        run: The finished run, carrying its own diagnostics paths — so the report
            describes that pass alone.
        bucket_factory: Builds the cache `Bucket` (the same factory the run used);
            called once here to list the seed and scan the manifests.
        seed_prefix: The flat seed prefix the run ingested.

    Returns:
        The `IngestReport`.
    """
    bucket = bucket_factory()
    pairing = pair_seed_keys((blob.name for blob in bucket.list_blobs(prefix=seed_prefix)), prefix=seed_prefix)
    counters = {name: read_counter(run.result, name) for name in _COUNTER_NAMES}
    consolidated = report.write_dead_letter_summary(
        bucket, records_prefix=run.dead_letter_prefix, summary_path=run.dead_letter_summary
    )
    if consolidated:
        _LOG.info(
            '%d dead-letter record(s) consolidated into gs://%s/%s', consolidated, bucket.name, run.dead_letter_summary
        )
    rep = report.build_report(bucket, unpaired_seeds=pairing.unpaired, counters=counters, dead_lettered=consolidated)
    _LOG.info('%s', report.render_report(rep))
    return rep
