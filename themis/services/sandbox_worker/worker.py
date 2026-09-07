"""Worker entrypoint: verify isolation, restore /workspace, serve one session, push what is left (sandbox-worker.md).

One trusted process per Job execution. The dispatcher claims a work item and injects its per-execution env
(``ANTHROPIC_WORK_ID`` / ``_ENVIRONMENT_ID`` / ``_SESSION_ID`` / ``_ENVIRONMENT_KEY``, plus the minted
``THEMIS_SESSION_TOKEN``); the deploy supplies the service URLs (``THEMIS_STORE_URL`` / ``_HELLO_URL`` /
``_EVIDENCE_URL`` / ``_SHEAF_URL``) and, optionally, the uid and gid bwrap runs as (``THEMIS_SANDBOX_HOST_UID`` /
``_GID``, both or neither). The worker:

1. ``Sandbox.verify()`` — a fail-closed boot gate: refuse to serve unless isolation is actually enforced here
   (empty netns, userns created, non-root guest, seccomp/arch covered). Off-platform (no bubblewrap) this raises,
   so the worker never runs model code unsandboxed.
2. Restore ``/workspace``: a guest-side ``git clone`` of the Analysis repository over the hatch, then the working
   document from the store where the repository has none. Both fail-closed. The repository is reached through the
   Sheaf service, which scopes every call by the session token the worker presents — the worker holds no bucket
   credential and never learns the Analysis id.
3. Ack the work item, restore proven — this moves it out of the dispatcher's reclaimable set
   (``reclaim_older_than_ms``), so a session running longer than that window is not reclaimed and
   re-dispatched mid-flight. A spawn that dies before restore stays unacked and re-surfaces.
4. Run ``EnvironmentWorker.handle_item()`` — the SDK loop for the one claimed session. Its tools are the
   ``agent_toolset_20260401`` file tools (read/write/edit/glob/grep), which the SDK confines to ``workdir``
   (=/workspace) and so run in the trusted worker, plus ``shell`` — the sandboxed replacement for ``bash``
   that marshals every command into the guest behind the hello/evidence-forwarding hatch.
5. Checkpoint the working document, push every branch the agent committed and left unpushed, and exit (one
   execution per spawn — scale-to-zero preserved).

The session token reaches the mirror's pre-receive hook by a file under the mirror's host-only root (`_mirror`), named
by path in the sync state the hook reads; the git processes that read guest bytes — the hatches' `upload-pack` and
`receive-pack`, the hook — run with a scrubbed environment, and the guest is bound to neither the mirror root nor the
worker's environment.

Host uid mapping. With ``THEMIS_SANDBOX_HOST_UID`` set the worker (root in its container) runs bwrap as that uid,
so every file the guest creates is owned by a uid that owns nothing else on the host, and a host-side ``git`` in
``/workspace`` fails git's ownership check. The deploy has to make every bind source reachable by that uid —
the guest rootfs and ``/workspace`` are world-traversable already, and the hatch sockets are bound under a
directory the worker hands to that uid here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import shutil
import signal
import tempfile
import urllib.parse
from collections.abc import Coroutine, Iterator, Sequence

import anthropic
import grpc
import grpc.aio
import postern
from anthropic.lib import environments
from anthropic.lib.tools import agent_toolset

from themis import sheaf
from themis.clients import id_token
from themis.clients.sheaf import store as remote_mod
from themis.clients.work_queue import client as work_queue_mod
from themis.services.sandbox_worker import git_hatches, guest_git, store_client
from themis.services.sandbox_worker import hatch as hatch_mod
from themis.services.sandbox_worker import sync as sync_mod
from themis.services.sandbox_worker import tool as tool_mod
from themis.sheaf.wire import bare

# The guest rootfs the Dockerfile assembles; postern binds it read-only as the guest's system dirs.
_GUEST_ROOTFS = '/opt/guest-root'
# postern binds the rootfs's /usr, /lib and /bin but not its /etc, so the guest's system gitconfig — the ext::
# allowance and the agent's identity — is bound on its own.
_GUEST_GITCONFIG = '/etc/gitconfig'
_WORKSPACE_ROOT = '/workspace'
# Must stay under the SDK's per-tool deadline (anthropic.lib TOOL_TIMEOUT, 150s): a sandbox run that
# outlives it makes the SDK abort the (non-cancellable) tool call, reporting a spurious timeout and
# skipping the post-call checkpoint.
_TOOL_TIMEOUT_S = 120
# Per-command bounds on the worker's own guest git, which runs outside any tool call. The restore's commands run
# before the ack, so their bound has to keep the restore inside the dispatcher's reclaim window (600 s), or a slow
# spawn is re-dispatched underneath itself; local mirror to local tree over a socket, a clone is disk-bound. The
# teardown's run after the ack and a push's wall time includes the hook's pack upload, so they get their own.
_HYDRATE_TIMEOUT_S = 120
_TEARDOWN_TIMEOUT_S = 600
# handle_item() reads these from the env deep in the SDK; listed here to fail loud at the boundary.
_SDK_ITEM_ENV = ('ANTHROPIC_WORK_ID', 'ANTHROPIC_ENVIRONMENT_ID', 'ANTHROPIC_SESSION_ID')
_HOST_UID_ENV = 'THEMIS_SANDBOX_HOST_UID'
_HOST_GID_ENV = 'THEMIS_SANDBOX_HOST_GID'
# The token file's name under the mirror root, and the name the mirror knows the repository by: for paths and log
# lines only, since which repository a call reaches is the session token's to decide.
_TOKEN_FILE = 'session-token.json'  # noqa: S105 — the file's name, not a token
_REPOSITORY = 'workspace'
# The Sheaf service's verdicts on the session token itself; every other status it answers is a fault. The document rpc
# has no retry policy, so its outages are named directly.
_TOKEN_REFUSED = frozenset({'PERMISSION_DENIED', 'UNAUTHENTICATED'})
_OUTAGE = frozenset({grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED})
_logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def _grpc_target(url: str) -> str:
    """The ``host:port`` dial target for an internal service's ``run.app`` URL."""
    host = urllib.parse.urlparse(url).netloc
    return host if ':' in host else f'{host}:443'


