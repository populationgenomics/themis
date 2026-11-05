"""The auth service's one copy of the ACL: loaded at start, refreshed, and refused once stale.

The holder loads the ACL when the service starts and refuses to start without a valid version. It
then re-reads the latest version every ``REFRESH_INTERVAL_SECONDS``, validates it, and swaps it in
whole, so both rpcs answer from one consistent copy. A version that cannot be read or does not
validate leaves the last good copy in force and is logged at ERROR as ``acl rejected: <reason>``,
which names the rule it broke. Every attempt sets ``themis_auth_acl_attempt_timestamp_seconds`` to
the time it ended, labelled with its outcome, so the alert can tell whether the latest attempt on any
instance was refused, and an instance that scales to zero leaves its last outcome behind. Once the
last good copy is older than the staleness limit, :meth:`AclHolder.current` raises
:class:`StaleAclError` until a valid version loads.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from opentelemetry import metrics

from themis.services.auth import acl
from themis.telemetry import operational_names

_logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 60
# Every rejection's log line starts with this.
REJECTED = 'acl rejected:'

ReadAcl = Callable[[], Awaitable[bytes]]
# Seconds on a monotonic scale; `time.monotonic` in the service.
Clock = Callable[[], float]


class StaleAclError(Exception):
    """The last good copy of the ACL is older than the staleness limit."""


class _RejectedError(Exception):
    """A version of the ACL could not be read or did not validate; the message says which and why."""


class LoadOutcomes:
    """The `themis_auth_acl_attempt_timestamp_seconds` gauge over one meter: when each outcome last happened."""

    def __init__(self, meter: metrics.Meter, *, clock: Callable[[], float] = time.time) -> None:
        """Create the gauge on `meter`.

        Args:
            meter: The meter the gauge is created on.
            clock: Returns the Unix time an outcome is set to; the system clock outside tests. Instances compare
                their outcomes by it, so it is wall time, never the holder's monotonic clock.
        """
        self._clock = clock
        self._gauge = meter.create_gauge(
            operational_names.ACL_ATTEMPT,
            description='Unix time of the latest attempt to load the ACL, by outcome: loaded, or why it was refused.',
        )

    def record(self, outcome: operational_names.AclLoadOutcome) -> None:
        """Set `outcome`'s series to now."""
        self._gauge.set(self._clock(), {operational_names.OUTCOME_LABEL: outcome.value})


class AclHolder:
    """The current snapshot of the ACL and when it last validated; build one with :meth:`load`."""

    def __init__(
        self,
        read: ReadAcl,
        loaded: tuple[acl.Snapshot, float],
        *,
        read_timeout_seconds: float,
        staleness_limit_seconds: float,
        clock: Clock,
        outcomes: LoadOutcomes,
    ) -> None:
        self._read = read
        self._read_timeout_seconds = read_timeout_seconds
        self._outcomes = outcomes
        # One attribute, so a swap replaces the snapshot and its load time together.
        self._loaded = loaded
        self._staleness_limit_seconds = staleness_limit_seconds
        self._clock = clock

    @classmethod
    async def load(
        cls,
        read: ReadAcl,
        *,
        read_timeout_seconds: float,
        staleness_limit_seconds: float,
        clock: Clock,
        outcomes: LoadOutcomes,
    ) -> AclHolder:
        """Read and validate the first version, or refuse to start.

        Args:
            read: Reads the current version of the ACL.
            read_timeout_seconds: How long one read may take before it is abandoned as failed.
            staleness_limit_seconds: How long the last good copy is answered from once versions stop
                validating or being readable.
            clock: The time source the copy's age is measured on.
            outcomes: Where the outcome of each attempt is recorded.

        Raises:
            SystemExit: The first version cannot be read or does not validate; the message names why.
        """
        try:
            snapshot = await _validated(read, read_timeout_seconds, outcomes)
        except _RejectedError as e:
            raise SystemExit(f'{REJECTED} {e}') from e
        return cls(
            read,
            (snapshot, clock()),
            read_timeout_seconds=read_timeout_seconds,
            staleness_limit_seconds=staleness_limit_seconds,
            clock=clock,
            outcomes=outcomes,
        )

    def current(self) -> acl.Snapshot:
        """The snapshot to answer from.

        Raises:
            StaleAclError: The last good copy is older than the staleness limit.
        """
        snapshot, loaded_at = self._loaded
        age = self._clock() - loaded_at
        if age > self._staleness_limit_seconds:
            raise StaleAclError(
                f'the last valid ACL loaded {age:.0f} s ago, past the {self._staleness_limit_seconds:g} s '
                'staleness limit'
            )
        return snapshot

    async def refresh(self) -> None:
        """Read and validate the current version; swap it in, or keep the last good copy and log why not."""
        try:
            snapshot = await _validated(self._read, self._read_timeout_seconds, self._outcomes)
        except _RejectedError:
            return
        self._loaded = (snapshot, self._clock())

    async def refresh_forever(self, interval_seconds: float) -> None:
        """Refresh every ``interval_seconds`` until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.refresh()


async def _validated(read: ReadAcl, timeout_seconds: float, outcomes: LoadOutcomes) -> acl.Snapshot:
    """The validated version ``read`` returns within ``timeout_seconds``, its load recorded.

    Raises:
        _RejectedError: It could not be read in time or did not validate, recorded and logged at ERROR before
            raising.
    """
    try:
        raw = await asyncio.wait_for(read(), timeout_seconds)
    except TimeoutError as e:
        outcomes.record(operational_names.AclLoadOutcome.TIMEOUT)
        _logger.error('%s no read finished within %g s', REJECTED, timeout_seconds)
        raise _RejectedError(f'no read finished within {timeout_seconds:g} s') from e
    except Exception as e:  # whatever the source raises: the refresher has to outlive any read failure
        outcomes.record(operational_names.AclLoadOutcome.UNREADABLE)
        _logger.exception('%s it could not be read: %s', REJECTED, e)
        raise _RejectedError(f'it could not be read: {e}') from e
    try:
        snapshot = acl.parse(raw)
    except acl.AclError as e:
        outcomes.record(operational_names.AclLoadOutcome.INVALID)
        _logger.error('%s %s', REJECTED, e)
        raise _RejectedError(str(e)) from e
    outcomes.record(operational_names.AclLoadOutcome.LOADED)
    return snapshot
