"""Mirror the Entrez status table of each pinned historical set, once per release.

A retired RefSeq version's status — replaced or suppressed, and by what — is stated by no file NCBI
publishes; hgvs-weaver-data's ``weaver-data-build status`` asks Entrez for it, version by version,
over the set's GenBank records, and the ``historical`` build reads the table it writes. Entrez is a
live answer, so the table cannot be pinned by URL the way the mirror pins a file. It is fetched once
for each historical release instead, over the mirrored records, and kept as a dated object with a
provenance sidecar recording its digest and what it was computed from::

    reference/<source>/<historical version>/status/<YYYY-MM-DD>-<records digest, 12 hex>.tsv
    reference/<source>/<historical version>/status/<YYYY-MM-DD>-<records digest, 12 hex>.tsv.provenance.json

From then on the build reads that object, so a store built from a historical set names, in its
inputs' provenance, exactly the table it was built with. A release with a table fetched over its
records as mirrored now is skipped. A version Entrez answers nothing for fails the run, and no
table is written: the build would refuse the set anyway.

Run from the repo root, after the mirror (the records it reads must be mirrored)::

    uv run --group resources python -m tools.resources.status --project P [--dry-run]

``NCBI_API_KEY``, when set, raises Entrez's rate limit from three requests a second to ten.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import enum
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

from google.cloud import storage
from weaver_data_provider import build
from weaver_data_provider.build import entrez

from tools.resources import manifest, mirror

_STATUS_DIRECTORY = 'status'
_SUFFIX = '.tsv'


class StatusError(Exception):
    """The status table could not be fetched whole, or what is stored is not a table the build can use."""


class Status(enum.Enum):
    SKIPPED = 'skip'
    FETCHED = 'fetched'
    WOULD_FETCH = 'would fetch'


@dataclasses.dataclass(frozen=True)
class Table:
    """One stored status table: its object name and the provenance its sidecar records."""

    name: str
    provenance: dict[str, object]


def prefix(dataset: str, records: manifest.Entry) -> str:
    """Where the status tables computed over `records` live: beside the records, under `status/`."""
    return f'{dataset}/{records.source}/{records.version}/{_STATUS_DIRECTORY}/'


def stored_tables(bucket: storage.Bucket, under: str) -> list[Table]:
    """Every complete status table under `under`, oldest first.

    A table is complete once its sidecar exists, which is written after it.

    Raises:
        StatusError: If a table has no sidecar: a fetch that stopped between the two writes, which
            nothing here overwrites, so it is for a person to delete.
    """
    names = {blob.name for blob in bucket.list_blobs(prefix=under)}
    tables = sorted(name for name in names if name.endswith(_SUFFIX))
    if orphans := [name for name in tables if name + mirror.PROVENANCE_SUFFIX not in names]:
        raise StatusError(f'status tables without provenance, from a fetch that did not finish: {orphans}')
    return [
        Table(name=name, provenance=json.loads(bucket.blob(name + mirror.PROVENANCE_SUFFIX).download_as_bytes()))
        for name in tables
    ]


def _fetched_over(table: Table, records: mirror.Mirrored) -> bool:
    """Whether `table` was fetched over exactly these records: the same URL and the same bytes."""
    over = table.provenance.get('records')
    return isinstance(over, dict) and (over.get('url'), over.get('digest'), over.get('digest_algorithm')) == (
        records.entry.url,
        records.digest,
        records.digest_algorithm,
    )


def the_table(assembly: manifest.Assembly, *, dataset: str, bucket: storage.Bucket) -> tuple[Table, int]:
    """The one status table fetched over the set's records as mirrored now, checked against its digest.

    A table fetched over other bytes — records re-mirrored after the publisher rewrote them — is not
    one: its statuses answer for versions the records may no longer hold.

    Returns:
        The table and the generation its object was checked at.

    Raises:
        StatusError: If no table was fetched over these records, or more than one: which to build
            from is then a person's call, not this pass's.
        mirror.MirrorError: If the records are not mirrored as the manifest pins them.
    """
    records = mirror.mirrored(assembly.object('historical/records'), dataset=dataset, bucket=bucket)
    tables = [t for t in stored_tables(bucket, prefix(dataset, records.entry)) if _fetched_over(t, records)]
    if len(tables) != 1:
        raise StatusError(
            f'{assembly.id}: a historical set is built from exactly one status table fetched over its records '
            f'({records.digest_algorithm} {records.digest}); found {[t.name for t in tables]}'
        )
    (table,) = tables
    blob = bucket.blob(table.name)
    if mirror.object_digest(blob) != (table.provenance.get('digest'), table.provenance.get('digest_algorithm')):
        raise StatusError(f'{table.name} no longer has the digest its provenance records')
    if blob.generation is None:
        raise StatusError(f'{table.name}: GCS reported no generation')
    return table, blob.generation


def _fetch(records: pathlib.Path, out: pathlib.Path) -> None:
    """Run ``weaver-data-build status`` over `records` into `out`.

    The command exits non-zero when Entrez answers nothing for a version; the table is then checked
    here as well, so a table missing a version cannot be stored whatever the command reported.

    Raises:
        StatusError: If the command failed or the table it wrote misses a version the records name.
    """
    result = subprocess.run(  # noqa: S603 - this interpreter, the package's own CLI, paths of our own
        [
            sys.executable,
            '-m',
            'weaver_data_provider.build.cli',
            'status',
            '--records',
            str(records),
            '--out',
            str(out),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise StatusError(f'weaver-data-build status exited {result.returncode}; see its output above')
    check_complete(records, out)


def check_complete(records: pathlib.Path, table: pathlib.Path) -> None:
    """Raise unless `table` has a row for every version `records` names.

    Raises:
        StatusError: Naming the versions Entrez answered nothing for, the first twenty of them.
    """
    if missing := set(entrez.versions_in(records)) - entrez.read_table(table).keys():
        raise StatusError(f'Entrez answered nothing for {len(missing)} versions: {sorted(missing)[:20]}')


def _provenance(records: mirror.Mirrored, table: pathlib.Path, *, fetched_at: dt.datetime) -> bytes:
    rows = entrez.read_table(table)
    record = {
        'records': {'entry': records.entry.id, **records.provenance()},
        'digest': hashlib.md5(table.read_bytes()).hexdigest(),  # noqa: S324 - an integrity digest, as GCS records it
        'digest_algorithm': 'md5',
        'versions': len(rows),
        'summary': entrez.summary(table),
        'fetched_by': build.BUILDER,
        'fetched_at': fetched_at.isoformat(),
    }
    return json.dumps(record, indent=1, sort_keys=True).encode()


def mirror_status(
    assembly: manifest.Assembly, *, dataset: str, bucket: storage.Bucket, today: dt.date, dry_run: bool
) -> Status:
    """Fetch and store the status table of `assembly`'s historical set, unless one was fetched over its records.

    Raises:
        StatusError: If a stored table is incomplete, or Entrez did not answer for every version.
        mirror.MirrorError: If the set's records are not mirrored as the manifest pins them.
    """
    records = mirror.mirrored(assembly.object('historical/records'), dataset=dataset, bucket=bucket)
    under = prefix(dataset, records.entry)
    if any(_fetched_over(table, records) for table in stored_tables(bucket, under)):
        return Status.SKIPPED
    if dry_run:
        return Status.WOULD_FETCH
    # The records' digest in the name, so records mirrored again the same day get a table of their own.
    name = f'{under}{today.isoformat()}-{records.digest[:12]}{_SUFFIX}'
    with tempfile.TemporaryDirectory() as work:
        local_records = pathlib.Path(work) / f'records.{records.entry.suffix}'
        records.download(bucket, local_records)
        table = pathlib.Path(work) / 'status.tsv'
        _fetch(local_records, table)
        sidecar = _provenance(records, table, fetched_at=dt.datetime.now(dt.UTC))
        # Generation 0: refused if the object exists, so a table is never replaced, even by one fetched the same day.
        bucket.blob(name).upload_from_filename(str(table), checksum='md5', if_generation_match=0)
    bucket.blob(name + mirror.PROVENANCE_SUFFIX).upload_from_string(
        sidecar, content_type='application/json', if_generation_match=0
    )
    return Status.FETCHED


def mirror_all(pinned: manifest.Manifest, *, bucket: storage.Bucket, today: dt.date, dry_run: bool) -> list[str]:
    """Mirror the status table of every assembly that nests a historical set; return the ids that failed."""
    failed: list[str] = []
    for assembly in pinned.assemblies.values():
        if assembly.historical is None:
            continue
        try:
            outcome = mirror_status(assembly, dataset=pinned.dataset, bucket=bucket, today=today, dry_run=dry_run)
            print(f'  {outcome.value:<12}  {assembly.id}/historical/status', file=sys.stderr)
        except (StatusError, mirror.MirrorError, *mirror.TRANSFER_ERRORS) as error:
            failed.append(f'{assembly.id}/historical/status')
            print(f'  FAILED        {assembly.id}/historical/status: {error}', file=sys.stderr)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project owning the resources bucket')
    parser.add_argument('--dry-run', action='store_true', help='report what would be fetched, write nothing')
    args = parser.parse_args()
    client = storage.Client(project=args.project)
    bucket = mirror.resources_bucket(client, args.project)
    if failed := mirror_all(manifest.load(), bucket=bucket, today=dt.datetime.now(dt.UTC).date(), dry_run=args.dry_run):
        print(f'{len(failed)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
