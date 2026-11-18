"""An `httpx2.MockTransport` harness speaking Anthropic's `/v1/messages` Server-Sent Events protocol.

Drives a real `anthropic.AsyncAnthropic` client over in-memory SSE streams, HTTP error responses, dropped
streams, stalled streams, and connection failures, while recording each parsed `/v1/messages` request.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import override

import anthropic
import httpx2
from anthropic.lib import credentials as anthropic_credentials

HOST = 'api.anthropic.com'
MESSAGES_PATH = '/v1/messages'
_DEFAULT_TOKEN = 'test-anthropic-access-token'  # noqa: S105 — synthetic test credential


def credentials() -> anthropic_credentials.AccessTokenProvider:
    """Return a fresh synthetic access-token provider for offline `AsyncAnthropic` calls."""
    return lambda *, force_refresh=False: anthropic_credentials.AccessToken(  # noqa: ARG005
        token=_DEFAULT_TOKEN
    )


@dataclasses.dataclass(frozen=True)
class Text:
    """A text content block streamed as `text_delta`."""

    text: str


@dataclasses.dataclass(frozen=True)
class Thinking:
    """A thinking content block streamed as `thinking_delta` + `signature_delta`."""

    thinking: str
    signature: str = 'sig'


@dataclasses.dataclass(frozen=True)
class ToolUse:
    """A `tool_use` content block streamed as `input_json_delta`."""

    id: str
    name: str
    input: Mapping[str, object]


type Block = Text | Thinking | ToolUse


@dataclasses.dataclass(frozen=True)
class Usage:
    """Token counts carried on `message_start` and `message_delta`."""

    input_tokens: int = 1000
    output_tokens: int = 10
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Policy refusal metadata carried on `message_delta.delta.stop_details`."""

    category: str | None = None
    explanation: str | None = None


@dataclasses.dataclass(frozen=True)
class MessageTurn:
    """A complete `/v1/messages` SSE stream ending with `message_stop`."""

    stop_reason: str | None = 'end_turn'
    content: Sequence[Block] = ()
    model: str = 'claude-sonnet-5'
    usage: Usage = dataclasses.field(default_factory=Usage)
    request_id: str = 'req'
    message_id: str = 'msg'
    refusal: Refusal | None = None


@dataclasses.dataclass(frozen=True)
class StatusError:
    """A non-2xx JSON error response returned before streaming begins."""

    status_code: int
    error_type: str
    message: str
    details: Mapping[str, object] | None = None
    request_id: str = 'req'


@dataclasses.dataclass(frozen=True)
class StreamError:
    """An HTTP 200 SSE stream emitting an `event: error` frame before or after `message_start`."""

    error_type: str
    message: str
    details: Mapping[str, object] | None = None
    after_start: bool = True
    model: str = 'claude-sonnet-5'
    usage: Usage = dataclasses.field(default_factory=Usage)
    request_id: str = 'req'


@dataclasses.dataclass(frozen=True)
class DroppedStream:
    """An HTTP 200 SSE stream raising a transport error mid-stream."""

    error: Exception = dataclasses.field(default_factory=lambda: httpx2.ReadError('connection reset'))
    after_start: bool = True
    model: str = 'claude-sonnet-5'
    usage: Usage = dataclasses.field(default_factory=Usage)
    request_id: str = 'req'


@dataclasses.dataclass(frozen=True)
class StalledStream:
    """An SSE turn that blocks awaiting `release` (never set by default), before or after `message_start`."""

    after_start: bool
    model: str = 'claude-sonnet-5'
    usage: Usage = dataclasses.field(default_factory=Usage)
    release: asyncio.Event | None = None
    request_id: str = 'req'


@dataclasses.dataclass(frozen=True)
class ConnectFailure:
    """A transport failure raised before any HTTP response arrives."""

    message: str = 'connection refused'


type Turn = MessageTurn | StatusError | StreamError | DroppedStream | StalledStream | ConnectFailure


@dataclasses.dataclass(frozen=True)
class RecordedRequest:
    """One `/v1/messages` POST captured by `Harness`."""

    headers: httpx2.Headers
    url: httpx2.URL
    body: dict[str, object]

    @property
    def messages(self) -> list[dict[str, object]]:
        raw = self.body['messages']
        if not isinstance(raw, list):
            raise TypeError(f'expected messages list, got {type(raw).__name__}')
        return raw

    def content_blocks(self, message_index: int = 0) -> list[dict[str, object]]:
        """The content blocks of `messages[message_index]`, requiring a block list."""
        content = self.messages[message_index]['content']
        if not isinstance(content, list):
            raise TypeError(f'expected content block list, got {type(content).__name__}')
        return content


