"""An index call retried through its transient failures: a dropped connection, HTTP 429, or a 5xx.

Bulk ingest resolves a whole seed in one shard, so an unretried blip from any index would abort the run. Each try
after the first waits twice as long as the one before it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx2


async def send(
    request: Callable[[], Awaitable[httpx2.Response]], *, attempts: int, base_delay: float
) -> httpx2.Response:
    """Send `request` until it answers with a 2xx, a non-transient failure, or `attempts` run out.

    Args:
        request: Sends the request once.
        attempts: Tries before a transient failure escapes; 1 sends once, for a caller whose own retry policy owns
            backoff.
        base_delay: Seconds before the second try; each later one waits twice the one before.

    Returns:
        The 2xx response.

    Raises:
        ValueError: If `attempts` is not positive.
        httpx2.HTTPStatusError: If the index answers with a non-transient status (a 4xx other than 429), or keeps
            answering with a transient one past the budget.
        httpx2.TransportError: If the connection keeps failing past the budget.
    """
    if attempts < 1:
        raise ValueError(f'attempts must be positive, got {attempts}')
    for attempt in range(attempts):
        try:
            response = await request()
            response.raise_for_status()
            return response
        except (httpx2.TransportError, httpx2.HTTPStatusError) as exc:
            if not is_transient(exc) or attempt == attempts - 1:
                raise
        await asyncio.sleep(base_delay * 2**attempt)
    raise AssertionError('unreachable: the retry loop returns or raises on the final attempt')


def is_transient(exc: httpx2.TransportError | httpx2.HTTPStatusError) -> bool:
    """Whether a failure may clear on a retry: a transport error, HTTP 429, or a 5xx."""
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return True
