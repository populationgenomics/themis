"""Loopback TLS OTLP gRPC server for verifying Telemetry API exporters end-to-end."""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import ipaddress
from collections.abc import Callable, Iterator
from typing import override

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import credentials as google_credentials

from themis.telemetry import telemetry_api


class FixedTokenCredentials(google_credentials.Credentials):
    """Credentials whose refresh produces a fixed bearer token, with no network."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._fixed_token = token

    @override
    def refresh(self, request: object) -> None:
        del request
        self.token = self._fixed_token


def _loopback_cert() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, '127.0.0.1')])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


@contextlib.contextmanager
def serving_tls(monkeypatch: pytest.MonkeyPatch, register: Callable[[grpc.Server], None]) -> Iterator[None]:
    """Serve an OTLP gRPC collector over TLS on ``127.0.0.1:0`` and point ``telemetry_api`` at it."""
    assert telemetry_api.ENDPOINT == 'https://telemetry.googleapis.com:443'
    key_pem, cert_pem = _loopback_cert()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        server = grpc.server(pool)
        register(server)
        port = server.add_secure_port('127.0.0.1:0', grpc.ssl_server_credentials([(key_pem, cert_pem)]))
        real_ssl = grpc.ssl_channel_credentials
        server.start()
        try:
            with monkeypatch.context() as mp:
                mp.setattr(grpc, 'ssl_channel_credentials', lambda: real_ssl(root_certificates=cert_pem))
                mp.setattr(telemetry_api, 'ENDPOINT', f'https://127.0.0.1:{port}')
                yield
        finally:
            server.stop(None).wait()
