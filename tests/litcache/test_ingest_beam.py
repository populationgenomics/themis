"""Tests for the Beam seed-ingestion wrapper (`themis.litcache.ingest_beam`).

Two layers: `pair_seed_keys` is pure and tested offline; `build_pipeline` is driven
on the DirectRunner over a fake-gcs-server bucket + the throwaway Postgres the
pipeline/crosswalk tests share (Docker-gated). The DirectRunner runs the SDK harness
in-process (`direct_running_mode=in_memory`), so a worker factory can hand back the
very bucket the driver seeded — shared through a module-level registry keyed by a
token (test-only state; the factories must be picklable, so they cannot close over
the instance directly).
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import json
import pathlib
import threading
import time
import typing
import urllib.parse
from collections.abc import Callable, Iterable, Iterator

import apache_beam as beam
import httpx2
import litfetch
import pg8000.dbapi
import pg8000.exceptions
import protovalidate
import pytest
import testcontainers.postgres
from apache_beam.options import pipeline_options
from apache_beam.runners import runner
from apache_beam.testing import test_pipeline
from apache_beam.utils import shared
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud import tasks_v2

from themis.litcache import crosswalk, enqueue, identity, ingest_beam, openalex, pipeline
from themis.litcache.models import litcache_pb2, work_pb2
from themis.testing import openalex as fake_openalex

_FIXTURES = pathlib.Path(__file__).parent.parent / 'fixtures' / 'litcache'
_NONOA = _FIXTURES / 'nonoa'
_NOW = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)
_LATER = _NOW + datetime.timedelta(hours=1)  # a second pass over the same bucket

# Buckets shared between the driver and the in-process DirectRunner workers: the
# factory is a top-level function (picklable) that looks the bucket up by token, so
# every worker sees the instance the driver seeded.
_BUCKETS: dict[str, gcs.Bucket] = {}


def _bucket_for(token: str) -> gcs.Bucket:
    return _BUCKETS[token]


# Every task each test's run tried to create, by the test's token: the DoFn's enqueuer is built in the
# runner, so the test reads what it asked for back through a registry, as it reaches its bucket.
_TASKS: dict[str, list[tasks_v2.Task]] = {}


class _TasksClient:
    def __init__(self, token: str) -> None:
        self._token = token

    def create_task(self, *, parent: str, task: tasks_v2.Task) -> tasks_v2.Task:  # noqa: ARG002 — the client's signature
        held = any(t.name == task.name for t in _TASKS[self._token])
        _TASKS[self._token].append(task)
        if held:
            raise api_exceptions.AlreadyExists(task.name)
        return task


_WORK_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/l/queues/themis-convert-bulk',
    worker_url='https://worker.example',
    invoker_service_account_email='invoker@p.iam.gserviceaccount.com',
)


def _enqueuer_for(token: str) -> enqueue.Enqueuer:
    return enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', _TasksClient(token)), _WORK_TARGET)


def _no_fetchers() -> list[litfetch.Fetcher]:
    """Close the OA branch so ingestion stays offline (non-OA papers only)."""
    return []


def _resolve_handler(request: httpx2.Request) -> httpx2.Response:
    """Resolve the batch: idconv maps nothing (OA closed), OpenAlex echoes each DOI."""
    if 'openalex' in request.url.host:
        flt = request.url.params.get('filter', '')
        dois = flt.removeprefix('doi:').split('|') if flt.startswith('doi:') else []
        results = [
            {
                'id': f'https://openalex.org/{doi}',
                'doi': f'https://doi.org/{doi}',
                'title': f'Synthetic {doi}',
                'display_name': f'Synthetic {doi}',
                'publication_date': '2020-01-01',
                'ids': {},  # no pmid → resolves via OpenAlex's own record
                'type': 'article',
                'primary_location': {'source': {'display_name': 'Synthetic Journal'}},
            }
            for doi in dois
        ]
        return httpx2.Response(200, content=json.dumps({'meta': {'count': len(results)}, 'results': results}).encode())
    if 'idconv' in request.url.path:
        return httpx2.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode('utf-8'))
    raise AssertionError(f'unexpected resolve request (Crossref/efetch not expected here): {request.url}')


def _resolve_transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(_resolve_handler)


def _licence() -> pipeline.LicenceFacts:
    return pipeline.LicenceFacts(
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    )


def _manifests(bucket: gcs.Bucket) -> list[litcache_pb2.Manifest]:
    return [
        litcache_pb2.Manifest.FromString(blob.download_as_bytes())
        for blob in bucket.list_blobs(prefix='papers/')
        if blob.name.endswith('/manifest.pb')
    ]


def _crosswalk(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    """Every claimed external id and the doc_id it resolves to."""
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return {str(external_id): str(doc_id) for external_id, doc_id in cur.fetchall()}


# --- pair_seed_keys (pure, no Docker) -------------------------------------------


def test_pair_seed_keys_pairs_by_stem() -> None:
    keys = ['ingest/10.1%2Fa.json', 'ingest/10.1%2Fa.pdf', 'ingest/10.2%2Fb.pdf', 'ingest/10.2%2Fb.json']
    pairing = ingest_beam.pair_seed_keys(keys)

    assert [r.bucket_key for r in pairing.refs] == ['10.1%2Fa.json', '10.2%2Fb.json']
    assert pairing.refs[0].json_key == 'ingest/10.1%2Fa.json'
    assert pairing.refs[0].pdf_key == 'ingest/10.1%2Fa.pdf'
    assert pairing.unpaired == []


def test_pair_seed_keys_reports_unpaired() -> None:
    keys = ['ingest/a.json', 'ingest/a.pdf', 'ingest/lonely.json', 'ingest/orphan.pdf', 'ingest/weird.txt']
    pairing = ingest_beam.pair_seed_keys(keys)

    assert [r.bucket_key for r in pairing.refs] == ['a.json']
    assert set(pairing.unpaired) == {'ingest/lonely.json', 'ingest/orphan.pdf', 'ingest/weird.txt'}


def test_pair_seed_keys_ignores_keys_outside_the_prefix() -> None:
    keys = ['papers/x/manifest.pb', 'ingest/a.json', 'ingest/a.pdf']
    pairing = ingest_beam.pair_seed_keys(keys)

    assert [r.bucket_key for r in pairing.refs] == ['a.json']
    assert pairing.unpaired == []


def test_seeds_sharing_a_claim_key_but_not_a_pmid_resolve_apart() -> None:
    by_doi = identity.determine_identity('10.1%2Fabc.json', identity.DoclingOrigin(filename=None, binary_hash=None))
    by_pmid = identity.determine_identity(
        '30000001.json', identity.DoclingOrigin(filename='10.1%2Fabc.pdf', binary_hash=None)
    )
    assert by_doi.claim_key == by_pmid.claim_key

    assert ingest_beam._resolve_request(by_doi).join_key != ingest_beam._resolve_request(by_pmid).join_key


def test_build_pipeline_rejects_non_positive_limit() -> None:
    # Validation happens driver-side (before any list or worker/DB), so this needs
    # neither Docker nor a real backend — the factories are never reached.
    def _unused_bucket() -> gcs.Bucket:
        raise AssertionError('bucket_factory must not be called when the limit is invalid')

    def _unused_conn() -> crosswalk.Connection:
        raise AssertionError('conn_factory must not be called when the limit is invalid')

    with pytest.raises(ValueError, match='limit must be positive'):
        ingest_beam.build_pipeline(
            beam.Pipeline(),
            bucket_factory=_unused_bucket,
            conn_factory=_unused_conn,
            licence=_licence(),
            now=_NOW,
            openalex_api_key_factory=_openalex_api_key,
            limit=0,
            ask=None,
        )


# --- _MintConnection ------------------------------------------------------------

_MINT_RESULT = crosswalk.MintResult(doc_id='doc-1', minted=True, linked_doc_ids=())


class _FakeConn:
    """A crosswalk connection recording the recovery calls `_MintConnection` makes."""

    def __init__(self, rollback_error: Exception | None) -> None:
        self.rollbacks = 0
        self.closed = False
        self._rollback_error = rollback_error

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._rollback_error is not None:
            raise self._rollback_error

    def close(self) -> None:
        self.closed = True


class _Dialer:
    """A `ConnFactory` recording every connection it hands out."""

    def __init__(self, rollback_error: Exception | None = None) -> None:
        self.dialed: list[_FakeConn] = []
        self._rollback_error = rollback_error

    def __call__(self) -> crosswalk.Connection:
        conn = _FakeConn(self._rollback_error)
        self.dialed.append(conn)
        return typing.cast('crosswalk.Connection', conn)


def _mints(monkeypatch: pytest.MonkeyPatch, *outcomes: Exception | None) -> None:
    """Drive `crosswalk.mint` through `outcomes`, raising one or returning the fixed result."""
    remaining = list(outcomes)

    def _mint(_conn: crosswalk.Connection, _external_ids: object) -> crosswalk.MintResult:
        outcome = remaining.pop(0)
        if outcome is not None:
            raise outcome
        return _MINT_RESULT

    monkeypatch.setattr(crosswalk, 'mint', _mint)


def test_mint_connection_dials_once_and_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _mints(monkeypatch, None, None)
    dialer = _Dialer()
    holder = ingest_beam._MintConnection(dialer)

    assert holder.mint(['doi:a']) == _MINT_RESULT
    assert holder.mint(['doi:b']) == _MINT_RESULT
    assert len(dialer.dialed) == 1


def test_write_paper_instances_share_one_connection_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # Runner v2 runs several `_WritePaperFn` instances per worker; the whole point of the
    # `Shared` wiring is that they land on one connection. Driving `_MintConnection`
    # directly cannot see that, so this goes through the DoFn.
    _mints(monkeypatch, None, None)
    dialer = _Dialer()
    token = shared.Shared()

    def _fn() -> ingest_beam._WritePaperFn:
        return ingest_beam._WritePaperFn(
            bucket_factory=lambda: typing.cast('gcs.Bucket', None),
            conn_factory=dialer,
            shared_conn=token,
            dead_letter_prefix='diagnostics/dead_letters/test/',
            licence=_licence(),
            now=_NOW,
            fetchers_factory=None,
            file_sources_factory=None,
            ask=None,
        )

    first, second = _fn(), _fn()
    first.setup()
    second.setup()

    # Both instances hold the same connection object, so nothing rests on `Shared`'s
    # single-slot keepalive surviving another stage's acquire.
    assert first._mint_conn is second._mint_conn
    assert first._mint(['doi:a']) == _MINT_RESULT
    assert second._mint(['doi:b']) == _MINT_RESULT
    assert len(dialer.dialed) == 1


def test_mint_connection_rolls_back_a_failed_transaction_and_keeps_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A statement-level failure aborts the transaction but not the session: rolling back
    # leaves the same connection usable for the next paper.
    _mints(monkeypatch, pg8000.exceptions.DatabaseError('deadlock detected'), None)
    dialer = _Dialer()
    holder = ingest_beam._MintConnection(dialer)

    with pytest.raises(pg8000.exceptions.DatabaseError):
        holder.mint(['doi:a'])
    assert holder.mint(['doi:b']) == _MINT_RESULT

    assert len(dialer.dialed) == 1
    assert dialer.dialed[0].rollbacks == 1
    assert not dialer.dialed[0].closed


def test_mint_connection_redials_after_the_session_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloud SQL restarting mid-run must not poison every remaining paper on the worker.
    _mints(monkeypatch, pg8000.exceptions.InterfaceError('network error'), None)
    dialer = _Dialer()
    holder = ingest_beam._MintConnection(dialer)

    with pytest.raises(pg8000.exceptions.InterfaceError):
        holder.mint(['doi:a'])
    assert holder.mint(['doi:b']) == _MINT_RESULT

    assert len(dialer.dialed) == 2
    assert dialer.dialed[0].closed
    assert dialer.dialed[0].rollbacks == 0


def test_mint_connection_redials_when_the_rollback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rollback that cannot be issued means the session is gone, whatever the mint raised.
    _mints(monkeypatch, pg8000.exceptions.DatabaseError('statement timeout'), None)
    dialer = _Dialer(rollback_error=pg8000.exceptions.InterfaceError('connection is closed'))
    holder = ingest_beam._MintConnection(dialer)

    with pytest.raises(pg8000.exceptions.DatabaseError):
        holder.mint(['doi:a'])
    assert holder.mint(['doi:b']) == _MINT_RESULT

    assert len(dialer.dialed) == 2
    assert dialer.dialed[0].closed


def test_mint_connection_serializes_concurrent_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    # pg8000 is not thread-safe, so no two bundle processors may be inside a mint at once.
    guard = threading.Lock()
    in_flight = 0
    peak = 0

    def _mint(_conn: crosswalk.Connection, _external_ids: object) -> crosswalk.MintResult:
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.005)
        with guard:
            in_flight -= 1
        return _MINT_RESULT

    monkeypatch.setattr(crosswalk, 'mint', _mint)
    dialer = _Dialer()
    holder = ingest_beam._MintConnection(dialer)

    threads = [threading.Thread(target=holder.mint, args=(['doi:a'],)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert peak == 1
    assert len(dialer.dialed) == 1


# --- build_pipeline (DirectRunner + Postgres) -----------------------------------


@pytest.fixture
def conn_factory(
    postgres_container: testcontainers.postgres.PostgresContainer,
) -> Callable[[], pg8000.dbapi.Connection]:
    """A picklable factory opening a fresh connection to the migration-applied container."""
    return functools.partial(
        pg8000.dbapi.connect,
        host=postgres_container.get_container_host_ip(),
        port=int(postgres_container.get_exposed_port(5432)),
        user='test',
        password='test',
        database='litcache',
    )


# Distinct DOI bucket keys give two distinct identities from the same bytes (the
# docling origin filename is opaque, so the DOI key is the identity signal).
_SEEDED_STEMS = ('10.5555%2Fsynthetic.aaa', '10.5555%2Fsynthetic.bbb')


def _drop(bucket: gcs.Bucket, *stems: str, pdf: bytes | None = None) -> None:
    """Drop a non-OA seed pair under `ingest/` for each stem."""
    if pdf is None:
        pdf = (_NONOA / 'source.pdf').read_bytes()
    for stem in stems:
        bucket.blob(f'ingest/{stem}.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
        bucket.blob(f'ingest/{stem}.pdf').upload_from_string(pdf)


def _seed_keys(bucket: gcs.Bucket) -> list[str]:
    return sorted(blob.name for blob in bucket.list_blobs(prefix='ingest/'))


@pytest.fixture
def seeded_bucket(request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket) -> Iterator[tuple[str, gcs.Bucket]]:
    """A registered fake-gcs bucket seeded with two non-OA papers under `ingest/`."""
    token = request.node.name
    _drop(gcs_bucket, *_SEEDED_STEMS)
    _BUCKETS[token] = gcs_bucket
    yield token, gcs_bucket
    del _BUCKETS[token]


def _options() -> pipeline_options.PipelineOptions:
    # In-process harness so the registry-backed bucket factory hands back the seeded
    # instance; the DirectRunner default is in_memory but pin it for determinism.
    return pipeline_options.PipelineOptions(['--direct_running_mode=in_memory'])


def _openalex_api_key() -> openalex.ApiKey:
    return fake_openalex.API_KEY


def _run(
    token: str,
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    *,
    limit: int | None = None,
    ask: ingest_beam.WorkAsk | None = None,
) -> runner.PipelineResult:
    with test_pipeline.TestPipeline(options=_options()) as root:
        ingest_beam.build_pipeline(
            root,
            ask=ask,
            bucket_factory=functools.partial(_bucket_for, token),
            conn_factory=conn_factory,
            licence=_licence(),
            now=_NOW,
            openalex_api_key_factory=_openalex_api_key,
            limit=limit,
            fetchers_factory=_no_fetchers,
            transport_factory=_resolve_transport,
        )
    return root.result


def _dead_letters(bucket: gcs.Bucket) -> list[dict[str, object]]:
    """Every dead-letter record on the bucket — what the run recorded, and why."""
    return [
        json.loads(blob.download_as_bytes())
        for blob in bucket.list_blobs(prefix='diagnostics/dead_letters/')
        if blob.name.endswith('.json')
    ]


def test_build_pipeline_ingests_both_papers(
    conn: pg8000.dbapi.Connection,
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
) -> None:
    token, bucket = seeded_bucket
    _run(token, conn_factory)

    manifests = _manifests(bucket)
    assert len(manifests) == 2
    assert {m.external_ids.doi for m in manifests} == {'10.5555/synthetic.aaa', '10.5555/synthetic.bbb'}

    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT count(*) FROM litcache.crosswalk')
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 2


@pytest.mark.usefixtures('conn')  # applies the schema; the run claims via its own connection
def test_each_paper_committed_is_asked_for_once_with_its_generation(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    _TASKS[token] = []
    ask = ingest_beam.WorkAsk(enqueuer=functools.partial(_enqueuer_for, token))
    try:
        _run(token, conn_factory, ask=ask)
        tasks = list(_TASKS[token])
        # Skips both papers, and asks again at the generations it finds: a retried bundle skips what its
        # failed attempt committed, which that attempt may not have asked for.
        _drop(bucket, *_SEEDED_STEMS)
        _run(token, conn_factory, ask=ask)
        again = _TASKS[token][len(tasks) :]
    finally:
        del _TASKS[token]

    manifests = {m.doc_id: m for m in _manifests(bucket)}
    requests = [work_pb2.WorkRequest.FromString(t.http_request.body) for t in tasks]
    assert len(manifests) == 2
    assert sorted(r.doc_id for r in requests) == sorted(manifests)
    assert sorted(t.name for t in again) == sorted(t.name for t in tasks)  # the queue drops them as held
    assert all(not r.targets for r in requests)
    for task, request in zip(tasks, requests, strict=True):
        blob = bucket.get_blob(f'papers/{request.doc_id}/manifest.pb')
        assert blob is not None
        assert task.name.endswith(f'/tasks/{request.doc_id}--gen-{blob.generation}')


@pytest.mark.parametrize(
    ('targets', 'error'),
    [(('Main-Text',), protovalidate.ValidationError), (('transcriptoin',), ValueError)],
)
def test_a_run_naming_targets_the_worker_cannot_carry_is_refused_before_it_starts(
    targets: tuple[str, ...], error: type[Exception]
) -> None:
    ask = ingest_beam.WorkAsk(enqueuer=functools.partial(_enqueuer_for, 'unused'), targets=targets)

    def _unused_bucket() -> gcs.Bucket:
        raise AssertionError('bucket_factory must not be called when the targets are invalid')

    def _unused_conn() -> crosswalk.Connection:
        raise AssertionError('conn_factory must not be called when the targets are invalid')

    def _unused_key() -> openalex.ApiKey:
        raise AssertionError('openalex_api_key_factory must not be called when the targets are invalid')

    with pytest.raises(error):
        ingest_beam.build_pipeline(
            beam.Pipeline(),
            bucket_factory=_unused_bucket,
            conn_factory=_unused_conn,
            licence=_licence(),
            now=_NOW,
            openalex_api_key_factory=_unused_key,
            ask=ask,
        )


@pytest.mark.usefixtures('conn')  # applies the schema; the run claims via its own connection
def test_build_pipeline_limit_caps_ingestion(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    # Two papers seeded; limit=1 ingests only the first in stem order ("aaa" < "bbb").
    _run(token, conn_factory, limit=1)

    manifests = _manifests(bucket)
    assert len(manifests) == 1
    assert {m.external_ids.doi for m in manifests} == {'10.5555/synthetic.aaa'}


def test_build_pipeline_re_run_adopts_the_incumbent_doc_ids(
    conn: pg8000.dbapi.Connection,
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
) -> None:
    token, bucket = seeded_bucket
    _run(token, conn_factory)
    first_ids = _crosswalk(conn)

    _drop(bucket, *_SEEDED_STEMS)
    _run(token, conn_factory)

    # A second pass claims nothing new. Only the crosswalk can show that: the manifests are
    # create-only (`writer.write_paper`), so they are unchanged whichever branch the run takes, and
    # a skip leaves no other trace. The skip itself is `test_pipeline.test_re_run_is_idempotent_and_skips`;
    # what this adds is that the Beam wrapper, with its shared mint connection, does not re-mint.
    assert _crosswalk(conn) == first_ids


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_report_run_summarizes_what_the_run_left(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    # An extra unpaired seed object: it is excluded from ingestion and surfaced.
    bucket.blob('ingest/lonely.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    result = _run(token, conn_factory)

    rep = ingest_beam.report_run(ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token))

    # The report totals: two papers written, the one unpaired seed surfaced, and no
    # no_text_layer flags (the synthetic text pdf is char-addressable). Every field
    # asserted here is counted off the bucket, not read back out of Beam's metrics.
    assert rep.papers_total == 2
    assert rep.unpaired_seeds == ['ingest/lonely.json']
    assert rep.flagged == []
    assert rep.dead_lettered == 0
    assert _seed_keys(bucket) == ['ingest/lonely.json']
    assert len(_manifests(bucket)) == 2


def _unresolvable_transport() -> httpx2.MockTransport:
    """A transport where neither idconv nor OpenAlex resolves any DOI."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'openalex' in request.url.host:
            return httpx2.Response(200, content=json.dumps({'meta': {'count': 0}, 'results': []}).encode())
        if 'idconv' in request.url.path:
            return httpx2.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode())
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx2.MockTransport(handler)


