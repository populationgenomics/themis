"""Upstream TLS with SPKI/CA pinning for the proxy's forward legs (self-hosted-sandbox.md §6).

The L4 firewall admits any connection to Anthropic's IP range, so cert validation — not the firewall —
authenticates the upstream and stops a hijacked IP from receiving the injected credential. Pin at
**SPKI granularity** (the sha256 of a cert's SubjectPublicKeyInfo), accepting a match against **any**
cert in the presented chain, so an intermediate/CA pin survives Anthropic's leaf-cert rotation where a
leaf pin would hard-fail every session.

Python's ``ssl`` has no pin callback, so the pinning connector inspects the verified chain right after
the handshake — before any request bytes are sent — and drops the connection on a mismatch.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ssl

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import serialization


def spki_pin(cert_der: bytes) -> str:
    """The base64 sha256 of a DER certificate's SubjectPublicKeyInfo (the HPKP-style pin)."""
    public_key = x509.load_der_x509_certificate(cert_der).public_key()
    spki = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def pins_satisfied(chain_der: list[bytes], allowed_pins: frozenset[str]) -> bool:
    """Whether any certificate in the presented chain matches an allowed SPKI pin."""
    return any(spki_pin(cert) in allowed_pins for cert in chain_der)


class PinnedConnector(aiohttp.TCPConnector):
    """A TCP connector that enforces SPKI/CA pinning on the (already CA-validated) upstream chain."""

    def __init__(self, allowed_pins: frozenset[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._allowed_pins = allowed_pins

    # Override a private aiohttp hook — the one point after the handshake but before the request (with
    # its injected credential) is written, so a mismatched upstream never receives the credential.
    async def _wrap_create_connection(  # type: ignore[override]
        self, *args: object, **kwargs: object
    ) -> tuple[asyncio.Transport, asyncio.Protocol]:
        transport, protocol = await super()._wrap_create_connection(*args, **kwargs)  # type: ignore[arg-type]
        ssl_object: ssl.SSLObject | None = transport.get_extra_info('ssl_object')
        # Fail closed: a credential-injecting connector must never proceed over a plaintext (no
        # ssl_object) or unpinned upstream.
        if ssl_object is None or not pins_satisfied(list(ssl_object.get_verified_chain()), self._allowed_pins):
            transport.close()
            raise aiohttp.ClientConnectionError('upstream TLS pin not satisfied')
        return transport, protocol
