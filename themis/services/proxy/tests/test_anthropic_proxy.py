"""Tests for the Anthropic reverse proxy (allowlist, CONNECT, credential injection, no-redirect, streaming)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import aiohttp
from aiohttp import test_utils, web

from themis.services.proxy import allowlist as allowlist_mod
from themis.services.proxy import anthropic_proxy

_SID = 'sesn_abc'
_ALLOW = allowlist_mod.AnthropicAllowlist(session_id=_SID, work_id=_SID, environment_id='env_1')


async def _echo_headers(request: web.Request) -> web.Response:
    return web.json_response(
        {
            'authorization': request.headers.get('Authorization'),
            'x-api-key': request.headers.get('X-Api-Key'),
            'accept-encoding': request.headers.get('Accept-Encoding'),
        }
    )


async def _redirect(_request: web.Request) -> web.Response:
    return web.Response(status=302, headers={'Location': 'https://evil.example/'})


async def _stream_three(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
    await response.prepare(request)
    for i in range(3):
        await response.write(f'data: {i}\n\n'.encode())
    await response.write_eof()
    return response


async def _run[T](
    upstream_handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    calls: Callable[[test_utils.TestClient], Awaitable[T]],
) -> T:
    upstream_app = web.Application()
    upstream_app.router.add_route('*', '/{tail:.*}', upstream_handler)
    upstream = test_utils.TestServer(upstream_app)
    await upstream.start_server()

    session = aiohttp.ClientSession(auto_decompress=False)
    proxy = anthropic_proxy.AnthropicProxy(
        session, upstream_base=str(upstream.make_url('/')).rstrip('/'), allowlist=_ALLOW, environment_key='ENVKEY'
    )
    proxy_app = web.Application()
    proxy_app.router.add_route('*', '/{tail:.*}', proxy.handle)
    try:
        async with test_utils.TestClient(test_utils.TestServer(proxy_app)) as client:
            return await calls(client)
    finally:
        await session.close()
        await upstream.close()


def test_injects_environment_key_and_strips_client_api_key() -> None:
    async def calls(client: test_utils.TestClient) -> dict[str, str | None]:
        resp = await client.get(f'/v1/sessions/{_SID}', headers={'X-Api-Key': 'client-key'})
        assert resp.status == 200
        return await resp.json()

    body = asyncio.run(_run(_echo_headers, calls))
    assert body['authorization'] == 'Bearer ENVKEY'
    assert body['x-api-key'] is None


def test_forwards_client_accept_encoding() -> None:
    async def calls(client: test_utils.TestClient) -> dict[str, str | None]:
        resp = await client.get(f'/v1/sessions/{_SID}', headers={'Accept-Encoding': 'gzip, br'})
        assert resp.status == 200
        return await resp.json()

    # The proxy no longer inspects the response body, so content negotiation passes through untouched:
    # the client's Accept-Encoding reaches upstream unchanged.
    assert asyncio.run(_run(_echo_headers, calls))['accept-encoding'] == 'gzip, br'


def test_off_allowlist_path_is_forbidden() -> None:
    async def calls(client: test_utils.TestClient) -> int:
        return (await client.get('/v1/skills')).status

    assert asyncio.run(_run(_echo_headers, calls)) == 403


def test_connect_is_refused_by_the_handler() -> None:
    # Drive handle() directly so the handler's own 405 is exercised, not the framework's pre-routing
    # 404 (which a `>= 400` assertion would pass even if the CONNECT branch were deleted).
    class _NoUpstream:
        def request(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError('CONNECT must be refused before any upstream request')

    proxy = anthropic_proxy.AnthropicProxy(
        cast(aiohttp.ClientSession, _NoUpstream()),
        upstream_base='http://upstream',
        allowlist=_ALLOW,
        environment_key='ENVKEY',
    )
    request = test_utils.make_mocked_request('CONNECT', f'/v1/sessions/{_SID}')
    response = asyncio.run(proxy.handle(request))
    assert response.status == 405


def test_redirect_is_not_followed() -> None:
    async def calls(client: test_utils.TestClient) -> tuple[int, str | None]:
        resp = await client.get(f'/v1/sessions/{_SID}', allow_redirects=False)
        return resp.status, resp.headers.get('Location')

    status, location = asyncio.run(_run(_redirect, calls))
    assert status == 302
    assert location == 'https://evil.example/'  # returned to the agent, never followed with the credential


def test_streams_the_response_body() -> None:
    async def calls(client: test_utils.TestClient) -> bytes:
        resp = await client.get(f'/v1/sessions/{_SID}/events')
        return await resp.read()

    assert asyncio.run(_run(_stream_three, calls)) == b'data: 0\n\ndata: 1\n\ndata: 2\n\n'
