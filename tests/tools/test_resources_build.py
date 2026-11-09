"""Tests for tools.resources.build: build ids follow the inputs, builds land once, and nothing is overwritten."""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import pathlib
import subprocess
import sys
import threading
import time

import pytest
from google.cloud import storage
from weaver_data_provider import genome as genome_mod

from tests.tools import conftest
from tools.resources import build, manifest, mirror, status

_FASTA = gzip.compress(b'>NC_000001.11 Homo sapiens chromosome 1\nACGTACGTAC\nGTACGTACGT\n')


def _no_genome_dir(_: build.Build) -> pathlib.Path:
    raise AssertionError('no Ensembl store is run here')


def _genome(pinned: manifest.Manifest, bucket: storage.Bucket) -> build.Build:
    (genome,) = [
        p.build for p in build.plan(pinned, bucket=bucket, genomes=_no_genome_dir) if p.label == 'ncbi-grch38 genome'
    ]
    assert genome is not None
    return genome


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


def test_a_store_placed_against_a_genome_built_this_run_waits_for_it(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored, tmp_path: pathlib.Path
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)  # not built in the bucket, so only the settled directory can answer
    genomes = build._Genomes(gcs_bucket, tmp_path, build.Abandon())
    genomes.expect(genome)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(genomes, genome)
        try:
            with pytest.raises(concurrent.futures.TimeoutError):
                waiting.result(timeout=0.2)
            genomes.settle(genome, tmp_path / 'built')
            assert waiting.result(timeout=10) == tmp_path / 'built'
        finally:
            genomes.settle(genome, None)  # a waiter left blocked would hang the pool's exit


def test_a_store_placed_against_a_genome_built_earlier_downloads_it(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored, tmp_path: pathlib.Path
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    build.build_all(pinned, bucket=gcs_bucket, dry_run=False)
    genomes = build._Genomes(gcs_bucket, tmp_path, build.Abandon())
    genomes.expect(genome)
    genomes.settle(genome, None)
    directory = genomes(genome)
    assert (directory / genome_mod.CATALOGUE).exists()


def test_a_store_placed_against_a_genome_whose_build_failed_fails(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored, tmp_path: pathlib.Path
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    genomes = build._Genomes(gcs_bucket, tmp_path, build.Abandon())
    genomes.expect(genome)
    genomes.settle(genome, None)
    with pytest.raises(build.BuildError, match='is not built'):
        genomes(genome)


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


def test_a_kept_genome_whose_build_fails_is_settled_so_nothing_waits_for_it(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored, tmp_path: pathlib.Path
) -> None:
    put_mirrored(pinned.assembly('ncbi-grch38').object('genome'), _FASTA)
    genome = _genome(pinned, gcs_bucket)
    gcs_bucket.blob(f'{genome.prefix}/provenance.json').upload_from_string('{}')  # a build that stopped partway
    genomes = build._Genomes(gcs_bucket, tmp_path, build.Abandon())
    genomes.expect(genome)
    planned = build.Planned(genome.label, genome, keep=True)
    failed = build._make(
        planned, bucket=gcs_bucket, genomes=genomes, abandon=build.Abandon(), scratch=str(tmp_path), dry_run=False
    )
    assert failed
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        asked = pool.submit(genomes, genome)
        try:
            with pytest.raises(build.BuildError, match='stopped before writing'):
                asked.result(timeout=10)
        finally:
            genomes.settle(genome, None)  # a waiter left blocked would hang the pool's exit


def test_an_error_no_build_absorbs_abandons_the_pass_at_once_and_waits_out_the_uploads_begun(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[bool] = []

    def make(planned: build.Planned, *, abandon: build.Abandon, **_: object) -> bool:
        if planned.label == 'ncbi-grch38 genome':  # first in the plan: mid-upload when a later build raises
            abandoned_in_time = abandon._set.wait(timeout=10)
            time.sleep(0.2)  # the upload already begun, finishing
            seen.append(abandoned_in_time)
            return False
        raise RuntimeError(planned.label)

    monkeypatch.setattr(build, '_make', make)
    monkeypatch.setattr(build.tempfile, 'tempdir', str(tmp_path))
    with pytest.raises(RuntimeError):
        build.build_all(pinned, bucket=gcs_bucket, dry_run=True)
    assert seen == [True]


class _Stop(BaseException):
    """Stands in for the job's SIGTERM."""


def test_a_stopped_pass_does_not_wait_out_the_uploads_begun(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    reached, release = threading.Event(), threading.Event()
    seen: list[bool] = []

    def make(planned: build.Planned, *, abandon: build.Abandon, **_: object) -> bool:
        if planned.label == 'ncbi-grch38 genome':
            reached.set()
            release.wait(timeout=10)
            seen.append(abandon._set.is_set())
        return False

    def stop(*_: object, **__: object) -> None:
        reached.wait(timeout=10)
        raise _Stop

    monkeypatch.setattr(build, '_make', make)
    monkeypatch.setattr(build.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(build.concurrent.futures, 'wait', stop)
    try:
        with pytest.raises(_Stop):
            build.build_all(pinned, bucket=gcs_bucket, dry_run=True)
        assert seen == []  # returned with the upload still in progress
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while not seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen == [True]


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
