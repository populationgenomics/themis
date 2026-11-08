"""Mirror the pinned upstream reference data into the resources bucket, byte for byte.

``resources/reference.toml`` pins every upstream object by URL; this copies each into the
``reference/`` dataset of ``<project>-resources`` unchanged, and writes a provenance sidecar
beside it. Nothing is transformed — a bgzip recompression, an index, a subset is a derived
artifact for a later pass, written to its own prefix and citing the entry id it came from.

Run from the repo root, which ``python -m`` needs on ``sys.path`` to import ``tools``::

    uv run --group resources python -m tools.resources.mirror --project P [--dry-run]

Auth is the caller's own ``gcloud`` ADC.

What a re-run guarantees, precisely, because the sidecar is read as evidence later. An entry is
skipped only when the mirrored object is still there *and* its checksum — read back from GCS, not
taken from the sidecar's own claim — matches what provenance recorded for this URL. So corruption
or replacement on the bucket side is caught on the next run, cheaply and without re-downloading.

Caught, not repaired: the mirror never replaces an object, so what a reader was pinned to cannot
change under it. Anything at a destination the run cannot vouch for — a corrupted object, one
mirrored from a URL the manifest no longer pins, an object whose sidecar never landed because a run
stopped between the two writes — fails that entry, naming what to delete. The one exception is the
last case with a publisher digest: an object without a sidecar whose md5 is the publisher's is
adopted, its sidecar written, since its bytes are already proven.
What that does **not** catch is an upstream mutating a file it already versioned: detecting that
needs the bytes again, and a re-run does not fetch them. For an entry carrying the publisher's own
digest the recorded value is that digest, so the mirror is pinned to the publisher's truth; for an
entry without one the recorded value is whatever first arrived, and is trusted from then on.

Two transfer paths, because most of the bytes need neither downloading nor hashing. A ``gs://``
upstream is copied server-side, so the ~4 GB already published to GCS never touches this machine,
and GCS's own rewrite checksums cover the copy. An ``https://`` upstream is streamed to a
temporary file while being hashed, verified, then uploaded.

The gene-disease refresh job's object store is deliberately not reused: it moves whole objects as
``bytes``, which is right for its dumps and wrong for a 900 MB assembly.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as dt
import enum
import hashlib
import json
import pathlib
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator

import google.resumable_media
import requests
from google.api_core import exceptions as google_exceptions
from google.cloud import storage

from tools.resources import manifest

_BUCKET_SUFFIX = 'resources'
PROVENANCE_SUFFIX = '.provenance.json'
_CHUNK = 8 * 1024 * 1024
# A rewrite of a multi-gigabyte object returns a continuation token rather than completing, so the
# copy loops until GCS reports it done.
_REWRITE_ATTEMPTS = 10_000


# What a transfer to, from or within GCS raises when it does not complete: a refused request (a write to a name
# that exists, under a grant that cannot replace one), a download whose bytes fail their checksum, an upstream
# fetch that failed. Each fails the one item it was for.
TRANSFER_ERRORS = (google_exceptions.GoogleAPIError, google.resumable_media.DataCorruption, requests.RequestException)


class MirrorError(Exception):
    """An upstream did not match what the manifest pinned, or a transfer did not complete."""


class Status(enum.Enum):
    """What a mirror call did, kept apart from how it is reported."""

    SKIPPED = 'skip'
    MIRRORED = 'mirrored'
    ADOPTED = 'adopted'
    WOULD_MIRROR = 'would mirror'


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One entry's result."""

    entry: manifest.Entry
    status: Status
    size: int = 0


def _hex(encoded: str | None) -> str | None:
    """A GCS checksum — base64 in the API — as the hex every other digest here is written in."""
    return base64.b64decode(encoded).hex() if encoded else None


def object_digest(blob: storage.Blob) -> tuple[str, str] | None:
    """The stored object's checksum and which algorithm it is, or ``None`` if it is not there.

    GCS omits `md5Hash` for an object it composed, so crc32c is the fallback rather than an empty
    md5: a recorded digest has to name what it is, or a later comparison silently passes.
    """
    blob.reload()
    if digest := _hex(blob.md5_hash):
        return digest, 'md5'
    if digest := _hex(blob.crc32c):
        return digest, 'crc32c'
    return None


def _stored_provenance(bucket: storage.Bucket, name: str) -> dict[str, object] | None:
    blob = bucket.blob(name + PROVENANCE_SUFFIX)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _up_to_date(entry: manifest.Entry, stored: dict[str, object] | None, destination: storage.Blob) -> bool:
    """Whether the mirrored object still is what provenance says was put there for this URL."""
    if stored is None or stored.get('url') != entry.url:
        return False
    if entry.md5 is not None and stored.get('digest') != entry.md5:
        return False
    if not destination.exists():
        return False
    actual = object_digest(destination)
    return actual is not None and (stored.get('digest'), stored.get('digest_algorithm')) == actual


