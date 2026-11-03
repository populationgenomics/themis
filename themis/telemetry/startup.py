"""Startup timing: a log line for each step a service takes before it serves, so a cold start can be attributed.

Cloud Run logs when it starts an instance and when the instance's startup probe passes, and nothing in between. A
`StartupClock` fills that gap from inside the process. Its first line marks the entry point and carries the CPU time
the process spent reaching it, which is the interpreter's start and the module imports. Each later line names a step,
how long it took, and the time since the entry point. Read against Cloud Run's two lines, they split a cold start into
the platform's share (instance start to the first line, less the CPU time), the imports', and each step's.

The lines are logged at INFO under this module's logger, so an entry point that logs at WARNING enables this logger
alone.
"""

from __future__ import annotations

import logging
import time

_logger = logging.getLogger(__name__)


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
