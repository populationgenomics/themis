"""Mirror the pinned upstream reference data into the resources bucket, byte for byte.

``resources/reference.toml`` pins every upstream object by URL; this copies each into the
``reference/`` dataset of ``<project>-resources`` unchanged, and writes a provenance sidecar
beside it. Nothing is transformed — a bgzip recompression, an index, a subset is a derived
artifact for a later pass, written to its own prefix and citing the entry id it came from.

Run from the repo root, which ``python -m`` needs on ``sys.path`` to import ``tools``::

    uv run --group resources python -m tools.resources.mirror [--project P] [--dry-run]

Auth is the caller's own ``gcloud`` ADC.

What a re-run guarantees, precisely, because the sidecar is read as evidence later. An entry is
skipped only when the mirrored object is still there *and* its checksum — read back from GCS, not
taken from the sidecar's own claim — matches what provenance recorded for this URL. So corruption
or replacement on the bucket side is caught on the next run, cheaply and without re-downloading.
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
import datetime
import enum
import hashlib
import json
import pathlib
import re
import sys
import tempfile
import tomllib
import urllib.parse
from collections.abc import Iterator

import requests
from google.cloud import storage

_MANIFEST = pathlib.Path(__file__).resolve().parents[2] / 'resources' / 'reference.toml'
_DEFAULT_PROJECT = 'cpg-themis-dev'
_BUCKET_SUFFIX = 'resources'
_PROVENANCE_SUFFIX = '.provenance.json'
_CHUNK = 8 * 1024 * 1024
# A rewrite of a multi-gigabyte object returns a continuation token rather than completing, so the
# copy loops until GCS reports it done.
_REWRITE_ATTEMPTS = 10_000
_MD5_HEX = re.compile(r'^[0-9a-f]{32}$')


class MirrorError(Exception):
    """The manifest is inconsistent, or an upstream did not match what it pinned."""


# The filename each role takes, and the suffixes it admits. An index is absent: it never declares a
# suffix, only the extension it adds to its genome's, so there is no combination to admit or refuse.
_ROLE_SUFFIXES: dict[str, frozenset[str]] = {
    # `tar.gz` is a publisher's archive of a genome, mirrored as it ships; the derived pass extracts
    # `genome.fa` beside it, against the digest the archive carries internally.
    'genome': frozenset({'fa', 'fa.gz', 'tar.gz'}),
    'annotation': frozenset({'gff3.gz'}),
    'report': frozenset({'tsv'}),
    'summary': frozenset({'tsv.gz'}),
    'complete-set': frozenset({'tsv'}),
}
_ASSEMBLY_FIELDS = frozenset(
    {'id', 'source', 'version', 'seqid', 'licence', 'consumer', 'genome', 'annotation', 'report', 'index'}
)
_TABLE_FIELDS = frozenset({'id', 'source', 'version', 'role', 'suffix', 'url', 'md5', 'licence', 'consumer'})
# What a spec nested inside an assembly may carry. `extension` is the index's alone — it takes its
# genome's suffix and adds to it rather than declaring one.
_OBJECT_FIELDS = frozenset({'url', 'suffix', 'md5', 'extension'})


@dataclasses.dataclass(frozen=True)
class Entry:
    """One pinned upstream object and where it lands.

    Flattened out of the manifest rather than written there: an assembly declares its genome,
    annotation and index together, and this is what the mirror walks. The destination is composed
    from `source`, `version` and `role`, so every release of every source lands in the same shape
    and a bump is additive — two versions coexist under one source, and nothing is renamed.
    """

    id: str
    url: str
    source: str
    version: str
    role: str
    suffix: str
    md5: str | None
    licence: str
    consumer: str

    @property
    def is_gcs(self) -> bool:
        return self.url.startswith('gs://')

    @property
    def object(self) -> str:
        """Where this lands under the dataset prefix: ``<source>/<version>/<role>.<suffix>``.

        An index composes as its genome's name plus an extension, which is how htslib finds it —
        it opens `genome.fa.fai`, and reports anything else as a missing index rather than a
        misnamed one.
        """
        return f'{self.source}/{self.version}/{self.role}.{self.suffix}'


class Status(enum.Enum):
    """What a mirror call did, kept apart from how it is reported."""

    SKIPPED = 'skip'
    MIRRORED = 'mirrored'
    WOULD_MIRROR = 'would mirror'


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One entry's result."""

    entry: Entry
    status: Status
    size: int = 0


def _require(item: dict[str, object], fields: frozenset[str], *, named: str, source: str) -> None:
    """Raise unless `item` carries exactly the fields it may.

    An unknown key is almost always a typo in an optional one, which would otherwise read as absent
    and drop the object out of whatever that key feeds.
    """
    if missing := {'id', 'source', 'version', 'licence', 'consumer'} - item.keys():
        raise MirrorError(f'{source}: {named} omits {sorted(missing)}')
    if unknown := item.keys() - fields:
        raise MirrorError(f'{source}: {named} carries unknown {sorted(unknown)}')


