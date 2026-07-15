"""HTTP/1.1 reverse proxy for the Anthropic route (self-hosted-sandbox.md §6).

The one agent-facing REST route. It forwards to a fixed upstream (``api.anthropic.com`` — never a
client-named host), permits only this session's/work item's paths (``allowlist``), injects the
environment key as ``Authorization: Bearer`` while stripping any client ``X-Api-Key``, refuses
``CONNECT``, and never follows a credential-carrying redirect. The response streams back unbuffered
with no idle timeout — the worker's SSE event stream is minutes-quiet between tool calls, so a
buffering proxy would stall tool delivery.

Upstream TLS (plain CA-validated) lives on the ``aiohttp.ClientSession`` the entrypoint builds.
"""

from __future__ import annotations

from collections.abc import Mapping

import aiohttp
from aiohttp import web

from themis.services.proxy import allowlist as allowlist_mod

# Per-hop headers (RFC 7230 §6.1) never forwarded end to end.
_HOP_BY_HOP = frozenset(
    {
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailers',
        'transfer-encoding',
        'upgrade',
    }
)
# Request headers the proxy owns: the upstream host + framing, and the client's credentials (the
# proxy strips them and injects its own). Content negotiation passes through untouched — the proxy
# streams the response body byte-for-byte and never inspects it.
_STRIP_REQUEST = _HOP_BY_HOP | {'host', 'content-length', 'x-api-key', 'authorization'}
# Response headers the streaming server layer re-derives.
_STRIP_RESPONSE = _HOP_BY_HOP | {'content-length'}

_METHOD_NOT_ALLOWED = 405
_FORBIDDEN = 403


class AnthropicProxy:
    """Reverse-proxy the Anthropic route with credential injection and path allowlisting."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        upstream_base: str,
        allowlist: allowlist_mod.AnthropicAllowlist,
        environment_key: str,
    ) -> None:
        self._session = session
        self._upstream_base = upstream_base.rstrip('/')
        self._allowlist = allowlist
        self._authorization = f'Bearer {environment_key}'

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method == 'CONNECT':
            return web.Response(status=_METHOD_NOT_ALLOWED, text='CONNECT not permitted')
        if not self._allowlist.permits(request.rel_url.raw_path):
            return web.Response(status=_FORBIDDEN, text='path not permitted')
        # Match the raw encoded path (request.path is pre-decoded, so canonicalize would never see a
        # real %2f); forward path+query so `?beta=true` reaches upstream.
        upstream_url = self._upstream_base + request.path_qs
        async with self._session.request(
            request.method,
            upstream_url,
            headers=self._request_headers(request.headers),
            data=request.content,
            allow_redirects=False,  # never follow a 3xx with the injected credential attached
        ) as upstream:
            response = web.StreamResponse(status=upstream.status, headers=_filter(upstream.headers, _STRIP_RESPONSE))
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():  # per-chunk flush, no buffering
                await response.write(chunk)
            await response.write_eof()
            return response

    def _request_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        forwarded = _filter(headers, _STRIP_REQUEST)
        forwarded['Authorization'] = self._authorization
        return forwarded


def _filter(headers: Mapping[str, str], strip: frozenset[str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in strip}
