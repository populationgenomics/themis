"""Concurrent publishers, and no lost updates: the gate the rest of the design waits behind.

A lost update is silent — nothing raises, nothing logs, a line a writer appended simply is not
there. So these tests assert three independent things:

* every line survives, exactly once — the property a caller cares about;
* the branch has one commit per append — no attempt vanished into an overwritten tree;
* the ref document's sequence numbers form a dense chain with no gaps or repeats — proof that the
  compare-and-swap, not luck, is what serialised the writers.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from concurrent import futures
from typing import override

from themis import sheaf
from themis.sheaf.tests import conftest

REF = 'refs/heads/main'
LOG_PATH = 'annotations/review.jsonl'
THREAD_WRITERS = 8
THREAD_APPENDS = 4
PROCESS_WRITERS = 6
PROCESS_APPENDS = 3
# Contention, not workload, drives replays: every lost race means another writer committed.
GENEROUS_RETRIES = 500

# The directory `themis` lives in, so a writer subprocess can import the package.
_IMPORT_ROOT = pathlib.Path(sheaf.__file__).resolve().parents[2]

_WRITER_SCRIPT = """
import sys

from themis import sheaf
from themis.sheaf.tests import conftest

root, gitdir, name, count, retries = sys.argv[1:6]
writer = conftest.GitRepo.open(sheaf.LocalBackend(root), 'projects/demo', gitdir, retries=int(retries))
author = conftest.Author(name, name + '@example.org')
for index in range(int(count)):
    writer.append_line(
        ref='refs/heads/main',
        path='annotations/review.jsonl',
        line=f'{name}:{index}',
        author=author,
        message=f'mark {name}:{index}',
    )
"""


def _append_all(writer: conftest.GitRepo, name: str, count: int) -> None:
    author = conftest.Author(name, f'{name}@example.org')
    for index in range(count):
        writer.append_line(
            ref=REF,
            path=LOG_PATH,
            line=f'{name}:{index}',
            author=author,
            message=f'mark {name}:{index}',
        )


def test_no_lost_updates_across_threads(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    expected = {f'w{w}:{i}' for w in range(THREAD_WRITERS) for i in range(THREAD_APPENDS)}

    def worker(index: int) -> None:
        # Each writer gets its own object database, the way separate hosts would.
        writer = conftest.GitRepo.open(
            backend,
            'projects/demo',
            tmp_path / f'writer-{index}.git',
            retries=GENEROUS_RETRIES,
        )
        _append_all(writer, f'w{index}', THREAD_APPENDS)

    with futures.ThreadPoolExecutor(max_workers=THREAD_WRITERS) as pool:
        for result in [pool.submit(worker, i) for i in range(THREAD_WRITERS)]:
            result.result()

    _assert_all_survived(backend, tmp_path / 'verify.git', expected)


def test_no_lost_updates_across_processes(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    """Separate processes, so the win is the store's and not the GIL's."""
    script = tmp_path / 'writer.py'
    script.write_text(_WRITER_SCRIPT, 'utf-8')
    expected = {f'p{w}:{i}' for w in range(PROCESS_WRITERS) for i in range(PROCESS_APPENDS)}
    # The subprocess does not inherit pytest's `pythonpath` ini setting.
    env = {**os.environ, 'PYTHONPATH': str(_IMPORT_ROOT)}

    running = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(backend.root),
                str(tmp_path / f'pwriter-{index}.git'),
                f'p{index}',
                str(PROCESS_APPENDS),
                str(GENEROUS_RETRIES),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for index in range(PROCESS_WRITERS)
    ]
    for process in running:
        _out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err.decode()

    _assert_all_survived(backend, tmp_path / 'verify.git', expected)


def _assert_all_survived(backend: sheaf.LocalBackend, git_dir: pathlib.Path, expected: set[str]) -> None:
    reader = conftest.GitRepo.open(backend, 'projects/demo', git_dir)
    lines = reader.read_log(ref=REF, path=LOG_PATH)

    assert len(lines) == len(expected), f'expected {len(expected)} lines, found {len(lines)}'
    assert set(lines) == expected
    assert len(set(lines)) == len(lines), 'a line was duplicated'
    assert len(reader.history(REF)) == len(expected), 'a commit was lost'

    store = sheaf.Store(backend, 'projects/demo')
    sequences = sorted(doc.sequence for doc in store.transitions())
    assert sequences == list(range(1, len(expected) + 1)), f'ref document sequence is not dense: {sequences}'


class LastWriteWinsBackend(sheaf.LocalBackend):
    """A backend with the precondition removed — what this design is defending against."""

    @override
    def cas_mutable(self, key: str, data: bytes, expected: sheaf.Generation | None) -> sheaf.Generation:
        """Swap against whatever is current, discarding what the caller last observed."""
        del expected
        try:
            current = self.get_mutable(key).generation
        except sheaf.NotFound:
            current = None
        return super().cas_mutable(key, data, current)


def test_the_harness_can_actually_fail(tmp_path: pathlib.Path) -> None:
    """Negative control: drop the precondition and a write really does vanish.

    Without this, a green concurrency suite proves only that the tests never raced.
    """
    sha_a, sha_b = 'a' * 40, 'b' * 40

    sound = sheaf.Store(sheaf.LocalBackend(tmp_path / 'sound'), 'p')
    base = sound.read()
    sound.publish(base, sheaf.Intent(ref_updates={'refs/heads/one': sheaf.RefUpdate(None, sha_a)}))
    try:
        sound.publish(base, sheaf.Intent(ref_updates={'refs/heads/two': sheaf.RefUpdate(None, sha_b)}))
    except sheaf.RaceLost:
        pass
    else:
        raise AssertionError('a stale publish was accepted')
    assert sound.read().refs == {'refs/heads/one': sha_a}

    broken = sheaf.Store(LastWriteWinsBackend(tmp_path / 'broken'), 'p')
    base = broken.read()
    broken.publish(base, sheaf.Intent(ref_updates={'refs/heads/one': sheaf.RefUpdate(None, sha_a)}))
    broken.publish(base, sheaf.Intent(ref_updates={'refs/heads/two': sheaf.RefUpdate(None, sha_b)}))
    # The second writer built on a snapshot that predated the first, so the first ref is simply gone.
    assert broken.read().refs == {'refs/heads/two': sha_b}