def _object(
    spec: dict[str, object], *, role: str, suffix: str, parent: dict[str, object], source: str, named: str
) -> Entry:
    """One declared object as a flat entry, inheriting what its parent declares once.

    A nested spec gets the same unknown-key refusal a table gets, and for the same reason:
    `md5sum = "…"` on a genome would otherwise read as absent, silently dropping the publisher
    pinning the sidecar then claims for it.
    """
    allowed = _OBJECT_FIELDS if spec is not parent else _TABLE_FIELDS
    if unknown := spec.keys() - allowed:
        raise MirrorError(f'{source}: {named} carries unknown {sorted(unknown)}')
    url = spec.get('url')
    if not isinstance(url, str) or not url.startswith(('https://', 'gs://')):
        raise MirrorError(f'{source}: {named} has url {url!r}; only https:// and gs:// are fetched')
    md5 = spec.get('md5')
    if md5 is not None:
        if not isinstance(md5, str) or not _MD5_HEX.match(md5):
            raise MirrorError(f'{source}: {named} has md5 {md5!r}, which is not 32 hex digits')
        if url.startswith('gs://'):
            # Never compared: GCS reports base64 and a publisher reports hex, and a server-side copy
            # is checksummed by GCS anyway. Accepting it would leave the object re-copying every
            # run, which on a 3 GB assembly is not a small mistake.
            raise MirrorError(f'{source}: {named} is a gs:// upstream, whose checksum GCS supplies; drop its md5')
    return Entry(
        id=named,
        url=url,
        source=str(parent['source']),
        version=str(parent['version']),
        role=role,
        suffix=suffix,
        md5=md5,
        licence=str(parent['licence']),
        consumer=str(parent['consumer']),
    )


def _declared_suffix(spec: dict[str, object], *, role: str, source: str, named: str) -> str:
    """The suffix an object declares, checked against what its role admits.

    Only for a declared one: an index's suffix is composed from its genome's, which has been
    checked already, so there is nothing left to admit or refuse.
    """
    suffix = spec.get('suffix')
    if not isinstance(suffix, str) or suffix not in _ROLE_SUFFIXES[role]:
        raise MirrorError(
            f'{source}: {named} is a {role} with suffix {suffix!r}; allowed are {sorted(_ROLE_SUFFIXES[role])}'
        )
    return suffix


def _spec(item: dict[str, object], key: str, *, named: str, source: str) -> dict[str, object] | None:
    """The table an assembly declares under `key`, or `None` when it declares none.

    A present key whose value is not a table raises rather than reading as absent. `[[assembly.
    index]]` for `[assembly.index]` is array-of-tables by muscle memory — valid TOML, and a list —
    and dropping it silently is the failure the nesting exists to prevent: the index never mirrors,
    the run reports clean, and htslib later calls the index missing rather than misnamed.
    """
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MirrorError(
            f'{source}: {named} declares {key} as {type(value).__name__}, not a table — '
            f'`[[{named}.{key}]]` where `[{named}.{key}]` was meant?'
        )
    return value


def _assembly_entries(item: dict[str, object], *, source: str) -> Iterator[Entry]:
    """The genome, annotation, report and index one assembly declares.

    The index takes its genome's suffix plus the extension it adds, so the two cannot disagree:
    htslib opens `genome.fa.fai` and reports anything else as a missing index, not a misnamed one.
    """
    named = str(item['id'])
    _require(item, _ASSEMBLY_FIELDS, named=named, source=source)
    if not item.get('seqid'):
        raise MirrorError(f'{source}: {named} must say how it names sequences')
    genome = _spec(item, 'genome', named=named, source=source)
    if genome is None:
        raise MirrorError(f'{source}: {named} declares no genome')
    genome_suffix = _declared_suffix(genome, role='genome', source=source, named=f'{named}/genome')
    yield _object(genome, role='genome', suffix=genome_suffix, parent=item, source=source, named=f'{named}/genome')
    for role in ('annotation', 'report'):
        spec = _spec(item, role, named=named, source=source)
        if spec is not None:
            yield _object(
                spec,
                role=role,
                suffix=_declared_suffix(spec, role=role, source=source, named=f'{named}/{role}'),
                parent=item,
                source=source,
                named=f'{named}/{role}',
            )
    index = _spec(item, 'index', named=named, source=source)
    if index is not None:
        extension = index.get('extension')
        if not isinstance(extension, str) or not extension:
            raise MirrorError(f'{source}: {named}/index must declare the extension it adds to the genome')
        yield _object(
            index,
            role='genome',
            suffix=f'{genome_suffix}.{extension}',
            parent=item,
            source=source,
            named=f'{named}/index',
        )


