"""Build hgvs-weaver-data stores and genomes from the mirror, into the resources bucket beside it.

Each build reads only mirrored objects — and, for a historical set, its mirrored Entrez status table
— runs the package's ``weaver-data-build`` over local copies, and uploads the result under a prefix
named for the mirror entry it is built from and for a build id::

    reference-genomes/ncbi/<version>/<build>     one per NCBI assembly: the genome's blocks and catalogue
    reference-stores/<source>/<version>/<build>  one per publisher and assembly: shards, tables, manifest

The build id is a digest of everything the build is a function of: each input's provenance (its URL
and digest, as the mirror recorded and GCS still holds), the releases its shards are cut under, the
assembly, and the builder's version. Change any of them and the build lands beside the one it
supersedes; change none and the prefix already exists, so the build is skipped. Nothing is ever
overwritten: every object is written with a precondition GCS refuses if the object exists.

A prefix is complete once its last object is written — a store's ``manifest.bagz``, a genome's
``sequences.bagz`` — which is what readers refuse a prefix without. A prefix that exists without it is
a build that stopped partway; this pass will not skip it, since it is not the artefact, nor write
into it, so it fails, for a person to delete.

The builds run at once, each in a thread of its own. The work is the builder's subprocess, single-threaded,
so together they take as long as the longest chain — the GRCh38 genome, then the Ensembl store placed
against it — rather than the sum of all five; the Job's memory is sized for every build's scratch at once.
Each line of output names the build it is from.

Run from the repo root, after the mirror and the status table::

    uv run --group resources python -m tools.resources.build --project P [--dry-run]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import functools
import gzip
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence

from google.cloud import storage
from weaver_data_provider import build as weaver_build
from weaver_data_provider import genome as genome_mod
from weaver_data_provider import store as store_mod

from tools.resources import manifest, mirror, status

_STORES = 'reference-stores'
_GENOMES = 'reference-genomes'
# Written first, so a prefix records what it was built from even if the build stops before completing.
_PROVENANCE = 'provenance.json'
_BUILD_ID_HEX = 16
# Enters every build id beside the builder's version: bump it when a recipe here changes what a build is
# given — a flag, a role passed, the shards' order — so the change builds afresh rather than being skipped.
_RECIPE = 1
_RELEASE_HEADER = '#!annotation-source NCBI RefSeq '
# Shards are named by their release, and a store stacks them in the order given; the historical set
# sits beneath the current release.
_HISTORICAL_SHARD_PREFIX = '10-'
_CURRENT_SHARD_PREFIX = '20-'


class BuildError(Exception):
    """A build could not run as declared, or what it would write to is not in a state to write."""


# What fails one build without abandoning the rest: a refusal of ours, an input not mirrored as pinned, a transfer
# that did not complete, and a local read or write that failed (a malformed sidecar, a truncated gzip, a full disk).
_BUILD_ERRORS = (
    BuildError,
    mirror.MirrorError,
    status.StatusError,
    manifest.ManifestError,
    OSError,
    ValueError,
    *mirror.TRANSFER_ERRORS,
)


@dataclasses.dataclass(frozen=True)
class Input:
    """One object a build reads: where it is in the bucket, where it goes locally, and what identifies it."""

    name: str
    local: str
    provenance: Mapping[str, object]
    checksum: str
    generation: int

    def download(self, bucket: storage.Bucket, root: pathlib.Path) -> pathlib.Path:
        path = root / self.local
        path.parent.mkdir(parents=True, exist_ok=True)
        bucket.blob(self.name).download_to_filename(
            str(path), checksum=self.checksum, if_generation_match=self.generation
        )
        return path


def _mirrored_input(entry: manifest.Entry, *, dataset: str, bucket: storage.Bucket) -> Input:
    found = mirror.mirrored(entry, dataset=dataset, bucket=bucket)
    return Input(
        name=found.name,
        local=entry.object,
        provenance={'entry': entry.id, **found.provenance()},
        checksum=found.digest_algorithm,
        generation=found.generation,
    )


def _status_input(assembly: manifest.Assembly, *, dataset: str, bucket: storage.Bucket) -> Input:
    table, generation = status.the_table(assembly, dataset=dataset, bucket=bucket)
    return Input(
        name=table.name,
        local=table.name.removeprefix(f'{dataset}/'),
        provenance={
            'entry': f'{assembly.id}/historical/status',
            'object': table.name,
            'digest': table.provenance['digest'],
            'digest_algorithm': table.provenance['digest_algorithm'],
        },
        checksum=str(table.provenance['digest_algorithm']),
        generation=generation,
    )


# Runs one build: given each input's local path by role, the directory to write the artefact into, and the
# runner of its builder steps.
Recipe = Callable[[Mapping[str, pathlib.Path], pathlib.Path, 'Runner'], None]


@dataclasses.dataclass(frozen=True)
class Build:
    """One artefact to build: its kind, where it lands, what it reads and how it is cut.

    `identity` is everything the artefact is a function of besides its inputs' bytes — the
    releases, the assembly, the builder — so it enters the build id beside the inputs.
    """

    label: str
    root: str
    entry_prefix: str
    completion: str
    identity: Mapping[str, object]
    inputs: Mapping[str, Input]
    recipe: Recipe
    # A build this one reads the output of, which has to be had before this one's inputs are fetched.
    prerequisite: Build | None = None

    @property
    def provenance(self) -> dict[str, object]:
        return {**self.identity, 'inputs': {role: dict(value.provenance) for role, value in self.inputs.items()}}

    @property
    def build_id(self) -> str:
        encoded = json.dumps(self.provenance, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()[:_BUILD_ID_HEX]

    @property
    def prefix(self) -> str:
        return f'{self.root}/{self.entry_prefix}/{self.build_id}'


def _say(text: str) -> None:
    """One line on stderr, in a single write, so lines from builds running at once do not interleave."""
    sys.stderr.write(f'{text}\n')
    sys.stderr.flush()


class Abandon:
    """Whether the pass is abandoned, and the builder children to kill when it is.

    A pass is abandoned on the way out for an exception none of its builds can absorb — SIGTERM, a bug.
    Its builder children are killed, and its builds stop before their next download or builder step
    rather than running on for half an hour each: the interpreter waits for every worker thread before
    it exits. An upload already started is let finish rather than leave a partial prefix — unless the
    process is killed first, as Cloud Run does seconds after a SIGTERM.
    """

    def __init__(self) -> None:
        self._set = threading.Event()
        self._children: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            self._set.set()
            for child in self._children:
                child.kill()

    def check(self) -> None:
        """Raise if the pass is abandoned.

        Raises:
            BuildError: If it is.
        """
        if self._set.is_set():
            raise BuildError('abandoned')

    @contextlib.contextmanager
    def watching(self, child: subprocess.Popen[str]) -> Iterator[None]:
        """Kill `child` if the pass is abandoned while the block runs, or was already."""
        with self._lock:
            if self._set.is_set():
                child.kill()
            self._children.add(child)
        try:
            yield
        finally:
            with self._lock:
                self._children.discard(child)


@dataclasses.dataclass(frozen=True)
class Runner:
    """Runs one build's ``weaver-data-build`` steps, each line of their report prefixed with the build's label."""

    label: str
    abandon: Abandon

    def cli(self, *args: str) -> str:
        """Run ``weaver-data-build`` and return what it printed on stdout, its report relayed to stderr.

        Raises:
            BuildError: If the command exits non-zero, or the pass is abandoned.
        """
        self.abandon.check()
        command = [sys.executable, '-m', 'weaver_data_provider.build.cli', *args]
        _say(f'  [{self.label}] $ weaver-data-build {" ".join(args)}')
        with (
            tempfile.TemporaryFile('w+') as stdout,
            subprocess.Popen(  # noqa: S603 - our own CLI
                command, stdout=stdout, stderr=subprocess.PIPE, text=True, errors='replace'
            ) as process,
            self.abandon.watching(process),
        ):
            assert process.stderr is not None  # noqa: S101 - stderr=PIPE
            for line in process.stderr:
                _say(f'  [{self.label}] {line.rstrip()}')
            returncode = process.wait()
            stdout.seek(0)
            printed = stdout.read()
        self.abandon.check()
        if returncode != 0:
            raise BuildError(f'weaver-data-build {args[0]} exited {returncode}; see its output above')
        return printed

    def shard(self, *args: str) -> pathlib.Path:
        """Cut one shard and return its path, the last line the command prints."""
        lines = self.cli(*args).splitlines()
        if not lines or not lines[-1].strip():
            raise BuildError(f'weaver-data-build {args[0]} printed no shard path')
        return pathlib.Path(lines[-1].strip())


def check_release(annotation: pathlib.Path, release: str) -> None:
    """Raise unless an NCBI annotation's header names `release` as its annotation source.

    Raises:
        BuildError: If the header names another release, or none in its first lines.
    """
    with gzip.open(annotation, 'rt', encoding='ascii', errors='replace') as handle:
        for _, line in zip(range(50), handle, strict=False):
            if line.startswith(_RELEASE_HEADER):
                found = line.removeprefix(_RELEASE_HEADER).strip()
                if found != release:
                    raise BuildError(f'{annotation.name}: the header names release {found}, the manifest {release}')
                return
            if not line.startswith('#'):
                break
    raise BuildError(f'{annotation.name}: no `{_RELEASE_HEADER.strip()}` line in its header')


def _flags(flag: str, paths: Sequence[pathlib.Path]) -> list[str]:
    return [part for path in paths for part in (flag, str(path))]


def _genome(assembly: manifest.Assembly, grc: str, *, dataset: str, bucket: storage.Bucket) -> Build:
    def recipe(local: Mapping[str, pathlib.Path], out: pathlib.Path, runner: Runner) -> None:
        runner.cli('genome', '--assembly', grc, '--fasta', str(local['genome']), '--out', str(out))

    return Build(
        label=f'{assembly.id} genome',
        root=_GENOMES,
        entry_prefix=f'{assembly.source}/{assembly.version}',
        completion=genome_mod.CATALOGUE,
        identity={'artefact': 'genome', 'assembly': grc, 'builder': weaver_build.BUILDER, 'recipe': _RECIPE},
        inputs={'genome': _mirrored_input(assembly.object('genome'), dataset=dataset, bucket=bucket)},
        recipe=recipe,
    )


def _release(assembly: manifest.Assembly) -> str:
    if assembly.release is None:
        raise BuildError(f'{assembly.id} names no annotation release, which its store is cut under')
    return assembly.release


def _alignment_roles(assembly: manifest.Assembly) -> list[str]:
    """The alignment BAMs an NCBI assembly declares: the curated one, and the model one where published."""
    return [role for role in ('alignments', 'model-alignments') if role in assembly.objects]


def _crosswalk_inputs(pinned: manifest.Manifest, *, bucket: storage.Bucket) -> dict[str, Input]:
    return {
        role: _mirrored_input(pinned.table(table), dataset=pinned.dataset, bucket=bucket)
        for role, table in (('hgnc', 'hgnc-complete-set'), ('mane', 'mane-summary'))
    }


def _with_indexes(roles: Sequence[str], *, prefix: str = '') -> list[str]:
    """`roles` with the index each nests, which the build downloads beside it but never names."""
    return [path for role in roles for path in (f'{prefix}{role}', f'{prefix}{role}/index')]


def _refseq_store(assembly: manifest.Assembly, grc: str, *, pinned: manifest.Manifest, bucket: storage.Bucket) -> Build:
    release = _release(assembly)
    alignments = _alignment_roles(assembly)
    paths = ['annotation', 'transcripts', 'proteins', 'records', *_with_indexes(alignments)]
    if assembly.historical is not None:
        paths += [
            'historical/annotation',
            'historical/records',
            *_with_indexes(['alignments'], prefix='historical/'),
        ]
    inputs = {path: _mirrored_input(assembly.object(path), dataset=pinned.dataset, bucket=bucket) for path in paths}
    inputs |= _crosswalk_inputs(pinned, bucket=bucket)
    if assembly.historical is not None:
        inputs['historical/status'] = _status_input(assembly, dataset=pinned.dataset, bucket=bucket)
    releases = [release] if assembly.historical is None else [assembly.historical, release]

    def recipe(local: Mapping[str, pathlib.Path], out: pathlib.Path, runner: Runner) -> None:
        check_release(local['annotation'], release)
        shards = out / 'shards'
        common = [
            '--assembly',
            grc,
            '--hgnc',
            str(local['hgnc']),
            '--mane',
            str(local['mane']),
            '--shards',
            str(shards),
        ]
        cut: list[pathlib.Path] = []
        if assembly.historical is not None:
            cut.append(
                runner.shard(
                    'historical', *common, '--release', assembly.historical, '--prefix', _HISTORICAL_SHARD_PREFIX,
                    '--annotation', str(local['historical/annotation']),
                    '--records', str(local['historical/records']),
                    '--alignments', str(local['historical/alignments']),
                    '--status', str(local['historical/status']),
                )
            )  # fmt: skip
        cut.append(
            runner.shard(
                'refseq', *common, '--release', release, '--prefix', _CURRENT_SHARD_PREFIX,
                '--annotation', str(local['annotation']),
                '--transcripts', str(local['transcripts']),
                '--proteins', str(local['proteins']),
                '--records', str(local['records']),
                *_flags('--alignments', [local[role] for role in alignments]),
            )
        )  # fmt: skip
        runner.cli('index', '--assembly', grc, '--out', str(out), *map(str, cut))

    return Build(
        label=f'{assembly.id} store',
        root=_STORES,
        entry_prefix=f'{assembly.source}/{assembly.version}',
        completion=store_mod.MANIFEST,
        identity={
            'artefact': 'store',
            'assembly': grc,
            'releases': releases,
            'builder': weaver_build.BUILDER,
            'recipe': _RECIPE,
        },
        inputs=inputs,
        recipe=recipe,
    )


def _ensembl_store(
    assembly: manifest.Assembly,
    grc: str,
    *,
    ncbi: manifest.Assembly,
    genome: Build,
    pinned: manifest.Manifest,
    bucket: storage.Bucket,
    genome_dir: Callable[[Build], pathlib.Path],
) -> Build:
    """Ensembl's store, placed against the NCBI genome and carrying NCBI's alignments for the MANE partners."""
    release = _release(assembly)
    own = ['whole-annotation', 'whole-annotation-gtf', 'transcripts', 'ncrna', 'proteins']
    inputs = {path: _mirrored_input(assembly.object(path), dataset=pinned.dataset, bucket=bucket) for path in own}
    alignments = _alignment_roles(ncbi)
    for path in _with_indexes(alignments):
        inputs[f'ncbi/{path}'] = _mirrored_input(ncbi.object(path), dataset=pinned.dataset, bucket=bucket)
    inputs |= _crosswalk_inputs(pinned, bucket=bucket)
    # The genome the shard is placed against is a build of its own; its identity enters this one whole.
    identity = {
        'artefact': 'store',
        'assembly': grc,
        'releases': [release],
        'builder': weaver_build.BUILDER,
        'recipe': _RECIPE,
    }

    def recipe(local: Mapping[str, pathlib.Path], out: pathlib.Path, runner: Runner) -> None:
        shard = runner.shard(
            'ensembl', '--assembly', grc, '--release', release,
            '--annotation', str(local['whole-annotation']),
            '--completeness', str(local['whole-annotation-gtf']),
            '--transcripts', str(local['transcripts']), '--transcripts', str(local['ncrna']),
            '--proteins', str(local['proteins']),
            '--genome', str(genome_dir(genome)),
            *_flags('--alignments', [local[f'ncbi/{role}'] for role in alignments]),
            '--hgnc', str(local['hgnc']), '--mane', str(local['mane']),
            '--shards', str(out / 'shards'),
        )  # fmt: skip
        runner.cli('index', '--assembly', grc, '--out', str(out), str(shard))

    return Build(
        label=f'{assembly.id} store',
        root=_STORES,
        entry_prefix=f'{assembly.source}/{assembly.version}',
        completion=store_mod.MANIFEST,
        identity={**identity, 'genome': genome.prefix},
        inputs=inputs,
        recipe=recipe,
        prerequisite=genome,
    )


# The builds this pass makes, by manifest assembly: each NCBI assembly gets its genome and RefSeq store,
# and an Ensembl assembly gets its store on the NCBI genome of the same assembly.
_REFSEQ = (('ncbi-grch38', 'GRCh38'), ('ncbi-grch37', 'GRCh37'))
_ENSEMBL = (('ensembl-grch38', 'GRCh38', 'ncbi-grch38'),)


def exists(bucket: storage.Bucket, build: Build) -> bool:
    """Whether the build's prefix holds a complete artefact.

    Raises:
        BuildError: If the prefix holds objects but not the one written last.
    """
    if not any(True for _ in bucket.list_blobs(prefix=f'{build.prefix}/', max_results=1)):
        return False
    if not bucket.blob(f'{build.prefix}/{build.completion}').exists():
        raise BuildError(
            f'{build.label}: gs://{bucket.name}/{build.prefix}/ holds a build that stopped before writing '
            f'{build.completion}; delete it to rebuild'
        )
    return True


def _upload(bucket: storage.Bucket, build: Build, out: pathlib.Path) -> int:
    """Write the artefact under its prefix, the completion object last; return the bytes written.

    Each object is written at generation 0, which GCS refuses if the object exists.
    """
    files = sorted(path for path in out.rglob('*') if path.is_file())
    completion = out / build.completion
    if completion not in files:
        raise BuildError(f'{build.label}: the builder wrote no {build.completion}')
    bucket.blob(f'{build.prefix}/{_PROVENANCE}').upload_from_string(
        json.dumps(build.provenance, indent=1, sort_keys=True), content_type='application/json', if_generation_match=0
    )
    for path in [*(f for f in files if f != completion), completion]:
        name = f'{build.prefix}/{path.relative_to(out).as_posix()}'
        bucket.blob(name).upload_from_filename(str(path), checksum='crc32c', if_generation_match=0)
    return sum(path.stat().st_size for path in files)


def run(build: Build, *, bucket: storage.Bucket, work: pathlib.Path, abandon: Abandon) -> pathlib.Path:
    """Download the build's inputs, run it, and upload the artefact; return the local artefact directory.

    Raises:
        BuildError: If the build fails, or the pass is abandoned before its upload.
        OSError: If a local read or write fails.
        google.api_core.exceptions.GoogleAPIError: If a transfer fails (the rest of `mirror.TRANSFER_ERRORS` too).
    """
    inputs = work / 'inputs'
    local = {}
    for role, value in build.inputs.items():
        abandon.check()
        local[role] = value.download(bucket, inputs)
    out = work / 'out'
    build.recipe(local, out, Runner(build.label, abandon))
    size = _upload(bucket, build, out)
    _say(f'  built         {build.label} -> {build.prefix}  {size / 1e6:.1f} MB')
    return out


class _Genomes:
    """The genomes an Ensembl build is placed against: built this run, or downloaded from their prefix.

    A genome this run builds is expected before the builds start; asking for it waits until its build
    has ended, and takes the directory it built, or downloads it from its prefix if it was not built
    here (it already existed). Every expected genome has to be settled, or whoever asks for it waits
    for ever.
    """

    def __init__(self, bucket: storage.Bucket, root: pathlib.Path, abandon: Abandon) -> None:
        self._bucket = bucket
        self._root = root
        self._abandon = abandon
        self._expected: dict[str, concurrent.futures.Future[pathlib.Path | None]] = {}
        self._downloaded: dict[str, pathlib.Path] = {}

    def expect(self, build: Build) -> None:
        self._expected[build.prefix] = concurrent.futures.Future()

    def settle(self, build: Build, directory: pathlib.Path | None) -> None:
        """Record that the genome's build has ended, with what it built or None; only the first call counts."""
        future = self._expected[build.prefix]
        if not future.done():
            future.set_result(directory)

    def __call__(self, build: Build) -> pathlib.Path:
        if (future := self._expected.get(build.prefix)) is not None and (built := future.result()) is not None:
            return built
        if build.prefix in self._downloaded:
            return self._downloaded[build.prefix]
        self._abandon.check()
        if not exists(self._bucket, build):
            raise BuildError(f'{build.label} is not built at {build.prefix}, and an Ensembl store is placed against it')
        directory = self._root / build.build_id
        directory.mkdir(parents=True, exist_ok=True)
        for name in (genome_mod.BLOCKS, genome_mod.CATALOGUE):
            self._bucket.blob(f'{build.prefix}/{name}').download_to_filename(str(directory / name), checksum='crc32c')
        self._downloaded[build.prefix] = directory
        return directory


