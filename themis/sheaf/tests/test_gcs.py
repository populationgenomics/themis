"""The GCS backend against fake-gcs-server: the compare-and-swap, under contention, for real.

`test_backend_contract.py` runs the primitive contract against this backend; what is here is
everything built on top of it — the publish protocol, the concurrency harness, and a full
clone-and-push through real git — exercised against an actual implementation of the GCS JSON API
rather than a local-directory stand-in.

One gap is pinned rather than hidden: fake-gcs-server does not implement object versioning
(`not implemented: fs storage type does not support versioning yet`, and its memory backend accepts
the setting while retaining nothing), so nothing here covers the ref-state log, and
`test_history_is_unavailable_without_versioning` fails the day the emulator catches up.
"""

from __future__ import annotations

import pathlib
import subprocess
from concurrent import futures

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis import sheaf
from themis.sheaf.backends import gcs
from themis.sheaf.tests import conftest
from themis.sheaf.wire import protect, server

REPO, REF = 'projects/case', 'refs/heads/main'
LOG = 'annotations/assertions.jsonl'
REVIEWER = conftest.Author('Reviewer One', 'reviewer.one@example.org')
SHA_A, SHA_B = 'a' * 40, 'b' * 40
WRITERS, APPENDS = 6, 3


def test_publish_and_read_back(gcs_backend: gcs.GcsBackend) -> None:
    store = sheaf.Store(gcs_backend, REPO)
    assert store.read().generation is None
    after = store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'])),
    )
    assert after.tip(REF) == SHA_A
    assert store.read().packs == after.packs


def test_a_stale_snapshot_loses(gcs_backend: gcs.GcsBackend) -> None:
    """GCS generations are opaque and non-sequential, so this is the case worth checking for real."""
    store = sheaf.Store(gcs_backend, REPO)
    stale = store.read()
    store.publish(stale, conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)})))
    with pytest.raises(sheaf.RaceLost):
        store.publish(
            stale, conftest.logged(stale, sheaf.Intent(ref_updates={'refs/heads/other': sheaf.RefUpdate(None, SHA_B)}))
        )


def test_generations_are_treated_as_opaque(gcs_backend: gcs.GcsBackend) -> None:
    """A real GCS generation is a microsecond timestamp, not a counter. Nothing may assume otherwise."""
    store = sheaf.Store(gcs_backend, REPO)
    first = store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )
    second = store.publish(
        first, conftest.logged(first, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}))
    )
    assert second.generation != first.generation
    assert second.generation is not None
    assert second.generation > 1_000_000, 'a GCS generation is not a small dense integer'


def test_a_commit_published_straight_to_the_store(gcs_backend: gcs.GcsBackend, tmp_path: pathlib.Path) -> None:
    writer = conftest.GitRepo.open(gcs_backend, REPO, tmp_path / 'writer.git')
    writer.append_line(ref=REF, path=LOG, line='{"code":"PM2"}', author=REVIEWER, message='review PM2')
    writer.append_line(ref=REF, path=LOG, line='{"code":"PP3"}', author=REVIEWER, message='review PP3')

    cold = conftest.GitRepo.open(gcs_backend, REPO, tmp_path / 'cold.git')
    assert cold.read_log(ref=REF, path=LOG) == ['{"code":"PM2"}', '{"code":"PP3"}']
    assert len(cold.history(REF)) == 2


def test_no_lost_updates_against_a_real_precondition(gcs_backend: gcs.GcsBackend, tmp_path: pathlib.Path) -> None:
    """The harness that gates everything, run against GCS semantics instead of a local filesystem."""
    expected = {f'w{w}:{i}' for w in range(WRITERS) for i in range(APPENDS)}

    def worker(index: int) -> None:
        writer = conftest.GitRepo.open(gcs_backend, REPO, tmp_path / f'writer-{index}.git', retries=200)
        author = conftest.Author(f'w{index}', f'w{index}@example.org')
        for i in range(APPENDS):
            writer.append_line(ref=REF, path=LOG, line=f'w{index}:{i}', author=author, message=f'mark {i}')

    with futures.ThreadPoolExecutor(max_workers=WRITERS) as pool:
        for done in [pool.submit(worker, i) for i in range(WRITERS)]:
            done.result()

    reader = conftest.GitRepo.open(gcs_backend, REPO, tmp_path / 'verify.git')
    lines = reader.read_log(ref=REF, path=LOG)
    assert set(lines) == expected
    assert len(lines) == len(expected), 'a line was dropped or duplicated'
    assert len(reader.history(REF)) == len(expected)


