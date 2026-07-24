"""`_serve` orchestration ordering, with every collaborator faked.

Asserts the sequence the worker must hold regardless of the SDK internals: verify the sandbox (fail-closed boot gate)
→ restore ``/workspace`` → ack the work item (restore proven) → serve the session → final checkpoint → tear down; and
that a restore failure acks + stops the item terminally instead of serving. The dispatch mechanics are covered in
``test_session_dispatch.py``; here only the wiring order and cleanup matter.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Self, cast

import grpc
import grpc.aio
import pytest
from anthropic.lib import environments
from anthropic.lib.tools import agent_toolset

from themis.services.sandbox_worker import worker


class _Recorder(list[str]):
    def event(self, name: str) -> None:
        self.append(name)


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        'THEMIS_SESSION_TOKEN',
        'ANTHROPIC_ENVIRONMENT_KEY',
        'THEMIS_STORE_URL',
        'THEMIS_HELLO_URL',
        'ANTHROPIC_WORK_ID',
        'ANTHROPIC_ENVIRONMENT_ID',
        'ANTHROPIC_SESSION_ID',
    ):
        monkeypatch.setenv(name, f'value-for-{name}')


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


def _patch_serve_collaborators(
    monkeypatch: pytest.MonkeyPatch, log: _Recorder, tmp_path: pathlib.Path, *, restore_error: Exception | None = None
) -> dict[str, list[str]]:
    """Replace every ``_serve`` collaborator with an event-logging fake.

    ``restore_error`` makes the workspace restore raise it, to drive the failure path. Returns a record of the
    work ids passed to ``ack`` / ``stop``, so a test can assert the *work* item (not the session) is acked.
    """
    acked: dict[str, list[str]] = {'ack': [], 'stop': []}
    sync_tags = iter(('hello_sync.close',))

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

        async def checkpoint(self) -> None:
            log.event('checkpoint')

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
    return acked


@pytest.mark.usefixtures('_env')
def test_serve_orders_verify_restore_ack_serve_checkpoint_then_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    log = _Recorder()
    acked = _patch_serve_collaborators(monkeypatch, log, tmp_path)

    asyncio.run(worker._serve())

    assert log[:5] == ['verify', 'restore', 'ack', 'serve', 'checkpoint']
    assert set(log[5:]) == {'hatch.close', 'hello_sync.close', 'accessor.close', 'sandbox.close'}
    # the WORK item is acked (not the session) — acking the wrong id leaves the real item reclaimable.
    assert acked['ack'] == ['value-for-ANTHROPIC_WORK_ID']
    assert acked['stop'] == []


@pytest.mark.usefixtures('_env')
def test_serve_acks_and_stops_the_item_on_store_restore_error_without_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # A store restore error is terminal: ack (stop reclaim) + stop (end the item) and never serve, else the
    # item would sit unacked and be reclaimed into the same failure on the next drain.
    log = _Recorder()
    restore_error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata())
    acked = _patch_serve_collaborators(monkeypatch, log, tmp_path, restore_error=restore_error)

    asyncio.run(worker._serve())

    assert log[:4] == ['verify', 'restore', 'ack', 'stop']
    assert 'serve' not in log
    assert 'checkpoint' not in log
    assert set(log[4:]) == {'hatch.close', 'hello_sync.close', 'accessor.close', 'sandbox.close'}
    assert acked['ack'] == ['value-for-ANTHROPIC_WORK_ID']
    assert acked['stop'] == ['value-for-ANTHROPIC_WORK_ID']


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(
    'missing',
    [
        'THEMIS_SESSION_TOKEN',
        'ANTHROPIC_ENVIRONMENT_KEY',
        'THEMIS_STORE_URL',
        'THEMIS_HELLO_URL',
        'ANTHROPIC_WORK_ID',
        'ANTHROPIC_ENVIRONMENT_ID',
        'ANTHROPIC_SESSION_ID',
    ],
)
def test_serve_fails_loud_on_a_missing_env_var(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    # a required value absent at boot must raise before any sandbox launch, not fail deep in the SDK
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(worker._serve())


def test_session_tools_drop_bash_keep_file_tools_and_append_shell(tmp_path: pathlib.Path) -> None:
    # Only arbitrary execution (bash) is sandboxed; the workdir-confined file tools run in the trusted worker.
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path)
    shell = cast('environments.BetaAnyRunnableTool', object())  # appended verbatim as the final tool
    tools = worker._tools_for_session(ctx, shell)
    file_names = {tool.name for tool in tools if tool is not shell}
    assert file_names == {'read', 'write', 'edit', 'glob', 'grep'}
    assert tools[-1] is shell
