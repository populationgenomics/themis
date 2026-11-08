"""What ``resources/reference.toml`` pins, parsed and checked.

The manifest nests: an assembly declares its genome, annotation, sequence sets and indexes together,
so they cannot disagree about how sequences are named. The mirror walks the objects flat
(`Manifest.entries`); the build pass reads them by assembly (`Manifest.assemblies`), since a build
takes one assembly's objects together.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import tomllib
from collections.abc import Iterator, Mapping

MANIFEST = pathlib.Path(__file__).resolve().parents[2] / 'resources' / 'reference.toml'
_MD5_HEX = re.compile(r'^[0-9a-f]{32}$')


class ManifestError(Exception):
    """The manifest is inconsistent, or names an object it does not declare."""


# The filename each role takes, and the suffixes it admits. An index is absent: it never declares a
# suffix, only the extension it adds to its genome's, so there is no combination to admit or refuse.
ROLE_SUFFIXES: dict[str, frozenset[str]] = {
    # `tar.gz` is a publisher's archive of a genome, mirrored as it ships; the derived pass extracts
    # `genome.fa` beside it, against the digest the archive carries internally.
    'genome': frozenset({'fa', 'fa.gz', 'tar.gz'}),
    'annotation': frozenset({'gff3.gz'}),
    # Ensembl's annotation of the whole assembly — patches, haplotypes and scaffolds as well as the
    # chromosomes — beside the chromosome-only `annotation` the variant annotation pipeline reads,
    # and the same annotation as GTF, whose tags say which CDS ends are incomplete.
    'whole-annotation': frozenset({'gff3.gz'}),
    'whole-annotation-gtf': frozenset({'gtf.gz'}),
    # The transcripts' GenBank records: the CDS each record states, and for a retired version, its
    # successor.
    'records': frozenset({'gbff.gz'}),
    'transcripts': frozenset({'fa.gz'}),
    'ncrna': frozenset({'fa.gz'}),
    'proteins': frozenset({'fa.gz'}),
    'alignments': frozenset({'bam'}),
    'model-alignments': frozenset({'bam'}),
    'report': frozenset({'tsv'}),
    'summary': frozenset({'tsv.gz'}),
    'complete-set': frozenset({'tsv'}),
}
# The roles an assembly may declare beside its genome, in the order the mirror walks them.
ASSEMBLY_ROLES = (
    'annotation',
    'whole-annotation',
    'whole-annotation-gtf',
    'transcripts',
    'ncrna',
    'proteins',
    'records',
    'alignments',
    'model-alignments',
    'report',
)
_ASSEMBLY_FIELDS = frozenset(
    {'id', 'source', 'version', 'release', 'seqid', 'licence', 'consumer', 'genome', 'index', 'historical'}
    | set(ASSEMBLY_ROLES)
)
# NCBI's historical set: the replaced and suppressed versions of its transcripts, published beside an
# annotation release as a release of its own, on the same assembly and naming sequences the same way.
HISTORICAL_ROLES = ('annotation', 'records', 'alignments')
_HISTORICAL_FIELDS = frozenset({'version', *HISTORICAL_ROLES})
_TABLE_FIELDS = frozenset({'id', 'source', 'version', 'role', 'suffix', 'url', 'md5', 'licence', 'consumer'})
# What a spec nested inside an assembly may carry. `extension` is an index's alone — it takes the
# indexed object's suffix and adds to it rather than declaring one. `index` nests an index under the
# object it indexes (a BAM's `.bai`, a FASTA's `.fai`).
_OBJECT_FIELDS = frozenset({'url', 'suffix', 'md5', 'extension', 'index'})


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


@dataclasses.dataclass(frozen=True)
class Assembly:
    """One assembly and the objects it declares, by their path under its id.

    A path is the entry id with the assembly's id dropped: `genome`, `alignments`, `alignments/index`,
    `historical/records`.
    """

    id: str
    source: str
    version: str
    # The publisher's name for the annotation release, where an assembly declares one: the name a
    # store's shard is cut under.
    release: str | None
    # The version the historical set is published under, where the assembly nests one.
    historical: str | None
    objects: Mapping[str, Entry]

    def object(self, path: str) -> Entry:
        """The object declared at `path`.

        Raises:
            ManifestError: If the assembly declares nothing there.
        """
        try:
            return self.objects[path]
        except KeyError:
            raise ManifestError(f'{self.id} declares no {path}') from None


@dataclasses.dataclass(frozen=True)
class Manifest:
    """The dataset prefix, every assembly by id and every table by id."""

    dataset: str
    assemblies: Mapping[str, Assembly]
    tables: Mapping[str, Entry]

    @property
    def entries(self) -> list[Entry]:
        """Every object the manifest pins, assemblies' first, in declaration order."""
        return [entry for assembly in self.assemblies.values() for entry in assembly.objects.values()] + list(
            self.tables.values()
        )

    def assembly(self, assembly_id: str) -> Assembly:
        """The assembly declared under `assembly_id`.

        Raises:
            ManifestError: If there is none.
        """
        try:
            return self.assemblies[assembly_id]
        except KeyError:
            raise ManifestError(f'the manifest declares no assembly {assembly_id!r}') from None

    def table(self, table_id: str) -> Entry:
        """The table declared under `table_id`.

        Raises:
            ManifestError: If there is none.
        """
        try:
            return self.tables[table_id]
        except KeyError:
            raise ManifestError(f'the manifest declares no table {table_id!r}') from None


