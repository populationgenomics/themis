"""Tests for `themis.common.sql` — the IAM-authed Cloud SQL connect wiring.

`iam_connect` is exercised through a real `google.cloud.sql.connector.Connector` over a loopback
Cloud SQL Admin API and loopback mTLS socket, proving that IAM auth embeds the caller's token in
the ephemeral certificate request, dials port 3307 over TLS, and returns `pg8000`'s connection.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import http.server
import ipaddress
import json
import pathlib
import socket
import ssl
import tempfile
import threading
from collections.abc import Iterator
from concurrent import futures
from typing import override

import google.auth
import pg8000.dbapi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import credentials as google_credentials
from google.cloud.sql import connector

from themis.common import sql

_SERVICES = pathlib.Path(sql.__file__).resolve().parents[1] / 'services'
_SQL_SERVER_PROXY_PORT = 3307


class _IamCredentials(google_credentials.Credentials):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._iam_token = token

    @override
    def refresh(self, request: object) -> None:
        del request
        self.token = self._iam_token
        self.expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1)


@contextlib.contextmanager
def _background_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


@contextlib.contextmanager
def _cloudsql_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, object, list[tuple[str, dict[str, object]]], list[tuple[str, str, str | None]]]]:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'sql-ca')])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    now = dt.datetime.now(dt.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode('ascii')

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'proj:inst')])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(2)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))]), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ephemeral_calls: list[tuple[str, dict[str, object]]] = []
    pg_calls: list[tuple[str, str, str | None]] = []
    client_cert_subjects: list[str] = []
    server_verified = threading.Event()
    connected = threading.Event()

    class _AdminHandler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            assert self.path == '/sql/v1beta4/projects/proj/instances/inst/connectSettings'
            body = json.dumps(
                {
                    'region': 'region',
                    'databaseVersion': 'POSTGRES_16',
                    'ipAddresses': [{'type': 'PRIMARY', 'ipAddress': '127.0.0.1'}],
                    'serverCaCert': {'cert': ca_pem},
                }
            ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            ephemeral_calls.append((self.path, req))
            pub_key = serialization.load_pem_public_key(str(req['public_key']).encode('ascii'))
            assert isinstance(pub_key, rsa.RSAPublicKey)
            client_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'sql-client')])
            eph_cert = (
                x509.CertificateBuilder()
                .subject_name(client_name)
                .issuer_name(ca_name)
                .public_key(pub_key)
                .serial_number(3)
                .not_valid_before(now - dt.timedelta(hours=1))
                .not_valid_after(now + dt.timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub_key), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
                .sign(ca_key, hashes.SHA256())
            )
            body = json.dumps(
                {'ephemeralCert': {'cert': eph_cert.public_bytes(serialization.Encoding.PEM).decode('ascii')}}
            ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with contextlib.ExitStack() as stack:
        admin_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _AdminHandler)
        stack.callback(admin_server.server_close)
        admin_thread = threading.Thread(target=admin_server.serve_forever, daemon=True)
        admin_thread.start()
        stack.callback(admin_thread.join, timeout=5)
        stack.callback(admin_server.shutdown)

        listener = socket.create_server(('127.0.0.1', 0))
        stack.callback(listener.close)
        listener.settimeout(5)
        tls_port = listener.getsockname()[1]

        def _accept_tls() -> None:
            try:
                raw_conn, _ = listener.accept()
            except OSError:
                return
            raw_conn.settimeout(5)
            with (
                raw_conn,
                tempfile.NamedTemporaryFile('wb') as cert_file,
                tempfile.NamedTemporaryFile('wb') as key_file,
            ):
                cert_file.write(srv_cert.public_bytes(serialization.Encoding.PEM))
                cert_file.flush()
                key_file.write(
                    srv_key.private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
                    )
                )
                key_file.flush()
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert_file.name, key_file.name)
                ctx.load_verify_locations(cadata=ca_pem)
                ctx.verify_mode = ssl.CERT_REQUIRED
                with ctx.wrap_socket(raw_conn, server_side=True) as tls_conn:
                    peer_der = tls_conn.getpeercert(binary_form=True)
                    assert peer_der is not None
                    peer_cert = x509.load_der_x509_certificate(peer_der)
                    cn = peer_cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
                    client_cert_subjects.append(str(cn))
                    server_verified.set()
                    connected.wait(timeout=5)

        tls_pool = stack.enter_context(futures.ThreadPoolExecutor(max_workers=1))
        tls_future = tls_pool.submit(_accept_tls)

        def _finish_tls() -> None:
            connected.set()
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            listener.close()
            tls_future.result(timeout=5)

        stack.callback(_finish_tls)

        sentinel = object()
        real_create_connection = socket.create_connection

        def _redirect_proxy_port(
            address: tuple[str | None, int],
            timeout: float | None = None,
            source_address: tuple[bytes | bytearray | str, int] | None = None,
            *,
            all_errors: bool = False,
        ) -> socket.socket:
            assert address == ('127.0.0.1', _SQL_SERVER_PROXY_PORT)
            kwargs: dict[str, object] = {'all_errors': all_errors}
            if timeout is not None:
                kwargs['timeout'] = timeout
            if source_address is not None:
                kwargs['source_address'] = source_address
            return real_create_connection(('127.0.0.1', tls_port), **kwargs)  # pyright: ignore[reportArgumentType]

        def _pg_connect(
            user: str, *, database: str, password: str | None, sock: ssl.SSLSocket, **_kw: object
        ) -> object:
            assert isinstance(sock, ssl.SSLSocket)
            assert server_verified.wait(timeout=5)
            pg_calls.append((user, database, password))
            connected.set()
            sock.close()
            return sentinel

        with monkeypatch.context() as mp:
            mp.setattr(socket, 'create_connection', _redirect_proxy_port)
            mp.setattr(pg8000.dbapi, 'connect', _pg_connect)
            yield f'http://127.0.0.1:{admin_server.server_port}', sentinel, ephemeral_calls, pg_calls

    assert client_cert_subjects == ['sql-client']


def test_iam_connect_dials_with_pg8000_and_iam_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        _background_loop() as loop,
        _cloudsql_loopback(monkeypatch) as (endpoint, sentinel, ephemeral_calls, pg_calls),
        connector.Connector(
            credentials=_IamCredentials('iam-access-token'),
            sqladmin_api_endpoint=endpoint,
            loop=loop,
            refresh_strategy=connector.RefreshStrategy.LAZY,
        ) as pool,
    ):
        conn = sql.iam_connect(
            pool,
            connection_name='proj:region:inst',
            database='themis',
            db_user='themis-auth@proj.iam',
        )

    assert conn is sentinel
    [(path, ephemeral_body)] = ephemeral_calls
    assert path == '/sql/v1beta4/projects/proj/instances/inst:generateEphemeralCert'
    assert ephemeral_body['access_token'] == 'iam-access-token'
    assert pg_calls == [('themis-auth@proj.iam', 'themis', None)]


def _constructs_a_connector(path: pathlib.Path) -> bool:
    """Whether `path` calls `Connector(...)`, through the module or imported by name."""
    tree = ast.parse(path.read_text('utf-8'), filename=str(path))
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == 'Connector')
            or (isinstance(node.func, ast.Name) and node.func.id == 'Connector')
        )
        for node in ast.walk(tree)
    )


def test_no_service_builds_its_own_cloud_sql_connector() -> None:
    """The refresh strategy is a property of the runtime, not of the call site that happens to dial.

    A service that builds its own gets the library's default, which refreshes on a background task a
    CPU-idling Cloud Run instance does not run — and nothing about the call site makes that visible.
    `lazy_connector` is where the determination is recorded, so it is the only place a service
    constructs one; a service dialling past it is the drift this catches, not a style preference.

    A name check over the source, so it is an early warning and not the enforcement: it sees a
    construction spelled in a service's own file, and not one reached through a helper elsewhere in
    the tree that builds a default connector. That path has no check, only the docstring above.
    """
    assert _SERVICES.is_dir(), f'{_SERVICES} is not the services tree — the walk is looking in the wrong place'
    builders = sorted(
        path.relative_to(_SERVICES.parent).as_posix()
        for path in _SERVICES.rglob('*.py')
        if _constructs_a_connector(path)
    )
    assert not builders


def test_the_shared_connector_refreshes_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lazy_connector` generates no client key while idle, deferring certificate work until a dial."""
    loops: list[asyncio.AbstractEventLoop] = []
    real_new_event_loop = asyncio.new_event_loop
    real_generate_private_key = rsa.generate_private_key
    idle_keys_generated = 0

    def new_event_loop() -> asyncio.AbstractEventLoop:
        loop = real_new_event_loop()
        loops.append(loop)
        return loop

    def count_private_keys(*, public_exponent: int, key_size: int, backend: object = None) -> rsa.RSAPrivateKey:
        del backend
        nonlocal idle_keys_generated
        idle_keys_generated += 1
        return real_generate_private_key(public_exponent=public_exponent, key_size=key_size)

    monkeypatch.setattr(asyncio, 'new_event_loop', new_event_loop)
    monkeypatch.setattr(rsa, 'generate_private_key', count_private_keys)
    monkeypatch.setattr(
        google.auth, 'default', lambda **_kwargs: (google_credentials.AnonymousCredentials(), 'themis-test')
    )
    pool = sql.lazy_connector()
    try:
        assert isinstance(pool, connector.Connector)
        [loop] = loops
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
        assert idle_keys_generated == 0
    finally:
        pool.close()
        for loop in loops:
            loop.close()