def _host_identity() -> tuple[int, int] | None:
    """The ``(uid, gid)`` bwrap runs as, from the env; ``None`` leaves it at the worker's own.

    Raises:
        SystemExit: If one of the pair is set without the other, or either is not an integer.
    """
    uid, gid = os.environ.get(_HOST_UID_ENV), os.environ.get(_HOST_GID_ENV)
    if uid is None and gid is None:
        return None
    if uid is None or gid is None:
        raise SystemExit(f'{_HOST_UID_ENV} and {_HOST_GID_ENV} are set together or not at all')
    try:
        return int(uid), int(gid)
    except ValueError as exc:
        raise SystemExit(f'{_HOST_UID_ENV} and {_HOST_GID_ENV} must be integers: {exc}') from exc


def _build_profile(host_identity: tuple[int, int] | None) -> postern.SandboxProfile:
    """The profile the worker serves with: the guest rootfs, one writable /workspace, and the host uid mapping."""
    uid, gid = host_identity if host_identity is not None else (None, None)
    return postern.SandboxProfile(
        rootfs=pathlib.Path(_GUEST_ROOTFS),
        workspace=pathlib.Path(_WORKSPACE_ROOT),
        ro_binds=[(_GUEST_ROOTFS + _GUEST_GITCONFIG, _GUEST_GITCONFIG)],
        host_uid=uid,
        host_gid=gid,
    )


def _socket_dir(host_identity: tuple[int, int] | None) -> pathlib.Path:
    """A private directory for the hatch sockets, owned by the worker, traversable by whoever bwrap runs as.

    Every hatch socket is chmod'd world-writable by postern, so the directory is the whole host-side
    access control; and bwrap has to open each socket to bind it into the guest, so the directory
    has to be traversable by bwrap's own uid. Traversable, not owned: an owner could widen the mode
    and expose every socket in it.
    """
    path = pathlib.Path(tempfile.mkdtemp(prefix='themis-hatch-'))
    if host_identity is not None:
        path.chmod(0o711)
    return path


def _tools_for_session(
    ctx: agent_toolset.AgentToolContext, shell: environments.BetaAnyRunnableTool
) -> Sequence[environments.BetaAnyRunnableTool]:
    """The session's tools: the workdir-confined ``agent_toolset_20260401`` file tools plus the sandboxed ``shell``.

    ``bash`` is dropped — it is the one toolset entry that runs unconfined, so ``shell`` (which marshals into
    the postern guest) is its isolated replacement. The remaining file tools resolve every path against
    ``ctx.workdir`` (=/workspace) and reject escapes, so they run in the trusted worker without weakening
    isolation.
    """
    confined = [tool for tool in agent_toolset.beta_agent_toolset_20260401(ctx) if tool.name != 'bash']
    return [*confined, shell]


async def _fail_item(work_queue: work_queue_mod.WorkQueue, work_id: str) -> None:
    """Ack and stop an item whose session can never be served: acked to stop reclaim, stopped to end it."""
    await work_queue.ack(work_id)
    await work_queue.stop(work_id)


