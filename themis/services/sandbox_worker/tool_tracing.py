"""Spans over a session and each tool call it makes, read in Cloud Trace.

One trace per session: `session` opens its root span, and every tool call the SDK dispatches inside it is a child
span, so a call's wall time, the launch of the guest it ran in included, is read off the session's timeline. A span
carries the tool's name and, for a call that raised or was cancelled, the exception's type: never the call's input,
its result or an exception's message, which hold the agent's working data. Attribute names are OpenTelemetry's GenAI
semantic conventions. The trace ends at the worker: the services the hatch and the mirror reach start traces of their
own.

Spans reach Cloud Trace only once the process installs a tracer provider (`themis.telemetry.tracing`); until then
each is a no-op.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import override

from anthropic.lib import tools
from anthropic.types import beta
from opentelemetry import trace

_TRACER = trace.get_tracer(__name__)

_OPERATION_NAME = 'gen_ai.operation.name'
_TOOL_NAME = 'gen_ai.tool.name'
_CONVERSATION_ID = 'gen_ai.conversation.id'
_ERROR_TYPE = 'error.type'
_EXECUTE_TOOL = 'execute_tool'
# What the SDK runs when the session ends, looked up on the tool it was handed.
_CLEANUPS = ('aclose', 'close', '_context_manager')


def mark_failed(span: trace.Span, error_type: str) -> None:
    """Record that the work `span` covers failed, as `error_type`, without raising out of it."""
    span.set_attribute(_ERROR_TYPE, error_type)
    span.set_status(trace.StatusCode.ERROR)


@contextlib.contextmanager
def _span(name: str, attributes: Mapping[str, str]) -> Iterator[trace.Span]:
    # OpenTelemetry's default recording puts an exception's message on the span, and it can quote the workspace; it
    # also misses a cancellation, which is how the SDK's per-call timeout ends a call.
    with _TRACER.start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except BaseException as exc:
            mark_failed(span, f'{type(exc).__module__}.{type(exc).__qualname__}')
            raise


@contextlib.contextmanager
def session(session_id: str) -> Iterator[trace.Span]:
    """The span every tool call `session_id` makes under it is a child of; an exception out of it marks it failed."""
    with _span('sandbox session', {_CONVERSATION_ID: session_id}) as span:
        yield span


class TracedTool(tools.BetaAsyncBuiltinFunctionTool):
    """An async tool as the session sees it, each call wrapped in a span named for the tool.

    Attributes:
        tool: The tool each call runs.
    """

    def __init__(self, tool: tools.BetaAsyncFunctionTool | tools.BetaAsyncBuiltinFunctionTool) -> None:
        """Stand in for `tool`, whose name and schema the session is shown.

        Raises:
            ValueError: If `tool` has a cleanup the SDK runs when the session ends: the SDK finds it on the tool it
                was handed, so behind this wrapper it would never run.
        """
        if any(getattr(tool, cleanup, None) is not None for cleanup in _CLEANUPS):
            raise ValueError(f'tool {tool.name!r} has a cleanup the SDK would not find behind a span')
        self.tool = tool

    @override
    def to_dict(self) -> beta.BetaToolUnionParam:
        return self.tool.to_dict()

    @override
    async def call(self, input: object) -> tools.BetaFunctionToolResultType:
        with _span(f'{_EXECUTE_TOOL} {self.name}', {_OPERATION_NAME: _EXECUTE_TOOL, _TOOL_NAME: self.name}):
            return await self.tool.call(input)
