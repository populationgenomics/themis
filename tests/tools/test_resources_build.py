"""Tests for tools.resources.build: build ids follow the inputs, builds land once, and nothing is overwritten."""

from __future__ import annotations

import contextlib
import gzip
import http.server
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from typing import TextIO, override

import pytest
import requests
from google.auth import credentials
from google.cloud import storage
from weaver_data_provider import genome as genome_mod
from weaver_data_provider import store as store_mod

from tests.tools import conftest
from tools.resources import build, job, manifest, mirror, status

_FASTA = gzip.compress(b'>NC_000001.11 Homo sapiens chromosome 1\nACGTACGTAC\nGTACGTACGT\n')


def _no_genome_dir(_: build.Build) -> pathlib.Path:
    raise AssertionError('no Ensembl store is run here')


def _genome(pinned: manifest.Manifest, bucket: storage.Bucket) -> build.Build:
    (genome,) = [
        p.build for p in build.plan(pinned, bucket=bucket, genomes=_no_genome_dir) if p.label == 'ncbi-grch38 genome'
    ]
    assert genome is not None
    return genome


def _ensembl_store(pinned: manifest.Manifest, bucket: storage.Bucket) -> build.Build:
    (store,) = [
        p.build for p in build.plan(pinned, bucket=bucket, genomes=_no_genome_dir) if p.label == 'ensembl-grch38 store'
    ]
    assert store is not None
    return store


def _mirror_ensembl_inputs(
    pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored, *, genome_bytes: bytes = _FASTA
) -> None:
    ncbi = pinned.assembly('ncbi-grch38')
    put_mirrored(ncbi.object('genome'), genome_bytes)
    put_mirrored(ncbi.object('alignments'), b'bam')
    put_mirrored(ncbi.object('alignments/index'), b'bai')
    ensembl = pinned.assembly('ensembl-grch38')
    for path, entry in ensembl.objects.items():
        if path != 'genome':
            put_mirrored(entry, path.encode())
    for table in pinned.tables.values():
        put_mirrored(table, table.id.encode())


def _stub_ensembl_popen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen_genomes: list[pathlib.Path],
    on_genome_start: Callable[[], None] | None = None,
) -> None:
    """Intercept ``weaver-data-build`` subprocess launches for ``ensembl`` and ``index`` steps."""
    real_popen = subprocess.Popen
    cli_prefix = [sys.executable, '-m', 'weaver_data_provider.build.cli']

    def popen(
        command: Sequence[str],
        *,
        stdout: TextIO | None = None,
        stderr: int | None = None,
        text: bool = True,
        errors: str | None = None,
    ) -> subprocess.Popen[str]:
        cmd = list(command)
        if cmd[:3] == cli_prefix:
            args = cmd[3:]
            if args[0] == 'genome' and on_genome_start is not None:
                on_genome_start()
            elif args[0] == 'ensembl':
                genome_dir = pathlib.Path(args[args.index('--genome') + 1])
                assert (genome_dir / genome_mod.CATALOGUE).is_file()
                assert (genome_dir / genome_mod.BLOCKS).is_file()
                seen_genomes.append(genome_dir)
                shards = pathlib.Path(args[args.index('--shards') + 1])
                shards.mkdir(parents=True, exist_ok=True)
                shard = shards / 'ensembl.bagz'
                shard.write_bytes(b'shard')
                return real_popen(
                    [sys.executable, '-c', f'print({str(shard)!r})'],
                    stdout=stdout,
                    stderr=stderr,
                    text=text,
                    errors=errors,
                )
            elif args[0] == 'index':
                out = pathlib.Path(args[args.index('--out') + 1])
                out.mkdir(parents=True, exist_ok=True)
                (out / store_mod.MANIFEST).write_bytes(b'manifest')
                return real_popen([sys.executable, '-c', ''], stdout=stdout, stderr=stderr, text=text, errors=errors)
        return real_popen(cmd, stdout=stdout, stderr=stderr, text=text, errors=errors)

    monkeypatch.setattr(build.subprocess, 'Popen', popen)