def load_manifest(path: pathlib.Path = _MANIFEST) -> tuple[str, list[Entry]]:
    """The dataset prefix and every object the manifest pins, flattened.

    Raises:
        MirrorError: If the file is not a manifest, something omits a required field or carries an
            unknown one, or two objects would be written to one destination.
    """
    raw = tomllib.loads(path.read_text('utf-8'))
    if 'dataset' not in raw:
        raise MirrorError(f'{path.name}: a manifest needs a `dataset`')
    entries: list[Entry] = []
    for item in raw.get('assembly', []):
        entries.extend(_assembly_entries(item, source=path.name))
    for item in raw.get('table', []):
        named = str(item['id'])
        _require(item, _TABLE_FIELDS, named=named, source=path.name)
        role = str(item.get('role'))
        if role not in _ROLE_SUFFIXES:
            raise MirrorError(f'{path.name}: {named} has role {role!r}; known roles are {sorted(_ROLE_SUFFIXES)}')
        entries.append(
            _object(
                item,
                role=role,
                suffix=_declared_suffix(item, role=role, source=path.name, named=named),
                parent=item,
                source=path.name,
                named=named,
            )
        )
    if not entries:
        raise MirrorError(f'{path.name}: a manifest needs at least one `[[assembly]]` or `[[table]]`')
    written: dict[str, str] = {}
    for entry in entries:
        if entry.object in written:
            raise MirrorError(f'{path.name}: {entry.id} and {written[entry.object]} both write {entry.object!r}')
        written[entry.object] = entry.id
    return raw['dataset'], entries


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
    blob = bucket.blob(name + _PROVENANCE_SUFFIX)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _up_to_date(entry: Entry, stored: dict[str, object] | None, destination: storage.Blob) -> bool:
    """Whether the mirrored object still is what provenance says was put there for this URL."""
    if stored is None or stored.get('url') != entry.url:
        return False
    if entry.md5 is not None and stored.get('digest') != entry.md5:
        return False
    if not destination.exists():
        return False
    actual = object_digest(destination)
    return actual is not None and (stored.get('digest'), stored.get('digest_algorithm')) == actual


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


def _copy_within_gcs(client: storage.Client, entry: Entry, destination: storage.Blob) -> None:
    """Server-side copy of a ``gs://`` upstream; the bytes never reach this machine."""
    parsed = urllib.parse.urlparse(entry.url)
    source = client.bucket(parsed.netloc).blob(parsed.path.lstrip('/'))
    token = None
    for _ in range(_REWRITE_ATTEMPTS):
        token, _, _ = destination.rewrite(source, token=token)
        if token is None:
            return
    raise MirrorError(f'{entry.id}: rewrite did not complete')


def _provenance(entry: Entry, *, digest: str, algorithm: str, size: int) -> bytes:
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
        'mirrored_at': datetime.datetime.now(datetime.UTC).isoformat(),
    }
    return json.dumps(record, indent=1, sort_keys=True).encode()


def mirror(entry: Entry, *, dataset: str, bucket: storage.Bucket, client: storage.Client, dry_run: bool) -> Outcome:
    """Mirror one entry.

    Raises:
        MirrorError: If the upstream's digest is not the one the manifest pinned, or a copy did
            not complete.
    """
    name = f'{dataset}/{entry.object}'
    destination = bucket.blob(name)
    if _up_to_date(entry, _stored_provenance(bucket, name), destination):
        return Outcome(entry, Status.SKIPPED)
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
            destination.upload_from_filename(str(path), checksum='md5')
        algorithm = 'md5'
    bucket.blob(name + _PROVENANCE_SUFFIX).upload_from_string(
        _provenance(entry, digest=digest, algorithm=algorithm, size=size), content_type='application/json'
    )
    return Outcome(entry, Status.MIRRORED, size)


def _report(outcome: Outcome) -> str:
    if outcome.status is Status.MIRRORED:
        return f'  mirrored      {outcome.entry.id}  {outcome.size / 1e6:.1f} MB'
    return f'  {outcome.status.value:<12}  {outcome.entry.id}'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', default=_DEFAULT_PROJECT, help='GCP project owning the resources bucket')
    parser.add_argument('--dry-run', action='store_true', help='report what would transfer, write nothing')
    parser.add_argument(
        '--only',
        action='append',
        metavar='ID',
        help='an assembly or table id (`ncbi-grch38`), or one object within it (`ncbi-grch38/genome`)',
    )
    args = parser.parse_args()

    dataset, entries = load_manifest()
    if args.only:
        wanted = set(args.only)
        chosen = [entry for entry in entries if entry.id in wanted or entry.id.split('/')[0] in wanted]
        if unknown := wanted - {entry.id for entry in chosen} - {entry.id.split('/')[0] for entry in chosen}:
            parser.error(f'no such assembly, table or object: {sorted(unknown)}')
        entries = chosen

    client = storage.Client(project=args.project)
    bucket = client.bucket(f'{args.project}-{_BUCKET_SUFFIX}')
    print(f'{len(entries)} entries -> gs://{bucket.name}/{dataset}/', file=sys.stderr)
    # One entry's failure does not abandon the rest: a transfer of this size is worth resuming from
    # what landed, and the exit status still reports that something did not.
    failed: list[str] = []
    for entry in entries:
        try:
            print(
                _report(mirror(entry, dataset=dataset, bucket=bucket, client=client, dry_run=args.dry_run)),
                file=sys.stderr,
            )
        except (MirrorError, requests.RequestException) as error:
            failed.append(entry.id)
            print(f'  FAILED        {entry.id}: {error}', file=sys.stderr)
    if failed:
        print(f'{len(failed)} of {len(entries)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