def _forbidden_conn() -> pg8000.dbapi.Connection:
    raise AssertionError('a dead-lettered (unresolved) paper must not open a DB connection')


def test_build_pipeline_dead_letters_unresolvable_papers(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # A paper whose DOI resolves nowhere is recorded under the dead-letter prefix and
    # the run completes — no crosswalk connection is even opened (mint is never reached).
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.ghost.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.ghost.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                ask=None,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_unresolvable_transport,
            )
        result = root.result

        # Recorded as unresolved rather than failed, and nothing written. The record carries which
        # of the two it was, where the counter only carried how many.
        records = _dead_letters(gcs_bucket)
        assert [r['reason'] for r in records] == ['metadata unresolved']
        assert 'synthetic.ghost' in str(records[0]['key'])
        assert _manifests(gcs_bucket) == []
        assert len(_seed_keys(gcs_bucket)) == 2  # kept for the next run to retry

        # The report counts the records on the bucket, so a run whose metrics never
        # reached the driver still reports its failures rather than a clean 0.
        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.dead_lettered == 1

        # A later pass over the same bucket, with the failing seed gone, reports its own
        # failures — not the bucket's history. The launcher gates on `dead_lettered`, so a
        # stale count would fail every later run.
        for blob in list(gcs_bucket.list_blobs(prefix='ingest/')):
            blob.delete()
        with test_pipeline.TestPipeline(options=_options()) as second_root:
            ingest_beam.build_pipeline(
                second_root,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_LATER,
                ask=None,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_unresolvable_transport,
            )
        second = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(second_root.result, _LATER), functools.partial(_bucket_for, token)
        )
        assert second.dead_lettered == 0
        # the first pass's record and summary are still there, under its own stamp
        assert list(gcs_bucket.list_blobs(prefix=ingest_beam.dead_letter_paths(_NOW)[0]))
    finally:
        del _BUCKETS[token]