@dataclasses.dataclass(frozen=True)
class Mirrored:
    """A mirrored object, as its provenance records it and as checked against what GCS holds.

    `generation` pins the object read: a download at that generation fails rather than returning
    bytes that replaced the ones checked.
    """

    entry: manifest.Entry
    name: str
    digest: str
    digest_algorithm: str
    generation: int

    def provenance(self) -> dict[str, str]:
        """What identifies the bytes: where they came from and their digest, the algorithm named."""
        return {'url': self.entry.url, 'digest': self.digest, 'digest_algorithm': self.digest_algorithm}

    def download(self, bucket: storage.Bucket, path: pathlib.Path) -> None:
        """The object's bytes, written to `path` and checked against its digest in transit.

        Raises:
            google.api_core.exceptions.PreconditionFailed: If the object was replaced since it was checked.
            google.resumable_media.DataCorruption: If the bytes that arrived do not match the digest.
        """
        bucket.blob(self.name).download_to_filename(
            str(path), checksum=self.digest_algorithm, if_generation_match=self.generation
        )


def mirrored(entry: manifest.Entry, *, dataset: str, bucket: storage.Bucket) -> Mirrored:
    """The mirrored object for `entry`, checked to be what the manifest pins and what provenance records.

    Raises:
        MirrorError: If the entry is not mirrored, was mirrored from another URL or against another
            pinned digest, or the stored object no longer has the digest its provenance records.
    """
    name = f'{dataset}/{entry.object}'
    stored = _stored_provenance(bucket, name)
    if stored is None:
        raise MirrorError(f'{entry.id}: not mirrored; no provenance at {name}{PROVENANCE_SUFFIX}')
    if stored.get('url') != entry.url:
        raise MirrorError(f'{entry.id}: mirrored from {stored.get("url")}, but the manifest pins {entry.url}')
    if entry.md5 is not None and stored.get('digest') != entry.md5:
        raise MirrorError(f'{entry.id}: provenance records {stored.get("digest")}, the manifest pins md5 {entry.md5}')
    blob = bucket.blob(name)
    if not blob.exists():
        raise MirrorError(f'{entry.id}: provenance exists but the object {name} does not')
    actual = object_digest(blob)
    recorded = (str(stored.get('digest')), str(stored.get('digest_algorithm')))
    if actual != recorded or blob.generation is None:
        raise MirrorError(f'{entry.id}: {name} has digest {actual}, its provenance records {recorded}')
    return Mirrored(
        entry=entry, name=name, digest=recorded[0], digest_algorithm=recorded[1], generation=blob.generation
    )


@contextlib.contextmanager
def _downloaded(url: str) -> Iterator[tuple[pathlib.Path, str, int]]:
    """The upstream streamed to a temporary file, with its md5 and size.

    Transport compression is declined, because a body the server gzips would be stored decoded and
    silently stop being the bytes the publisher serves.
    """
    digest = hashlib.md5()  # noqa: S324 - matching the publisher's own checksum, not a security use
    size = 0
    with tempfile.TemporaryDirectory() as work:
        path = pathlib.Path(work) / 'payload'
        with requests.get(url, stream=True, timeout=(30, 600), headers={'Accept-Encoding': 'identity'}) as response:
            response.raise_for_status()
            with path.open('wb') as handle:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
        yield path, digest.hexdigest(), size


def _copy_within_gcs(client: storage.Client, entry: manifest.Entry, destination: storage.Blob) -> None:
    """Server-side copy of a ``gs://`` upstream; the bytes never reach this machine."""
    parsed = urllib.parse.urlparse(entry.url)
    source = client.bucket(parsed.netloc).blob(parsed.path.lstrip('/'))
    token = None
    for _ in range(_REWRITE_ATTEMPTS):
        token, _, _ = destination.rewrite(source, token=token, if_generation_match=0)
        if token is None:
            return
    raise MirrorError(f'{entry.id}: rewrite did not complete')


def _provenance(entry: manifest.Entry, *, digest: str, algorithm: str, size: int) -> bytes:
    record = {
        'entry': entry.id,
        'url': entry.url,
        'digest': digest,
        'digest_algorithm': algorithm,
        # Whether the digest is the publisher's own assertion or merely what first arrived. Only
        # the former pins the mirror to the upstream; the latter pins it to itself.
        'digest_source': 'publisher' if entry.md5 else 'first-fetch',
        'size': size,
        'licence': entry.licence,
        'mirrored_at': dt.datetime.now(dt.UTC).isoformat(),
    }
    return json.dumps(record, indent=1, sort_keys=True).encode()