@contextlib.contextmanager
def _gcs_proxy(
    upstream: str,
    bucket_name: str,
    *,
    on_request: Callable[[str, str, bytes], int | None] | None = None,
) -> Iterator[storage.Bucket]:
    """Forward GCS HTTP calls to ``fake-gcs-server``, running ``on_request(method, path, body)`` first."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _forward(self) -> None:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            decoded_path = urllib.parse.unquote(self.path)
            status_override = on_request(self.command, decoded_path, body) if on_request is not None else None
            if status_override is not None:
                payload = json.dumps({'error': {'code': status_override, 'message': 'aborted'}}).encode()
                self.send_response(status_override)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            forwarded = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'content-length')}
            resp = requests.request(self.command, f'{upstream}{self.path}', data=body, headers=forwarded, timeout=10)
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ('transfer-encoding', 'content-encoding', 'content-length', 'connection'):
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def do_PUT(self) -> None:
            self._forward()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        client = storage.Client(
            project='themis-test',
            credentials=credentials.AnonymousCredentials(),
            client_options={'api_endpoint': f'http://{host}:{port}'},
        )
        with contextlib.closing(client):
            yield client.bucket(bucket_name)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_genome_is_built_once_under_its_build_id(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    build.build_all(pinned, bucket=gcs_bucket, dry_run=False)
    assert build.exists(gcs_bucket, genome)
    written = {blob.name: blob.generation for blob in gcs_bucket.list_blobs(prefix=f'{genome.prefix}/')}
    assert f'{genome.prefix}/{genome_mod.CATALOGUE}' in written
    recorded = json.loads(gcs_bucket.blob(f'{genome.prefix}/provenance.json').download_as_bytes())
    assert recorded == genome.provenance

    build.build_all(pinned, bucket=gcs_bucket, dry_run=False)
    assert {blob.name: blob.generation for blob in gcs_bucket.list_blobs(prefix=f'{genome.prefix}/')} == written


def test_a_changed_input_lands_beside_the_build_it_supersedes(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.assembly('ncbi-grch38').object('genome')
    put_mirrored(entry, _FASTA)
    before = _genome(pinned, gcs_bucket)
    put_mirrored(entry, gzip.compress(b'>NC_000001.11\nTTTT\n'))
    after = _genome(pinned, gcs_bucket)
    assert after.build_id != before.build_id
    assert after.prefix.rsplit('/', 1)[0] == before.prefix.rsplit('/', 1)[0]


def test_a_partial_prefix_is_refused_rather_than_skipped_or_written_into(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    gcs_bucket.blob(f'{genome.prefix}/provenance.json').upload_from_string('{}')
    with pytest.raises(build.BuildError, match='stopped before writing'):
        build.exists(gcs_bucket, genome)
    assert 'ncbi-grch38 genome' in build.build_all(pinned, bucket=gcs_bucket, dry_run=False)


def test_an_object_mirrored_from_another_url_is_not_built_from(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.assembly('ncbi-grch38').object('genome')
    put_mirrored(entry, _FASTA)
    sidecar = gcs_bucket.blob(f'{pinned.dataset}/{entry.object}{mirror.PROVENANCE_SUFFIX}')
    sidecar.upload_from_string(
        json.dumps({**json.loads(sidecar.download_as_bytes()), 'url': 'https://example.org/old'})
    )
    (genome,) = [
        p for p in build.plan(pinned, bucket=gcs_bucket, genomes=_no_genome_dir) if p.label == 'ncbi-grch38 genome'
    ]
    assert genome.build is None
    assert 'manifest pins' in str(genome.error)


def test_a_store_placed_against_a_genome_built_this_run_waits_for_and_reuses_it(
    gcs_endpoint: str,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gcs_bucket.name is not None
    _mirror_ensembl_inputs(pinned, put_mirrored)
    seen_genomes: list[pathlib.Path] = []
    downloaded_paths: list[str] = []
    _stub_ensembl_popen(monkeypatch, seen_genomes=seen_genomes)

    def record_downloads(method: str, path: str, _body: bytes) -> int | None:
        if method == 'GET' and '/download/' in path:
            downloaded_paths.append(path)
        return None

    with _gcs_proxy(gcs_endpoint, gcs_bucket.name, on_request=record_downloads) as proxied_bucket:
        failed = build.build_all(pinned, bucket=proxied_bucket, dry_run=False)

    assert 'ncbi-grch38 genome' not in failed
    assert 'ensembl-grch38 store' not in failed
    assert len(seen_genomes) == 1
    assert downloaded_paths
    assert not any(genome_mod.CATALOGUE in path for path in downloaded_paths)
    assert build.exists(gcs_bucket, _ensembl_store(pinned, gcs_bucket))


def test_a_store_placed_against_a_genome_built_earlier_downloads_it(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    build.build_all(pinned, bucket=gcs_bucket, dry_run=False)
    _mirror_ensembl_inputs(pinned, put_mirrored)
    seen_genomes: list[pathlib.Path] = []

    def must_not_rebuild_genome() -> None:
        raise AssertionError('already-built genome should be downloaded, not rebuilt')

    _stub_ensembl_popen(monkeypatch, seen_genomes=seen_genomes, on_genome_start=must_not_rebuild_genome)

    failed = build.build_all(pinned, bucket=gcs_bucket, dry_run=False)

    assert 'ncbi-grch38 genome' not in failed
    assert 'ensembl-grch38 store' not in failed
    assert len(seen_genomes) == 1
    assert build.exists(gcs_bucket, _ensembl_store(pinned, gcs_bucket))


def test_a_store_placed_against_a_genome_whose_build_failed_settles_and_fails(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mirror_ensembl_inputs(pinned, put_mirrored, genome_bytes=b'not a gzipped fasta')

    failed = build.build_all(pinned, bucket=gcs_bucket, dry_run=False)

    assert 'ncbi-grch38 genome' in failed
    assert 'ensembl-grch38 store' in failed
    assert 'is not built' in capsys.readouterr().err


def test_a_kept_genome_with_a_partial_prefix_settles_so_dependent_stores_do_not_hang(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mirror_ensembl_inputs(pinned, put_mirrored)
    genome = _genome(pinned, gcs_bucket)
    gcs_bucket.blob(f'{genome.prefix}/provenance.json').upload_from_string('{}')

    failed = build.build_all(pinned, bucket=gcs_bucket, dry_run=False)

    assert 'ncbi-grch38 genome' in failed
    assert 'ensembl-grch38 store' in failed
    assert 'stopped before writing' in capsys.readouterr().err


def test_a_builder_step_names_its_build_on_every_line(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = build.Runner('some build', build.Abandon())
    with pytest.raises(build.BuildError, match='exited'):
        runner.cli('genome', '--assembly', 'GRCh38', '--fasta', str(tmp_path / 'absent.fa.gz'), '--out', str(tmp_path))
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) > 1
    assert all(line.startswith('  [some build] ') for line in lines)


def test_abandoning_a_pass_kills_its_running_builder_children() -> None:
    abandon = build.Abandon()
    with (
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], text=True) as child,
        abandon.watching(child),
    ):
        abandon()
        assert child.wait(timeout=10) != 0


def test_an_error_no_build_absorbs_kills_running_builder_steps_at_once(
    tmp_path: pathlib.Path,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build.tempfile, 'tempdir', str(tmp_path))
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    put_mirrored(pinned.assembly('ncbi-grch37').object('genome'), _FASTA)
    child_running = threading.Event()
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def popen(
        command: Sequence[str],
        *,
        stdout: TextIO | None = None,
        stderr: int | None = None,
        text: bool = True,
        errors: str | None = None,
    ) -> subprocess.Popen[str]:
        joined = ' '.join(command)
        if 'GCF_1.40_GRCh38' in joined:
            # Exits 0 after 5s if not killed; SIGKILL from `abandon()` ends it immediately (<0).
            child = real_popen(
                [sys.executable, '-c', 'import time; time.sleep(5)'],
                stdout=stdout,
                stderr=stderr,
                text=text,
                errors=errors,
            )
            spawned.append(child)
            child_running.set()
            return child
        assert child_running.wait(timeout=10)
        raise RuntimeError('unexpected bug in grch37')

    monkeypatch.setattr(build.subprocess, 'Popen', popen)
    try:
        with pytest.raises(RuntimeError, match='unexpected bug in grch37'):
            build.build_all(pinned, bucket=gcs_bucket, dry_run=False)
        assert len(spawned) == 1
        assert spawned[0].returncode is not None
        assert spawned[0].returncode < 0
    finally:
        for child in spawned:
            child.kill()
            child.wait(timeout=5)


def test_an_error_no_build_absorbs_waits_out_uploads_already_begun(
    tmp_path: pathlib.Path,
    gcs_endpoint: str,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gcs_bucket.name is not None
    monkeypatch.setattr(build.tempfile, 'tempdir', str(tmp_path))
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    put_mirrored(pinned.assembly('ncbi-grch37').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    upload_started = threading.Event()
    abandon_fired = threading.Event()
    real_popen = subprocess.Popen
    real_abandon_call = build.Abandon.__call__

    def notify_abandon(abandon: build.Abandon) -> None:
        real_abandon_call(abandon)
        abandon_fired.set()

    def popen(
        command: Sequence[str],
        *,
        stdout: TextIO | None = None,
        stderr: int | None = None,
        text: bool = True,
        errors: str | None = None,
    ) -> subprocess.Popen[str]:
        if 'GCF_1.25_GRCh37' in ' '.join(command):
            assert upload_started.wait(timeout=10)
            raise RuntimeError('unexpected bug in grch37')
        return real_popen(command, stdout=stdout, stderr=stderr, text=text, errors=errors)

    def pause_first_upload(method: str, _path: str, body: bytes) -> int | None:
        if method == 'POST' and genome.prefix.encode() in body and not upload_started.is_set():
            upload_started.set()
            assert abandon_fired.wait(timeout=10)
        return None

    monkeypatch.setattr(build.Abandon, '__call__', notify_abandon)
    monkeypatch.setattr(build.subprocess, 'Popen', popen)
    with (
        _gcs_proxy(gcs_endpoint, gcs_bucket.name, on_request=pause_first_upload) as proxied_bucket,
        pytest.raises(RuntimeError, match='unexpected bug in grch37'),
    ):
        build.build_all(pinned, bucket=proxied_bucket, dry_run=False)

    assert build.exists(gcs_bucket, genome)


def test_a_stopped_pass_does_not_wait_out_the_uploads_begun(
    tmp_path: pathlib.Path,
    gcs_endpoint: str,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gcs_bucket.name is not None
    monkeypatch.setattr(build.tempfile, 'tempdir', str(tmp_path))
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    sigterm_sent = threading.Event()
    release_upload = threading.Event()
    work_cleaned = threading.Event()
    worker_threads: list[threading.Thread] = []
    real_rmtree = shutil.rmtree

    def tracked_rmtree(path: str | os.PathLike[str], *, ignore_errors: bool = False) -> None:
        real_rmtree(path, ignore_errors=ignore_errors)
        worker_threads.append(threading.current_thread())
        work_cleaned.set()

    def stop_on_upload(method: str, _path: str, body: bytes) -> int | None:
        if method == 'POST' and genome.prefix.encode() in body:
            if not sigterm_sent.is_set():
                sigterm_sent.set()
                os.kill(os.getpid(), signal.SIGTERM)
            release_upload.wait(timeout=10)
            return 403
        return None

    monkeypatch.setattr(build.shutil, 'rmtree', tracked_rmtree)
    with _gcs_proxy(gcs_endpoint, gcs_bucket.name, on_request=stop_on_upload) as proxied_bucket:
        try:
            with pytest.raises(job.Stopped), job.stoppable():
                build.build_all(pinned, bucket=proxied_bucket, dry_run=False)
            assert not work_cleaned.is_set()
            assert not build.exists(gcs_bucket, genome)
        finally:
            release_upload.set()
            assert work_cleaned.wait(timeout=10)
            for worker in worker_threads:
                worker.join(timeout=10)


def test_an_abandoned_pass_starts_no_more_builder_steps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_child(*_: object, **__: object) -> None:
        raise AssertionError('a builder step started after the pass was abandoned')

    monkeypatch.setattr(build.subprocess, 'Popen', no_child)
    abandon = build.Abandon()
    abandon()
    with pytest.raises(build.BuildError, match='abandoned'):
        build.Runner('some build', abandon).cli('genome', '--out', str(tmp_path))


def _mirror_refseq_inputs(pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored) -> None:
    assembly = pinned.assembly('ncbi-grch38')
    for path, entry in assembly.objects.items():
        put_mirrored(entry, path.encode())
    for table in pinned.tables.values():
        put_mirrored(table, table.id.encode())


def _status_table(bucket: storage.Bucket, pinned: manifest.Manifest, day: str, body: bytes) -> None:
    records = pinned.assembly('ncbi-grch38').object('historical/records')
    name = f'{status.prefix(pinned.dataset, records)}{day}.tsv'
    bucket.blob(name).upload_from_string(body)
    digest = mirror.object_digest(bucket.blob(name))
    assert digest is not None
    fetched_over = mirror.mirrored(records, dataset=pinned.dataset, bucket=bucket).provenance()
    provenance = {'records': fetched_over, 'digest': digest[0], 'digest_algorithm': digest[1]}
    bucket.blob(name + mirror.PROVENANCE_SUFFIX).upload_from_string(json.dumps(provenance))


def _refseq_store(pinned: manifest.Manifest, bucket: storage.Bucket) -> build.Build:
    (store,) = [
        p.build for p in build.plan(pinned, bucket=bucket, genomes=_no_genome_dir) if p.label == 'ncbi-grch38 store'
    ]
    assert store is not None
    return store


def test_the_status_table_a_historical_set_is_built_with_enters_the_build_id(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    _mirror_refseq_inputs(pinned, put_mirrored)
    _status_table(gcs_bucket, pinned, '2026-11-08', b'a')
    first = _refseq_store(pinned, gcs_bucket)
    assert str(first.inputs['historical/status'].provenance['object']).endswith('2026-11-08.tsv')
    records = pinned.assembly('ncbi-grch38').object('historical/records')
    for blob in gcs_bucket.list_blobs(prefix=status.prefix(pinned.dataset, records)):
        blob.delete()
    _status_table(gcs_bucket, pinned, '2026-12-08', b'b')
    assert _refseq_store(pinned, gcs_bucket).build_id != first.build_id


def test_a_store_with_a_historical_set_and_no_status_table_is_not_planned(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    _mirror_refseq_inputs(pinned, put_mirrored)
    (store,) = [
        p for p in build.plan(pinned, bucket=gcs_bucket, genomes=_no_genome_dir) if p.label == 'ncbi-grch38 store'
    ]
    assert store.build is None
    assert 'exactly one status table' in str(store.error)


def test_an_annotation_of_another_release_is_refused(tmp_path: pathlib.Path) -> None:
    annotation = tmp_path / 'annotation.gff3.gz'
    annotation.write_bytes(gzip.compress(b'##gff-version 3\n#!annotation-source NCBI RefSeq GCF_1.40-RS_2024_08\n'))
    with pytest.raises(build.BuildError, match=r'names release GCF_1\.40-RS_2024_08'):
        build.check_release(annotation, 'GCF_1.40-RS_2025_08')


def test_an_annotation_naming_no_release_is_refused(tmp_path: pathlib.Path) -> None:
    annotation = tmp_path / 'annotation.gff3.gz'
    annotation.write_bytes(gzip.compress(b'##gff-version 3\nNC_000001.11\tBestRefSeq\tgene\t1\t2\t.\t+\t.\tID=x\n'))
    with pytest.raises(build.BuildError, match='no `#!annotation-source'):
        build.check_release(annotation, 'GCF_1.40-RS_2025_08')


def test_the_committed_manifest_declares_what_every_planned_build_reads() -> None:
    # `plan` reads assemblies and objects by id; an id it names that the manifest lacks raises before any bucket read.
    pinned = manifest.load()
    for assembly_id, _ in build._REFSEQ:
        assembly = pinned.assembly(assembly_id)
        assert assembly.release is not None
        for path in ('genome', 'annotation', 'transcripts', 'proteins', 'records', 'alignments', 'alignments/index'):
            assembly.object(path)
    for assembly_id, _, ncbi_id in build._ENSEMBL:
        assembly = pinned.assembly(assembly_id)
        assert assembly.release is not None
        for path in ('whole-annotation', 'whole-annotation-gtf', 'transcripts', 'ncrna', 'proteins'):
            assembly.object(path)
        pinned.assembly(ncbi_id).object('genome')
    pinned.table('hgnc-complete-set')
    pinned.table('mane-summary')