_BOOK_PMID = '30000010'


def _unresolvable_with_efetch_transport() -> httpx2.MockTransport:
    """The unresolvable transport, with efetch answering every PMID with an empty set."""
    unresolvable = _unresolvable_transport()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'efetch' in request.url.path:
            return httpx2.Response(200, content=b'<?xml version="1.0"?><PubmedArticleSet></PubmedArticleSet>')
        return unresolvable.handle_request(request)

    return httpx2.MockTransport(handler)


def test_seeds_sharing_a_claim_key_are_dead_lettered_apart(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    token = request.node.name
    docling = json.loads((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.ghost.json').upload_from_string(json.dumps(docling))
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.ghost.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    # Keyed by a PMID, and naming the first seed's DOI in its Docling origin: the same claim key.
    docling['origin']['filename'] = '10.5555%2Fsynthetic.ghost.pdf'
    gcs_bucket.blob('ingest/30000009.json').upload_from_string(json.dumps(docling))
    gcs_bucket.blob('ingest/30000009.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                ask=None,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_unresolvable_with_efetch_transport,
            )

        records = _dead_letters(gcs_bucket)
        assert sorted(str(r['key']) for r in records) == ['10.5555%2Fsynthetic.ghost.json', '30000009.json']
        assert {r['claim_key'] for r in records} == {'doi:10.5555/synthetic.ghost'}
    finally:
        _BUCKETS.pop(token, None)


def _precondition_failing_efetch_transport() -> httpx2.MockTransport:
    """A transport where efetch answers the book PMID with a record stating no Bookshelf accession."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'efetch' in request.url.path:
            record_set = (_FIXTURES / 'bookshelf' / 'efetch.xml').read_bytes()
            content = record_set.replace(b'<ArticleId IdType="bookaccession">NBK900001</ArticleId>', b'')
            return httpx2.Response(200, content=content)
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx2.MockTransport(handler)


def _drifting_openalex_transport() -> httpx2.MockTransport:
    """A transport where OpenAlex answers each DOI with a work whose declared key holds a value of another shape."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'openalex' in request.url.host:
            flt = request.url.params.get('filter', '')
            dois = flt.removeprefix('doi:').split('|') if flt.startswith('doi:') else []
            results = [
                {
                    'id': f'https://openalex.org/{doi}',
                    'doi': f'https://doi.org/{doi}',
                    'title': 'Drifted',
                    'ids': {},
                    'publication_year': 'soon',
                }
                for doi in dois
            ]
            return httpx2.Response(
                200, content=json.dumps({'meta': {'count': len(results)}, 'results': results}).encode()
            )
        if 'idconv' in request.url.path:
            return httpx2.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode())
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx2.MockTransport(handler)


def test_build_pipeline_dead_letters_a_record_its_reader_cannot_read(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # A paper whose OpenAlex record holds a field a reader reads in a shape it cannot read is
    # dead-lettered as schema drift naming the field, and the run completes without a crosswalk
    # connection.
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.drift.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.drift.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                ask=None,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_drifting_openalex_transport,
            )
        result = root.result

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        reason = str(records[0]['reason'])
        assert reason.startswith('schema drift: ')
        assert 'publication_year' in reason
        assert _manifests(gcs_bucket) == []

        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.dead_lettered == 1
        assert rep.counters['paper_schema_drift'] == 1
        assert rep.counters['paper_unresolved'] == 0
    finally:
        del _BUCKETS[token]


_AMBIGUOUS_DOI = '10.5555/synthetic.ambiguous'


def _ambiguous_openalex_transport() -> httpx2.MockTransport:
    """A transport where OpenAlex holds two works of one DOI, and idconv maps nothing."""
    works = [fake_openalex.work(work_id=w, doi=_AMBIGUOUS_DOI) for w in ('W1', 'W2')]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            return fake_openalex.answer(works, request)
        if 'idconv' in request.url.path:
            return httpx2.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode())
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx2.MockTransport(handler)


def test_build_pipeline_dead_letters_a_paper_openalex_holds_two_works_of(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # Taking either work could give the paper another work's record, so the paper is dead-lettered naming both.
    token = request.node.name
    stem = urllib.parse.quote(_AMBIGUOUS_DOI, safe='')
    gcs_bucket.blob(f'ingest/{stem}.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob(f'ingest/{stem}.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                ask=None,
                fetchers_factory=_no_fetchers,
                transport_factory=_ambiguous_openalex_transport,
            )
        result = root.result

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        reason = str(records[0]['reason'])
        assert reason.startswith('ambiguous OpenAlex work: ')
        assert 'openalex.org/W1' in reason
        assert 'openalex.org/W2' in reason
        assert _manifests(gcs_bucket) == []

        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.counters['paper_ambiguous_work'] == 1
    finally:
        del _BUCKETS[token]


def _failing_efetch_transport() -> httpx2.MockTransport:
    """A transport where efetch answers 400 to every call naming `_BOOK_PMID`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'efetch' in request.url.path:
            assert _BOOK_PMID in request.content.decode()
            return httpx2.Response(400)
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx2.MockTransport(handler)


def test_build_pipeline_dead_letters_a_paper_efetch_fails_on(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # One PMID efetch fails on alone costs its paper, not the run; the seed is kept, so the next run retries it.
    token = request.node.name
    _drop(gcs_bucket, _BOOK_PMID)
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                ask=None,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_failing_efetch_transport,
            )
        result = root.result

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        assert str(records[0]['reason']).startswith('index failed: ')
        assert records[0]['pmid'] == _BOOK_PMID
        assert _seed_keys(gcs_bucket) == [f'ingest/{_BOOK_PMID}.json', f'ingest/{_BOOK_PMID}.pdf']

        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.counters['paper_index_failed'] == 1
        assert rep.counters['paper_unresolved'] == 0
    finally:
        del _BUCKETS[token]


def test_build_pipeline_dead_letters_a_record_failing_the_precondition(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # A paper whose PMID efetch answers with a record that fails the store's precondition is
    # dead-lettered with that reason — distinct from 'metadata unresolved' — and the run completes
    # without opening a crosswalk connection.
    token = request.node.name
    gcs_bucket.blob(f'ingest/{_BOOK_PMID}.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob(f'ingest/{_BOOK_PMID}.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        with test_pipeline.TestPipeline(options=_options()) as root:
            ingest_beam.build_pipeline(
                root,
                ask=None,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                openalex_api_key_factory=_openalex_api_key,
                fetchers_factory=_no_fetchers,
                transport_factory=_precondition_failing_efetch_transport,
            )
        result = root.result

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        reason = str(records[0]['reason'])
        assert reason.startswith('precondition failed: ')
        assert 'no Bookshelf accession' in reason
        assert records[0]['pmid'] == _BOOK_PMID
        assert _manifests(gcs_bucket) == []

        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.dead_lettered == 1
        # Counted under its own name, not as unresolved: the record exists.
        assert rep.counters['paper_precondition_failed'] == 1
        assert rep.counters['paper_unresolved'] == 0
    finally:
        del _BUCKETS[token]


def _failing_conn() -> pg8000.dbapi.Connection:
    """A connection whose first use raises — forces the write half (mint) to fail."""

    class _Boom:
        def cursor(self) -> object:
            raise RuntimeError('simulated crosswalk failure')

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    return typing.cast('pg8000.dbapi.Connection', _Boom())


def test_build_pipeline_dead_letters_write_failures(request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket) -> None:
    # A paper that resolves but whose write half raises is dead-lettered with the
    # exception as the reason and counted `paper_failed`; the run completes rather than
    # one bad paper aborting the whole pass.
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        _run(token, _failing_conn)

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        assert 'simulated crosswalk failure' in str(records[0]['reason'])
        assert _manifests(gcs_bucket) == []
    finally:
        del _BUCKETS[token]


def test_build_pipeline_dead_letters_oversized_seed_pdf(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An oversized seed pdf is dead-lettered by its size (before download) and counted
    # paper_failed, so it can't OOM/stall the worker; the run carries on.
    monkeypatch.setattr(ingest_beam, '_MAX_SEED_PDF_BYTES', 4)  # any real pdf exceeds this
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fhuge.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fhuge.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        _run(token, _forbidden_conn)

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        assert 'too large' in str(records[0]['reason'])
        assert _manifests(gcs_bucket) == []
    finally:
        del _BUCKETS[token]


def test_build_pipeline_dead_letters_unreadable_seed(request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket) -> None:
    # A seed whose docling json is malformed fails identity extraction; it is dead-lettered
    # (keyed by the seed object) and counted paper_failed, and the run completes — the write
    # half is never reached, so no DB connection is opened.
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fbroken.json').upload_from_string(b'{not valid json')
    gcs_bucket.blob('ingest/10.5555%2Fbroken.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        _run(token, _forbidden_conn)

        records = _dead_letters(gcs_bucket)
        assert len(records) == 1
        assert 'not valid JSON' in str(records[0]['reason'])
        assert _manifests(gcs_bucket) == []
    finally:
        del _BUCKETS[token]


class _FailingBlob:
    """A blob whose read fails the way a transient GCS fault does."""

    def download_as_bytes(self) -> bytes:
        raise api_exceptions.ServiceUnavailable('simulated GCS 503')


class _FlakyBucket:
    """A real bucket with one key's download failing; everything else delegates."""

    def __init__(self, bucket: gcs.Bucket, failing_key: str) -> None:
        self._bucket = bucket
        self._failing_key = failing_key

    def __getattr__(self, name: str) -> object:
        return getattr(self._bucket, name)

    def blob(self, key: str) -> object:
        return _FailingBlob() if key == self._failing_key else self._bucket.blob(key)


def test_a_transient_seed_read_fails_the_bundle_rather_than_dead_lettering(
    request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket
) -> None:
    # A seed that cannot be read right now is not a seed that will never parse: it has to
    # reach Beam's bundle retries, not be written off as a permanent per-paper data problem.
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = typing.cast('gcs.Bucket', _FlakyBucket(gcs_bucket, 'ingest/10.5555%2Fsynthetic.aaa.json'))
    try:
        with pytest.raises(Exception, match='simulated GCS 503'):
            _run(token, _forbidden_conn)

        assert list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/')) == []
    finally:
        del _BUCKETS[token]


def test_paper_failed_totals_both_stages(request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket) -> None:
    # `paper_failed` is emitted by the identity stage and the write stage, and Beam keys a
    # metric result by (step, name) — so a run that fails one paper in each must report 2,
    # not whichever step the query happens to return first.
    token = request.node.name
    gcs_bucket.blob('ingest/10.5555%2Fbroken.json').upload_from_string(b'{not valid json')
    gcs_bucket.blob('ingest/10.5555%2Fbroken.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    gcs_bucket.blob('ingest/10.5555%2Fsynthetic.aaa.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())
    _BUCKETS[token] = gcs_bucket
    try:
        _run(token, _failing_conn)

        # Both papers dead-lettered and neither written: the bucket says so without asking Beam.
        assert len(list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))) == 2
        assert _manifests(gcs_bucket) == []
    finally:
        del _BUCKETS[token]


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_run_deletes_the_seeds_its_papers_hold(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    bucket.blob('ingest/lonely.pdf').upload_from_string((_NONOA / 'source.pdf').read_bytes())

    result = _run(token, conn_factory)

    assert len(_manifests(bucket)) == 2
    assert _seed_keys(bucket) == ['ingest/lonely.pdf']  # no counterpart: no run can ingest it
    assert ingest_beam.read_counter(result, 'seed_deleted') == 2


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_seed_dropped_again_for_a_committed_paper_is_deleted(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    _run(token, conn_factory)
    committed = {m.doc_id: m for m in _manifests(bucket)}

    _drop(bucket, *_SEEDED_STEMS)
    result = _run(token, conn_factory)

    assert {m.doc_id: m for m in _manifests(bucket)} == committed
    assert _seed_keys(bucket) == []
    assert ingest_beam.read_counter(result, 'paper_skipped') == 2


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_seed_whose_pdf_its_paper_does_not_hold_is_kept_and_dead_lettered(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    _run(token, conn_factory)

    # Another copy of a committed paper's pdf: the paper is skipped, and its pdf is not this one.
    stem = _SEEDED_STEMS[0]
    _drop(bucket, stem, pdf=(_NONOA / 'source.pdf').read_bytes() + b'\n%another copy\n')
    result = _run(token, conn_factory)

    assert _seed_keys(bucket) == [f'ingest/{stem}.json', f'ingest/{stem}.pdf']
    (record,) = _dead_letters(bucket)
    assert record['key'] == f'{stem}.json'
    assert str(record['reason']).startswith('seed not held: ')
    assert ingest_beam.read_counter(result, 'seed_not_held') == 1
    settled = sum(ingest_beam.read_counter(result, n) for n in ('seed_deleted', 'seed_not_held', 'seed_replaced'))
    stored = sum(ingest_beam.read_counter(result, n) for n in ('paper_written', 'paper_skipped'))
    assert settled == stored == 1


class _GenerationCheckedBlob:
    """A real blob whose delete honours `if_generation_match`, which fake-gcs-server ignores."""

    def __init__(self, bucket: gcs.Bucket, key: str) -> None:
        self._bucket = bucket
        self._blob = bucket.blob(key)

    def __getattr__(self, name: str) -> object:
        return getattr(self._blob, name)

    def delete(self, *, if_generation_match: int) -> None:
        current = self._bucket.get_blob(self._blob.name)
        if current is None:
            raise api_exceptions.NotFound(self._blob.name)
        if current.generation != if_generation_match:
            raise api_exceptions.PreconditionFailed(f'{self._blob.name} is at {current.generation}')
        self._blob.delete(if_generation_match=if_generation_match)


class _GenerationCheckedBucket:
    """A real bucket whose blobs' deletes honour their generation precondition; everything else delegates."""

    def __init__(self, bucket: gcs.Bucket) -> None:
        self._bucket = bucket

    def __getattr__(self, name: str) -> object:
        return getattr(self._bucket, name)

    def blob(self, key: str) -> object:
        return _GenerationCheckedBlob(self._bucket, key)


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
@pytest.mark.parametrize('suffix', ['pdf', 'json'])
def test_a_seed_dropped_again_while_the_run_read_it_is_kept(
    suffix: str,
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, bucket = seeded_bucket
    stem = _SEEDED_STEMS[0]
    dropped_again = b'%dropped again\n'
    held = pipeline.why_seed_not_held

    def drop_again_then_check(bucket_: gcs.Bucket, manifest: litcache_pb2.Manifest, seed_pdf: bytes) -> str | None:
        if manifest.external_ids.doi == '10.5555/synthetic.aaa':
            bucket_.blob(f'ingest/{stem}.{suffix}').upload_from_string(dropped_again)
        return held(bucket_, manifest, seed_pdf)

    monkeypatch.setattr(pipeline, 'why_seed_not_held', drop_again_then_check)
    _BUCKETS[token] = typing.cast('gcs.Bucket', _GenerationCheckedBucket(bucket))
    result = _run(token, conn_factory)

    # The pair waits for the next run, whichever of its objects was dropped again; the other paper's seed is deleted.
    assert _seed_keys(bucket) == [f'ingest/{stem}.json', f'ingest/{stem}.pdf']
    assert bucket.blob(f'ingest/{stem}.{suffix}').download_as_bytes() == dropped_again
    assert ingest_beam.read_counter(result, 'seed_replaced') == 1
    assert ingest_beam.read_counter(result, 'seed_deleted') == 1


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_seed_gone_before_the_write_stage_is_counted_and_the_run_completes(
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As a retried write bundle finds a seed its first attempt deleted: the run completes.
    token, bucket = seeded_bucket
    stem = _SEEDED_STEMS[0]
    extract = pipeline.extract_identity

    def extract_then_delete(seed: pipeline.SeedObject) -> identity.Identity:
        ident = extract(seed)
        if seed.bucket_key == f'{stem}.json':
            bucket.blob(f'ingest/{stem}.pdf').delete()
        return ident

    monkeypatch.setattr(pipeline, 'extract_identity', extract_then_delete)
    result = _run(token, conn_factory)

    assert {m.external_ids.doi for m in _manifests(bucket)} == {'10.5555/synthetic.bbb'}
    assert _dead_letters(bucket) == []
    assert ingest_beam.read_counter(result, 'seed_gone') == 1


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_seed_gone_after_the_driver_listed_it_is_counted_and_the_run_completes(
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As an overlapping run deletes a seed this one listed.
    token, bucket = seeded_bucket
    stem = _SEEDED_STEMS[0]
    pair = ingest_beam.pair_seed_keys

    def pair_then_delete(keys: Iterable[str], *, prefix: str = 'ingest/') -> ingest_beam.SeedPairing:
        pairing = pair(keys, prefix=prefix)
        bucket.blob(f'ingest/{stem}.pdf').delete()
        return pairing

    monkeypatch.setattr(ingest_beam, 'pair_seed_keys', pair_then_delete)
    result = _run(token, conn_factory)

    assert {m.external_ids.doi for m in _manifests(bucket)} == {'10.5555/synthetic.bbb'}
    assert _dead_letters(bucket) == []
    assert ingest_beam.read_counter(result, 'seed_gone') == 1


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_a_pdf_dropped_again_after_the_replacement_check_is_kept(
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The drop lands between the check and the delete, so the delete's own generation precondition is what keeps it.
    token, bucket = seeded_bucket
    stem = _SEEDED_STEMS[0]
    dropped_again = b'%dropped again\n'

    def drop_again_unseen(self: object, key: str, generation: int) -> bool:
        del self, generation
        if key == f'ingest/{stem}.pdf':
            bucket.blob(key).upload_from_string(dropped_again)
        return False

    monkeypatch.setattr(ingest_beam._WritePaperFn, '_replaced', drop_again_unseen)
    _BUCKETS[token] = typing.cast('gcs.Bucket', _GenerationCheckedBucket(bucket))
    result = _run(token, conn_factory)

    assert _seed_keys(bucket) == [f'ingest/{stem}.json', f'ingest/{stem}.pdf']
    assert bucket.blob(f'ingest/{stem}.pdf').download_as_bytes() == dropped_again
    assert ingest_beam.read_counter(result, 'seed_replaced') == 1