@contextlib.contextmanager
def _mirror(sheaf_url: str, session_token: str) -> Iterator[bare.BareRepo]:
    """The bare mirror of the Analysis repository over the Sheaf service, at a host-only path.

    The token file the mirror and its hook present is written beside the mirror, owner-readable
    only, and removed with the mirror on exit; the root is a fresh private directory the guest is
    never bound to. Exiting closes the store's channel, so a caller closes whatever still serves
    the mirror before leaving the block.
    """
    root = pathlib.Path(tempfile.mkdtemp(prefix='themis-mirror-'))
    token_file = root / _TOKEN_FILE
    try:
        remote_mod.write_credentials(token_file, remote_mod.Credentials(session_token=session_token, bearer=None))
        with remote_mod.RemoteStore(sheaf_url, token_file, repo=_REPOSITORY) as remote:
            yield bare.BareRepo(remote, root / 'mirror.git')
    finally:
        try:
            token_file.unlink(missing_ok=True)
        except OSError:
            _logger.exception('the token file %s was not removed', token_file)
        shutil.rmtree(root, ignore_errors=True)


def _is_verdict(exc: Exception) -> bool:
    """Whether a restore error is a store's verdict a respawn would meet again, rather than a fault it might not.

    A verdict: the store or the Sheaf service refusing the session token, or answering the document rpc with
    anything but an outage; a ref document that is corrupt or names a pack that is gone. A fault: either
    service out of reach or out of time, or a token file this worker wrote and cannot read back — the next
    spawn writes its own.
    """
    if isinstance(exc, sheaf.ServiceFault):
        return exc.code in _TOKEN_REFUSED
    if isinstance(exc, grpc.aio.AioRpcError):
        return exc.code() not in _OUTAGE
    if isinstance(exc, sheaf.CredentialsUnusable):
        return False
    return isinstance(exc, sheaf.SheafError)


async def _restore_or_fail_item(
    workspace_sync: sync_mod.WorkspaceSync, work_queue: work_queue_mod.WorkQueue, work_id: str
) -> bool:
    """Restore ``/workspace``, then ack the work item; on a store verdict, ack + stop it instead.

    The ack is deferred until restore proves: acking moves the item out of the dispatcher's reclaimable set
    (``reclaim_older_than_ms``), so a session outliving that window is not reclaimed and re-dispatched
    mid-flight, while a spawn that dies before restore stays unacked and correctly re-surfaces. A store
    verdict (`_is_verdict`) is terminal: a respawn would hit the same failure, so the item is acked to stop
    reclaim and stopped to end it, rather than left to loop. Anything else — a guest git failure, a local disk
    write, the service out of reach — is not terminal: it propagates, leaving the item unacked so reclaim
    retries it on a fresh spawn, which can clear a node-local fault or outlive an outage.

    Returns:
        Whether restore succeeded and the caller should serve the session.

    Raises:
        Exception: Any restore error that is not a verdict — propagated uncaught so the unacked item
            reclaims onto a fresh spawn.
    """
    try:
        await workspace_sync.restore()
    except Exception as exc:
        if not _is_verdict(exc):
            raise
        _logger.exception('restore failed; acking and stopping the work item %s', work_id)
        await _fail_item(work_queue, work_id)
        return False
    _logger.info('restored /workspace; acking work item %s', work_id)
    await work_queue.ack(work_id)
    return True


