"""HTTP/1.1 reverse proxy for the Anthropic route (self-hosted-sandbox.md §6).

The one agent-facing REST route. It forwards to a fixed upstream (``api.anthropic.com`` — never a
client-named host), permits only this session's/work item's paths (``allowlist``), injects the
environment key as ``Authorization: Bearer`` while stripping any client ``X-Api-Key``, refuses
``CONNECT``, and never follows a credential-carrying redirect. The response streams back unbuffered
with no idle timeout — the worker's SSE event stream is minutes-quiet between tool calls, so a
buffering proxy would stall tool delivery.

Upstream TLS (including the SPKI pin) lives on the ``aiohttp.ClientSession`` the entrypoint builds
with the pinning connector.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

import aiohttp
from aiohttp import web

from themis.services.proxy import allowlist as allowlist_mod
from themis.services.proxy import stream_tap

_logger = logging.getLogger(__name__)

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
# Request headers the proxy owns: the upstream host + framing, the client's credentials, and
# content negotiation (accept-encoding re-pinned to identity in _request_headers).
_STRIP_REQUEST = _HOP_BY_HOP | {'host', 'content-length', 'x-api-key', 'authorization', 'accept-encoding'}
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
        on_end_turn: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._upstream_base = upstream_base.rstrip('/')
        self._allowlist = allowlist
        self._authorization = f'Bearer {environment_key}'
        self._on_end_turn = on_end_turn
        self._checkpoints: set[asyncio.Task[None]] = set()

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
            detector = stream_tap.EndTurnDetector() if self._on_end_turn else None
            async for chunk in upstream.content.iter_any():  # per-chunk flush, no buffering
                await response.write(chunk)
                if detector is not None and detector.feed(chunk):
                    self._start_checkpoint()  # runs concurrently with the worker's release grace (§9)
            await response.write_eof()
            return response

    def _start_checkpoint(self) -> None:
        if self._on_end_turn is None:
            return
        task = asyncio.create_task(self._run_end_turn())
        self._checkpoints.add(task)  # hold a reference so the task is not GC'd mid-flight
        task.add_done_callback(self._checkpoints.discard)

    async def _run_end_turn(self) -> None:
        if self._on_end_turn is None:
            return
        try:
            await self._on_end_turn()
        except Exception:
            _logger.exception('end_turn checkpoint failed')

    def _request_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        forwarded = _filter(headers, _STRIP_REQUEST)
        forwarded['Authorization'] = self._authorization
        # Pin identity: the response body is streamed through undecoded, so an encoded
        # upstream reply would reach byte inspection still compressed.
        forwarded['Accept-Encoding'] = 'identity'
        return forwarded


def _filter(headers: Mapping[str, str], strip: frozenset[str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in strip}
