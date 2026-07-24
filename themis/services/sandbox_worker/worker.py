"""Worker entrypoint: verify isolation, restore /workspace, serve one session through postern, checkpoint (§4).

One trusted process per Job execution. The dispatcher claims a work item and injects its per-execution env
(``ANTHROPIC_WORK_ID`` / ``_ENVIRONMENT_ID`` / ``_SESSION_ID`` / ``_ENVIRONMENT_KEY``, plus the minted
``THEMIS_SESSION_TOKEN``). The worker:

1. ``Sandbox.verify()`` — a fail-closed boot gate: refuse to serve unless isolation is actually enforced here
   (empty netns, userns created, non-root guest, seccomp/arch covered). Off-platform (no bubblewrap) this raises,
   so the worker never runs model code unsandboxed.
2. Restore ``/workspace`` from the store (fail-closed on the working document).
3. Ack the work item, restore proven — this moves it out of the dispatcher's reclaimable set
   (``reclaim_older_than_ms``), so a session running longer than that window is not reclaimed and
   re-dispatched mid-flight. A spawn that dies before restore stays unacked and re-surfaces.
4. Run ``EnvironmentWorker.handle_item()`` — the SDK loop for the one claimed session. Its tools are the
   ``agent_toolset_20260401`` file tools (read/write/edit/glob/grep), which the SDK confines to ``workdir``
   (=/workspace) and so run in the trusted worker, plus ``shell`` — the sandboxed replacement for ``bash``
   that marshals every command into the guest behind the store/hello-forwarding hatch.
5. Checkpoint a final snapshot and exit (one execution per spawn — scale-to-zero preserved).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import urllib.parse
from collections.abc import Sequence

import anthropic
import grpc
import grpc.aio
import postern
from anthropic.lib import environments
from anthropic.lib.tools import agent_toolset

from themis.clients import id_token
from themis.clients.work_queue import client as work_queue_mod
from themis.services.sandbox_worker import hatch as hatch_mod
from themis.services.sandbox_worker import store_client
from themis.services.sandbox_worker import sync as sync_mod
from themis.services.sandbox_worker import tool as tool_mod

# The guest rootfs the Dockerfile assembles; postern binds it read-only as the guest's system dirs.
_GUEST_ROOTFS = '/opt/guest-root'
_WORKSPACE_ROOT = '/workspace'
# EnvironmentWorker downloads the session agent's skills into ``{workdir}/skills`` each session; workdir is
# /workspace so the guest reaches them at the managed-agents convention path. They are re-downloaded every
# spawn, so keep them out of the checkpoint (a stale copy must not persist / be restored).
_SKILLS_DIRNAME = 'skills'
# Must stay under the SDK's per-tool deadline (anthropic.lib TOOL_TIMEOUT, 150s): a sandbox run that
# outlives it makes the SDK abort the (non-cancellable) tool call, reporting a spurious timeout and
# skipping the post-call checkpoint.
_TOOL_TIMEOUT_S = 120
# handle_item() reads these from the env deep in the SDK; listed here to fail loud at the boundary.
_SDK_ITEM_ENV = ('ANTHROPIC_WORK_ID', 'ANTHROPIC_ENVIRONMENT_ID', 'ANTHROPIC_SESSION_ID')
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


def _build_profile() -> postern.SandboxProfile:
    """The profile the worker serves with: the guest rootfs and one writable /workspace (postern defaults else)."""
    return postern.SandboxProfile(rootfs=pathlib.Path(_GUEST_ROOTFS), workspace=pathlib.Path(_WORKSPACE_ROOT))


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


async def _restore_or_fail_item(
    workspace_sync: sync_mod.WorkspaceSync, work_queue: work_queue_mod.WorkQueue, work_id: str
) -> bool:
    """Restore ``/workspace``, then ack the work item; on a store restore error, ack + stop it instead.

    The ack is deferred until restore proves: acking moves the item out of the dispatcher's reclaimable set
    (``reclaim_older_than_ms``), so a session outliving that window is not reclaimed and re-dispatched
    mid-flight, while a spawn that dies before restore stays unacked and correctly re-surfaces. A store error
    resolving the working document is terminal — a respawn would hit the same failure — so the item is acked
    to stop reclaim and stopped to end it, rather than left to loop. A non-store error (e.g. a local disk
    write) is not terminal: it propagates, leaving the item unacked so reclaim retries it on a fresh spawn,
    which can clear a node-local fault.

    Returns:
        Whether restore succeeded and the caller should serve the session.

    Raises:
        Exception: Any restore error other than a store ``grpc.aio.AioRpcError`` — propagated uncaught so the
            unacked item reclaims onto a fresh spawn.
    """
    try:
        await workspace_sync.restore()
    except grpc.aio.AioRpcError:
        _logger.exception('restore failed; acking and stopping the work item %s', work_id)
        await work_queue.ack(work_id)
        await work_queue.stop(work_id)
        return False
    _logger.info('restored /workspace; acking work item %s', work_id)
    await work_queue.ack(work_id)
    return True


async def _serve() -> None:
    session_token = _require('THEMIS_SESSION_TOKEN')
    environment_key = _require('ANTHROPIC_ENVIRONMENT_KEY')
    store_url = _require('THEMIS_STORE_URL')
    hello_url = _require('THEMIS_HELLO_URL')
    for name in _SDK_ITEM_ENV:
        _require(name)
    session_id = os.environ['ANTHROPIC_SESSION_ID']
    work_id = os.environ['ANTHROPIC_WORK_ID']
    environment_id = os.environ['ANTHROPIC_ENVIRONMENT_ID']

    profile = _build_profile()
    try:
        postern.Sandbox(profile).verify()
    except (postern.IsolationError, RuntimeError) as exc:
        raise SystemExit(f'isolation self-test failed, refusing to serve: {exc}') from exc
    _logger.info('isolation verified; serving session %s (work %s)', session_id, work_id)

    store_credentials = id_token.channel_credentials(store_url)
    hello_credentials = id_token.channel_credentials(hello_url)
    # The worker's own async channel to the store drives checkpoint/restore. The hatch runs a synchronous
    # grpc.server, so its hello forwarder dials over a synchronous channel.
    async with (
        anthropic.AsyncAnthropic(
            auth_token=environment_key,
            default_headers={'anthropic-beta': environments.MANAGED_AGENTS_BETA},
        ) as client,
        grpc.aio.secure_channel(_grpc_target(store_url), store_credentials) as async_store,
    ):
        work_queue = work_queue_mod.AnthropicWorkQueue(client, environment_id=environment_id)
        hello_sync = grpc.secure_channel(_grpc_target(hello_url), hello_credentials)
        hatch = hatch_mod.build_hatch(hello_sync, session_token=session_token)
        sandbox = postern.Sandbox(profile, hatch=hatch)
        accessor = sandbox.accessor()
        workspace_sync = sync_mod.WorkspaceSync(
            store_client.GrpcStore(async_store, session_token=session_token),
            accessor=accessor,
            exclude={_SKILLS_DIRNAME},
        )
        try:
            if not await _restore_or_fail_item(workspace_sync, work_queue, work_id):
                return
            shell = tool_mod.make_shell(sandbox, workspace_sync, timeout=_TOOL_TIMEOUT_S)
            worker = environments.EnvironmentWorker(
                client, tools=lambda ctx: _tools_for_session(ctx, shell), workdir=sandbox.workspace
            )
            await worker.handle_item()
            await workspace_sync.checkpoint()
            _logger.info('session %s complete; final checkpoint written', session_id)
        finally:
            hatch.close()
            hello_sync.close()
            accessor.close()
            sandbox.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get('THEMIS_LOG', 'INFO'))
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