def test_history_is_unavailable_without_versioning(gcs_backend: gcs.GcsBackend) -> None:
    """fake-gcs-server does not implement object versioning, so the reflog cannot be covered here.

    Recorded as a test so the absence is a stated fact rather than an assumption nobody rechecked.
    """
    store = sheaf.Store(gcs_backend, REPO)
    snapshot = store.read()
    for sha in (SHA_A, SHA_B):
        snapshot = store.publish(
            snapshot,
            conftest.logged(
                snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(snapshot.tip(REF), sha)}, packs=[sha.encode()])
            ),
        )
    assert len(store.transitions()) == 1, 'if this ever fails, the emulator gained versioning — widen the tests'


def test_clone_and_push_against_gcs(
    gcs_backend: gcs.GcsBackend,
    gcs_endpoint: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real git, real hook subprocess, real GCS API: the whole path in one test."""
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', gcs_endpoint)

    writer = conftest.GitRepo.open(gcs_backend, REPO, tmp_path / 'writer.git')
    writer.append_line(ref=REF, path=LOG, line='{"code":"PM2"}', author=REVIEWER, message='review PM2')

    git = ['git', '-c', 'user.email=agent@x', '-c', 'user.name=Agent', '-c', 'init.defaultBranch=main']
    with server.SheafGitServer.over_backend(
        gcs_backend, tmp_path / 'bare', repos={REPO}, protection=protect.Protection.unprotected()
    ) as instance:
        work = tmp_path / 'work'
        subprocess.run(
            [*git, 'clone', '-q', instance.url(REPO), str(work)], capture_output=True, check=True, timeout=180
        )
        assert (work / LOG).read_text('utf-8') == '{"code":"PM2"}\n'

        (work / 'documents').mkdir()
        (work / 'documents' / 'report.md').write_text('drafted by the pushing side\n', 'utf-8')
        subprocess.run([*git, 'add', '-A'], cwd=work, capture_output=True, check=True)
        subprocess.run([*git, 'commit', '-m', 'draft'], cwd=work, capture_output=True, check=True)
        pushed = subprocess.run(
            [*git, 'push', 'origin', 'main'], cwd=work, capture_output=True, text=True, timeout=180, check=False
        )
        assert pushed.returncode == 0, pushed.stderr

    assert writer.read_log(ref=REF, path=LOG) == ['{"code":"PM2"}']
    assert len(sheaf.Store(gcs_backend, REPO).read().packs) == 2


def test_each_request_is_a_span_of_the_operation_it_serves(
    gcs_backend: gcs.GcsBackend, recorded_spans: in_memory_span_exporter.InMemorySpanExporter
) -> None:
    gcs_backend.cas_mutable('refs', b'doc', None)
    gcs_backend.put_immutable('packs/p', b'PACK')
    recorded_spans.clear()

    with trace.get_tracer(__name__).start_as_current_span('rpc'):
        gcs_backend.get_mutable('refs')
        gcs_backend.put_immutable('packs/p', b'PACK')

    spans = recorded_spans.get_finished_spans()
    (rpc,) = [s for s in spans if s.name == 'rpc']
    assert rpc.context is not None
    requests = [s for s in spans if s is not rpc]
    assert [s.name for s in requests] == ['gcs.get_blob', 'gcs.download', 'gcs.upload']
    assert all(s.parent is not None and s.parent.span_id == rpc.context.span_id for s in requests)
    assert all(s.kind is trace.SpanKind.CLIENT for s in requests)
    assert all(s.attributes is not None and s.attributes['gcs.bucket'] == gcs_backend.bucket.name for s in requests)
    # A create that finds the object already there is the outcome asked for, not a failed request.
    (upload,) = [s for s in requests if s.name == 'gcs.upload']
    assert upload.status.is_unset
    assert upload.attributes is not None
    assert upload.attributes['gcs.already_present'] is True
