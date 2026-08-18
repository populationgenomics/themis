"""Offline stand-ins for Google's token endpoint.

The exchange is the only seam that must reach the network, so it is the one faked here: a
``google.auth.transport.Request`` that answers the refresh grant with claims the test chose.
Kept out of the production modules so neither ships a way to mint a token nobody signed.
"""

from __future__ import annotations

import base64
import datetime
import json
from collections.abc import Mapping
from typing import override

from google.auth import transport

CLIENT_ID = 'client.apps.googleusercontent.com'
CLIENT_SECRET = 'client-secret'
REFRESH_TOKEN = 'refresh-token'
EMAIL = 'developer@example.org'
# The form Google echoes back, not the `email` shorthand a request may use: oauthlib and
# google-auth both diff granted against requested, so a fixture using the shorthand would
# model a response that cannot occur and hide the mismatch.
GRANTED_SCOPE = 'openid https://www.googleapis.com/auth/userinfo.email'


def unsigned_jwt(audience: str = CLIENT_ID, email: str | None = EMAIL, expires_in: int = 3600) -> str:
    """Build a JWT carrying the given claims, signed by nobody.

    The production code decodes without verifying — the real token arrives from Google over TLS
    — so an unsigned token exercises the same path.

    Args:
        audience: The ``aud`` claim.
        email: The ``email`` claim; ``None`` omits it.
        expires_in: Seconds from now until ``exp``.

    Returns:
        The encoded JWT.
    """
    expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=expires_in)
    claims: dict[str, object] = {'aud': audience, 'exp': int(expires_at.timestamp())}
    if email is not None:
        claims['email'] = email
    header: dict[str, object] = {'alg': 'RS256', 'typ': 'JWT'}
    signature: dict[str, object] = {'signature': 'unverified'}
    return '.'.join(_segment(part) for part in (header, claims, signature))


class _Response(transport.Response):
    def __init__(self, status: int, data: bytes) -> None:
        self._status = status
        self._data = data

    @property
    @override
    def status(self) -> int:
        return self._status

    @property
    @override
    def headers(self) -> Mapping[str, str]:
        return {'Content-Type': 'application/json'}

    @property
    @override
    def data(self) -> bytes:
        return self._data


class FixtureTransport(transport.Request):
    """Answers the refresh grant from a canned response, recording what was sent."""

    def __init__(self, body: Mapping[str, object], status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[dict[str, object]] = []

    @override
    def __call__(
        self,
        url: str,
        method: str = 'GET',
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
        **kwargs: object,
    ) -> _Response:
        self.calls.append({'url': url, 'method': method, 'body': body, 'headers': headers, **kwargs})
        return _Response(self.status, json.dumps(self.body).encode('utf-8'))


def granting(id_token: str | None = None) -> FixtureTransport:
    """A transport whose refresh grant succeeds, returning ``id_token`` if one is given."""
    granted: dict[str, object] = {
        'access_token': 'access-token',
        'expires_in': 3600,
        'scope': GRANTED_SCOPE,
        'token_type': 'Bearer',
    }
    if id_token is not None:
        granted['id_token'] = id_token
    return FixtureTransport(granted)


def refusing() -> FixtureTransport:
    """A transport whose refresh grant fails the way a revoked consent does."""
    return FixtureTransport({'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'}, 400)


def _segment(payload: Mapping[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode('utf-8')).rstrip(b'=').decode('ascii')
