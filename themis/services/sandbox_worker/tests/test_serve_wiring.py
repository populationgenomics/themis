"""`_serve` orchestration ordering, with every collaborator faked.

Asserts the sequence the worker must hold regardless of the SDK internals: verify the sandbox (fail-closed boot gate)
→ restore ``/workspace`` → ack the work item (restore proven) → serve the session → teardown (document checkpoint and
push) → clean up; that a store verdict at restore — the Sheaf service's included — acks + stops the item terminally
instead of serving; and that the mirror runs over a `RemoteStore` whose token file is the worker's alone. The dispatch
mechanics are covered in ``test_session_dispatch.py``; here only the wiring order and cleanup matter.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import stat
from typing import ClassVar, Self, cast

import grpc
import grpc.aio
import pytest
from anthropic.lib import environments
from anthropic.lib.tools import agent_toolset

from themis import sheaf
from themis.clients.sheaf import store as remote_mod
from themis.services.sandbox_worker import worker
from themis.sheaf.wire import bare

_REQUIRED_ENV = (
    'THEMIS_SESSION_TOKEN',
    'ANTHROPIC_ENVIRONMENT_KEY',
    'THEMIS_STORE_URL',
    'THEMIS_HELLO_URL',
    'THEMIS_EVIDENCE_URL',
    'THEMIS_SHEAF_URL',
    'ANTHROPIC_WORK_ID',
    'ANTHROPIC_ENVIRONMENT_ID',
    'ANTHROPIC_SESSION_ID',
)
# A loopback target nothing listens on: the channel is lazy, and the faked hatches never dial it. A `RemoteStore`
# refuses any other plain `host:port`, so the URL cannot be a placeholder like the rest.
_SHEAF_TARGET = '127.0.0.1:1'
_SESSION_TOKEN = 'value-for-THEMIS_SESSION_TOKEN'
_CLEANUP = {
    'hatch.close',
    'hatches.close',
    'hello_sync.close',
    'evidence_sync.close',
    'accessor.close',
    'sandbox.close',
}


class _Recorder(list[str]):
    def event(self, name: str) -> None:
        self.append(name)


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _REQUIRED_ENV:
        monkeypatch.setenv(name, f'value-for-{name}')
    monkeypatch.setenv('THEMIS_SHEAF_URL', _SHEAF_TARGET)
    monkeypatch.delenv(worker._HOST_UID_ENV, raising=False)
    monkeypatch.delenv(worker._HOST_GID_ENV, raising=False)


class _AioChannel:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Client:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


# Sentinel: the session loop is interrupted by SIGTERM, delivered to this process while it runs.
_SIGTERM = RuntimeError('SIGTERM')


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata())


async def _fail_session(serve_error: Exception | None) -> None:
    """End the faked session loop the way `serve_error` says: normally, by raising, or under SIGTERM."""
    if serve_error is _SIGTERM:
        # Only under the worker's own handler: with the default disposition this would kill pytest.
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)  # the handler cancels this task long before
    if serve_error is not None:
        raise serve_error


class _Mirrors(list[bare.BareRepo]):
    """Every mirror the faked hatches were built over, and what its token file looked like while serving."""

    def __init__(self) -> None:
        super().__init__()
        self.token_modes: list[int] = []

    def observe(self, mirror: bare.BareRepo) -> None:
        self.append(mirror)
        self.token_modes.append(stat.S_IMODE(pathlib.Path(mirror.store.descriptor()['token_file']).stat().st_mode))


def _patch_serve_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    log: _Recorder,
    tmp_path: pathlib.Path,
    *,
    restore_error: Exception | None = None,
    serve_error: Exception | None = None,
    mirrors: _Mirrors | None = None,
) -> dict[str, list[str]]:
    """Replace every ``_serve`` collaborator with an event-logging fake.

    ``restore_error`` makes the workspace restore raise it; ``serve_error`` makes the session loop raise it;
    ``mirrors`` records the mirror the hatches were built over. Returns a record of the work ids passed to
    ``ack`` / ``stop``, so a test can assert the *work* item (not the session) is acked.
    """
    acked: dict[str, list[str]] = {'ack': [], 'stop': []}
    sync_tags = iter(('hello_sync.close', 'evidence_sync.close'))

    class _Accessor:
        def close(self) -> None:
            log.event('accessor.close')

    class _Sandbox:
        def __init__(self, _profile: object, *, hatch: object = None) -> None:
            self.workspace = tmp_path
            self._serving = hatch is not None

        def verify(self, *, timeout: float = 30) -> None:
            del timeout
            log.event('verify')

        def accessor(self) -> _Accessor:
            return _Accessor()

        def close(self) -> None:
            if self._serving:
                log.event('sandbox.close')

    class _WorkspaceSync:
        def __init__(self, _store: object, **_kwargs: object) -> None: ...

        async def restore(self) -> None:
            log.event('restore')
            if restore_error is not None:
                raise restore_error

        async def teardown(self, session_id: str) -> None:
            await asyncio.sleep(0)  # a pending cancellation would land here, not on the log line
            log.event(f'teardown:{session_id}')

    class _WorkQueue:
        def __init__(self, _client: object, *, environment_id: str) -> None:
            del environment_id

        async def ack(self, work_id: str) -> None:
            log.event('ack')
            acked['ack'].append(work_id)

        async def stop(self, work_id: str) -> None:
            log.event('stop')
            acked['stop'].append(work_id)

    class _Worker:
        def __init__(self, _client: object, **_kwargs: object) -> None: ...

        async def handle_item(self) -> None:
            log.event('serve')
            await _fail_session(serve_error)

    class _SyncChannel:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        def close(self) -> None:
            log.event(self._tag)

    class _Hatch:
        def close(self) -> None:
            log.event('hatch.close')

    monkeypatch.setattr(worker.postern, 'Sandbox', _Sandbox)
    monkeypatch.setattr(worker.id_token, 'channel_credentials', lambda _url: object())
    monkeypatch.setattr(worker.grpc.aio, 'secure_channel', lambda *_a, **_k: _AioChannel())
    monkeypatch.setattr(worker.grpc, 'secure_channel', lambda *_a, **_k: _SyncChannel(next(sync_tags)))
    monkeypatch.setattr(worker.hatch_mod, 'build_hatch', lambda *_a, **_k: _Hatch())
    monkeypatch.setattr(worker.store_client, 'GrpcStore', lambda *_a, **_k: object())
    monkeypatch.setattr(worker.sync_mod, 'WorkspaceSync', _WorkspaceSync)
    monkeypatch.setattr(worker.work_queue_mod, 'AnthropicWorkQueue', _WorkQueue)
    monkeypatch.setattr(worker.tool_mod, 'make_shell', lambda *_a, **_k: object())
    monkeypatch.setattr(worker.anthropic, 'AsyncAnthropic', lambda **_k: _Client())
    monkeypatch.setattr(worker.environments, 'EnvironmentWorker', _Worker)
    _patch_repository_collaborators(monkeypatch, log, mirrors=mirrors)
    return acked


def _patch_repository_collaborators(
    monkeypatch: pytest.MonkeyPatch, log: _Recorder, *, mirrors: _Mirrors | None
) -> None:
    """Fake the hatches and the guest's git over the mirror; the mirror itself, and its store, are real."""

    class _GitHatches:
        hatches: ClassVar[list[object]] = []

        def __init__(self, mirror: bare.BareRepo, **_kwargs: object) -> None:
            if mirrors is not None:
                mirrors.observe(mirror)

        def guest_urls(self, _profile: object) -> tuple[str, str]:
            return 'ext::fetch', 'ext::push'

        def close(self) -> None:
            log.event('hatches.close')

    monkeypatch.setattr(worker.git_hatches, 'GitHatches', _GitHatches)
    monkeypatch.setattr(worker.guest_git, 'GuestGit', lambda *_a, **_k: object())


