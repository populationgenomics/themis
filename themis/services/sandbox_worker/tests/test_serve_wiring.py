"""`_serve` orchestration, sandbox profile, and session tool wiring, with external collaborators faked.

Asserts the sequence the worker must hold regardless of the SDK internals: verify the sandbox (fail-closed boot gate)
→ restore ``/workspace`` → ack the work item (restore proven) → serve the session → teardown (the push) → clean up;
that a repository verdict at restore acks + stops the item terminally instead of serving; that the mirror runs over a
`RemoteStore` whose token file is the worker's alone; and that `_serve` wires the host uid mapping, required request
bind, hatchless guest file tools, hatched `shell` tool, tool spans, memory-store refusal, and skills permissions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import signal
import stat
import typing
from collections import abc

import postern
import pytest
from anthropic.lib import environments, tools
from anthropic.lib.tools import agent_toolset
from anthropic.types import beta
from anthropic.types.beta import sessions
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis import sheaf
from themis.clients.sheaf import store as remote_mod
from themis.services.sandbox_worker import file_tools, worker
from themis.sheaf.wire import bare

_REQUIRED_ENV = (
    'THEMIS_SESSION_TOKEN',
    'ANTHROPIC_ENVIRONMENT_KEY',
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
    'sandbox.close',
    'file_sandbox.close',
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


class _Client:
    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


# Sentinel: the session loop is interrupted by SIGTERM, delivered to this process while it runs.
_SIGTERM = RuntimeError('SIGTERM')


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


type _ToolsFactory = abc.Callable[[agent_toolset.AgentToolContext], abc.Sequence[environments.BetaAnyRunnableTool]]


@dataclasses.dataclass
class _Inspection:
    """Observations recorded from ``_serve``'s faked collaborators during one run."""

    profiles: list[postern.SandboxProfile] = dataclasses.field(default_factory=list)
    request_dirs_present_at_verify: list[bool] = dataclasses.field(default_factory=list)
    hatchless_runs: list[tuple[list[str], str]] = dataclasses.field(default_factory=list)
    hatched_python_runs: list[str] = dataclasses.field(default_factory=list)
    memory_sync_intervals: list[float | None] = dataclasses.field(default_factory=list)


def _sandbox_cls(tmp_path: pathlib.Path, log: _Recorder, inspection: _Inspection | None) -> type:
    class _Sandbox:
        def __init__(self, profile: postern.SandboxProfile, *, hatch: object = None) -> None:
            self.workspace = tmp_path
            self._profile = profile
            self._serving = hatch is not None
            if inspection is not None:
                inspection.profiles.append(profile)

        def verify(self, *, timeout: float = 30) -> None:
            del timeout
            log.event('verify')
            if inspection is not None:
                inspection.request_dirs_present_at_verify.extend(
                    pathlib.Path(host).is_dir()
                    for host, guest in self._profile.required_ro_binds
                    if guest == file_tools.GUEST_REQUESTS
                )

        def run(self, argv: list[str], *, timeout: float = 60, max_output: int | None = None) -> postern.ProcResult:
            del timeout, max_output
            guest_request = pathlib.PurePosixPath(argv[-1])
            binds = {guest: host for host, guest in self._profile.required_ro_binds}
            host_request = pathlib.Path(binds[str(guest_request.parent)]) / guest_request.name
            payload = json.loads(host_request.read_text('utf-8'))
            tool_name = str(payload['tool'])
            if not self._serving and inspection is not None:
                inspection.hatchless_runs.append((argv[:3], tool_name))
            return postern.ProcResult(0, json.dumps({'content': f'ran:{tool_name}'}), '')

        def run_python(self, code: str, *, timeout: float = 60) -> postern.ProcResult:
            del timeout
            if self._serving and inspection is not None:
                inspection.hatched_python_runs.append(code)
            return postern.ProcResult(0, 'shell ok', '')

        def close(self) -> None:
            log.event('sandbox.close' if self._serving else 'file_sandbox.close')

    return _Sandbox


