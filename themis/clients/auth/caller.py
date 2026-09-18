"""The verified caller of a data-plane call: the service account behind the forwarded ID token.

Internal calls authenticate with Cloud Run's IAM: the caller presents a Google-signed ID token in the
``authorization`` metadata, Cloud Run admits the call only if that identity holds ``run.invoker``, and
then forwards the header to the container unchanged. Re-verifying it here — signature, issuer,
``email_verified`` — is what makes the calling service account knowable server-side at all
(rpc-authorization.md, Appendix). A ``CallerVerifier`` is that step; the Cloud Run one is the live
verifier, and a fixture verifier stands in offline.

A verifier yields ``None`` for a token that declines — absent, malformed, forged, expired, or issued by
another issuer — and raises for a failure that is not a decline: Google's signing certificates could not
be fetched. What the interceptor does with ``None`` — deny the call before any
context exists — is its decision; a raise propagates as the failure it is.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import override

import grpc.aio
from google.auth import exceptions as google_auth_exceptions
from google.auth import transport as google_auth_transport
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token

# Cloud Run forwards the caller's bearer ID token here after validating `run.invoker`. Lowercased:
# gRPC normalises metadata keys.
AUTHORIZATION_METADATA = 'authorization'

# Maps a call to the calling service account's verified email, or None when no verifiable identity is
# presented.
CallerVerifier = Callable[[grpc.aio.ServicerContext], Awaitable[str | None]]

# How long a fetched certificate set serves before it is fetched again when the response names no
# max-age. Google's own responses do name one, of the order of hours.
_DEFAULT_CERTS_TTL_SECONDS = 3600.0
_FETCH_TIMEOUT_SECONDS = 30
_MAX_AGE = re.compile(r'max-age=(\d+)')


class _CachedResponse(google_auth_transport.Response):
    def __init__(self, status: int, headers: Mapping[str, str], data: bytes) -> None:
        self._status = status
        self._headers = dict(headers)
        self._data = data

    @property
    @override
    def status(self) -> int:
        return self._status

    @property
    @override
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    @override
    def data(self) -> bytes:
        return self._data


class _CachingRequest(google_auth_transport.Request):
    """A ``google.auth`` transport that serves a URL's response from cache for its ``max-age``.

    ``verify_oauth2_token`` fetches Google's certificates through the transport it is handed, on every
    call; this is the transport that makes that one fetch per TTL rather than one per rpc. Each real
    fetch goes through a fresh ``requests``-backed transport, so no session is shared across the threads
    verification runs on. A fetch that fails raises — a failure to reach Google is not a decline.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, _CachedResponse]] = {}

    @override
    def __call__(
        self,
        url: str,
        method: str = 'GET',
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
        **kwargs: object,
    ) -> google_auth_transport.Response:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(url)
            if cached is not None and cached[0] > now:
                return cached[1]
        response = google_auth_requests.Request()(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=_FETCH_TIMEOUT_SECONDS if timeout is None else timeout,
            **kwargs,
        )
        cached_response = _CachedResponse(response.status, response.headers, response.data)
        if response.status == 200:
            with self._lock:
                self._cache[url] = (now + _ttl(response.headers), cached_response)
        return cached_response


def _ttl(headers: Mapping[str, str]) -> float:
    cache_control = headers.get('cache-control') or headers.get('Cache-Control') or ''
    match = _MAX_AGE.search(cache_control)
    return float(match.group(1)) if match else _DEFAULT_CERTS_TTL_SECONDS


def cloud_run_caller_verifier() -> CallerVerifier:
    """Build a ``CallerVerifier`` that reads the calling account from the forwarded ID token.

    Verification checks Google's signature, issuer and expiry, then reads the ``email`` claim, and
    only if ``email_verified`` is set. The token's audience is not checked: Cloud Run admits a call
    only for a token minted for this service, and an audience is not a credential — any account can
    mint a token for any audience, so what the call may reach is decided by the verified account
    against the principals the rpc admits, never by the audience it names.
    """
    request = _CachingRequest()

    async def verify_caller(context: grpc.aio.ServicerContext) -> str | None:
        token = _bearer_token(context)
        if token is None:
            return None
        try:
            # google-auth's verification is blocking (the certificate fetch, when the cache misses); keep
            # it off the event loop.
            claims = await asyncio.to_thread(google_id_token.verify_oauth2_token, token, request)
        except google_auth_exceptions.TransportError:
            raise  # the certificates could not be fetched: not a decline
        except (ValueError, google_auth_exceptions.GoogleAuthError):
            return None
        email = claims.get('email')
        if not isinstance(email, str) or claims.get('email_verified') is not True:
            return None
        return email

    return verify_caller


def fixture_caller_verifier_from_json(raw: str | None, *, var_name: str) -> CallerVerifier:
    """Build an offline ``CallerVerifier`` from a JSON bearer-token -> verified-email map.

    The offline stand-in for :func:`cloud_run_caller_verifier`: instead of verifying an ID token, map
    the presented bearer to the email it is treated as having verified.

    Args:
        raw: The JSON string — an object mapping each plaintext bearer to a service-account email, e.g.
            ``{"web-token": "themis-web@x.iam.gserviceaccount.com"}``. ``None`` (an unset env var) is
            an operator error; pass ``"{}"`` for an explicit empty set.
        var_name: The source env var, named in the fail-loud error messages.

    Returns:
        A ``CallerVerifier`` returning the seeded email for a known bearer and ``None`` for one it does
        not hold — the Cloud Run verifier's fail-closed contract.

    Raises:
        SystemExit: ``raw`` is unset, not JSON, or not an object of strings.
    """
    if raw is None:
        raise SystemExit(
            f'{var_name} is required for the fixture caller verifier: a JSON object of bearer -> email, '
            'or "{}" for an explicit empty set'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'{var_name} is not valid JSON: {e}') from e
    if not isinstance(seeds, dict) or not all(isinstance(v, str) for v in seeds.values()):
        raise SystemExit(f'{var_name} must be a JSON object of bearer -> email')

    async def verify_caller(context: grpc.aio.ServicerContext) -> str | None:
        token = _bearer_token(context)
        if token is None:
            return None
        return seeds.get(token)

    return verify_caller


def _bearer_token(context: grpc.aio.ServicerContext) -> str | None:
    """The bearer token from the ``authorization`` metadata, or ``None`` when absent or not a bearer."""
    for key, value in context.invocation_metadata() or ():
        if key == AUTHORIZATION_METADATA:
            scheme, _, token = value.partition(' ')
            if scheme.lower() == 'bearer' and token:
                return token
            return None
    return None
