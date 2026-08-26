"""Shared HTTP retry for the refresh job's upstream GETs.

Rides out the transient upstream failures a weekly job must survive — HTTP 429 and 5xx, and the
transient ``httpx`` transport errors (connect / read / protocol) — with exponential backoff; every
other status (200, 304, 404, other 4xx) is returned to the caller to interpret. Not a general
client: the refresh job owns the ``httpx.AsyncClient`` and passes it in.
"""

from __future__ import annotations

import asyncio

import httpx

# 429 (rate limited) and the retryable 5xx: transient, worth a backed-off retry. A 4xx other than
# 429 (e.g. 404) is a real answer the caller interprets, never retried.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 2.0


async def request_with_retry(
    client: httpx.AsyncClient, method: str, url: str, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Issue ``method url`` on ``client``, retrying 429/5xx and transient transport errors.

    A transient ``httpx.TransportError`` (connect / read / protocol) is retried on the same
    exponential backoff and attempt budget as a 429/5xx status, then re-raised once the budget is
    spent. ``httpx.HTTPStatusError`` is never raised here — a status is returned, not raised.

    Args:
        client: The caller-owned async client every request rides.
        method: The HTTP method (``GET``).
        url: The absolute request URL.
        headers: Optional request headers (e.g. ``If-None-Match``).

    Returns:
        The first non-retryable response, or the response of the final attempt once the retry
        budget is spent — the caller decides whether that status is an error.

    Raises:
        httpx.TransportError: If every attempt fails with a transient transport error.
    """
    for attempt in range(_MAX_ATTEMPTS - 1):
        try:
            response = await client.request(method, url, headers=headers)
            if response.status_code not in _RETRY_STATUS:
                return response
        except httpx.TransportError:
            pass  # a connect/read/protocol blip: retry on the same backoff, don't abort the run
        await asyncio.sleep(_BACKOFF_BASE_SECONDS * 2**attempt)
    return await client.request(method, url, headers=headers)
