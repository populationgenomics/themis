"""Fixtures for the sheaf suite, and the real-git seeding every test publishes through.

Most of it runs against `LocalBackend`: no network, no credentials, no cloud client. The GCS
backend is exercised against `fake-gcs-server` through the repo-root `conftest.py`, which owns the
Docker gate and the emulator container for all of themis — so the compare-and-swap the design rests
on is checked against a real implementation of the JSON API rather than a hand-written double.

Every commit the suite asserts on is built by the `git` binary: `GitRepo` runs `hash-object`,
`update-index`, `write-tree`, `commit-tree` and `pack-objects` against a bare `GIT_DIR` with no
working tree, then publishes the result through `Store`. Why the fixtures are an independent oracle:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping

import pytest
from google.cloud import storage

from themis import sheaf
from themis.sheaf import store as store_mod
from themis.sheaf.backends import gcs

BLOB_MODE = '100644'
GITATTRIBUTES = '.gitattributes'
UNION_MERGE_ATTRIBUTES = '*.jsonl merge=union\n'

GIT_IDENTITY = [
    '-c',
    'user.email=agent@example.org',
    '-c',
    'user.name=Agent',
    '-c',
    'init.defaultBranch=main',
    '-c',
    'protocol.version=2',
]


@pytest.fixture(scope='session', autouse=True)
def git_binary() -> str:
    """The git binary this suite drives throughout, or a failure — never a skip.

    Raises:
        RuntimeError: If git is not on PATH.
    """
    found = shutil.which('git')
    if found is None:
        raise RuntimeError('themis/sheaf/tests drives a real git client throughout; git is not on PATH')
    return found


def _hermetic_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """An environment git reads no host configuration from.

    The global and system config files are pointed at the null device, so a `commit.gpgsign`,
    `core.hooksPath` or `init.templateDir` on the developer's machine cannot change what these
    tests produce or whether they pass.
    """
    return {
        'PATH': os.environ.get('PATH', ''),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        **(extra or {}),
    }


def run_git(*args: str, cwd: str | os.PathLike[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git as a client would, capturing output."""
    return subprocess.run(
        ['git', *GIT_IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env=_hermetic_env(),
        timeout=120,
    )


def _plumb(*args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
    """Run a git plumbing command, returning stdout.

    Raises:
        RuntimeError: If git exits non-zero, with its stderr attached.
    """
    result = subprocess.run(
        ['git', *args],
        input=stdin,
        capture_output=True,
        check=False,
        env=_hermetic_env(env),
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} failed: {result.stderr.decode(errors="replace")}')
    return result.stdout


@dataclasses.dataclass(frozen=True)
class Author:
    """Who a seeded commit is attributed to, as opposed to what wrote it down."""

    name: str
    email: str


SERVICE = Author('sheaf', 'sheaf@localhost')

# Path -> (mode, object id), a whole tree flattened.
Entries = dict[str, tuple[str, str]]
# Given the parent tree, the file contents to write. Empty means there is nothing to do.
BuildFiles = Callable[[Entries], dict[str, bytes]]


class GitRepo:
    """A sheaf repository seeded by driving the git binary.

    Holds a bare `GIT_DIR` with no working tree and no refs of its own: somewhere for git to read
    the objects a snapshot names and write the ones a commit needs. The refs live in the store, and
    every publish goes through `Store.transact`, so a writer that loses the race re-derives its
    commit against whichever snapshot won.

    One instance per thread and per process, never shared: the object database and the record of
    which packs it holds are both unsynchronised.
    """

    def __init__(
        self,
        store: store_mod.Store,
        path: str | os.PathLike[str],
        *,
        retries: int = store_mod.DEFAULT_RETRIES,
    ) -> None:
        self.store = store
        self.path = pathlib.Path(path)
        self._retries = retries
        self._installed: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _plumb('init', '--bare', '--quiet', str(self.path))
        # No ref names the objects a commit is assembled from, so an auto-gc would prune them.
        self._git('config', 'gc.auto', '0')

    @classmethod
    def open(
        cls,
        backend: sheaf.Backend,
        repo: str,
        path: str | os.PathLike[str],
        *,
        retries: int = store_mod.DEFAULT_RETRIES,
    ) -> GitRepo:
        """Open `repo` in `backend`, keeping the object database under `path`."""
        return cls(store_mod.Store(backend, repo), path, retries=retries)

    def append_line(self, *, ref: str, path: str, line: str, author: Author, message: str) -> store_mod.Snapshot:
        """Append `line` to the log at `path` on `ref`."""

        def build_files(entries: Entries) -> dict[str, bytes]:
            existing = entries.get(path)
            previous = self._blob(existing[1]) if existing is not None else b''
            return {path: previous + line.encode() + b'\n'}

        return self._publish(ref=ref, author=author, message=message, build_files=build_files)

    def write_files(self, *, ref: str, files: Mapping[str, str], author: Author, message: str) -> store_mod.Snapshot:
        """Set `files` to the given contents, replacing whatever is there."""

        def build_files(_entries: Entries) -> dict[str, bytes]:
            return {path: content.encode() for path, content in files.items()}

        return self._publish(ref=ref, author=author, message=message, build_files=build_files)

    def ensure_attributes(
        self, *, ref: str, author: Author, content: str = UNION_MERGE_ATTRIBUTES
    ) -> store_mod.Snapshot:
        """Install `.gitattributes` if it is absent, so append logs union-merge."""

        def build_files(entries: Entries) -> dict[str, bytes]:
            return {} if GITATTRIBUTES in entries else {GITATTRIBUTES: content.encode()}

        return self._publish(
            ref=ref,
            author=author,
            message='install union merge for append logs',
            build_files=build_files,
        )

    def read_log(self, *, ref: str, path: str) -> list[str]:
        """Return the lines of the log at `path` on `ref`, oldest first.

        Returns:
            The file's lines, or an empty list if the ref or the path is absent.
        """
        snapshot = self.store.read()
        head = snapshot.head(ref)
        if head is None:
            return []
        self._hydrate(snapshot)
        entry = self._entries(head).get(path)
        if entry is None:
            return []
        return self._blob(entry[1]).decode().splitlines()

    def history(self, ref: str) -> list[str]:
        """Commit ids on `ref`, first-parent, newest first."""
        snapshot = self.store.read()
        head = snapshot.head(ref)
        if head is None:
            return []
        self._hydrate(snapshot)
        return self._git('rev-list', '--first-parent', head).decode().split()

    def _publish(self, *, ref: str, author: Author, message: str, build_files: BuildFiles) -> store_mod.Snapshot:
        """Build one commit with git and publish it, replaying on a lost race."""

        def build(snapshot: store_mod.Snapshot) -> store_mod.Intent | None:
            self._hydrate(snapshot)
            head = snapshot.head(ref)
            entries = self._entries(head) if head is not None else {}
            files = build_files(entries)
            if not files:
                return None
            for path, data in files.items():
                entries[path] = (BLOB_MODE, self._write_blob(data))
            commit = self._commit_tree(self._write_tree(entries), head, author, message)
            pack = self._pack(commit, snapshot.refs.values())
            # Built here, so whether or not this attempt wins, a later hydrate needs none of it.
            self._installed.add(store_mod.pack_id(pack))
            return store_mod.Intent(
                ref_updates={ref: store_mod.RefUpdate(old=head, new=commit)},
                packs=[pack],
                author=author.email,
            )

        return self.store.transact(build, retries=self._retries)

    def _hydrate(self, snapshot: store_mod.Snapshot) -> None:
        """Index every pack `snapshot` names that this object database has not seen."""
        for ident in snapshot.packs:
            if ident in self._installed:
                continue
            self._git('index-pack', '--stdin', stdin=self.store.fetch_pack(ident))
            self._installed.add(ident)

    def _entries(self, commit: str) -> Entries:
        """Flatten the tree at `commit` into path -> (mode, object id)."""
        entries: Entries = {}
        for record in self._git('ls-tree', '-r', '-z', commit).decode().split('\0'):
            if not record:
                continue
            meta, _, path = record.partition('\t')
            mode, _type, oid = meta.split(' ')
            entries[path] = (mode, oid)
        return entries

    def _blob(self, oid: str) -> bytes:
        """The contents of the blob `oid` names."""
        return self._git('cat-file', 'blob', oid)

    def _write_blob(self, data: bytes) -> str:
        """Store `data` as a blob and return its object id."""
        return self._git('hash-object', '-w', '--stdin', stdin=data).decode().strip()

    def _write_tree(self, entries: Entries) -> str:
        """Build the tree `entries` describes through a throwaway index."""
        index = self.path / f'seed-index-{uuid.uuid4().hex}'
        info = ''.join(f'{mode} {oid}\t{path}\n' for path, (mode, oid) in sorted(entries.items()))
        env = {'GIT_INDEX_FILE': str(index)}
        try:
            self._git('update-index', '--index-info', stdin=info.encode(), env=env)
            return self._git('write-tree', env=env).decode().strip()
        finally:
            index.unlink(missing_ok=True)

    def _commit_tree(self, tree: str, parent: str | None, author: Author, message: str) -> str:
        """Commit `tree` and return the commit id."""
        parents = ['-p', parent] if parent is not None else []
        return (
            self._git(
                'commit-tree',
                tree,
                *parents,
                '-m',
                message,
                env={
                    'GIT_AUTHOR_NAME': author.name,
                    'GIT_AUTHOR_EMAIL': author.email,
                    'GIT_COMMITTER_NAME': SERVICE.name,
                    'GIT_COMMITTER_EMAIL': SERVICE.email,
                },
            )
            .decode()
            .strip()
        )

    def _pack(self, commit: str, published: Iterable[str]) -> bytes:
        """A self-contained pack of everything reachable from `commit` and not already published."""
        excluded = sorted(set(published) - {commit})
        revs = f'{commit}\n' + ''.join(f'^{sha}\n' for sha in excluded)
        return self._git('pack-objects', '--revs', '--stdout', stdin=revs.encode())

    def _git(self, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        """Run git against this repository's bare `GIT_DIR`, returning stdout.

        Raises:
            RuntimeError: If git exits non-zero, with its stderr attached.
        """
        return _plumb(*args, stdin=stdin, env={'GIT_DIR': str(self.path), **(env or {})})


@pytest.fixture
def backend(tmp_path: pathlib.Path) -> sheaf.LocalBackend:
    """An object store rooted in a temporary directory."""
    return sheaf.LocalBackend(tmp_path / 'store')


@pytest.fixture
def gcs_backend(gcs_bucket: storage.Bucket) -> gcs.GcsBackend:
    """The real GCS backend, against the emulator."""
    return gcs.GcsBackend(gcs_bucket)


@pytest.fixture(params=['local', 'gcs'])
def any_backend(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> sheaf.Backend:
    """Run a test against every backend implementation of the same contract."""
    if request.param == 'local':
        return sheaf.LocalBackend(tmp_path / 'store')
    return request.getfixturevalue('gcs_backend')