def _sse_frame(event: str, payload: Mapping[str, object]) -> bytes:
    return f'event: {event}\ndata: {json.dumps(payload)}\n\n'.encode()


def _message_start_frame(*, message_id: str, model: str, usage: Usage, output_tokens: int) -> bytes:
    usage_payload: dict[str, object] = {
        'input_tokens': usage.input_tokens,
        'output_tokens': output_tokens,
    }
    if usage.cache_read_input_tokens is not None:
        usage_payload['cache_read_input_tokens'] = usage.cache_read_input_tokens
    if usage.cache_creation_input_tokens is not None:
        usage_payload['cache_creation_input_tokens'] = usage.cache_creation_input_tokens
    return _sse_frame(
        'message_start',
        {
            'type': 'message_start',
            'message': {
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'model': model,
                'content': [],
                'stop_reason': None,
                'stop_sequence': None,
                'usage': usage_payload,
            },
        },
    )


def _block_frames(index: int, block: Block) -> list[bytes]:
    if isinstance(block, Text):
        frames = [
            _sse_frame(
                'content_block_start',
                {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
            )
        ]
        if block.text:
            frames.append(
                _sse_frame(
                    'content_block_delta',
                    {
                        'type': 'content_block_delta',
                        'index': index,
                        'delta': {'type': 'text_delta', 'text': block.text},
                    },
                )
            )
        frames.append(_sse_frame('content_block_stop', {'type': 'content_block_stop', 'index': index}))
        return frames
    if isinstance(block, Thinking):
        return [
            _sse_frame(
                'content_block_start',
                {
                    'type': 'content_block_start',
                    'index': index,
                    'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''},
                },
            ),
            _sse_frame(
                'content_block_delta',
                {
                    'type': 'content_block_delta',
                    'index': index,
                    'delta': {'type': 'thinking_delta', 'thinking': block.thinking},
                },
            ),
            _sse_frame(
                'content_block_delta',
                {
                    'type': 'content_block_delta',
                    'index': index,
                    'delta': {'type': 'signature_delta', 'signature': block.signature},
                },
            ),
            _sse_frame('content_block_stop', {'type': 'content_block_stop', 'index': index}),
        ]
    return [
        _sse_frame(
            'content_block_start',
            {
                'type': 'content_block_start',
                'index': index,
                'content_block': {'type': 'tool_use', 'id': block.id, 'name': block.name, 'input': {}},
            },
        ),
        _sse_frame(
            'content_block_delta',
            {
                'type': 'content_block_delta',
                'index': index,
                'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block.input)},
            },
        ),
        _sse_frame('content_block_stop', {'type': 'content_block_stop', 'index': index}),
    ]


def _message_frames(turn: MessageTurn) -> list[bytes]:
    frames = [_message_start_frame(message_id=turn.message_id, model=turn.model, usage=turn.usage, output_tokens=1)]
    for idx, block in enumerate(turn.content):
        frames.extend(_block_frames(idx, block))
    delta: dict[str, object] = {'stop_reason': turn.stop_reason, 'stop_sequence': None}
    if turn.refusal is not None:
        delta['stop_details'] = {
            'type': 'refusal',
            'category': turn.refusal.category,
            'explanation': turn.refusal.explanation,
        }
    frames.append(
        _sse_frame(
            'message_delta',
            {
                'type': 'message_delta',
                'delta': delta,
                'usage': {'output_tokens': turn.usage.output_tokens},
            },
        )
    )
    frames.append(_sse_frame('message_stop', {'type': 'message_stop'}))
    return frames


def _error_payload(error_type: str, message: str, details: Mapping[str, object] | None) -> dict[str, object]:
    error: dict[str, object] = {'type': error_type, 'message': message}
    if details is not None:
        error['details'] = dict(details)
    return {'type': 'error', 'error': error}


class _AsyncSseStream(httpx2.AsyncByteStream):
    """Streams SSE frames, optionally awaiting an event or raising a transport error after them."""

    def __init__(
        self,
        frames: Sequence[bytes],
        *,
        stall_on: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self._frames = tuple(frames)
        self._stall_on = stall_on
        self._error = error

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame
        if self._stall_on is not None:
            await self._stall_on.wait()
        if self._error is not None:
            raise self._error


def _sse_headers(request_id: str) -> dict[str, str]:
    return {'content-type': 'text/event-stream', 'request-id': request_id}


async def _respond(turn: Turn, request: httpx2.Request) -> httpx2.Response:
    if isinstance(turn, ConnectFailure):
        raise httpx2.ConnectError(turn.message, request=request)
    if isinstance(turn, StatusError):
        body = json.dumps(_error_payload(turn.error_type, turn.message, turn.details)).encode()
        return httpx2.Response(
            turn.status_code,
            headers={'content-type': 'application/json', 'request-id': turn.request_id},
            content=body,
        )
    if isinstance(turn, MessageTurn):
        return httpx2.Response(
            200,
            headers=_sse_headers(turn.request_id),
            content=b''.join(_message_frames(turn)),
        )
    if isinstance(turn, StreamError):
        frames: list[bytes] = []
        if turn.after_start:
            frames.append(
                _message_start_frame(
                    message_id='msg', model=turn.model, usage=turn.usage, output_tokens=turn.usage.output_tokens
                )
            )
        frames.append(_sse_frame('error', _error_payload(turn.error_type, turn.message, turn.details)))
        return httpx2.Response(200, headers=_sse_headers(turn.request_id), content=b''.join(frames))
    if isinstance(turn, DroppedStream):
        frames = (
            [
                _message_start_frame(
                    message_id='msg', model=turn.model, usage=turn.usage, output_tokens=turn.usage.output_tokens
                )
            ]
            if turn.after_start
            else []
        )
        return httpx2.Response(
            200,
            headers=_sse_headers(turn.request_id),
            stream=_AsyncSseStream(frames, error=turn.error),
        )
    gate = turn.release if turn.release is not None else asyncio.Event()
    if not turn.after_start:
        await gate.wait()
    frames = (
        [
            _message_start_frame(
                message_id='msg', model=turn.model, usage=turn.usage, output_tokens=turn.usage.output_tokens
            )
        ]
        if turn.after_start
        else []
    )
    return httpx2.Response(
        200,
        headers=_sse_headers(turn.request_id),
        stream=_AsyncSseStream(frames, stall_on=gate if turn.after_start else None),
    )


class Harness:
    """A stateful `/v1/messages` SSE responder recording every incoming request.

    Answers either from a FIFO `turns` queue or from a custom `responder(RecordedRequest) -> Turn`
    when concurrent conversations must be routed by request content.
    """

    def __init__(
        self,
        turns: Sequence[Turn] = (),
        *,
        responder: Callable[[RecordedRequest], Turn] | None = None,
    ) -> None:
        if turns and responder is not None:
            raise ValueError('pass either turns or responder, not both')
        self._turns: collections.deque[Turn] = collections.deque(turns)
        self._responder = responder
        self.requests: list[RecordedRequest] = []
        self.transport = httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != 'POST' or request.url.host != HOST or request.url.path != MESSAGES_PATH:
            raise ValueError(f'unexpected Anthropic request: {request.method} {request.url}')
        body = json.loads(request.content.decode('utf-8'))
        if not isinstance(body, dict) or body.get('stream') is not True:
            raise ValueError(f'expected streamed /v1/messages JSON object, got {body!r}')
        recorded = RecordedRequest(headers=request.headers, url=request.url, body=body)
        self.requests.append(recorded)
        if self._responder is not None:
            turn = self._responder(recorded)
        elif self._turns:
            turn = self._turns.popleft()
        else:
            raise AssertionError(f'no scripted Anthropic turn left for request #{len(self.requests)}')
        return await _respond(turn, request)

    def http_client(self) -> httpx2.AsyncClient:
        """Build a fresh `httpx2.AsyncClient` bound to this harness's transport."""
        return httpx2.AsyncClient(transport=self.transport)

    def client(self, *, max_retries: int = 0) -> anthropic.AsyncAnthropic:
        """Build a real `anthropic.AsyncAnthropic` bound to this harness."""
        return anthropic.AsyncAnthropic(
            credentials=credentials(),
            http_client=self.http_client(),
            max_retries=max_retries,
        )
