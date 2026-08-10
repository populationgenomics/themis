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
from collections.abc import Callable, Iterator

import apache_beam as beam
import httpx
import litfetch
import pg8000.dbapi
import pg8000.exceptions
import pytest
import testcontainers.postgres
from apache_beam.options import pipeline_options
from apache_beam.runners import runner
from apache_beam.testing import test_pipeline
from apache_beam.utils import shared
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs

from themis.litcache import crosswalk, ingest_beam, pipeline
from themis.litcache.models import litcache_pb2

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


def _no_fetchers() -> list[litfetch.Fetcher]:
    """Close the OA branch so ingestion stays offline (non-OA papers only)."""
    return []


def _resolve_handler(request: httpx.Request) -> httpx.Response:
    """Resolve the batch: idconv maps nothing (OA closed), OpenAlex echoes each DOI."""
    if 'openalex' in request.url.host:
        flt = request.url.params.get('filter', '')
        dois = flt.removeprefix('doi:').split('|') if flt.startswith('doi:') else []
        results = [
            {
                'doi': f'https://doi.org/{doi}',
                'display_name': f'Synthetic {doi}',
                'publication_date': '2020-01-01',
                'ids': {},  # no pmid → resolves via OpenAlex's own record
                'type': 'article',
                'primary_location': {'source': {'display_name': 'Synthetic Journal'}},
            }
            for doi in dois
        ]
        return httpx.Response(200, content=json.dumps({'meta': {'count': len(results)}, 'results': results}).encode())
    if 'idconv' in request.url.path:
        return httpx.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode('utf-8'))
    raise AssertionError(f'unexpected resolve request (Crossref/efetch not expected here): {request.url}')


def _resolve_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_resolve_handler)


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
            limit=0,
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


@pytest.fixture
def seeded_bucket(request: pytest.FixtureRequest, gcs_bucket: gcs.Bucket) -> Iterator[tuple[str, gcs.Bucket]]:
    """A registered fake-gcs bucket seeded with two non-OA papers under `ingest/`."""
    token = request.node.name
    docling = (_NONOA / 'docling.json').read_bytes()
    pdf = (_NONOA / 'source.pdf').read_bytes()
    # Distinct DOI bucket keys give two distinct identities from the same bytes (the
    # docling origin filename is opaque, so the DOI key is the identity signal).
    for doi in ('10.5555%2Fsynthetic.aaa', '10.5555%2Fsynthetic.bbb'):
        gcs_bucket.blob(f'ingest/{doi}.json').upload_from_string(docling)
        gcs_bucket.blob(f'ingest/{doi}.pdf').upload_from_string(pdf)
    _BUCKETS[token] = gcs_bucket
    yield token, gcs_bucket
    del _BUCKETS[token]


def _options() -> pipeline_options.PipelineOptions:
    # In-process harness so the registry-backed bucket factory hands back the seeded
    # instance; the DirectRunner default is in_memory but pin it for determinism.
    return pipeline_options.PipelineOptions(['--direct_running_mode=in_memory'])


def _run(
    token: str, conn_factory: Callable[[], pg8000.dbapi.Connection], *, limit: int | None = None
) -> runner.PipelineResult:
    with test_pipeline.TestPipeline(options=_options()) as root:
        ingest_beam.build_pipeline(
            root,
            bucket_factory=functools.partial(_bucket_for, token),
            conn_factory=conn_factory,
            licence=_licence(),
            now=_NOW,
            limit=limit,
            fetchers_factory=_no_fetchers,
            transport_factory=_resolve_transport,
        )
    return root.result


def _counter(result: runner.PipelineResult, name: str) -> int:
    return ingest_beam.read_counter(result, name)


def test_build_pipeline_ingests_both_papers(
    conn: pg8000.dbapi.Connection,
    conn_factory: Callable[[], pg8000.dbapi.Connection],
    seeded_bucket: tuple[str, gcs.Bucket],
) -> None:
    token, bucket = seeded_bucket
    result = _run(token, conn_factory)

    # Per-stage counters: both papers seen, both minted fresh, both written.
    assert _counter(result, 'papers_seen') == 2
    assert _counter(result, 'doc_id_minted') == 2
    assert _counter(result, 'paper_written') == 2
    assert _counter(result, 'paper_skipped') == 0

    manifests = _manifests(bucket)
    assert len(manifests) == 2
    assert {m.external_ids.doi for m in manifests} == {'10.5555/synthetic.aaa', '10.5555/synthetic.bbb'}

    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT count(*) FROM litcache.crosswalk')
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 2


