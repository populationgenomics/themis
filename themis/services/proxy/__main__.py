"""Proxy entrypoint: restore /workspace, gate the agent, then forward and checkpoint (self-hosted-sandbox.md §4-§9).

Startup order matters. The proxy restores ``/workspace`` from the store (fail-closed on the working
document, fail-open on scratch), acks the work item only once restore is proven, and only then binds
its agent-facing listeners — the agent container's startup probe gates on those ports, so it always
boots onto the restored filesystem. During the run the HTTP listener reverse-proxies the Anthropic
route and drives the ``end_turn`` checkpoint off that stream; the localhost h2c gRPC listener forwards
the agent's internal-service calls. A SIGTERM triggers a best-effort final checkpoint. Everything is
configured from the dispatcher's per-execution env (fail-loud on a missing value).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import signal
import urllib.parse

import aiohttp
import grpc.aio
from aiohttp import web

from themis.clients import id_token
from themis.clients.work_queue import client as work_queue_mod
from themis.services.proxy import allowlist as allowlist_mod
from themis.services.proxy import anthropic_proxy as anthropic_mod
from themis.services.proxy import grpc_forward, store_client, tls
from themis.services.proxy import sync as sync_mod

_DEFAULT_BASE_URL = 'https://api.anthropic.com'
_logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def _grpc_host(url: str) -> str:
    """The service host without scheme or slash, for the gRPC ``:authority``.

    The ALB host-routes on the ``:authority``, so it must stay port-less to match the LB's port-less
    host rule; only the dial target (``_grpc_target``) carries ``:443``.
    """
    return urllib.parse.urlparse(url).netloc


def _grpc_target(url: str) -> str:
    host = _grpc_host(url)
    return host if ':' in host else f'{host}:443'


def build_anthropic_session(spki_pins: frozenset[str]) -> aiohttp.ClientSession:
    """The upstream client for the Anthropic route: SPKI-pinned, unbuffered, no idle timeout."""
    timeout = aiohttp.ClientTimeout(total=None, sock_read=None)  # SSE is minutes-quiet between tool calls
    return aiohttp.ClientSession(connector=tls.PinnedConnector(spki_pins), timeout=timeout, auto_decompress=False)


def _build_sync(session_token: str, internal_ca: bytes) -> sync_mod.WorkspaceSync:
    store_url = _require('THEMIS_STORE_URL')
    channel = grpc.aio.secure_channel(
        _grpc_target(store_url),
        id_token.channel_credentials(store_url, root_certificates=internal_ca),
        options=[('grpc.default_authority', _grpc_host(store_url))],
    )
    store = store_client.GrpcStore(channel, session_token=session_token)
    return sync_mod.WorkspaceSync(
        store,
        root=pathlib.Path(os.environ.get('THEMIS_WORKSPACE_ROOT', '/workspace')),
        document_path=pathlib.Path(_require('THEMIS_WORKING_DOCUMENT_PATH')),
    )


async def _restore_or_fail_item(
    workspace_sync: sync_mod.WorkspaceSync, work_queue: work_queue_mod.WorkQueue, work_id: str
) -> bool:
    """Restore ``/workspace`` and ack the item; on a store restore error, ack + stop it and report failure.

    A store error resolving the working document is terminal, not reclaimed: the document persists, so a
    re-triggered session restores it, whereas reclaiming would respawn into the same failure
    (self-hosted-sandbox.md §9). The ``ack`` stops reclaim; the ``stop`` terminates the item.

    Returns:
        Whether restore succeeded and the caller should proceed to serve the session.
    """
    try:
        await workspace_sync.restore()
    except grpc.aio.AioRpcError:
        _logger.exception('restore failed; failing the work item')
        await work_queue.ack(work_id)
        await work_queue.stop(work_id)
        return False
    _logger.info('restore complete; acking work item %s', work_id)
    await work_queue.ack(work_id)  # restore proven (§5)
    _logger.info('work item %s acked', work_id)
    return True


async def _start_http(proxy: anthropic_mod.AnthropicProxy) -> None:
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', proxy.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Bind all interfaces, not loopback: the agent's readiness gate is Cloud Run's TCP startup probe
    # on this port, and the probe dials the container interface, not 127.0.0.1. The job has no ingress,
    # so only the co-pod agent and the probe reach it (self-hosted-sandbox.md §6).
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get('THEMIS_PROXY_HTTP_PORT', '8080'))).start()  # noqa: S104


async def _start_grpc(session_token: str, internal_ca: bytes) -> grpc.aio.Server:
    forward_url = _require('THEMIS_FORWARD_UPSTREAM_URL')
    forward_channel = grpc.aio.secure_channel(
        _grpc_target(forward_url),
        id_token.channel_credentials(forward_url, root_certificates=internal_ca),
        options=[('grpc.default_authority', _grpc_host(forward_url))],
    )
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc_forward.ForwardProxy(
                forward_channel,
                allowed_methods=_require('THEMIS_FORWARD_METHODS').split(','),
                session_token=session_token,
            ),
        )
    )
    server.add_insecure_port(f'127.0.0.1:{int(os.environ.get("THEMIS_PROXY_GRPC_PORT", "8081"))}')  # h2c localhost
    await server.start()
    return server


def _install_sigterm_flush(workspace_sync: sync_mod.WorkspaceSync) -> None:
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def _flush() -> None:
        # asyncio holds only a weak reference to a task, so a bare create_task can be GC'd
        # before it finishes; keep a strong reference until it completes.
        task = loop.create_task(_final_checkpoint(workspace_sync))
        pending.add(task)
        task.add_done_callback(pending.discard)

    loop.add_signal_handler(signal.SIGTERM, _flush)


async def _final_checkpoint(workspace_sync: sync_mod.WorkspaceSync) -> None:
    try:
        await workspace_sync.checkpoint()
    except Exception:  # best-effort SIGTERM backstop; the end_turn checkpoint is the primary path (§9)
        _logger.exception('SIGTERM checkpoint flush failed')


async def _serve() -> None:
    session_token = _require('THEMIS_SESSION_TOKEN')
    environment_key = _require('ANTHROPIC_ENVIRONMENT_KEY')
    environment_id = _require('ANTHROPIC_ENVIRONMENT_ID')
    work_id = _require('ANTHROPIC_WORK_ID')
    upstream_base = os.environ.get('ANTHROPIC_BASE_URL', _DEFAULT_BASE_URL)
    anthropic_session = build_anthropic_session(frozenset(_require('THEMIS_ANTHROPIC_SPKI_PINS').split(',')))
    # The internal store and forward services sit behind the sandbox's internal load balancer, which
    # serves a self-signed certificate for their private hostnames (self-hosted-sandbox.md §8).
    internal_ca = _require('THEMIS_INTERNAL_CA_CERT').encode()

    workspace_sync = _build_sync(session_token, internal_ca)
    work_queue = work_queue_mod.AnthropicWorkQueue(
        anthropic_session, base_url=upstream_base, environment_id=environment_id, environment_key=environment_key
    )
    if not await _restore_or_fail_item(workspace_sync, work_queue, work_id):
        return  # restore failed: the item is acked + stopped, nothing to serve

    proxy = anthropic_mod.AnthropicProxy(
        anthropic_session,
        upstream_base=upstream_base,
        allowlist=allowlist_mod.AnthropicAllowlist(
            session_id=_require('ANTHROPIC_SESSION_ID'), work_id=work_id, environment_id=environment_id
        ),
        environment_key=environment_key,
        on_end_turn=workspace_sync.checkpoint,
    )
    # Bind the listeners only now: the agent's startup probe gates on them, so "ready" means restored.
    await _start_http(proxy)
    grpc_server = await _start_grpc(session_token, internal_ca)
    _install_sigterm_flush(workspace_sync)
    _logger.info('listeners bound; serving the session')
    await grpc_server.wait_for_termination()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