def _patch_serve_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    log: _Recorder,
    tmp_path: pathlib.Path,
    *,
    restore_error: Exception | None = None,
    serve_error: Exception | None = None,
    mirrors: _Mirrors | None = None,
    inspection: _Inspection | None = None,
    on_session: abc.Callable[[_ToolsFactory], abc.Coroutine[object, object, None]] | None = None,
) -> dict[str, list[str]]:
    """Replace every ``_serve`` collaborator with an event-logging fake.

    ``restore_error`` makes the workspace restore raise it; ``serve_error`` makes the session loop raise it;
    ``mirrors`` records the mirror the hatches were built over. Returns a record of the work ids passed to
    ``ack`` / ``stop``, so a test can assert the *work* item (not the session) is acked.
    """
    acked: dict[str, list[str]] = {'ack': [], 'stop': []}
    sync_tags = iter(('hello_sync.close', 'evidence_sync.close'))

    class _WorkspaceSync:
        def __init__(self, **_kwargs: object) -> None: ...

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
        def __init__(
            self,
            _client: object,
            *,
            tools: abc.Callable[[agent_toolset.AgentToolContext], abc.Sequence[environments.BetaAnyRunnableTool]],
            workdir: pathlib.Path,
            memory_sync_interval: float | None = environments.DEFAULT_MEMORY_SYNC_INTERVAL,
        ) -> None:
            self._tools = tools
            self._workdir = workdir
            if inspection is not None:
                inspection.memory_sync_intervals.append(memory_sync_interval)

        async def handle_item(self) -> None:
            log.event('serve')
            if on_session is not None:
                await on_session(self._tools)
            await _fail_session(serve_error)

    class _SyncChannel:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        def close(self) -> None:
            log.event(self._tag)

    class _Hatch:
        def close(self) -> None:
            log.event('hatch.close')

    monkeypatch.setattr(worker.postern, 'Sandbox', _sandbox_cls(tmp_path, log, inspection))
    monkeypatch.setattr(worker.id_token, 'channel_credentials', lambda _url: object())
    monkeypatch.setattr(worker.grpc, 'secure_channel', lambda *_a, **_k: _SyncChannel(next(sync_tags)))
    monkeypatch.setattr(worker.hatch_mod, 'build_hatch', lambda *_a, **_k: _Hatch())
    monkeypatch.setattr(worker.sync_mod, 'WorkspaceSync', _WorkspaceSync)
    monkeypatch.setattr(worker.work_queue_mod, 'AnthropicWorkQueue', _WorkQueue)
    monkeypatch.setattr(worker.anthropic, 'AsyncAnthropic', lambda **_k: _Client())
    monkeypatch.setattr(worker.environments, 'EnvironmentWorker', _Worker)
    _patch_repository_collaborators(monkeypatch, log, mirrors=mirrors)
    return acked


