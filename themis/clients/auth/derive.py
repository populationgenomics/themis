"""Derive a session's per-session bearer by MAC-signing its id (self-hosted-sandbox.md §7).

The bearer is ``HMAC(session-token-signing-key, session_id)``, the signing key a use-only Cloud KMS
MAC key whose material never leaves KMS. The BFF computes the bearer at session create (storing only
its hash); the dispatcher re-derives it at each spawn and injects it into the proxy. Both derive
identically, so the token is reproducible with no per-session secret at rest.

``SessionTokenDeriver`` is the port; ``kms_deriver`` signs through the KMS MAC key — the only real
authorizer. An in-process HMAC double for offline/test determinism lives in the test scaffolding.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

import google_crc32c
from google.cloud import kms_v1

SessionTokenDeriver = Callable[[str], Awaitable[str]]


def _encode(mac: bytes) -> str:
    return base64.urlsafe_b64encode(mac).decode().rstrip('=')


def _verified_bearer(mac: bytes, mac_crc32c: int | None, *, verified_data_crc32c: bool) -> str:
    """Encode the MAC as the bearer after checking KMS's transit-integrity CRCs (fail-loud)."""
    if not verified_data_crc32c:
        raise RuntimeError('KMS did not verify the request-data CRC — corrupted in transit')
    if mac_crc32c is None or google_crc32c.value(mac) != mac_crc32c:
        raise RuntimeError('KMS MAC response CRC mismatch — corrupted in transit')
    return _encode(mac)


def kms_deriver(key_version: str) -> SessionTokenDeriver:
    """Build a deriver that MAC-signs each ``session_id`` through the Cloud KMS MAC key version.

    Args:
        key_version: The MAC key's ``.../cryptoKeyVersions/<n>`` resource name — pinned, since a
            different version derives different bearers and would strand every live session.

    Returns:
        A ``SessionTokenDeriver`` returning the base64url bearer for a session id.
    """
    client = kms_v1.KeyManagementServiceAsyncClient()

    async def derive(session_id: str) -> str:
        data = session_id.encode()
        response = await client.mac_sign(
            request={'name': key_version, 'data': data, 'data_crc32c': google_crc32c.value(data)}
        )
        return _verified_bearer(
            response.mac,
            # proto-plus unwraps the Int64Value field to int | None at runtime.
            response.mac_crc32c,  # pyright: ignore[reportArgumentType]
            verified_data_crc32c=response.verified_data_crc32c,
        )

    return derive
