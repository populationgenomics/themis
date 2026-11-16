"""Spans around litcache work: recorded where the process has a tracer provider, a no-op everywhere else.

A span says that its work failed and with what type of exception, never the exception's message: a step's error
can quote the paper it was reading, and a trace carries ids, not content.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence

from opentelemetry import context as otel_context
from opentelemetry import trace

_TRACER = trace.get_tracer('themis.litcache')

DOC_ID = 'litcache.doc_id'
# The attempt a span's work belongs to, as the paper's job log names it.
ATTEMPT_ID = 'litcache.attempt_id'
ERROR_TYPE = 'error.type'


@contextlib.contextmanager
def span(
    name: str,
    attributes: Mapping[str, str | int | Sequence[str]],
    *,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
    parent: otel_context.Context | None = None,
    routine: tuple[type[BaseException], ...] = (),
) -> Iterator[trace.Span]:
    """A span around a piece of work, current while it runs and marked failed if it raises or is cancelled.

    Args:
        name: The span's name, one of a fixed few: what varies goes in `attributes`.
        attributes: What the work is on, as ids.
        kind: The span's kind; a request's is `SERVER`.
        parent: The context the span continues, for a request that carries its caller's; the current one when None.
        routine: Exceptions the work raises in its ordinary course, which pass through without marking the span.
    """
    with _TRACER.start_as_current_span(
        name, context=parent, kind=kind, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except routine:
            raise
        except BaseException as error:
            current.set_attribute(ERROR_TYPE, f'{type(error).__module__}.{type(error).__qualname__}')
            current.set_status(trace.StatusCode.ERROR)
            raise