def _patch_repository_collaborators(
    monkeypatch: pytest.MonkeyPatch, log: _Recorder, *, mirrors: _Mirrors | None
) -> None:
    """Fake the hatches and the guest's git over the mirror; the mirror itself, and its store, are real."""

    class _GitHatches:
        hatches: typing.ClassVar[list[object]] = []

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
        sheaf.CorruptRepository('names a pack that is gone'),
        sheaf.ServiceFault('PERMISSION_DENIED', 'the session token does not resolve'),
        sheaf.ServiceFault('UNAUTHENTICATED', 'no session token on the call'),
    ],
    ids=[
        'repository store',
        'token refused by the sheaf service',
        'no token reached the service',
    ],
)
def test_serve_acks_and_stops_the_item_on_a_repository_verdict_without_serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    restore_error: Exception,
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    # A verdict at restore is terminal: ack (stop reclaim) + stop (end the item) and never serve, else the
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
    # The session never served, so its trace says it failed rather than ending clean.
    (session,) = recorded_spans.get_finished_spans()
    assert session.status.status_code is trace.StatusCode.ERROR
    assert session.attributes is not None
    assert session.attributes['error.type'] == worker._RESTORE_REFUSED


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
        sheaf.CredentialsUnusable('the token file cannot be read'),
    ],
    ids=['guest git', 'sheaf service unreachable', 'token file unreadable'],
)
def test_serve_leaves_the_item_unacked_on_a_fault_that_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, restore_error: Exception
) -> None:
    # Not a verdict: a fresh spawn may clear it or outlive an outage, so the item stays reclaimable and the
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
@pytest.mark.parametrize(
    'present',
    [worker._HOST_UID_ENV, worker._HOST_GID_ENV],
)
def test_serve_requires_host_uid_and_gid_together(monkeypatch: pytest.MonkeyPatch, present: str) -> None:
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setenv(present, '61000')
    with pytest.raises(SystemExit, match='set together'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(('uid', 'gid'), [('nobody', '61000'), ('61000', 'nobody')])
def test_serve_requires_integer_host_uid_and_gid(monkeypatch: pytest.MonkeyPatch, uid: str, gid: str) -> None:
    monkeypatch.setenv(worker._HOST_UID_ENV, uid)
    monkeypatch.setenv(worker._HOST_GID_ENV, gid)
    with pytest.raises(SystemExit, match='integers'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_leaves_host_identity_unset_in_the_profile_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    inspection = _Inspection()
    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection)

    asyncio.run(worker._serve())

    assert len(inspection.profiles) == 3
    assert {(profile.host_uid, profile.host_gid) for profile in inspection.profiles} == {(None, None)}


@pytest.mark.usefixtures('_env')
def test_serve_carries_the_configured_host_identity_into_the_sandbox_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61001')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    inspection = _Inspection()
    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection)

    asyncio.run(worker._serve())

    assert len(inspection.profiles) == 3
    assert {(profile.host_uid, profile.host_gid) for profile in inspection.profiles} == {(61000, 61001)}


@pytest.mark.usefixtures('_env')
def test_serve_requires_the_file_tools_request_directory_in_the_sandbox_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # A required bind, not a -try one: a launch without the directory fails instead of the tool failing in the guest.
    inspection = _Inspection()
    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection)

    asyncio.run(worker._serve())

    assert len(inspection.profiles) == 3
    bound = {tuple(profile.required_ro_binds) for profile in inspection.profiles}
    [(required_binds,)] = [[b for b in binds if b[1] == file_tools.GUEST_REQUESTS] for binds in bound]
    host_requests = required_binds[0]
    assert all(file_tools.GUEST_REQUESTS not in dict(profile.ro_binds).values() for profile in inspection.profiles)
    assert inspection.request_dirs_present_at_verify == [True]
    assert not pathlib.Path(host_requests).exists()


def _session_with(*resources: object) -> beta.BetaManagedAgentsSession:
    return beta.BetaManagedAgentsSession.model_construct(id='sesn_1', resources=list(resources))


@pytest.mark.usefixtures('_env')
def test_serve_runs_each_session_file_tool_in_the_hatchless_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    inspection = _Inspection()
    file = sessions.BetaManagedAgentsFileResource.model_construct(type='file', file_id='file_1')
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with(file))
    served_names: list[str] = []
    results: dict[str, object] = {}

    async def call_file_tools(factory: _ToolsFactory) -> None:
        for tool in factory(ctx):
            served_names.append(tool.name)
            if tool.name != 'shell':
                assert isinstance(tool, tools.BetaAsyncBuiltinFunctionTool)
                results[tool.name] = await tool.call({'path': 'notes.txt'})

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection, on_session=call_file_tools)

    asyncio.run(worker._serve())

    expected_names = {'read', 'write', 'edit', 'glob', 'grep'}
    assert len(served_names) == 6
    assert set(served_names) == expected_names | {'shell'}
    assert results == {name: f'ran:{name}' for name in expected_names}
    assert {name for _, name in inspection.hatchless_runs} == expected_names
    assert {tuple(entrypoint) for entrypoint, _ in inspection.hatchless_runs} == {worker._GUEST_FILE_TOOL}
    assert inspection.hatched_python_runs == []