@dataclasses.dataclass(frozen=True)
class Planned:
    """A build the plan names, or why it could not be planned (an input not mirrored, say)."""

    label: str
    build: Build | None
    error: str | None = None
    # Whether the artefact is a genome an Ensembl store is placed against later in the run.
    keep: bool = False


def plan(
    pinned: manifest.Manifest, *, bucket: storage.Bucket, genomes: Callable[[Build], pathlib.Path]
) -> list[Planned]:
    """Every build this pass makes, in the order it makes them, each with its build id resolved.

    Planning reads only provenance, never the inputs' bytes, so it is what a dry run reports.
    """
    planned: list[Planned] = []
    ncbi_genomes: dict[str, Build] = {}

    def attempt(label: str, make: Callable[[], Build], *, keep: bool = False) -> Build | None:
        try:
            build = make()
        except _BUILD_ERRORS as error:
            planned.append(Planned(label, None, str(error)))
            return None
        planned.append(Planned(label, build, keep=keep))
        return build

    for assembly_id, grc in _REFSEQ:
        assembly = pinned.assembly(assembly_id)
        genome = attempt(
            f'{assembly_id} genome',
            functools.partial(_genome, assembly, grc, dataset=pinned.dataset, bucket=bucket),
            keep=assembly_id in {ncbi_id for _, _, ncbi_id in _ENSEMBL},
        )
        if genome is not None:
            ncbi_genomes[assembly_id] = genome
        attempt(f'{assembly_id} store', functools.partial(_refseq_store, assembly, grc, pinned=pinned, bucket=bucket))
    for assembly_id, grc, ncbi_id in _ENSEMBL:
        label = f'{assembly_id} store'
        if ncbi_id not in ncbi_genomes:
            planned.append(Planned(label, None, f'the genome it is placed against, {ncbi_id}, could not be planned'))
            continue
        make = functools.partial(
            _ensembl_store,
            pinned.assembly(assembly_id),
            grc,
            ncbi=pinned.assembly(ncbi_id),
            genome=ncbi_genomes[ncbi_id],
            pinned=pinned,
            bucket=bucket,
            genome_dir=genomes,
        )
        attempt(label, make)
    return planned