async def _serve() -> None:
    session_token = _require('THEMIS_SESSION_TOKEN')
    environment_key = _require('ANTHROPIC_ENVIRONMENT_KEY')
    store_url = _require('THEMIS_STORE_URL')
    hello_url = _require('THEMIS_HELLO_URL')
    evidence_url = _require('THEMIS_EVIDENCE_URL')
    sheaf_url = _require('THEMIS_SHEAF_URL')
    for name in _SDK_ITEM_ENV:
        _require(name)
    session_id = os.environ['ANTHROPIC_SESSION_ID']
    work_id = os.environ['ANTHROPIC_WORK_ID']
    environment_id = os.environ['ANTHROPIC_ENVIRONMENT_ID']

    host_identity = _host_identity()
    profile = _build_profile(host_identity)
    try:
        postern.Sandbox(profile).verify()
    except (postern.IsolationError, RuntimeError) as exc:
        raise SystemExit(f'isolation self-test failed, refusing to serve: {exc}') from exc
    _logger.info('isolation verified; serving session %s (work %s)', session_id, work_id)

    store_credentials = id_token.channel_credentials(store_url)
    hello_credentials = id_token.channel_credentials(hello_url)
    evidence_credentials = id_token.channel_credentials(evidence_url)
    # The worker's own async channel drives the document checkpoint/restore. The hatch runs a synchronous
    # grpc.server, so its forwarders dial over synchronous channels, as does the mirror's store.
    async with (
        anthropic.AsyncAnthropic(
            auth_token=environment_key,
            default_headers={'anthropic-beta': environments.MANAGED_AGENTS_BETA},
        ) as client,
        grpc.aio.secure_channel(_grpc_target(store_url), store_credentials) as async_store,
    ):
        work_queue = work_queue_mod.AnthropicWorkQueue(client, environment_id=environment_id)
        hello_sync = grpc.secure_channel(_grpc_target(hello_url), hello_credentials)
        evidence_sync = grpc.secure_channel(_grpc_target(evidence_url), evidence_credentials)
        socket_dir = _socket_dir(host_identity)
        hatch = hatch_mod.build_hatch(
            hello_channel=hello_sync,
            evidence_channel=evidence_sync,
            session_token=session_token,
            socket_path=socket_dir / 'hatch.sock',
        )
        # The mirror is the one host-side git, at a path the guest never sees.
        with _mirror(sheaf_url, session_token) as mirror:
            hatches = git_hatches.GitHatches(mirror, protection=git_hatches.PROTECTION, socket_dir=socket_dir)
            sandbox = postern.Sandbox(profile, hatch=[hatch, *hatches.hatches])
            fetch_url, push_url = hatches.guest_urls(profile)
            repository = guest_git.GuestGit(
                sandbox,
                hatches,
                fetch_url=fetch_url,
                push_url=push_url,
                hydrate_timeout=_HYDRATE_TIMEOUT_S,
                teardown_timeout=_TEARDOWN_TIMEOUT_S,
            )
            accessor = sandbox.accessor()
            workspace_sync = sync_mod.WorkspaceSync(
                store_client.GrpcStore(async_store, session_token=session_token),
                accessor=accessor,
                repository=repository,
            )
            try:
                if not await _restore_or_fail_item(workspace_sync, work_queue, work_id):
                    return
                shell = tool_mod.make_shell(sandbox, workspace_sync, timeout=_TOOL_TIMEOUT_S)
                worker = environments.EnvironmentWorker(
                    client, tools=lambda ctx: _tools_for_session(ctx, shell), workdir=sandbox.workspace
                )
                try:
                    await worker.handle_item()
                except BaseException:
                    # The session loop failing is no reason to lose what the agent committed: push, then re-raise.
                    try:
                        await _to_completion(workspace_sync.teardown(session_id))
                    except Exception:
                        _logger.exception('teardown after a failed session loop failed too')
                    raise
                await _to_completion(workspace_sync.teardown(session_id))
                _logger.info('session %s complete; document checkpointed and branches pushed', session_id)
            finally:
                hatch.close()
                hatches.close()
                hello_sync.close()
                evidence_sync.close()
                accessor.close()
                sandbox.close()
                shutil.rmtree(socket_dir, ignore_errors=True)


async def _to_completion[T](coroutine: Coroutine[object, object, T]) -> T:
    """Await `coroutine` so that a cancellation arriving meanwhile is deferred until it has finished.

    For the teardown: cancelled midway, its push thread would keep running while the `finally`
    below closes the hatches under it. The cancellation is re-raised once the coroutine is done; a
    second cancellation while it waits is not deferred.
    """
    inner = asyncio.ensure_future(coroutine)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        try:
            await inner
        except Exception:
            _logger.exception('teardown failed while a cancellation waited for it')
        raise


async def _serve_until_terminated() -> None:
    """Run `_serve`, turning SIGTERM into a cancellation so the teardown push gets its chance.

    Cloud Run delivers SIGTERM on the task timeout and on cancellation, then SIGKILL after a short
    grace period; left as the default, the process dies with the agent's unpushed commits. Cancelling
    the task instead reaches the `except BaseException` around the session loop. Best-effort: the
    cancellation is delivered only once the in-flight tool call returns, and the push then has to
    fit in what remains of the grace period.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None  # noqa: S101 — inside a running coroutine by construction
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await _serve()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    logging.basicConfig(level=os.environ.get('THEMIS_LOG', 'INFO'))
    try:
        asyncio.run(_serve_until_terminated())
    except asyncio.CancelledError:
        raise SystemExit('terminated by SIGTERM; teardown attempted') from None


if __name__ == '__main__':
    main()