def mirror(
    entry: manifest.Entry, *, dataset: str, bucket: storage.Bucket, client: storage.Client, dry_run: bool
) -> Outcome:
    """Mirror one entry.

    Raises:
        MirrorError: If the upstream's digest is not the one the manifest pinned, or a copy did
            not complete.
    """
    name = f'{dataset}/{entry.object}'
    destination = bucket.blob(name)
    stored = _stored_provenance(bucket, name)
    if _up_to_date(entry, stored, destination):
        return Outcome(entry, Status.SKIPPED)
    adoptable = stored is None and entry.md5 is not None and destination.exists()
    if adoptable and object_digest(destination) == (entry.md5, 'md5'):
        if not dry_run:
            _write_provenance(bucket, name, entry, digest=str(entry.md5), algorithm='md5', size=destination.size or 0)
        return Outcome(entry, Status.ADOPTED, destination.size or 0)
    if stored is not None or destination.exists():
        raise MirrorError(
            f'{entry.id}: gs://{bucket.name}/{name} or its sidecar is there but is not what the manifest pins, and the '
            f'mirror never replaces an object; delete both to mirror it again'
        )
    if dry_run:
        return Outcome(entry, Status.WOULD_MIRROR)

    if entry.is_gcs:
        _copy_within_gcs(client, entry, destination)
        stored = object_digest(destination)
        if stored is None:
            raise MirrorError(f'{entry.id}: GCS reported no checksum for the copy, so nothing can be pinned')
        digest, algorithm = stored
        size = destination.size or 0
    else:
        with _downloaded(entry.url) as (path, digest, size):
            if entry.md5 is not None and digest != entry.md5:
                raise MirrorError(f'{entry.id}: expected md5 {entry.md5}, upstream served {digest}')
            # `checksum='md5'` makes GCS reject the object server-side when the bytes that arrived
            # are not the bytes that were sent; the default verifies nothing, so a truncated upload
            # would land and get a sidecar asserting a digest the stored object does not have.
            destination.upload_from_filename(str(path), checksum='md5', if_generation_match=0)
        algorithm = 'md5'
    _write_provenance(bucket, name, entry, digest=digest, algorithm=algorithm, size=size)
    return Outcome(entry, Status.MIRRORED, size)


def _write_provenance(
    bucket: storage.Bucket, name: str, entry: manifest.Entry, *, digest: str, algorithm: str, size: int
) -> None:
    bucket.blob(name + PROVENANCE_SUFFIX).upload_from_string(
        _provenance(entry, digest=digest, algorithm=algorithm, size=size),
        content_type='application/json',
        if_generation_match=0,
    )


def _report(outcome: Outcome) -> str:
    if outcome.status in (Status.MIRRORED, Status.ADOPTED):
        return f'  {outcome.status.value:<12}  {outcome.entry.id}  {outcome.size / 1e6:.1f} MB'
    return f'  {outcome.status.value:<12}  {outcome.entry.id}'


def resources_bucket(client: storage.Client, project: str) -> storage.Bucket:
    """The project's resources bucket, the one the mirror and everything derived from it live in."""
    return client.bucket(f'{project}-{_BUCKET_SUFFIX}')


def mirror_all(
    entries: list[manifest.Entry], *, dataset: str, bucket: storage.Bucket, client: storage.Client, dry_run: bool
) -> list[str]:
    """Mirror every entry, reporting each on stderr, and return the ids of those that failed.

    One entry's failure does not abandon the rest: a transfer of this size is worth resuming from
    what landed, and the caller still learns that something did not.
    """
    print(f'{len(entries)} entries -> gs://{bucket.name}/{dataset}/', file=sys.stderr)
    failed: list[str] = []
    for entry in entries:
        try:
            print(
                _report(mirror(entry, dataset=dataset, bucket=bucket, client=client, dry_run=dry_run)), file=sys.stderr
            )
        except (MirrorError, *TRANSFER_ERRORS) as error:
            failed.append(entry.id)
            print(f'  FAILED        {entry.id}: {error}', file=sys.stderr)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project owning the resources bucket')
    parser.add_argument('--dry-run', action='store_true', help='report what would transfer, write nothing')
    parser.add_argument(
        '--only',
        action='append',
        metavar='ID',
        help='an assembly or table id (`ncbi-grch38`), or one object within it (`ncbi-grch38/genome`)',
    )
    args = parser.parse_args()

    pinned = manifest.load()
    entries = pinned.entries
    if args.only:
        wanted = set(args.only)
        chosen = [entry for entry in entries if entry.id in wanted or entry.id.split('/')[0] in wanted]
        if unknown := wanted - {entry.id for entry in chosen} - {entry.id.split('/')[0] for entry in chosen}:
            parser.error(f'no such assembly, table or object: {sorted(unknown)}')
        entries = chosen

    client = storage.Client(project=args.project)
    bucket = resources_bucket(client, args.project)
    failed = mirror_all(entries, dataset=pinned.dataset, bucket=bucket, client=client, dry_run=args.dry_run)
    if failed:
        print(f'{len(failed)} of {len(entries)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