def _make(
    planned: Planned, *, bucket: storage.Bucket, genomes: _Genomes, abandon: Abandon, scratch: str, dry_run: bool
) -> bool:
    """Make one planned build unless it exists, reporting it on stderr; return whether it failed."""
    try:
        try:
            if planned.build is None:
                raise BuildError(planned.error)
            build = planned.build
            if exists(bucket, build):
                _say(f'  skip          {build.label}  {build.prefix}')
                return False
            if dry_run:
                _say(f'  would build   {build.label}  {build.prefix}')
                return False
            if build.prerequisite is not None:
                genomes(build.prerequisite)
            work = pathlib.Path(tempfile.mkdtemp(dir=scratch))
            kept = False
            try:
                out = run(build, bucket=bucket, work=work, abandon=abandon)
                if planned.keep:
                    # The artefact is uploaded: a failure to free the inputs does not fail the build.
                    shutil.rmtree(work / 'inputs', ignore_errors=True)
                    genomes.settle(build, out)
                    kept = True
            finally:
                if not kept:
                    shutil.rmtree(work)
        finally:
            if planned.keep and planned.build is not None:
                genomes.settle(planned.build, None)
    except _BUILD_ERRORS as error:
        _say(f'  FAILED        {planned.label}: {error}')
        return True
    return False


