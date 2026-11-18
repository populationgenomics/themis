"""`_serve` orchestration, sandbox profile, and session tool wiring over loopback Sheaf and `FixtureWorkQueue`.

Exercises `worker._serve` and `worker._serve_until_terminated` end-to-end with real `WorkspaceSync`, `GuestGit`,
`GitHatches`, `BareRepo`, `RemoteStore`, loopback `SheafServicer`, `GrpcHatch`, `GuestFileTool`, `make_shell`, and
`FixtureWorkQueue`, replacing only `postern.Sandbox` (no `bubblewrap` off-Linux), Google ID-token channel credentials,
and the `EnvironmentWorker` session loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Coroutine, Iterator, Sequence

import anthropic
import grpc
import grpc.aio
import postern
import pytest
from anthropic.lib import environments, tools
from anthropic.lib.tools import agent_toolset
from anthropic.types import beta
from anthropic.types.beta import sessions
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter
from postern import grpc as postern_grpc
from postern import stream

from themis import sheaf
from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth.tests import fixture_session
from themis.clients.sheaf import store as remote_mod
from themis.clients.sheaf.tests import conftest as remote_conftest
from themis.clients.work_queue.tests import fixture_work_queue
from themis.rpc import sheaf_pb2_grpc
from themis.services.sandbox_worker import file_tools, git_hatches, guest_git, worker
from themis.services.sandbox_worker.guest import file_tool
from themis.services.sandbox_worker.tests import fakes
from themis.services.sheaf import servicer as sheaf_servicer_mod
from themis.services.sheaf.tests import conftest as sheaf_service_conftest
from themis.sheaf import refdoc
from themis.sheaf.wire import bare, protect
from themis.testing import in_process_grpc

_GIT = shutil.which('git') or 'git'

_HELLO_URL = 'https://hello.themis.example'
_EVIDENCE_URL = 'https://evidence.themis.example'
_WORK_ID = 'work_01ABC'
_ENVIRONMENT_ID = 'env_01ABC'
_SESSION_ID = 'sesn_01ABC'
_ENVIRONMENT_KEY = 'env-key-test'

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

type _ToolsFactory = Callable[[agent_toolset.AgentToolContext], Sequence[environments.BetaAnyRunnableTool]]
type _ScenarioSetup = Callable[[contextlib.ExitStack, pytest.MonkeyPatch, pathlib.Path], _Harness]


def _strip_grpc_fork_noise(stderr: str) -> str:
    """Drop macOS `pthread_atfork` lines gRPC writes onto the child stderr pipe before `execve`."""
    lines = [line for line in stderr.splitlines(keepends=True) if 'ev_poll_posix.cc:' not in line]
    return ''.join(lines)


# Sentinel: the session loop is interrupted by SIGTERM, delivered to this process while it runs.
_SIGTERM = RuntimeError('SIGTERM')


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    pairs = [fakes.GIT_CONFIG[i + 1].split('=', 1) for i in range(0, len(fakes.GIT_CONFIG), 2)]
    config_env: dict[str, str] = {'GIT_CONFIG_COUNT': str(len(pairs))}
    for idx, (key, value) in enumerate(pairs):
        config_env[f'GIT_CONFIG_KEY_{idx}'] = key
        config_env[f'GIT_CONFIG_VALUE_{idx}'] = value
    return {
        'PATH': os.environ['PATH'],
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        **config_env,
        **(extra or {}),
    }


@dataclasses.dataclass
class _Harness:
    """Observable state across one ``_serve`` run."""

    workspace: pathlib.Path
    backend: sheaf.LocalBackend
    store: sheaf.Store
    work_queue: fixture_work_queue.FixtureWorkQueue = dataclasses.field(
        default_factory=lambda: fixture_work_queue.FixtureWorkQueue([])
    )
    profiles: list[postern.SandboxProfile] = dataclasses.field(default_factory=list)
    sandboxes: list[_HostSandbox] = dataclasses.field(default_factory=list)
    verified: bool = False
    request_dirs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    request_dirs_present_at_verify: list[bool] = dataclasses.field(default_factory=list)
    socket_dirs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    grpc_hatches: list[postern_grpc.GrpcHatch] = dataclasses.field(default_factory=list)
    grpc_hatch_closes: int = 0
    stream_hatches: list[stream.StreamHatch] = dataclasses.field(default_factory=list)
    closed_channels: list[str] = dataclasses.field(default_factory=list)
    hatchless_runs: list[tuple[list[str], str]] = dataclasses.field(default_factory=list)
    hatched_python_runs: list[str] = dataclasses.field(default_factory=list)
    memory_sync_intervals: list[float | None] = dataclasses.field(default_factory=list)
    acked_at_session_start: list[list[str]] = dataclasses.field(default_factory=list)
    gitignore_at_session_start: list[bool] = dataclasses.field(default_factory=list)
    sessions_served: int = 0
    mirrors: list[bare.BareRepo] = dataclasses.field(default_factory=list)
    token_modes: list[int] = dataclasses.field(default_factory=list)


class _HostSandbox:
    """Off-Linux stand-in for ``postern.Sandbox`` running real git, ``file_tool.run``, and Python in ``workspace``."""

    def __init__(
        self,
        harness: _Harness,
        profile: postern.SandboxProfile,
        *,
        hatch: Sequence[object] | object | None = None,
    ) -> None:
        self._harness = harness
        self._profile = profile
        self.workspace = harness.workspace
        if hatch is None:
            self._hatches: list[object] = []
        elif isinstance(hatch, (list, tuple)):
            self._hatches = list(hatch)
        else:
            self._hatches = [hatch]
        self.closed = False
        harness.profiles.append(profile)
        harness.sandboxes.append(self)

    def verify(self, *, timeout: float = 30) -> None:
        del timeout
        self._harness.verified = True
        for host, guest in self._profile.required_ro_binds:
            if guest == file_tools.GUEST_REQUESTS:
                host_path = pathlib.Path(host)
                self._harness.request_dirs.append(host_path)
                self._harness.request_dirs_present_at_verify.append(host_path.is_dir())

    def _rewrite_git_arg(self, arg: str) -> str:
        for attached in self._hatches:
            if isinstance(attached, stream.StreamHatch):
                suffix = f' {stream.guest_socket_path(attached.guest_name)}'
                if arg.startswith('ext::') and arg.endswith(suffix):
                    return fakes.HostGitSandbox.url(attached)
        return arg

    @contextlib.contextmanager
    def _serving_hatches(self) -> Iterator[dict[str, str]]:
        extra_env: dict[str, str] = {'POSTERN_HATCH': ''}
        with contextlib.ExitStack() as stack:
            for attached in self._hatches:
                if isinstance(attached, postern_grpc.GrpcHatch):
                    stack.enter_context(attached.accepting())
                    extra_env['POSTERN_HATCH'] = os.fspath(attached.socket_path)
                elif isinstance(attached, stream.StreamHatch):
                    stack.enter_context(attached.accepting())
            yield extra_env

    def run(self, argv: list[str], *, timeout: float = 60, max_output: int | None = None) -> postern.ProcResult:
        del max_output
        assert self._harness.verified
        assert not self.closed
        if tuple(argv[:3]) == worker._GUEST_FILE_TOOL:
            assert self._hatches == []
            guest_request = pathlib.PurePosixPath(argv[-1])
            binds = {guest: host for host, guest in self._profile.required_ro_binds}
            host_request = pathlib.Path(binds[str(guest_request.parent)]) / guest_request.name
            payload = json.loads(host_request.read_text('utf-8'))
            self._harness.hatchless_runs.append((argv[:3], str(payload['tool'])))
            reply = file_tool.run(host_request, str(self.workspace))
            return postern.ProcResult(0, json.dumps(reply), '')
        if argv[0] != 'git':
            raise ValueError(f'unexpected guest command: {argv!r}')
        with self._serving_hatches() as extra_env:
            rewritten = [self._rewrite_git_arg(part) for part in argv[1:]]
            # `-C` instead of `cwd=` keeps CPython on `posix_spawn` rather than `fork`, avoiding gRPC atfork noise.
            result = subprocess.run(  # noqa: S603 — fixed git argv from GuestGit
                [_GIT, '-C', os.fspath(self.workspace), *rewritten],
                env=_git_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return postern.ProcResult(result.returncode, result.stdout, _strip_grpc_fork_noise(result.stderr))

    def run_python(self, code: str, *, timeout: float = 60) -> postern.ProcResult:
        assert self._harness.verified
        assert not self.closed
        if self._hatches:
            self._harness.hatched_python_runs.append(code)
        with self._serving_hatches() as extra_env:
            chdir_shim = f'import os\nos.chdir({os.fspath(self.workspace)!r})\n{code}'
            result = subprocess.run(  # noqa: S603 — trusted shim code in test workspace
                [sys.executable, '-c', chdir_shim],
                env=_git_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return postern.ProcResult(result.returncode, result.stdout, _strip_grpc_fork_noise(result.stderr))

    def close(self) -> None:
        self.closed = True


def _commit_in_workspace(workspace: pathlib.Path, rel_path: str, content: str) -> str:
    target = workspace / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, 'utf-8')
    ws = os.fspath(workspace)
    subprocess.run([_GIT, '-C', ws, 'add', '-f', rel_path], env=_git_env(), check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [_GIT, '-C', ws, 'commit', '-q', '-m', f'add {rel_path}'], env=_git_env(), check=True
    )
    rev = subprocess.run(  # noqa: S603
        [_GIT, '-C', ws, 'rev-parse', 'HEAD'], env=_git_env(), capture_output=True, text=True, check=True
    )
    return rev.stdout.strip()


def _detach_and_commit(workspace: pathlib.Path, rel_path: str, content: str) -> str:
    ws = os.fspath(workspace)
    subprocess.run([_GIT, '-C', ws, 'checkout', '-q', '--detach'], env=_git_env(), check=True)  # noqa: S603
    return _commit_in_workspace(workspace, rel_path, content)


async def _end_session(serve_error: Exception | None) -> None:
    if serve_error is _SIGTERM:
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)
    if serve_error is not None:
        raise serve_error


@contextlib.contextmanager
def _wired_serve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    sheaf_target: str | None = None,
    serve_error: Exception | None = None,
    on_session: Callable[[_ToolsFactory], Coroutine[object, object, None]] | None = None,
    on_mirror: Callable[[bare.BareRepo], None] | None = None,
) -> Iterator[_Harness]:
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    backend = sheaf.LocalBackend(tmp_path / 'sheaf-store')
    harness = _Harness(workspace=workspace, backend=backend, store=remote_conftest.direct(backend))
    real_secure_channel = grpc.secure_channel
    real_build_hatch = worker.hatch_mod.build_hatch
    creds_by_url: dict[str, grpc.ChannelCredentials] = {}
    channel_info: dict[grpc.Channel, tuple[str, grpc.ChannelCredentials]] = {}

    def sandbox_factory(profile: postern.SandboxProfile, *, hatch: object = None) -> _HostSandbox:
        return _HostSandbox(harness, profile, hatch=hatch)

    def channel_credentials(url: str) -> grpc.ChannelCredentials:
        assert url in {_HELLO_URL, _EVIDENCE_URL}
        creds = grpc.ssl_channel_credentials()
        creds_by_url[url] = creds
        return creds

    def secure_channel(target: str, credentials: grpc.ChannelCredentials) -> grpc.Channel:
        channel = real_secure_channel(target, credentials)
        channel_info[channel] = (target, credentials)
        real_close = channel.close

        def close() -> None:
            harness.closed_channels.append(target)
            real_close()

        channel.close = close
        return channel

    def build_hatch(
        *,
        hello_channel: grpc.Channel,
        evidence_channel: grpc.Channel,
        session_token: str,
        socket_path: str | os.PathLike[str] | None = None,
    ) -> postern_grpc.GrpcHatch:
        assert channel_info[hello_channel] == ('hello.themis.example:443', creds_by_url[_HELLO_URL])
        assert channel_info[evidence_channel] == ('evidence.themis.example:443', creds_by_url[_EVIDENCE_URL])
        assert session_token == os.environ['THEMIS_SESSION_TOKEN']
        hatch = real_build_hatch(
            hello_channel=hello_channel,
            evidence_channel=evidence_channel,
            session_token=session_token,
            socket_path=socket_path,
        )
        harness.grpc_hatches.append(hatch)
        real_close = hatch.close

        def close() -> None:
            harness.grpc_hatch_closes += 1
            real_close()

        hatch.close = close
        return hatch

    def build_work_queue(
        client: anthropic.AsyncAnthropic, *, environment_id: str
    ) -> fixture_work_queue.FixtureWorkQueue:
        assert isinstance(client, anthropic.AsyncAnthropic)
        assert environment_id == os.environ['ANTHROPIC_ENVIRONMENT_ID']
        return harness.work_queue

    class _ObservedGitHatches(git_hatches.GitHatches):
        def __init__(
            self,
            mirror: bare.BareRepo,
            *,
            protection: protect.Protection,
            socket_dir: pathlib.Path,
        ) -> None:
            assert harness.verified
            harness.mirrors.append(mirror)
            harness.socket_dirs.append(socket_dir)
            token_path = pathlib.Path(mirror.store.descriptor()['token_file'])
            harness.token_modes.append(stat.S_IMODE(token_path.stat().st_mode))
            if on_mirror is not None:
                on_mirror(mirror)
            super().__init__(mirror, protection=protection, socket_dir=socket_dir)
            harness.stream_hatches.extend(self.hatches)

    class _SessionDriver:
        def __init__(
            self,
            client: anthropic.AsyncAnthropic,
            *,
            tools: _ToolsFactory,
            workdir: pathlib.Path,
            memory_sync_interval: float | None = environments.DEFAULT_MEMORY_SYNC_INTERVAL,
        ) -> None:
            assert isinstance(client, anthropic.AsyncAnthropic)
            assert workdir == workspace
            self._tools = tools
            harness.memory_sync_intervals.append(memory_sync_interval)

        async def handle_item(self) -> None:
            harness.sessions_served += 1
            harness.acked_at_session_start.append(list(harness.work_queue.acked))
            harness.gitignore_at_session_start.append((workspace / '.gitignore').is_file())
            if on_session is not None:
                await on_session(self._tools)
            await _end_session(serve_error)

    monkeypatch.setattr(worker.postern, 'Sandbox', sandbox_factory)
    monkeypatch.setattr(worker.id_token, 'channel_credentials', channel_credentials)
    monkeypatch.setattr(worker.grpc, 'secure_channel', secure_channel)
    monkeypatch.setattr(worker.hatch_mod, 'build_hatch', build_hatch)
    monkeypatch.setattr(worker.work_queue_mod, 'AnthropicWorkQueue', build_work_queue)
    monkeypatch.setattr(worker.git_hatches, 'GitHatches', _ObservedGitHatches)
    monkeypatch.setattr(worker.environments, 'EnvironmentWorker', _SessionDriver)

    with contextlib.ExitStack() as stack:
        target = sheaf_target if sheaf_target is not None else stack.enter_context(remote_conftest.serving(backend))
        monkeypatch.setenv('THEMIS_SHEAF_URL', target)
        yield harness


def _assert_cleaned_up(harness: _Harness) -> None:
    """Assert every resource `_serve` opens for a session is closed and unlinked on exit."""
    assert len(harness.sandboxes) == 3
    assert [s.closed for s in harness.sandboxes[1:]] == [True, True]
    assert harness.grpc_hatch_closes == 1
    assert not pathlib.Path(harness.grpc_hatches[0].socket_path).exists()
    assert len(harness.stream_hatches) == 2
    for stream_hatch in harness.stream_hatches:
        with pytest.raises(RuntimeError), stream_hatch.accepting():
            pass
    assert harness.closed_channels == ['hello.themis.example:443', 'evidence.themis.example:443']
    assert len(harness.socket_dirs) == 1
    assert not harness.socket_dirs[0].exists()
    assert len(harness.request_dirs) == 1
    assert not harness.request_dirs[0].exists()
    assert len(harness.mirrors) == 1
    assert not harness.mirrors[0].path.parent.exists()


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SESSION_TOKEN', fixture_session.GOOD_TOKEN)
    monkeypatch.setenv('ANTHROPIC_ENVIRONMENT_KEY', _ENVIRONMENT_KEY)
    monkeypatch.setenv('THEMIS_HELLO_URL', _HELLO_URL)
    monkeypatch.setenv('THEMIS_EVIDENCE_URL', _EVIDENCE_URL)
    monkeypatch.setenv('THEMIS_SHEAF_URL', '127.0.0.1:1')
    monkeypatch.setenv('ANTHROPIC_WORK_ID', _WORK_ID)
    monkeypatch.setenv('ANTHROPIC_ENVIRONMENT_ID', _ENVIRONMENT_ID)
    monkeypatch.setenv('ANTHROPIC_SESSION_ID', _SESSION_ID)
    monkeypatch.delenv(worker._HOST_UID_ENV, raising=False)
    monkeypatch.delenv(worker._HOST_GID_ENV, raising=False)


@pytest.mark.usefixtures('_env')
def test_serve_orders_verify_restore_ack_serve_teardown_then_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    committed_sha: list[str] = []
    detached_sha: list[str] = []
    store_refs_mid_session: list[dict[str, str]] = []
    harness_box: list[_Harness] = []

    async def during_session(_factory: _ToolsFactory) -> None:
        harness = harness_box[0]
        sha = _commit_in_workspace(harness.workspace, 'report.md', 'agent finding\n')
        committed_sha.append(sha)
        detached_sha.append(_detach_and_commit(harness.workspace, 'detached.md', 'stranded on head\n'))
        store_refs_mid_session.append(dict(harness.store.read().refs))

    with _wired_serve(monkeypatch, tmp_path, on_session=during_session) as harness:
        harness_box.append(harness)
        asyncio.run(worker._serve())

    # Before the session ran, `/workspace` was hydrated with `.gitignore` and the work item (not session id) was acked.
    assert harness.gitignore_at_session_start == [True]
    assert harness.acked_at_session_start == [[_WORK_ID]]
    assert harness.work_queue.acked == [_WORK_ID]
    assert harness.work_queue.stopped == []
    # Unpushed commits landed in the store only on teardown, with stranded HEAD keyed on `session_id` (not `work_id`).
    assert store_refs_mid_session[0]['refs/heads/main'] != committed_sha[0]
    refs_after = harness.store.read().refs
    assert refs_after['refs/heads/main'] == committed_sha[0]
    assert refs_after[f'{protect.STRANDED_NAMESPACE}/{_SESSION_ID}/HEAD'] == detached_sha[0]
    _assert_cleaned_up(harness)


@pytest.mark.usefixtures('_env')
def test_the_mirror_runs_over_the_sheaf_service_with_a_token_file_of_the_workers_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # The worker's one credential for the repository is the session token, presented to the Sheaf service; the
    # hook finds it by the path in the store's descriptor, never in an environment or the sync state itself.
    with _wired_serve(monkeypatch, tmp_path) as harness:
        asyncio.run(worker._serve())
        (mirror,) = harness.mirrors
        assert isinstance(mirror.store, remote_mod.RemoteStore)
        descriptor = mirror.store.descriptor()
        assert descriptor['target'] == os.environ['THEMIS_SHEAF_URL']
        token_file = pathlib.Path(descriptor['token_file'])
        # Under the mirror root, which is never bound into the guest; in particular nowhere under /workspace.
        assert token_file.parent == mirror.path.parent
        assert not token_file.is_relative_to(harness.workspace)
        assert harness.token_modes == [0o600]
        assert fixture_session.GOOD_TOKEN not in bare.SyncState(generation=None, store=descriptor).to_json()
        # Gone with the mirror once the session is over.
        assert not token_file.exists()
        assert not mirror.path.parent.exists()


@contextlib.contextmanager
def _serving_unauthenticated(backend: sheaf.Backend) -> Iterator[str]:
    async def no_caller(_ctx: grpc.aio.ServicerContext) -> str | None:
        return None

    servicer = sheaf_servicer_mod.Servicer(backend, sheaf_service_conftest.LIMITS)
    authorizer = dataclasses.replace(sheaf_service_conftest.authorizer(), verify_caller=no_caller)
    gate = interceptor_mod.AuthInterceptor(authorizer)
    with in_process_grpc.serving_in_thread(
        lambda server: sheaf_pb2_grpc.add_SheafServicer_to_server(servicer, server), server_interceptors=(gate,)
    ) as target:
        yield target


def _verdict_corrupt_repository(
    stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> _Harness:
    harness = stack.enter_context(_wired_serve(monkeypatch, tmp_path))
    harness.store.publish(
        harness.store.read(),
        sheaf.Intent(
            ref_updates={
                'refs/heads/main': sheaf.RefUpdate(old=None, new='1' * 40),
                refdoc.REFLOG_REF: sheaf.RefUpdate(old=None, new='2' * 40),
            },
            packs=(b'PACK',),
        ),
    )
    for pack_file in (tmp_path / 'sheaf-store').rglob('*.pack'):
        pack_file.unlink()
    return harness


def _verdict_token_refused(
    stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> _Harness:
    harness = stack.enter_context(_wired_serve(monkeypatch, tmp_path))
    monkeypatch.setenv('THEMIS_SESSION_TOKEN', 'unknown-session-token')
    return harness


def _verdict_caller_unauthenticated(
    stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> _Harness:
    unauth_backend = sheaf.LocalBackend(tmp_path / 'unauth-store')
    target = stack.enter_context(_serving_unauthenticated(unauth_backend))
    return stack.enter_context(_wired_serve(monkeypatch, tmp_path, sheaf_target=target))


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(
    'prepare',
    [_verdict_corrupt_repository, _verdict_token_refused, _verdict_caller_unauthenticated],
    ids=[
        'repository store',
        'token refused by the sheaf service',
        'caller unauthenticated at the sheaf service',
    ],
)
def test_serve_acks_and_stops_the_item_on_a_repository_verdict_without_serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    prepare: _ScenarioSetup,
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    # A verdict at restore is terminal: ack (stop reclaim) + stop (end the item) and never serve, else the
    # item would sit unacked and be reclaimed into the same failure on the next drain.
    with contextlib.ExitStack() as stack:
        harness = prepare(stack, monkeypatch, tmp_path)
        asyncio.run(worker._serve())

    assert harness.sessions_served == 0
    assert harness.work_queue.acked == [_WORK_ID]
    assert harness.work_queue.stopped == [_WORK_ID]
    _assert_cleaned_up(harness)
    # The session never served, so its trace says it failed rather than ending clean.
    [session] = [span for span in recorded_spans.get_finished_spans() if span.name == 'sandbox session']
    assert session.status.status_code is trace.StatusCode.ERROR
    assert session.attributes is not None
    assert session.attributes['error.type'] == worker._RESTORE_REFUSED


@pytest.mark.usefixtures('_env')
def test_a_failed_session_loop_still_tears_down_before_the_error_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # What the agent committed is not the SDK's to lose: the push runs on the way out, then the failure surfaces.
    committed_sha: list[str] = []
    detached_sha: list[str] = []

    async def commit_before_crash(_factory: _ToolsFactory) -> None:
        committed_sha.append(_commit_in_workspace(tmp_path / 'workspace', 'saved.md', 'survives crash\n'))
        detached_sha.append(_detach_and_commit(tmp_path / 'workspace', 'detached.md', 'stranded on crash\n'))

    with (
        _wired_serve(
            monkeypatch, tmp_path, serve_error=RuntimeError('lease lost'), on_session=commit_before_crash
        ) as harness,
        pytest.raises(RuntimeError, match='lease lost'),
    ):
        asyncio.run(worker._serve())

    refs_after = harness.store.read().refs
    assert refs_after['refs/heads/main'] == committed_sha[0]
    assert refs_after[f'{protect.STRANDED_NAMESPACE}/{_SESSION_ID}/HEAD'] == detached_sha[0]
    _assert_cleaned_up(harness)


def _fault_guest_git(stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Harness:
    (tmp_path / 'workspace').mkdir(exist_ok=True)
    (tmp_path / 'workspace' / 'dirty.txt').write_text('blocks git clone\n', 'utf-8')
    return stack.enter_context(_wired_serve(monkeypatch, tmp_path))


def _fault_sheaf_unreachable(
    stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> _Harness:
    class _AlwaysDown(remote_conftest.Flaky):
        unavailable_reads = 999

    down_backend = sheaf.LocalBackend(tmp_path / 'down-store')
    target = stack.enter_context(remote_conftest.serving(down_backend, servicer_class=_AlwaysDown))
    return stack.enter_context(_wired_serve(monkeypatch, tmp_path, sheaf_target=target))


def _fault_token_unreadable(
    stack: contextlib.ExitStack, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> _Harness:
    def break_token(mirror: bare.BareRepo) -> None:
        pathlib.Path(mirror.store.descriptor()['token_file']).unlink()

    return stack.enter_context(_wired_serve(monkeypatch, tmp_path, on_mirror=break_token))


@pytest.mark.usefixtures('_env')
@pytest.mark.parametrize(
    ('prepare', 'expected_exc'),
    [
        (_fault_guest_git, guest_git.GitError),
        (_fault_sheaf_unreachable, sheaf.ServiceFault),
        (_fault_token_unreadable, sheaf.CredentialsUnusable),
    ],
    ids=['guest git', 'sheaf service unreachable', 'token file unreadable'],
)
def test_serve_leaves_the_item_unacked_on_a_fault_that_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    prepare: _ScenarioSetup,
    expected_exc: type[Exception],
) -> None:
    # Not a verdict: a fresh spawn may clear it or outlive an outage, so the item stays reclaimable and the
    # error propagates.
    with contextlib.ExitStack() as stack:
        harness = prepare(stack, monkeypatch, tmp_path)
        with pytest.raises(expected_exc):
            asyncio.run(worker._serve())

    assert harness.sessions_served == 0
    assert harness.work_queue.acked == []
    assert harness.work_queue.stopped == []
    _assert_cleaned_up(harness)
    assert not pathlib.Path(harness.mirrors[0].store.descriptor()['token_file']).exists()


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
    with _wired_serve(monkeypatch, tmp_path) as harness:
        asyncio.run(worker._serve())

    assert len(harness.profiles) == 3
    assert {(profile.host_uid, profile.host_gid) for profile in harness.profiles} == {(None, None)}


@pytest.mark.usefixtures('_env')
def test_serve_carries_the_configured_host_identity_into_the_sandbox_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61001')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    with _wired_serve(monkeypatch, tmp_path) as harness:
        asyncio.run(worker._serve())

    assert len(harness.profiles) == 3
    assert {(profile.host_uid, profile.host_gid) for profile in harness.profiles} == {(61000, 61001)}


@pytest.mark.usefixtures('_env')
def test_serve_requires_the_file_tools_request_directory_in_the_sandbox_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # A required bind, not a -try one: a launch without the directory fails instead of the tool failing in the guest.
    with _wired_serve(monkeypatch, tmp_path) as harness:
        asyncio.run(worker._serve())

    assert len(harness.profiles) == 3
    bound = {tuple(profile.required_ro_binds) for profile in harness.profiles}
    [(required_binds,)] = [[b for b in binds if b[1] == file_tools.GUEST_REQUESTS] for binds in bound]
    host_requests = required_binds[0]
    assert all(file_tools.GUEST_REQUESTS not in dict(profile.ro_binds).values() for profile in harness.profiles)
    assert harness.request_dirs_present_at_verify == [True]
    assert not pathlib.Path(host_requests).exists()


def _session_with(*resources: object) -> beta.BetaManagedAgentsSession:
    return beta.BetaManagedAgentsSession.model_construct(id='sesn_1', resources=list(resources))


@pytest.mark.usefixtures('_env')
def test_serve_runs_each_session_file_tool_in_the_hatchless_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    file = sessions.BetaManagedAgentsFileResource.model_construct(type='file', file_id='file_1')
    served_names: list[str] = []
    results: dict[str, object] = {}

    calls: dict[str, dict[str, object]] = {
        'write': {'file_path': 'notes.txt', 'content': 'alpha\nbeta\n'},
        'edit': {'file_path': 'notes.txt', 'old_string': 'beta', 'new_string': 'delta'},
        'read': {'file_path': 'notes.txt'},
        'glob': {'pattern': '*.txt'},
        'grep': {'pattern': 'delta'},
    }

    async def call_file_tools(factory: _ToolsFactory) -> None:
        ctx = agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with(file))
        by_name = {tool.name: tool for tool in factory(ctx)}
        served_names.extend(by_name)
        for name in ('write', 'edit', 'read', 'glob', 'grep'):
            tool = by_name[name]
            assert isinstance(tool, tools.BetaAsyncBuiltinFunctionTool)
            results[name] = await tool.call(calls[name])

    with _wired_serve(monkeypatch, tmp_path, on_session=call_file_tools) as harness:
        asyncio.run(worker._serve())

    expected_names = {'read', 'write', 'edit', 'glob', 'grep'}
    assert len(served_names) == 6
    assert set(served_names) == expected_names | {'shell'}
    assert (harness.workspace / 'notes.txt').read_text('utf-8') == 'alpha\ndelta\n'
    assert 'delta' in str(results['read'])
    assert 'notes.txt' in str(results['glob'])
    assert 'notes.txt' in str(results['grep'])
    assert {name for _, name in harness.hatchless_runs} == expected_names
    assert {tuple(entrypoint) for entrypoint, _ in harness.hatchless_runs} == {worker._GUEST_FILE_TOOL}
    # Only `GuestGit._first_commit`'s `.gitignore` write ran on the hatched sandbox; no file tool touched it.
    assert len(harness.hatched_python_runs) == 1
    assert '.gitignore' in harness.hatched_python_runs[0]


@pytest.mark.usefixtures('_env')
def test_serve_runs_the_session_shell_tool_in_the_hatched_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    outputs: list[object] = []

    async def call_shell(factory: _ToolsFactory) -> None:
        ctx = agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with())
        built = {tool.name: tool for tool in factory(ctx)}
        shell_tool = built['shell']
        assert isinstance(shell_tool, tools.BetaAsyncBuiltinFunctionTool)
        outputs.append(
            await shell_tool.call(
                {'command': 'printf "shell ok\\n" && printf "written\\n" > from_shell.txt', 'intent': 'greet'}
            )
        )

    with _wired_serve(monkeypatch, tmp_path, on_session=call_shell) as harness:
        asyncio.run(worker._serve())

    assert outputs == ['shell ok']
    assert (harness.workspace / 'from_shell.txt').read_text('utf-8') == 'written\n'
    assert any('from_shell.txt' in code for code in harness.hatched_python_runs)
    assert harness.hatchless_runs == []


@pytest.mark.usefixtures('_env')
def test_serve_records_each_session_tool_call_as_a_child_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    inputs: dict[str, dict[str, object]] = {
        'write': {'file_path': 'notes.txt', 'content': 'line\n'},
        'edit': {'file_path': 'notes.txt', 'old_string': 'line', 'new_string': 'updated'},
        'read': {'file_path': 'notes.txt'},
        'glob': {'pattern': '*.txt'},
        'grep': {'pattern': 'updated'},
        'shell': {'command': 'echo hi', 'intent': 'greet'},
    }

    async def call_every_tool(factory: _ToolsFactory) -> None:
        ctx = agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with())
        by_name = {tool.name: tool for tool in factory(ctx)}
        for name in ('write', 'edit', 'read', 'glob', 'grep', 'shell'):
            tool = by_name[name]
            assert isinstance(tool, tools.BetaAsyncBuiltinFunctionTool)
            await tool.call(inputs[name])

    with _wired_serve(monkeypatch, tmp_path, on_session=call_every_tool):
        asyncio.run(worker._serve())

    spans = recorded_spans.get_finished_spans()
    [session_span] = [span for span in spans if span.name == 'sandbox session']
    assert session_span.context is not None
    tool_spans = [span for span in spans if span.name.startswith('execute_tool ')]
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

    async def build(factory: _ToolsFactory) -> None:
        factory(agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with()))

    with _wired_serve(monkeypatch, tmp_path, on_session=build), pytest.raises(RuntimeError, match='not the file tools'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_disables_worker_memory_sync(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _wired_serve(monkeypatch, tmp_path) as harness:
        asyncio.run(worker._serve())

    assert harness.memory_sync_intervals == [None]


@pytest.mark.usefixtures('_env')
def test_serve_refuses_a_session_with_a_memory_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = sessions.BetaManagedAgentsMemoryStoreResource.model_construct(type='memory_store', memory_store_id='m_1')

    async def build(factory: _ToolsFactory) -> None:
        factory(agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with(store)))

    with _wired_serve(monkeypatch, tmp_path, on_session=build), pytest.raises(ValueError, match='memory store'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_refuses_a_tool_context_without_a_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build(factory: _ToolsFactory) -> None:
        factory(agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace'))

    with _wired_serve(monkeypatch, tmp_path, on_session=build), pytest.raises(ValueError, match='no session'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_serve_leaves_skills_permissions_untouched_without_a_host_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build(factory: _ToolsFactory) -> None:
        skills = tmp_path / 'workspace' / 'skills'
        skills.mkdir(mode=0o700)  # as the SDK downloads them, after restore and before it asks for the tools
        skills.chmod(0o700)
        factory(agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with()))

    with _wired_serve(monkeypatch, tmp_path, on_session=build):
        asyncio.run(worker._serve())
    assert stat.S_IMODE((tmp_path / 'workspace' / 'skills').stat().st_mode) == 0o700


@pytest.mark.usefixtures('_env')
def test_serve_opens_skills_to_reading_under_a_host_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61001')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)

    async def build(factory: _ToolsFactory) -> None:
        skills = tmp_path / 'workspace' / 'skills'
        skills.mkdir(mode=0o700)
        skills.chmod(0o700)
        factory(agent_toolset.AgentToolContext(workdir=tmp_path / 'workspace', session=_session_with()))

    with _wired_serve(monkeypatch, tmp_path, on_session=build):
        asyncio.run(worker._serve())
    assert stat.S_IMODE((tmp_path / 'workspace' / 'skills').stat().st_mode) == 0o755


@pytest.mark.usefixtures('_env')
def test_a_host_identity_needs_a_root_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    monkeypatch.setenv(worker._HOST_UID_ENV, '61000')
    monkeypatch.setenv(worker._HOST_GID_ENV, '61000')
    with pytest.raises(SystemExit, match='not root'):
        asyncio.run(worker._serve())


@pytest.mark.usefixtures('_env')
def test_sigterm_cancels_the_session_and_teardown_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Cloud Run's task timeout and cancellation arrive as SIGTERM; the push has to fit in before SIGKILL.
    committed_sha: list[str] = []

    async def commit_before_sigterm(_factory: _ToolsFactory) -> None:
        committed_sha.append(_commit_in_workspace(tmp_path / 'workspace', 'partial.md', 'saved on sigterm\n'))

    with (
        _wired_serve(monkeypatch, tmp_path, serve_error=_SIGTERM, on_session=commit_before_sigterm) as harness,
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(worker._serve_until_terminated())

    assert harness.store.read().refs['refs/heads/main'] == committed_sha[0]
    _assert_cleaned_up(harness)
