"""Proxy entrypoint: restore /workspace, gate the agent, then forward and checkpoint (self-hosted-sandbox.md §4-§9).

Startup order matters. The proxy restores ``/workspace`` from the store (fail-closed on the working
document, fail-open on scratch), acks the work item only once restore is proven, and only then binds
its agent-facing listeners — the agent container's startup probe gates on those ports, so it always
boots onto the restored filesystem. During the run the HTTP listener reverse-proxies the Anthropic
route; a separate subscription to the session's event stream drives the working-document checkpoint at
each ``session.status_idle`` turn boundary; the localhost h2c gRPC listener forwards the agent's
internal-service calls. That subscription also ends the run: on ``session.status_terminated`` it flushes
a final checkpoint and stops the listeners so the Job task completes.
Everything is configured from the dispatcher's per-execution env (fail-loud on a missing value).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import urllib.parse
from collections.abc import AsyncIterable

import aiohttp
import anthropic
import grpc.aio
from aiohttp import web
from anthropic.types.beta import sessions as anthropic_sessions

from themis.clients import id_token
from themis.clients.work_queue import client as work_queue_mod
from themis.services.proxy import allowlist as allowlist_mod
from themis.services.proxy import anthropic_proxy as anthropic_mod
from themis.services.proxy import grpc_forward, store_client
from themis.services.proxy import sync as sync_mod

_DEFAULT_BASE_URL = 'https://api.anthropic.com'
# Bound the store write so a cold-started or unreachable store fails loud rather than hanging.
_CHECKPOINT_TIMEOUT_S = 180
# A dropped session-event stream is expected on a long-lived connection; back off before reconnecting.
_RECONNECT_BACKOFF_S = 2.0
_CONNECT_TIMEOUT_S = 10.0
# Read timeout is the liveness floor: the server heartbeats a healthy stream well within it, so it never
# false-trips, but a dead (half-open) connection delivers nothing and trips it → the SDK raises a timeout
# (an APIConnectionError subclass) and we reconnect. Must exceed the server's heartbeat interval.
_STREAM_READ_TIMEOUT_S = 600.0
# On a clean session end, drain in-flight forward RPCs for up to this long before stopping the server.
_SHUTDOWN_GRACE_S = 5.0
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


def build_anthropic_session() -> aiohttp.ClientSession:
    """The upstream client for the Anthropic route: unbuffered, no idle timeout."""
    timeout = aiohttp.ClientTimeout(total=None, sock_read=None)  # SSE is minutes-quiet between tool calls
    return aiohttp.ClientSession(timeout=timeout, auto_decompress=False)


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


async def _serve() -> None:
    session_token = _require('THEMIS_SESSION_TOKEN')
    environment_key = _require('ANTHROPIC_ENVIRONMENT_KEY')
    environment_id = _require('ANTHROPIC_ENVIRONMENT_ID')
    work_id = _require('ANTHROPIC_WORK_ID')
    upstream_base = os.environ.get('ANTHROPIC_BASE_URL', _DEFAULT_BASE_URL)
    anthropic_session = build_anthropic_session()
    # The internal store and forward services sit behind the sandbox's internal load balancer, which
    # serves a self-signed certificate for their private hostnames (self-hosted-sandbox.md §8).
    internal_ca = _require('THEMIS_INTERNAL_CA_CERT').encode()

    # One SDK client for both the work queue (ack/stop) and the turn-watch event stream. Its read
    # timeout is the stream's liveness floor; ack/stop override it per-call (_ACK_TIMEOUT_S).
    timeout = anthropic.Timeout(_STREAM_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
    async with anthropic.AsyncAnthropic(
        auth_token=environment_key, base_url=upstream_base, timeout=timeout
    ) as anthropic_client:
        workspace_sync = _build_sync(session_token, internal_ca)
        work_queue = work_queue_mod.AnthropicWorkQueue(anthropic_client, environment_id=environment_id)
        if not await _restore_or_fail_item(workspace_sync, work_queue, work_id):
            return  # restore failed: the item is acked + stopped, nothing to serve

        session_id = _require('ANTHROPIC_SESSION_ID')
        proxy = anthropic_mod.AnthropicProxy(
            anthropic_session,
            upstream_base=upstream_base,
            allowlist=allowlist_mod.AnthropicAllowlist(
                session_id=session_id, work_id=work_id, environment_id=environment_id
            ),
            environment_key=environment_key,
        )
        # Bind the listeners only now: the agent's startup probe gates on them, so "ready" means restored.
        await _start_http(proxy)
        grpc_server = await _start_grpc(session_token, internal_ca)
        # Checkpoint the working document on each turn boundary: the worker CLI reads events paginated, so
        # end_turn never crosses the reverse-proxied traffic — the proxy reads the session event stream (§9).
        turn_watcher = asyncio.create_task(_watch_turns(anthropic_client, session_id, workspace_sync))
        _logger.info('listeners bound; serving the session')
        # The turn-watcher owns the session lifecycle: it returns on session.status_terminated (a clean end)
        # and raises on an unrecoverable stream fault. Either ends the serve — a stopped watcher means the
        # proxy would otherwise keep forwarding with no checkpointing.
        serve = asyncio.create_task(grpc_server.wait_for_termination())
        try:
            await asyncio.wait({serve, turn_watcher}, return_when=asyncio.FIRST_COMPLETED)
            if turn_watcher.done():
                turn_watcher.result()  # re-raise an unrecoverable stream fault
        finally:
            turn_watcher.cancel()
            await grpc_server.stop(_SHUTDOWN_GRACE_S)  # drain in-flight forwards; unblocks wait_for_termination
            serve.cancel()
            await asyncio.gather(serve, turn_watcher, return_exceptions=True)


async def _watch_turns(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    workspace_sync: sync_mod.WorkspaceSync,
) -> None:
    """Checkpoint the working document on each turn boundary of the session (§9).

    The worker CLI reads its events paginated, so an ``end_turn`` never crosses the reverse-proxied
    agent traffic. The proxy holds the environment key and the upstream, so it subscribes to the
    session's own event stream via the Anthropic SDK and checkpoints on ``session.status_idle`` /
    ``end_turn``.

    A dropped stream is expected on a long-lived connection: reconnect, re-page the event history, and
    dedupe by event id so a boundary that fell in the drop gap still checkpoints. A 4xx status (auth,
    permission, an unknown session) is a real fault that retrying can't fix — it propagates loudly; a
    429/5xx is a transient upstream condition and reconnects like a drop.

    On ``session.status_terminated`` — seen live or on a reconnect's history re-page — the run is over:
    flush a final checkpoint and return, which drives ``_serve`` to stop the listeners so the Job task
    completes.
    """
    seen: set[str] = set()
    primed = False
    while True:
        try:
            # The restored /workspace already reflects every boundary up to the latest checkpoint, so
            # the first history pass seeds the high-water mark without re-checkpointing; a reconnect
            # re-pages and checkpoints any boundary — and detects a termination — that fell in the gap.
            terminated = await _process_events(
                client.beta.sessions.events.list(session_id), workspace_sync, seen, checkpoint=primed
            )
            primed = True
            if not terminated:
                async with await client.beta.sessions.events.stream(session_id) as stream:
                    terminated = await _process_events(stream, workspace_sync, seen, checkpoint=True)
            if terminated:
                _logger.info('session terminated; final checkpoint, then stopping the proxy')
                await _checkpoint(workspace_sync)
                return
            # The stream closed with no terminated event — a drop, not a session end. Back off and
            # reconnect; re-paging the history covers any boundary that fell in the gap.
            _logger.info('turn-watch stream closed; reconnecting')
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
        except asyncio.CancelledError:
            raise
        except anthropic.APIStatusError as error:
            if error.status_code < 500 and error.status_code != 429:
                _logger.exception('turn-watch stream: unrecoverable %s; not reconnecting', error.status_code)
                raise
            _logger.warning('turn-watch stream: retryable %s; reconnecting', error.status_code, exc_info=True)
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
        except anthropic.APIConnectionError:
            _logger.warning('turn-watch stream dropped; reconnecting', exc_info=True)
            await asyncio.sleep(_RECONNECT_BACKOFF_S)


async def _process_events(
    events: AsyncIterable[anthropic_sessions.BetaManagedAgentsStreamSessionEvents],
    workspace_sync: sync_mod.WorkspaceSync,
    seen: set[str],
    *,
    checkpoint: bool,
) -> bool:
    """Checkpoint each new turn boundary; return whether ``session.status_terminated`` was seen.

    ``checkpoint`` gates the boundary checkpoint. The first history pass seeds ``seen`` as a high-water
    mark with ``checkpoint=False`` — the restored /workspace already reflects every prior boundary, so
    re-checkpointing each would re-upload the whole scratch tree once per historical turn. The live
    stream and a reconnect's history re-page pass ``checkpoint=True`` so a boundary that fell in a drop
    gap is still checkpointed. Termination is detected on every pass, so a session that ends during a gap
    shuts down on reconnect rather than re-streaming an already-ended session.
    """
    async for event in events:
        if _is_new_turn_boundary(event, seen) and checkpoint:
            await _checkpoint(workspace_sync)
        if event.type == 'session.status_terminated':
            return True
    return False


def _is_new_turn_boundary(event: anthropic_sessions.BetaManagedAgentsStreamSessionEvents, seen: set[str]) -> bool:
    """True on the first sighting of a ``session.status_idle`` / ``end_turn`` boundary (marks it seen)."""
    if event.type != 'session.status_idle':
        return False
    if event.stop_reason.type != 'end_turn':
        return False
    if event.id in seen:
        return False
    seen.add(event.id)
    return True


async def _checkpoint(workspace_sync: sync_mod.WorkspaceSync) -> None:
    try:
        await asyncio.wait_for(workspace_sync.checkpoint(), _CHECKPOINT_TIMEOUT_S)
    except TimeoutError:
        _logger.error('working-document checkpoint timed out after %ss', _CHECKPOINT_TIMEOUT_S)
    except Exception:
        _logger.exception('working-document checkpoint failed')
    else:
        _logger.info('working-document checkpointed')


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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
