"""Proxy entrypoint: the Anthropic reverse proxy and the gRPC forward proxy on one event loop.

Two agent-facing listeners, configured entirely from the per-execution env the dispatcher injects
into this container (the session/work/environment ids, the environment key, the session token). The
HTTP listener reverse-proxies the Anthropic route; the localhost h2c gRPC listener forwards the
agent's internal-service calls. Both bind ``127.0.0.1`` — reachable from the agent container over the
shared network namespace, never from outside the instance.

This is the forwarding half of the proxy; the workspace-sync + restore/ack lifecycle lands in the
same binary next.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import grpc.aio
from aiohttp import web

from themis.clients import id_token
from themis.services.proxy import allowlist as allowlist_mod
from themis.services.proxy import anthropic_proxy as anthropic_mod
from themis.services.proxy import grpc_forward, tls

_DEFAULT_BASE_URL = 'https://api.anthropic.com'


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def _grpc_target(url: str) -> str:
    host = url.split('://', 1)[-1].rstrip('/')
    return host if ':' in host else f'{host}:443'


def build_anthropic_session(spki_pins: frozenset[str]) -> aiohttp.ClientSession:
    """The upstream client for the Anthropic route: SPKI-pinned, unbuffered, no idle timeout."""
    timeout = aiohttp.ClientTimeout(total=None, sock_read=None)  # SSE is minutes-quiet between tool calls
    return aiohttp.ClientSession(connector=tls.PinnedConnector(spki_pins), timeout=timeout, auto_decompress=False)


async def _serve() -> None:
    allow = allowlist_mod.AnthropicAllowlist(
        session_id=_require('ANTHROPIC_SESSION_ID'),
        work_id=_require('ANTHROPIC_WORK_ID'),
        environment_id=_require('ANTHROPIC_ENVIRONMENT_ID'),
    )
    proxy = anthropic_mod.AnthropicProxy(
        build_anthropic_session(frozenset(_require('THEMIS_ANTHROPIC_SPKI_PINS').split(','))),
        upstream_base=os.environ.get('ANTHROPIC_BASE_URL', _DEFAULT_BASE_URL),
        allowlist=allow,
        environment_key=_require('ANTHROPIC_ENVIRONMENT_KEY'),
    )
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', proxy.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', int(os.environ.get('THEMIS_PROXY_HTTP_PORT', '8080'))).start()

    forward_url = _require('THEMIS_FORWARD_UPSTREAM_URL')
    forward_channel = grpc.aio.secure_channel(_grpc_target(forward_url), id_token.channel_credentials(forward_url))
    grpc_server = grpc.aio.server()
    grpc_server.add_generic_rpc_handlers(
        (
            grpc_forward.ForwardProxy(
                forward_channel,
                allowed_methods=_require('THEMIS_FORWARD_METHODS').split(','),
                session_token=_require('THEMIS_SESSION_TOKEN'),
            ),
        )
    )
    grpc_server.add_insecure_port(f'127.0.0.1:{int(os.environ.get("THEMIS_PROXY_GRPC_PORT", "8081"))}')  # h2c localhost
    await grpc_server.start()
    await grpc_server.wait_for_termination()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
