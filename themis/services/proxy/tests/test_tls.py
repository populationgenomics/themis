"""Tests for SPKI pinning (pure pin computation + chain matching)."""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from themis.services.proxy import tls


def _self_signed_der() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'test')])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_spki_pin_is_stable_and_sha256_sized() -> None:
    der = _self_signed_der()
    assert tls.spki_pin(der) == tls.spki_pin(der)
    assert len(tls.spki_pin(der)) == 44  # base64 of a 32-byte sha256 digest


def test_pins_satisfied_matches_any_chain_cert() -> None:
    leaf, intermediate = _self_signed_der(), _self_signed_der()
    # An intermediate/CA pin accepts the chain even though the leaf differs — survives leaf rotation.
    assert tls.pins_satisfied([leaf, intermediate], frozenset({tls.spki_pin(intermediate)}))


def test_pins_satisfied_rejects_an_unknown_pin() -> None:
    assert not tls.pins_satisfied([_self_signed_der()], frozenset({'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='}))