@pytest.mark.usefixtures('_env')
def test_serve_orders_verify_restore_ack_serve_teardown_then_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    log = _Recorder()
    acked = _patch_serve_collaborators(monkeypatch, log, tmp_path)

    asyncio.run(worker._serve())

    assert log[:5] == ['verify', 'restore', 'ack', 'serve', 'teardown:value-for-ANTHROPIC_SESSION_ID']
    assert set(log[5:]) == _CLEANUP
    # the WORK item is acked (not the session) — acking the wrong id leaves the real item reclaimable.
    assert acked['ack'] == ['value-for-ANTHROPIC_WORK_ID']
    assert acked['stop'] == []


@pytest.mark.usefixtures('_env')
def test_the_mirror_runs_over_the_sheaf_service_with_a_token_file_of_the_workers_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # The worker's one credential for the repository is the session token, presented to the Sheaf service; the
    # hook finds it by the path in the store's descriptor, never in an environment or the sync state itself.
    log = _Recorder()
    mirrors = _Mirrors()
    _patch_serve_collaborators(monkeypatch, log, tmp_path, mirrors=mirrors)

    asyncio.run(worker._serve())

    (mirror,) = mirrors
    assert isinstance(mirror.store, remote_mod.RemoteStore)
    descriptor = mirror.store.descriptor()
    assert descriptor['target'] == _SHEAF_TARGET
    token_file = pathlib.Path(descriptor['token_file'])
    # Under the mirror root, which is never bound into the guest; in particular nowhere under /workspace.
    assert token_file.parent == mirror.path.parent
    assert not token_file.is_relative_to(tmp_path)
    assert mirrors.token_modes == [0o600]
    assert _SESSION_TOKEN not in bare.SyncState(generation=None, store=descriptor).to_json()
    # Gone with the mirror once the session is over.
    assert not token_file.exists()
    assert not mirror.path.parent.exists()


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(
    'restore_error',
    [
        _rpc_error(grpc.StatusCode.PERMISSION_DENIED),
        _rpc_error(grpc.StatusCode.INTERNAL),
        sheaf.CorruptRepository('names a pack that is gone'),
        sheaf.ServiceFault('PERMISSION_DENIED', 'the session token does not resolve'),
        sheaf.ServiceFault('UNAUTHENTICATED', 'no session token on the call'),
    ],
    ids=[
        'document rpc refuses the token',
        'document rpc fails',
        'repository store',
        'token refused by the sheaf service',
        'no token reached the service',
    ],
)
def test_serve_acks_and_stops_the_item_on_a_store_verdict_without_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, restore_error: Exception
) -> None:
    # A store verdict at restore is terminal: ack (stop reclaim) + stop (end the item) and never serve, else the
    # item would sit unacked and be reclaimed into the same failure on the next drain.
    log = _Recorder()
    acked = _patch_serve_collaborators(monkeypatch, log, tmp_path, restore_error=restore_error)

    asyncio.run(worker._serve())

    assert log[:4] == ['verify', 'restore', 'ack', 'stop']
    assert 'serve' not in log
    assert not any(event.startswith('teardown') for event in log)
    assert set(log[4:]) == _CLEANUP
    assert acked['ack'] == ['value-for-ANTHROPIC_WORK_ID']
    assert acked['stop'] == ['value-for-ANTHROPIC_WORK_ID']


