"""Startup timing: a log line for each step a service takes before it serves, so a cold start can be attributed.

Cloud Run logs when it starts an instance and when the instance's startup probe passes, and nothing in between. A
`StartupClock` fills that gap from inside the process. Its first line marks the entry point and carries the CPU time
the process spent reaching it, which is the interpreter's start and the module imports. Each later line names a step,
how long it took, and the time since the entry point. Read against Cloud Run's two lines, they split a cold start into
the platform's share (instance start to the first line, less the CPU time), the imports', and each step's.

The lines are logged at INFO under this module's logger, so an entry point that logs at WARNING enables this logger
alone.

A step can be a span as well (`StartupClock.span`, under `trace_root`), so a start that finishes shows as one timeline
in Cloud Trace. The lines stay the record of a start that is killed: each is written as its step finishes, where a span
waits for the next batch export and the trace's root for startup to end.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator

from opentelemetry import trace

_logger = logging.getLogger(__name__)
_TRACER = trace.get_tracer(__name__)

# The root span of the trace a start records.
ROOT_SPAN = 'startup'


class StartupClock:
    """Times one process's start. Build it first in the entry point; its construction logs the first line."""

    def __init__(self) -> None:
        self._started = self._last = time.monotonic()
        _logger.info('startup: entry point reached after %.3f s of process CPU time', time.process_time())

    def step(self, name: str) -> None:
        """Log that the step `name` has just finished: how long since the previous step, and since the entry point."""
        now = time.monotonic()
        _logger.info(
            'startup: %s took %.3f s (%.3f s since the entry point)', name, now - self._last, now - self._started
        )
        self._last = now

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        """Run the step `name` inside a span of that name, then log it as `step` does.

        The span is a child of the span current where the step runs, and is recorded once the process has a
        tracer provider; before that only the line is. A step that raises is marked failed and logs no line.
        """
        with _span(name):
            yield
        self.step(name)


@contextlib.contextmanager
def trace_root() -> Iterator[None]:
    """The span a start's traced steps go under, `ROOT_SPAN`, which makes the start one trace.

    Enter it once the process has its tracer provider: a span started before that is never recorded, and each step
    under it would be a trace of its own.
    """
    with _span(ROOT_SPAN):
        yield


@contextlib.contextmanager
def _span(name: str) -> Iterator[None]:
    # Marked here: OpenTelemetry marks a span failed for an `Exception` only, and a start fails loud with `SystemExit`.
    with _TRACER.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        try:
            yield
        except BaseException as error:
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR, f'{type(error).__name__}: {error}'))
            raise