@pytest.mark.usefixtures('conn')  # applies the schema; the run claims via its own connection
def test_build_pipeline_limit_caps_ingestion(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    # Two papers seeded; limit=1 ingests only the first in stem order ("aaa" < "bbb").
    result = _run(token, conn_factory, limit=1)

    assert _counter(result, 'papers_seen') == 1
    assert _counter(result, 'paper_written') == 1
    manifests = _manifests(bucket)
    assert len(manifests) == 1
    assert {m.external_ids.doi for m in manifests} == {'10.5555/synthetic.aaa'}


@pytest.mark.usefixtures('conn')  # applies the schema; the run claims via its own connection
def test_build_pipeline_re_run_skips_cached_papers(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, _bucket = seeded_bucket
    _run(token, conn_factory)
    second = _run(token, conn_factory)

    # The second run adopts the incumbent doc_ids and skips both (manifests exist).
    assert _counter(second, 'papers_seen') == 2
    assert _counter(second, 'doc_id_minted') == 0
    assert _counter(second, 'doc_id_adopted') == 2
    assert _counter(second, 'paper_written') == 0
    assert _counter(second, 'paper_skipped') == 2


@pytest.mark.usefixtures('conn')  # applies the schema; the run mints via its own connection
def test_report_run_summarizes_and_leaves_seed_intact(
    conn_factory: Callable[[], pg8000.dbapi.Connection], seeded_bucket: tuple[str, gcs.Bucket]
) -> None:
    token, bucket = seeded_bucket
    # An extra unpaired seed object: it is excluded from ingestion and surfaced.
    bucket.blob('ingest/lonely.json').upload_from_string((_NONOA / 'docling.json').read_bytes())
    result = _run(token, conn_factory)

    rep = ingest_beam.report_run(ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token))

    # The report totals: two papers written, the one unpaired seed surfaced, and no
    # no_text_layer flags (the synthetic text pdf is char-addressable).
    assert rep.papers_total == 2
    assert rep.unpaired_seeds == ['ingest/lonely.json']
    assert rep.flagged == []
    assert rep.counters['paper_written'] == 2
    assert rep.dead_lettered == 0

    # The report never deletes: the seed prefix is left intact for the manual
    # teardown (`themis.litcache.report.teardown_seed`).
    assert list(bucket.list_blobs(prefix='ingest/')) != []
    assert len(_manifests(bucket)) == 2


def _unresolvable_transport() -> httpx.MockTransport:
    """A transport where neither idconv nor OpenAlex resolves any DOI."""

    def handler(request: httpx.Request) -> httpx.Response:
        if 'openalex' in request.url.host:
            return httpx.Response(200, content=json.dumps({'meta': {'count': 0}, 'results': []}).encode())
        if 'idconv' in request.url.path:
            return httpx.Response(200, content=json.dumps({'status': 'ok', 'records': []}).encode())
        raise AssertionError(f'unexpected resolve request: {request.url}')

    return httpx.MockTransport(handler)


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
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_NOW,
                fetchers_factory=_no_fetchers,
                transport_factory=_unresolvable_transport,
            )
        result = root.result

        assert _counter(result, 'papers_seen') == 1
        assert _counter(result, 'paper_unresolved') == 1
        assert _counter(result, 'paper_written') == 0
        dead_lettered = list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))
        assert len(dead_lettered) == 1
        assert 'synthetic.ghost' in dead_lettered[0].name

        # The report counts the records on the bucket, so a run whose metrics never
        # reached the driver still reports its failures rather than a clean 0.
        rep = ingest_beam.report_run(
            ingest_beam.IngestionRun.stamped(result, _NOW), functools.partial(_bucket_for, token)
        )
        assert rep.dead_lettered == 1

        # A later pass over the same bucket, with the failing seed gone, reports its own
        # failures — not the bucket's history. The gate on `dead_lettered` is the
        # precondition for tearing the seed down, so a stale count blocks that forever.
        for blob in list(gcs_bucket.list_blobs(prefix='ingest/')):
            blob.delete()
        with test_pipeline.TestPipeline(options=_options()) as second_root:
            ingest_beam.build_pipeline(
                second_root,
                bucket_factory=functools.partial(_bucket_for, token),
                conn_factory=_forbidden_conn,
                licence=_licence(),
                now=_LATER,
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
        result = _run(token, _failing_conn)

        assert _counter(result, 'papers_seen') == 1
        assert _counter(result, 'paper_failed') == 1
        assert _counter(result, 'paper_written') == 0
        dead_lettered = list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))
        assert len(dead_lettered) == 1
        record = json.loads(dead_lettered[0].download_as_bytes())
        assert 'simulated crosswalk failure' in record['reason']
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
        result = _run(token, _forbidden_conn)

        assert _counter(result, 'papers_seen') == 1
        assert _counter(result, 'paper_failed') == 1
        assert _counter(result, 'paper_written') == 0
        dead_lettered = list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))
        assert len(dead_lettered) == 1
        assert 'too large' in json.loads(dead_lettered[0].download_as_bytes())['reason']
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
        result = _run(token, _forbidden_conn)

        assert _counter(result, 'papers_seen') == 1
        assert _counter(result, 'paper_failed') == 1
        assert _counter(result, 'paper_written') == 0
        dead_lettered = list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))
        assert len(dead_lettered) == 1
        record = json.loads(dead_lettered[0].download_as_bytes())
        assert 'not valid JSON' in record['reason']
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
        result = _run(token, _failing_conn)

        assert _counter(result, 'papers_seen') == 2
        assert _counter(result, 'paper_written') == 0
        assert _counter(result, 'paper_failed') == 2
        assert len(list(gcs_bucket.list_blobs(prefix='diagnostics/dead_letters/'))) == 2
    finally:
        del _BUCKETS[token]