@pytest.mark.usefixtures('_env')
def test_a_failed_session_loop_still_tears_down_before_the_error_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # What the agent committed is not the SDK's to lose: the push runs on the way out, then the failure surfaces.
    log = _Recorder()
    _patch_serve_collaborators(monkeypatch, log, tmp_path, serve_error=RuntimeError('lease lost'))

    with pytest.raises(RuntimeError, match='lease lost'):
        asyncio.run(worker._serve())

    assert log[:5] == ['verify', 'restore', 'ack', 'serve', 'teardown:value-for-ANTHROPIC_SESSION_ID']
    assert set(log[5:]) == _CLEANUP


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(
    'restore_error',
    [
        RuntimeError('clone failed'),
        sheaf.ServiceFault('UNAVAILABLE', 'the sheaf service could not be reached'),
        _rpc_error(grpc.StatusCode.UNAVAILABLE),
        _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED),
        sheaf.CredentialsUnusable('the token file cannot be read'),
    ],
    ids=['guest git', 'sheaf service unreachable', 'store unreachable', 'store out of time', 'token file unreadable'],
)
def test_serve_leaves_the_item_unacked_on_a_fault_that_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, restore_error: Exception
) -> None:
    # Not a store verdict: a fresh spawn may clear it or outlive an outage, so the item stays reclaimable and the
    # error propagates.
    log = _Recorder()
    mirrors = _Mirrors()
    acked = _patch_serve_collaborators(monkeypatch, log, tmp_path, restore_error=restore_error, mirrors=mirrors)

    with pytest.raises(type(restore_error)) as raised:
        asyncio.run(worker._serve())

    assert raised.value is restore_error

    assert acked == {'ack': [], 'stop': []}
    assert set(log[2:]) == _CLEANUP
    assert not pathlib.Path(mirrors[0].store.descriptor()['token_file']).exists()  # cleaned on the failure path too


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize('missing', _REQUIRED_ENV)
def test_serve_fails_loud_on_a_missing_env_var(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    # a required value absent at boot must raise before any sandbox launch, not fail deep in the SDK
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_host_identity_is_both_or_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    assert worker._host_identity() is None
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    with pytest.raises(SystemExit, match=worker._HOST_GID_ENV):
        worker._host_identity()
    monkeypatch.setenv(worker._HOST_GID_ENV, '61000')
    assert worker._host_identity() == (61000, 61000)
    monkeypatch.setenv(worker._HOST_GID_ENV, 'nobody')
    with pytest.raises(SystemExit, match='integers'):
        worker._host_identity()


def test_the_profile_carries_the_host_identity_into_bwraps_credentials() -> None:
    profile = worker._build_profile((61000, 61001))
    assert (profile.host_uid, profile.host_gid) == (61000, 61001)
    assert (worker._build_profile(None).host_uid, worker._build_profile(None).host_gid) == (None, None)


def test_session_tools_drop_bash_keep_file_tools_and_append_shell(tmp_path: pathlib.Path) -> None:
    # Only arbitrary execution (bash) is sandboxed; the workdir-confined file tools run in the trusted worker.
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path)
    shell = cast('environments.BetaAnyRunnableTool', object())  # appended verbatim as the final tool
    tools = worker._tools_for_session(ctx, shell)
    file_names = {tool.name for tool in tools if tool is not shell}
    assert file_names == {'read', 'write', 'edit', 'glob', 'grep'}
    assert tools[-1] is shell


@pytest.mark.usefixtures('_env')
def test_sigterm_cancels_the_session_and_teardown_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Cloud Run's task timeout and cancellation arrive as SIGTERM; the push has to fit in before SIGKILL.
    log = _Recorder()
    _patch_serve_collaborators(monkeypatch, log, tmp_path, serve_error=_SIGTERM)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._serve_until_terminated())

    assert log[:5] == ['verify', 'restore', 'ack', 'serve', 'teardown:value-for-ANTHROPIC_SESSION_ID']
    assert set(log[5:]) == _CLEANUP
