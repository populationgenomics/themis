"""Tests for tools.resources.mirror: what is at a destination is skipped, adopted or refused, never replaced."""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import http.server
import io
import json
import os
import signal
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from typing import override

import pytest
import requests
import requests.adapters
from google.auth import credentials
from google.cloud import storage

from tests.tools import conftest
from tools.resources import job, manifest, mirror

_BYTES = b'publisher bytes'
# Well above `mirror._CHUNK` so a single write exceeds one `iter_content` chunk even if the chunk size grows.
_BLOCK = 4 * 1024 * 1024
_ADVERTISED_STREAM_BYTES = 512 * 1024 * 1024


def _pinned_entry(pinned: manifest.Manifest) -> manifest.Entry:
    entry = pinned.table('hgnc-complete-set')
    return manifest.Entry(**{**entry.__dict__, 'md5': hashlib.md5(_BYTES).hexdigest()})  # noqa: S324


def _mirror(entry: manifest.Entry, pinned: manifest.Manifest, bucket: storage.Bucket) -> mirror.Outcome:
    return mirror.mirror(
        entry, dataset=pinned.dataset, bucket=bucket, client=bucket.client, dry_run=False, abandoned=threading.Event()
    )


@contextlib.contextmanager
def _upstream_server(serve: Callable[[http.server.BaseHTTPRequestHandler], None]) -> Iterator[str]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            serve(self)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _send_bytes(handler: http.server.BaseHTTPRequestHandler, payload: bytes) -> None:
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def test_an_object_mirrored_as_pinned_is_skipped(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.table('hgnc-complete-set')
    put_mirrored(entry, _BYTES)
    assert _mirror(entry, pinned, gcs_bucket).status is mirror.Status.SKIPPED


def test_an_object_whose_sidecar_never_landed_is_adopted_when_its_md5_is_the_publishers(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    entry = _pinned_entry(pinned)
    gcs_bucket.blob(f'{pinned.dataset}/{entry.object}').upload_from_string(_BYTES)
    assert _mirror(entry, pinned, gcs_bucket).status is mirror.Status.ADOPTED
    assert mirror.mirrored(entry, dataset=pinned.dataset, bucket=gcs_bucket).digest == entry.md5


def test_an_object_without_a_sidecar_and_no_publisher_digest_is_refused_not_replaced(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    entry = pinned.table('hgnc-complete-set')
    blob = gcs_bucket.blob(f'{pinned.dataset}/{entry.object}')
    blob.upload_from_string(b'whatever a stopped run left')
    blob.reload()
    with pytest.raises(mirror.MirrorError, match='never replaces an object'):
        _mirror(entry, pinned, gcs_bucket)
    blob.reload()
    assert blob.download_as_bytes() == b'whatever a stopped run left'


def test_an_object_mirrored_from_a_url_no_longer_pinned_is_refused(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.table('hgnc-complete-set')
    put_mirrored(entry, _BYTES)
    sidecar = gcs_bucket.blob(f'{pinned.dataset}/{entry.object}{mirror.PROVENANCE_SUFFIX}')
    sidecar.upload_from_string(
        json.dumps({**json.loads(sidecar.download_as_bytes()), 'url': 'https://example.org/old'})
    )
    with pytest.raises(mirror.MirrorError, match='delete both'):
        _mirror(entry, pinned, gcs_bucket)


def _break(bucket: storage.Bucket, pinned: manifest.Manifest, entry: manifest.Entry) -> None:
    bucket.blob(f'{pinned.dataset}/{entry.object}{mirror.PROVENANCE_SUFFIX}').upload_from_string(
        json.dumps({'url': 'https://example.org/old'})
    )


def test_every_entry_is_reported_once_and_the_failures_come_back_in_manifest_order(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = pinned.entries
    for entry in entries:
        put_mirrored(entry, entry.id.encode())
    broken = [entries[-1], entries[1]]
    for entry in broken:
        _break(gcs_bucket, pinned, entry)
    failed = mirror.mirror_all(
        entries, dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True
    )
    assert failed == [entries[1].id, entries[-1].id]
    reported = [line.split()[-1] for line in capsys.readouterr().err.splitlines() if line.startswith('  skip')]
    assert sorted(reported) == sorted(entry.id for entry in entries if entry not in broken)


def test_entries_from_different_hosts_are_mirrored_at_once(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    together = threading.Barrier(4, timeout=10)

    def serve(handler: http.server.BaseHTTPRequestHandler) -> None:
        together.wait()
        _send_bytes(handler, _BYTES)

    with contextlib.ExitStack() as stack:
        bases = [stack.enter_context(_upstream_server(serve)) for _ in range(4)]
        entries = [
            manifest.Entry(**{**entry.__dict__, 'url': f'{base}/file', 'md5': None})
            for entry, base in zip(pinned.entries, bases, strict=False)
        ]
        failed = mirror.mirror_all(
            entries, dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=False
        )

    assert failed == []
    expected_md5 = hashlib.md5(_BYTES).hexdigest()  # noqa: S324
    for entry in entries:
        assert mirror.mirrored(entry, dataset=pinned.dataset, bucket=gcs_bucket).digest == expected_md5


def test_one_host_has_at_most_its_slots_in_flight(
    gcs_endpoint: str, gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    assert gcs_bucket.name is not None
    cond = threading.Condition()
    gate = threading.Event()
    admitted_a: set[str] = set()
    in_flight_a = 0
    in_flight_b = 0
    peak_a = 0

    raw_a = pinned.entries[:8]
    raw_b = pinned.entries[8:10]
    id_by_object_a = {entry.object: entry.id for entry in raw_a}

    class _SlotObserver(requests.adapters.HTTPAdapter):
        @override
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            decoded_url = urllib.parse.unquote(request.url or '')
            if request.method == 'GET' and '/o/' in decoded_url:
                for obj, entry_id in id_by_object_a.items():
                    if f'{pinned.dataset}/{obj}{mirror.PROVENANCE_SUFFIX}' in decoded_url:
                        with cond:
                            admitted_a.add(entry_id)
                            cond.notify_all()
            return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    def serve_a(handler: http.server.BaseHTTPRequestHandler) -> None:
        nonlocal in_flight_a, peak_a
        with cond:
            in_flight_a += 1
            peak_a = max(peak_a, in_flight_a)
            cond.notify_all()
        gate.wait(timeout=10)
        with cond:
            in_flight_a -= 1
            cond.notify_all()
        _send_bytes(handler, _BYTES)

    def serve_b(handler: http.server.BaseHTTPRequestHandler) -> None:
        nonlocal in_flight_b
        with cond:
            in_flight_b += 1
            cond.notify_all()
        gate.wait(timeout=10)
        _send_bytes(handler, _BYTES)

    session = requests.Session()
    session.mount('http://', _SlotObserver())
    client = storage.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
        _http=session,
    )
    with (
        contextlib.closing(session),
        contextlib.closing(client),
        _upstream_server(serve_a) as base_a,
        _upstream_server(serve_b) as base_b,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as coord_pool,
    ):
        entries_a = [
            manifest.Entry(**{**entry.__dict__, 'url': f'{base_a}/{entry.id}', 'md5': None}) for entry in raw_a
        ]
        entries_b = [
            manifest.Entry(**{**entry.__dict__, 'url': f'{base_b}/{entry.id}', 'md5': None}) for entry in raw_b
        ]
        entries = [*entries_a, *entries_b]

        def release_once_wave_arrives() -> None:
            # `entries_b` are submitted after `entries_a`: once both `entries_b` reach `serve_b` and every entry
            # admitted on `host_a` has finished its GCS checks and reached `serve_a`, the host semaphore must hold
            # the remaining `entries_a` back.
            with cond:
                assert cond.wait_for(
                    lambda: in_flight_b == len(entries_b) and in_flight_a >= 2 and in_flight_a == len(admitted_a),
                    timeout=10,
                )
                assert 2 <= len(admitted_a) < len(entries_a)
            gate.set()

        coord_future = coord_pool.submit(release_once_wave_arrives)
        try:
            failed = mirror.mirror_all(
                entries, dataset=pinned.dataset, bucket=client.bucket(gcs_bucket.name), client=client, dry_run=False
            )
        finally:
            gate.set()
        coord_future.result(timeout=10)

    assert failed == []
    assert 2 <= peak_a < len(entries_a)


def test_an_error_no_entry_absorbs_abandons_the_pass_at_once_and_waits_out_the_writes_begun(
    gcs_endpoint: str, gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    assert gcs_bucket.name is not None
    first_raw, second_raw, third_raw = pinned.entries[:3]
    first_uploading = threading.Event()
    third_streaming = threading.Event()
    second_sidecar_returned = threading.Event()
    third_aborted = threading.Event()
    release_third_tail = threading.Event()
    third_tail_reached = threading.Event()

    def serve(handler: http.server.BaseHTTPRequestHandler) -> None:
        if handler.path == '/first':
            _send_bytes(handler, _BYTES)
            return
        # `/third`: advertise 512 MiB and stream chunks until `third` observes `run.abandoned` and closes the socket
        # (setting `third_aborted`); hold the final tail behind `release_third_tail` so a broken `abandoned` check
        # stalls instead of completing.
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(_ADVERTISED_STREAM_BYTES))
        handler.end_headers()
        handler.wfile.write(b'x' * _BLOCK)
        handler.wfile.flush()
        third_streaming.set()
        assert second_sidecar_returned.wait(timeout=10)
        try:
            for _ in range(100):
                if release_third_tail.is_set():
                    break
                handler.wfile.write(b'x' * _BLOCK)
                handler.wfile.flush()
            release_third_tail.wait(timeout=10)
            third_tail_reached.set()
        except (BrokenPipeError, ConnectionResetError):
            third_aborted.set()

    # Corrupt `second`'s provenance sidecar with invalid JSON: `json.JSONDecodeError` is not a `TRANSFER_ERRORS`
    # member, so `_mirror_one` does not absorb it and `mirror_all` abandons the pass.
    first_sidecar_bytes = f'{first_raw.object}{mirror.PROVENANCE_SUFFIX}'.encode()
    second_sidecar = f'{pinned.dataset}/{second_raw.object}{mirror.PROVENANCE_SUFFIX}'
    gcs_bucket.blob(second_sidecar).upload_from_string(b'not-json')

    class _CoordinatingAdapter(requests.adapters.HTTPAdapter):
        @override
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            decoded_url = urllib.parse.unquote(request.url or '')
            raw_body = request.body if isinstance(request.body, bytes) else b''
            if request.method == 'POST' and first_raw.object.encode('utf-8') in raw_body:
                if first_sidecar_bytes in raw_body:
                    # Hold `first`'s final provenance write until `third_aborted` proves `mirror_all` has already
                    # entered `except Exception:` and set `run.abandoned`, so `pool.shutdown(wait=False)` would fail.
                    assert third_aborted.wait(timeout=10)
                else:
                    first_uploading.set()
            if request.method == 'GET' and '/download/' in decoded_url and second_sidecar in decoded_url:
                assert first_uploading.wait(timeout=10)
                assert third_streaming.wait(timeout=10)
                response = super().send(
                    request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
                )
                second_sidecar_returned.set()
                return response
            return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    session = requests.Session()
    session.mount('http://', _CoordinatingAdapter())
    client = storage.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
        _http=session,
    )
    expected_md5 = hashlib.md5(_BYTES).hexdigest()  # noqa: S324
    with contextlib.closing(session), contextlib.closing(client), _upstream_server(serve) as base:
        first = manifest.Entry(**{**first_raw.__dict__, 'url': f'{base}/first', 'md5': None})
        second = manifest.Entry(**{**second_raw.__dict__, 'url': f'{base}/second', 'md5': None})
        third = manifest.Entry(**{**third_raw.__dict__, 'url': f'{base}/third', 'md5': None})
        bucket = client.bucket(gcs_bucket.name)
        try:
            with pytest.raises(json.JSONDecodeError):
                mirror.mirror_all(
                    [first, second, third], dataset=pinned.dataset, bucket=bucket, client=client, dry_run=False
                )
            assert mirror.mirrored(first, dataset=pinned.dataset, bucket=gcs_bucket).digest == expected_md5
            assert not third_tail_reached.is_set()
        finally:
            release_third_tail.set()

    assert list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{third.object}')) == []


def test_a_stopped_pass_does_not_wait_out_the_writes_begun(
    gcs_endpoint: str, gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    assert gcs_bucket.name is not None
    entry_raw = pinned.entries[0]
    sigterm_sent = threading.Event()
    release_upload = threading.Event()
    worker_threads: list[threading.Thread] = []

    class _StopOnUploadAdapter(requests.adapters.HTTPAdapter):
        @override
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            raw_body = request.body if isinstance(request.body, bytes) else b''
            if request.method == 'POST' and entry_raw.object.encode('utf-8') in raw_body:
                if not sigterm_sent.is_set():
                    worker_threads.append(threading.current_thread())
                    sigterm_sent.set()
                    os.kill(os.getpid(), signal.SIGTERM)
                release_upload.wait(timeout=10)
                aborted = requests.Response()
                aborted.status_code = 403
                aborted.headers['Content-Type'] = 'application/json'
                aborted.raw = io.BytesIO(b'{"error": {"code": 403, "message": "stopped"}}')
                aborted.request = request
                aborted.url = request.url or ''
                return aborted
            return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    session = requests.Session()
    session.mount('http://', _StopOnUploadAdapter())
    client = storage.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
        _http=session,
    )
    with (
        contextlib.closing(session),
        contextlib.closing(client),
        _upstream_server(lambda handler: _send_bytes(handler, _BYTES)) as base,
    ):
        entry = manifest.Entry(**{**entry_raw.__dict__, 'url': f'{base}/file', 'md5': None})
        bucket = client.bucket(gcs_bucket.name)
        try:
            with pytest.raises(job.Stopped), job.stoppable():
                mirror.mirror_all([entry], dataset=pinned.dataset, bucket=bucket, client=client, dry_run=False)
            assert [t.is_alive() for t in worker_threads] == [True]
            assert list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{entry.object}')) == []
        finally:
            release_upload.set()
            for worker_thread in worker_threads:
                worker_thread.join(timeout=10)


def test_an_abandoned_download_stops_between_chunks_and_writes_nothing(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    abandoned = threading.Event()
    release_tail = threading.Event()
    tail_reached = threading.Event()

    def serve(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(_ADVERTISED_STREAM_BYTES))
        handler.end_headers()
        handler.wfile.write(b'x' * _BLOCK)
        handler.wfile.flush()
        abandoned.set()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            handler.wfile.write(b'x' * _BLOCK)
            handler.wfile.flush()
            release_tail.wait(timeout=10)
            tail_reached.set()

    entry_raw = pinned.table('hgnc-complete-set')
    with _upstream_server(serve) as base:
        entry = manifest.Entry(**{**entry_raw.__dict__, 'url': f'{base}/file', 'md5': None})
        try:
            with pytest.raises(mirror.MirrorError, match='abandoned'):
                mirror.mirror(
                    entry,
                    dataset=pinned.dataset,
                    bucket=gcs_bucket,
                    client=gcs_bucket.client,
                    dry_run=False,
                    abandoned=abandoned,
                )
            assert not tail_reached.is_set()
        finally:
            release_tail.set()

    assert not list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{entry.object}'))


def test_an_abandoned_copy_within_gcs_writes_nothing(gcs_bucket: storage.Bucket, pinned: manifest.Manifest) -> None:
    gcs_bucket.blob('upstream/file').upload_from_string(b'upstream bytes')
    entry = pinned.table('hgnc-complete-set')
    entry = manifest.Entry(**{**entry.__dict__, 'url': f'gs://{gcs_bucket.name}/upstream/file', 'md5': None})
    abandoned = threading.Event()
    abandoned.set()
    with pytest.raises(mirror.MirrorError, match='abandoned'):
        mirror.mirror(
            entry,
            dataset=pinned.dataset,
            bucket=gcs_bucket,
            client=gcs_bucket.client,
            dry_run=False,
            abandoned=abandoned,
        )
    assert not list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{entry.object}'))