def build_all(pinned: manifest.Manifest, *, bucket: storage.Bucket, dry_run: bool) -> list[str]:
    """Make every planned build that does not exist yet, all at once; return the labels of those that failed.

    One build's failure does not abandon the rest, as in the mirror. Each build's inputs are deleted
    once it is uploaded: on Cloud Run the scratch disk is memory.
    """
    abandon = Abandon()
    # Removed only once every build has ended: on the way out for SIGTERM, builds still finishing an upload read
    # from it, and it goes with the process.
    scratch = tempfile.mkdtemp()
    genomes = _Genomes(bucket, pathlib.Path(scratch) / 'genomes', abandon)
    planned = plan(pinned, bucket=bucket, genomes=genomes)
    for each in planned:
        if each.keep and each.build is not None:
            genomes.expect(each.build)
    # A thread for every build, so one waiting on its genome never holds up the genome's own build.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(len(planned), 1))
    try:
        futures = [
            (
                each.label,
                pool.submit(
                    _make, each, bucket=bucket, genomes=genomes, abandon=abandon, scratch=scratch, dry_run=dry_run
                ),
            )
            for each in planned
        ]
        # Whichever build raises what it cannot absorb, the pass is abandoned then, not when its turn comes.
        concurrent.futures.wait([future for _, future in futures], return_when=concurrent.futures.FIRST_EXCEPTION)
        for label, future in futures:
            if future.done() and (error := future.exception()) is not None:
                error.add_note(f'raised by the {label} build')
                raise error
        failed = [label for label, future in futures if future.result()]
    except Exception:
        # Wait out the uploads already begun, so nothing of this attempt is still writing once the job marks it
        # finished; abandoned, the rest stop at their next step.
        abandon()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    except BaseException:
        # SIGTERM: SIGKILL follows within seconds, sooner than an upload in progress would end, and waiting would
        # cost the attempt its finished marker. Its uploads end with the process instead.
        abandon()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    shutil.rmtree(scratch)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project owning the resources bucket')
    parser.add_argument('--dry-run', action='store_true', help='report each build id and whether it exists')
    args = parser.parse_args()
    client = storage.Client(project=args.project)
    if failed := build_all(manifest.load(), bucket=mirror.resources_bucket(client, args.project), dry_run=args.dry_run):
        print(f'{len(failed)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
