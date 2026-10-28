"""The auth service's one copy of the ACL: loaded at start, refreshed, and refused once stale.

The holder loads the ACL when the service starts and refuses to start without a valid version. It
then re-reads the latest version every ``REFRESH_INTERVAL_SECONDS``, validates it, and swaps it in
whole, so both rpcs answer from one consistent copy. A version that cannot be read or does not
validate leaves the last good copy in force and is logged at ERROR as ``acl rejected: <reason>``,
the line the alert matches. Once the last good copy is older than the staleness limit,
:meth:`AclHolder.current` raises :class:`StaleAclError` until a valid version loads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from themis.services.auth import acl

_logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 60
# Every rejection's log line starts with this; the alert on rejections matches it.
REJECTED = 'acl rejected:'

ReadAcl = Callable[[], Awaitable[bytes]]
# Seconds on a monotonic scale; `time.monotonic` in the service.
Clock = Callable[[], float]


class StaleAclError(Exception):
    """The last good copy of the ACL is older than the staleness limit."""


class _RejectedError(Exception):
    """A version of the ACL could not be read or did not validate; the message says which and why."""


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
    ) -> None:
        self._read = read
        self._read_timeout_seconds = read_timeout_seconds
        # One attribute, so a swap replaces the snapshot and its load time together.
        self._loaded = loaded
        self._staleness_limit_seconds = staleness_limit_seconds
        self._clock = clock

    @classmethod
    async def load(
        cls, read: ReadAcl, *, read_timeout_seconds: float, staleness_limit_seconds: float, clock: Clock
    ) -> AclHolder:
        """Read and validate the first version, or refuse to start.

        Args:
            read: Reads the current version of the ACL.
            read_timeout_seconds: How long one read may take before it is abandoned as failed.
            staleness_limit_seconds: How long the last good copy is answered from once versions stop
                validating or being readable.
            clock: The time source the copy's age is measured on.

        Raises:
            SystemExit: The first version cannot be read or does not validate; the message names why.
        """
        try:
            snapshot = await _validated(read, read_timeout_seconds)
        except _RejectedError as e:
            raise SystemExit(f'{REJECTED} {e}') from e
        return cls(
            read,
            (snapshot, clock()),
            read_timeout_seconds=read_timeout_seconds,
            staleness_limit_seconds=staleness_limit_seconds,
            clock=clock,
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
            snapshot = await _validated(self._read, self._read_timeout_seconds)
        except _RejectedError:
            return
        self._loaded = (snapshot, self._clock())

    async def refresh_forever(self, interval_seconds: float) -> None:
        """Refresh every ``interval_seconds`` until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.refresh()


async def _validated(read: ReadAcl, timeout_seconds: float) -> acl.Snapshot:
    """The validated version ``read`` returns within ``timeout_seconds``.

    Raises:
        _RejectedError: It could not be read in time or did not validate, logged at ERROR before raising.
    """
    try:
        raw = await asyncio.wait_for(read(), timeout_seconds)
    except TimeoutError as e:
        _logger.error('%s no read finished within %g s', REJECTED, timeout_seconds)
        raise _RejectedError(f'no read finished within {timeout_seconds:g} s') from e
    except Exception as e:  # whatever the source raises: the refresher has to outlive any read failure
        _logger.exception('%s it could not be read: %s', REJECTED, e)
        raise _RejectedError(f'it could not be read: {e}') from e
    try:
        return acl.parse(raw)
    except acl.AclError as e:
        _logger.error('%s %s', REJECTED, e)
        raise _RejectedError(str(e)) from e