@pytest.mark.usefixtures('_env')
def test_serve_runs_the_session_shell_tool_in_the_hatched_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    inspection = _Inspection()
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with())
    outputs: list[object] = []

    async def call_shell(factory: _ToolsFactory) -> None:
        built = {tool.name: tool for tool in factory(ctx)}
        shell_tool = built['shell']
        assert isinstance(shell_tool, tools.BetaAsyncBuiltinFunctionTool)
        outputs.append(await shell_tool.call({'command': 'echo hi', 'intent': 'greet'}))

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection, on_session=call_shell)

    asyncio.run(worker._serve())

    assert outputs == ['shell ok']
    assert len(inspection.hatched_python_runs) == 1
    assert "'echo hi'" in inspection.hatched_python_runs[0]
    assert inspection.hatchless_runs == []


@pytest.mark.usefixtures('_env')
def test_serve_records_each_session_tool_call_as_a_child_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with())

    async def call_every_tool(factory: _ToolsFactory) -> None:
        for tool in factory(ctx):
            assert isinstance(tool, tools.BetaAsyncBuiltinFunctionTool)
            payload = {'command': 'echo hi', 'intent': 'greet'} if tool.name == 'shell' else {'path': 'notes.txt'}
            await tool.call(payload)

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=call_every_tool)

    asyncio.run(worker._serve())

    spans = recorded_spans.get_finished_spans()
    [session_span] = [span for span in spans if span.name == 'sandbox session']
    assert session_span.context is not None
    tool_spans = [span for span in spans if span is not session_span]
    assert {span.name for span in tool_spans} == {
        'execute_tool read',
        'execute_tool write',
        'execute_tool edit',
        'execute_tool glob',
        'execute_tool grep',
        'execute_tool shell',
    }
    assert all(span.parent is not None and span.parent.span_id == session_span.context.span_id for span in tool_spans)


@pytest.mark.usefixtures('_env')
def test_serve_refuses_a_toolset_the_guest_does_not_serve(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_toolset = agent_toolset.beta_agent_toolset_20260401
    monkeypatch.setattr(agent_toolset, 'beta_agent_toolset_20260401', lambda ctx: sdk_toolset(ctx)[:-1])
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with())

    async def build(factory: _ToolsFactory) -> None:
        factory(ctx)

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=build)

    with pytest.raises(RuntimeError, match='not the file tools'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_disables_worker_memory_sync(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inspection = _Inspection()
    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, inspection=inspection)

    asyncio.run(worker._serve())

    assert inspection.memory_sync_intervals == [None]


@pytest.mark.usefixtures('_env')
def test_serve_refuses_a_session_with_a_memory_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = sessions.BetaManagedAgentsMemoryStoreResource.model_construct(type='memory_store', memory_store_id='m_1')
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with(store))

    async def build(factory: _ToolsFactory) -> None:
        factory(ctx)

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=build)

    with pytest.raises(ValueError, match='memory store'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_refuses_a_tool_context_without_a_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build(factory: _ToolsFactory) -> None:
        factory(agent_toolset.AgentToolContext(workdir=tmp_path))

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=build)

    with pytest.raises(ValueError, match='no session'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_leaves_skills_permissions_untouched_without_a_host_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'skills').mkdir(mode=0o700)  # as the SDK downloads them, before it asks for the tools
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with())

    async def build(factory: _ToolsFactory) -> None:
        factory(ctx)

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=build)

    asyncio.run(worker._serve())
    assert stat.S_IMODE((tmp_path / 'skills').stat().st_mode) == 0o700


@pytest.mark.usefixtures('_env')
def test_serve_opens_skills_to_reading_under_a_host_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61001')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    (tmp_path / 'skills').mkdir(mode=0o700)
    ctx = agent_toolset.AgentToolContext(workdir=tmp_path, session=_session_with())

    async def build(factory: _ToolsFactory) -> None:
        factory(ctx)

    _patch_serve_collaborators(monkeypatch, _Recorder(), tmp_path, on_session=build)

    asyncio.run(worker._serve())
    assert stat.S_IMODE((tmp_path / 'skills').stat().st_mode) == 0o755


@pytest.mark.usefixtures('_env')
def test_a_host_identity_needs_a_root_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.geteuid() == 0:
        pytest.skip('the refusal is for a worker that is not root')
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61000')
    with pytest.raises(SystemExit, match='not root'):
        asyncio.run(worker._serve())


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