def _require(item: dict[str, object], fields: frozenset[str], *, named: str, source: str) -> None:
    """Raise unless `item` carries exactly the fields it may.

    An unknown key is almost always a typo in an optional one, which would otherwise read as absent
    and drop the object out of whatever that key feeds.
    """
    if missing := {'id', 'source', 'version', 'licence', 'consumer'} - item.keys():
        raise ManifestError(f'{source}: {named} omits {sorted(missing)}')
    if unknown := item.keys() - fields:
        raise ManifestError(f'{source}: {named} carries unknown {sorted(unknown)}')


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
        raise ManifestError(f'{source}: {named} carries unknown {sorted(unknown)}')
    if 'extension' in spec and 'suffix' in spec:
        raise ManifestError(
            f'{source}: {named} declares both a suffix and an extension; an index declares only the latter'
        )
    url = spec.get('url')
    if not isinstance(url, str) or not url.startswith(('https://', 'gs://')):
        raise ManifestError(f'{source}: {named} has url {url!r}; only https:// and gs:// are fetched')
    md5 = spec.get('md5')
    if md5 is not None:
        if not isinstance(md5, str) or not _MD5_HEX.match(md5):
            raise ManifestError(f'{source}: {named} has md5 {md5!r}, which is not 32 hex digits')
        if url.startswith('gs://'):
            # Never compared: GCS reports base64 and a publisher reports hex, and a server-side copy
            # is checksummed by GCS anyway. Accepting it would leave the object re-copying every
            # run, which on a 3 GB assembly is not a small mistake.
            raise ManifestError(f'{source}: {named} is a gs:// upstream, whose checksum GCS supplies; drop its md5')
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
    if not isinstance(suffix, str) or suffix not in ROLE_SUFFIXES[role]:
        raise ManifestError(
            f'{source}: {named} is a {role} with suffix {suffix!r}; allowed are {sorted(ROLE_SUFFIXES[role])}'
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
        raise ManifestError(
            f'{source}: {named} declares {key} as {type(value).__name__}, not a table — '
            f'`[[{named}.{key}]]` where `[{named}.{key}]` was meant?'
        )
    return value


def _index_entry(
    index: dict[str, object], item: dict[str, object], *, role: str, suffix: str, source: str, named: str
) -> Entry:
    """The index of one object, landing beside it as the object's name plus the extension it adds.

    htslib opens `genome.fa.fai` or `alignments.bam.bai` and reports anything else as a missing
    index rather than a misnamed one, so the name is composed, never declared.
    """
    extension = index.get('extension')
    if not isinstance(extension, str) or not extension:
        raise ManifestError(f'{source}: {named} must declare the extension it adds to the object it indexes')
    if 'index' in index:
        raise ManifestError(f'{source}: {named} nests an index under an index; nothing reads one')
    return _object(index, role=role, suffix=f'{suffix}.{extension}', parent=item, source=source, named=named)


def _index_entries(
    spec: dict[str, object], item: dict[str, object], *, role: str, suffix: str, source: str, named: str
) -> Iterator[Entry]:
    """The index an object nests under itself, if it declares one."""
    index = _spec(spec, 'index', named=named, source=source)
    if index is not None:
        yield _index_entry(index, item, role=role, suffix=suffix, source=source, named=f'{named}/index')


def _assembly_entries(item: dict[str, object], *, source: str) -> Iterator[Entry]:
    """The genome one assembly declares, and the annotation, sequence sets, report and index beside it.

    The index takes its genome's suffix plus the extension it adds, so the two cannot disagree:
    htslib opens `genome.fa.fai` and reports anything else as a missing index, not a misnamed one.
    """
    named = str(item['id'])
    _require(item, _ASSEMBLY_FIELDS, named=named, source=source)
    if not item.get('seqid'):
        raise ManifestError(f'{source}: {named} must say how it names sequences')
    genome = _spec(item, 'genome', named=named, source=source)
    if genome is None:
        raise ManifestError(f'{source}: {named} declares no genome')
    genome_suffix = _declared_suffix(genome, role='genome', source=source, named=f'{named}/genome')
    yield _object(genome, role='genome', suffix=genome_suffix, parent=item, source=source, named=f'{named}/genome')
    yield from _index_entries(genome, item, role='genome', suffix=genome_suffix, source=source, named=f'{named}/genome')
    for role in ASSEMBLY_ROLES:
        spec = _spec(item, role, named=named, source=source)
        if spec is not None:
            suffix = _declared_suffix(spec, role=role, source=source, named=f'{named}/{role}')
            yield _object(spec, role=role, suffix=suffix, parent=item, source=source, named=f'{named}/{role}')
            yield from _index_entries(spec, item, role=role, suffix=suffix, source=source, named=f'{named}/{role}')
    index = _spec(item, 'index', named=named, source=source)
    if index is not None:
        yield _index_entry(index, item, role='genome', suffix=genome_suffix, source=source, named=f'{named}/index')
    historical = _spec(item, 'historical', named=named, source=source)
    if historical is not None:
        yield from _historical_entries(historical, item, source=source, named=f'{named}/historical')


def _historical_entries(
    historical: dict[str, object], item: dict[str, object], *, source: str, named: str
) -> Iterator[Entry]:
    """The objects of the historical set an assembly nests, landing under the set's own version.

    The set inherits the assembly's source, licence and consumer, and its sequence naming, so it
    declares none of them; only its version is its own, so a newer set lands beside the older.
    """
    if unknown := historical.keys() - _HISTORICAL_FIELDS:
        raise ManifestError(f'{source}: {named} carries unknown {sorted(unknown)}')
    version = historical.get('version')
    if not isinstance(version, str) or not version:
        raise ManifestError(f'{source}: {named} must declare the version it is published under')
    parent = {**item, 'version': version}
    for role in HISTORICAL_ROLES:
        spec = _spec(historical, role, named=named, source=source)
        if spec is None:
            raise ManifestError(f'{source}: {named} declares no {role}')
        suffix = _declared_suffix(spec, role=role, source=source, named=f'{named}/{role}')
        yield _object(spec, role=role, suffix=suffix, parent=parent, source=source, named=f'{named}/{role}')
        yield from _index_entries(spec, parent, role=role, suffix=suffix, source=source, named=f'{named}/{role}')


def _optional_string(item: dict[str, object], key: str, *, source: str, named: str) -> str | None:
    value = item.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ManifestError(f'{source}: {named} has {key} {value!r}, which is not a name')
    return value


def _assembly(item: dict[str, object], *, source: str) -> Assembly:
    named = str(item.get('id'))
    objects = {entry.id.removeprefix(f'{named}/'): entry for entry in _assembly_entries(item, source=source)}
    historical = item.get('historical')
    return Assembly(
        id=named,
        source=str(item['source']),
        version=str(item['version']),
        release=_optional_string(item, 'release', source=source, named=named),
        historical=str(historical['version']) if isinstance(historical, dict) else None,
        objects=objects,
    )


def _table(item: dict[str, object], *, source: str) -> Entry:
    named = str(item['id'])
    _require(item, _TABLE_FIELDS, named=named, source=source)
    role = str(item.get('role'))
    if role not in ROLE_SUFFIXES:
        raise ManifestError(f'{source}: {named} has role {role!r}; known roles are {sorted(ROLE_SUFFIXES)}')
    suffix = _declared_suffix(item, role=role, source=source, named=named)
    return _object(item, role=role, suffix=suffix, parent=item, source=source, named=named)


def load(path: pathlib.Path = MANIFEST) -> Manifest:
    """Every assembly and table the manifest pins.

    Raises:
        ManifestError: If the file is not a manifest, something omits a required field or carries an
            unknown one, two items share an id, or two objects would be written to one destination.
    """
    raw = tomllib.loads(path.read_text('utf-8'))
    if 'dataset' not in raw:
        raise ManifestError(f'{path.name}: a manifest needs a `dataset`')
    assemblies: dict[str, Assembly] = {}
    tables: dict[str, Entry] = {}
    for item in raw.get('assembly', []):
        assembly = _assembly(item, source=path.name)
        if assembly.id in assemblies:
            raise ManifestError(f'{path.name}: two assemblies are declared as {assembly.id}')
        assemblies[assembly.id] = assembly
    for item in raw.get('table', []):
        table = _table(item, source=path.name)
        if table.id in tables or table.id in assemblies:
            raise ManifestError(f'{path.name}: {table.id} is declared twice')
        tables[table.id] = table
    manifest = Manifest(dataset=raw['dataset'], assemblies=assemblies, tables=tables)
    if not manifest.entries:
        raise ManifestError(f'{path.name}: a manifest needs at least one `[[assembly]]` or `[[table]]`')
    written: dict[str, str] = {}
    for entry in manifest.entries:
        if entry.object in written:
            raise ManifestError(f'{path.name}: {entry.id} and {written[entry.object]} both write {entry.object!r}')
        written[entry.object] = entry.id
    return manifest
