"""Tests for tools.resources.status: one dated, complete status table per historical release, never replaced."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess

import pytest
from google.cloud import storage

from tests.tools import conftest
from tools.resources import manifest, mirror, status

_TODAY = dt.date(2026, 11, 8)
_VERSIONS = ('NM_000059.3', 'NM_000546.5')


def _write_table(path: pathlib.Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ['accession_version\tstatus\treplaced_by', *('\t'.join(row) for row in rows)]
    path.write_text('\n'.join(lines) + '\n', 'ascii')


@pytest.fixture
def entrez_answers_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entrez stood in for at its one network edge: the command answers every version the records name."""

    def fetch(records: pathlib.Path, out: pathlib.Path) -> None:
        _write_table(out, [('NM_000059.3', 'replaced', 'NM_000059.4'), ('NM_000546.5', 'live', '')])
        status.check_complete(records, out)

    monkeypatch.setattr(status, '_fetch', fetch)


@pytest.fixture
def historical(pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored) -> manifest.Assembly:
    assembly = pinned.assembly('ncbi-grch38')
    put_mirrored(assembly.object('historical/records'), conftest.gbff(*_VERSIONS))
    return assembly


@pytest.mark.usefixtures('entrez_answers_everything')
def test_a_fetched_table_is_stored_dated_with_its_digest_and_found_again(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, historical: manifest.Assembly
) -> None:
    fetched = status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)
    assert fetched is status.Status.FETCHED
    table, _ = status.the_table(historical, dataset=pinned.dataset, bucket=gcs_bucket)
    assert table.name.rsplit('/', 1)[-1].startswith('2026-11-08-')
    fetched_over = table.provenance['records']
    assert isinstance(fetched_over, dict)
    assert fetched_over['url'] == historical.object('historical/records').url
    assert (table.provenance['digest'], 'md5') == mirror.object_digest(gcs_bucket.blob(table.name))


@pytest.mark.usefixtures('entrez_answers_everything')
def test_a_release_with_a_table_is_not_fetched_again(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, historical: manifest.Assembly
) -> None:
    status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)
    later = _TODAY + dt.timedelta(days=30)
    again = status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=later, dry_run=False)
    assert again is status.Status.SKIPPED


def test_a_table_missing_a_version_is_refused(tmp_path: pathlib.Path) -> None:
    records = tmp_path / 'records.gbff.gz'
    records.write_bytes(conftest.gbff(*_VERSIONS))
    table = tmp_path / 'status.tsv'
    _write_table(table, [('NM_000059.3', 'replaced', 'NM_000059.4')])
    with pytest.raises(status.StatusError, match=r'1 versions: \[.NM_000546.5.\]'):
        status.check_complete(records, table)


def test_nothing_is_stored_when_entrez_leaves_a_version_unanswered(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    historical: manifest.Assembly,
) -> None:
    def fetch(records: pathlib.Path, out: pathlib.Path) -> None:
        _write_table(out, [('NM_000059.3', 'replaced', 'NM_000059.4')])
        status.check_complete(records, out)

    monkeypatch.setattr(status, '_fetch', fetch)
    with pytest.raises(status.StatusError, match='answered nothing'):
        status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)
    assert not list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/ncbi/GCF_1.40-RS_2024_08_historical/status/'))


def test_a_table_without_provenance_stops_the_run(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, historical: manifest.Assembly
) -> None:
    under = status.prefix(pinned.dataset, historical.object('historical/records'))
    gcs_bucket.blob(f'{under}2026-01-01.tsv').upload_from_string('accession_version\tstatus\treplaced_by\n')
    with pytest.raises(status.StatusError, match='did not finish'):
        status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)


def test_the_build_refuses_a_choice_between_two_tables(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, historical: manifest.Assembly
) -> None:
    under = status.prefix(pinned.dataset, historical.object('historical/records'))
    for day in ('2026-01-01', '2026-02-01'):
        gcs_bucket.blob(f'{under}{day}.tsv').upload_from_string('x')
        gcs_bucket.blob(f'{under}{day}.tsv{mirror.PROVENANCE_SUFFIX}').upload_from_string(json.dumps({}))
    with pytest.raises(status.StatusError, match='exactly one'):
        status.the_table(historical, dataset=pinned.dataset, bucket=gcs_bucket)


def test_records_not_mirrored_are_refused(gcs_bucket: storage.Bucket, pinned: manifest.Manifest) -> None:
    with pytest.raises(mirror.MirrorError, match='not mirrored'):
        status.mirror_status(
            pinned.assembly('ncbi-grch38'), dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False
        )


def test_a_table_the_command_reports_whole_is_still_refused_when_it_misses_a_version(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    historical: manifest.Assembly,
) -> None:
    # Entrez stood in for at the subprocess: the command exits 0 having written a table short of a version.
    def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[bytes]:
        del check
        _write_table(pathlib.Path(command[command.index('--out') + 1]), [('NM_000059.3', 'replaced', 'NM_000059.4')])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(status.StatusError, match='answered nothing'):
        status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)


@pytest.mark.usefixtures('entrez_answers_everything')
def test_records_mirrored_again_with_other_bytes_need_a_table_of_their_own(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    historical: manifest.Assembly,
    put_mirrored: conftest.PutMirrored,
) -> None:
    status.mirror_status(historical, dataset=pinned.dataset, bucket=gcs_bucket, today=_TODAY, dry_run=False)
    put_mirrored(historical.object('historical/records'), conftest.gbff(*_VERSIONS, 'NM_000546.6'))
    with pytest.raises(status.StatusError, match='exactly one status table fetched over its records'):
        status.the_table(historical, dataset=pinned.dataset, bucket=gcs_bucket)
