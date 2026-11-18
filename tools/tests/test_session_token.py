"""The session-token tool's discovery and signing over a loopback KMS gRPC server."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import datetime as dt
import pathlib
import stat
from collections.abc import Coroutine, Iterator, Sequence
from typing import override

import google_crc32c
import grpc
import grpc.aio
import pytest
from google.auth import credentials as credentials_mod
from google.auth import exceptions
from google.cloud import kms_v1
from google.cloud.kms_v1.services.key_management_service import transports

from themis.clients.sheaf import store as remote_mod
from themis.rpc import sandbox_options_pb2
from tools import session_token

_KEY_RING = 'projects/p/locations/australia-southeast1/keyRings/themis'
_PULUMI_ENCRYPT_KEY = f'{_KEY_RING}/cryptoKeys/themis-pulumi-state-key'
_SIGNING_KEY = f'{_KEY_RING}/cryptoKeys/themis-session-token-signing-key-abc'
# Unpadded urlsafe base64 of `b'\xfb\xff\x03\x04'` is `'-_8DBA'`.
_FIXED_MAC = b'\xfb\xff\x03\x04'
_EXPECTED_ENCODED = '-_8DBA'


class _Login(credentials_mod.Credentials):
    """Stand-in Google credentials: refresh succeeds, or fails with ``error``."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self._error = error

    @override
    def refresh(self, request: object) -> None:
        del request
        if self._error is not None:
            raise self._error
        self.token = 'access-token'  # noqa: S105 — a stand-in, not a secret
        self.expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1)


@contextlib.contextmanager
def _kms_loopback(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mac_keys: Sequence[str] = (),
    list_status: tuple[grpc.StatusCode, str] | None = None,
    login: _Login | None = None,
    expected_signer: credentials_mod.Credentials | None = None,
) -> Iterator[list[kms_v1.MacSignRequest]]:
    """Serve Cloud KMS ``ListCryptoKeys`` and ``MacSign`` over loopback gRPC.

    The ring always holds a non-MAC Pulumi encryption key alongside ``mac_keys``, so omitting
    ``filter='purpose=MAC'`` returns the wrong key count and fails.
    """
    ring_keys = [
        kms_v1.CryptoKey(name=_PULUMI_ENCRYPT_KEY, purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT),
        *[kms_v1.CryptoKey(name=name, purpose=kms_v1.CryptoKey.CryptoKeyPurpose.MAC) for name in mac_keys],
    ]
    mac_requests: list[kms_v1.MacSignRequest] = []
    caller_cred = login or _Login()
    sync_channels: list[grpc.Channel] = []
    async_channels: list[tuple[asyncio.AbstractEventLoop, grpc.aio.Channel]] = []

    def list_crypto_keys(
        request: kms_v1.ListCryptoKeysRequest, context: grpc.ServicerContext
    ) -> kms_v1.ListCryptoKeysResponse:
        if list_status is not None:
            context.abort(list_status[0], list_status[1])
        if request.parent != _KEY_RING:
            context.abort(grpc.StatusCode.NOT_FOUND, f'unexpected key ring {request.parent!r}')
        if request.filter == 'purpose=MAC':
            matched = [k for k in ring_keys if k.purpose == kms_v1.CryptoKey.CryptoKeyPurpose.MAC]
        elif request.filter == '':
            matched = list(ring_keys)
        else:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f'unsupported filter {request.filter!r}')
        return kms_v1.ListCryptoKeysResponse(crypto_keys=matched)

    def mac_sign(request: kms_v1.MacSignRequest, _context: grpc.ServicerContext) -> kms_v1.MacSignResponse:
        mac_requests.append(request)
        verified = request.data_crc32c == google_crc32c.value(request.data)
        return kms_v1.MacSignResponse(
            name=request.name,
            mac=_FIXED_MAC,
            mac_crc32c=google_crc32c.value(_FIXED_MAC),
            verified_data_crc32c=verified,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        server = grpc.server(pool)
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    'google.cloud.kms.v1.KeyManagementService',
                    {
                        'ListCryptoKeys': grpc.unary_unary_rpc_method_handler(
                            list_crypto_keys,
                            request_deserializer=kms_v1.ListCryptoKeysRequest.deserialize,
                            response_serializer=kms_v1.ListCryptoKeysResponse.serialize,
                        ),
                        'MacSign': grpc.unary_unary_rpc_method_handler(
                            mac_sign,
                            request_deserializer=kms_v1.MacSignRequest.deserialize,
                            response_serializer=kms_v1.MacSignResponse.serialize,
                        ),
                    },
                )
            ]
        )
        port = server.add_insecure_port('127.0.0.1:0')
        target = f'127.0.0.1:{port}'
        real_sync_client = kms_v1.KeyManagementServiceClient
        real_async_client = kms_v1.KeyManagementServiceAsyncClient

        def caller_credentials(*, quota_project: str | None) -> _Login:
            assert quota_project == 'p'
            return caller_cred

        def build_sync_client(*, credentials: credentials_mod.Credentials) -> kms_v1.KeyManagementServiceClient:
            assert credentials is caller_cred
            channel = grpc.insecure_channel(target)
            sync_channels.append(channel)
            return real_sync_client(transport=transports.KeyManagementServiceGrpcTransport(channel=channel))

        def build_async_client(
            *, credentials: credentials_mod.Credentials | None = None
        ) -> kms_v1.KeyManagementServiceAsyncClient:
            assert credentials is not None
            if expected_signer is not None:
                assert credentials is expected_signer
            channel = grpc.aio.insecure_channel(target)
            async_channels.append((asyncio.get_running_loop(), channel))
            return real_async_client(transport=transports.KeyManagementServiceGrpcAsyncIOTransport(channel=channel))

        real_asyncio_run = asyncio.run

        def _run_and_close_channels[T](coro: Coroutine[object, object, T]) -> T:
            async def _wrapped() -> T:
                try:
                    return await coro
                finally:
                    current = asyncio.get_running_loop()
                    for loop, channel in list(async_channels):
                        if loop is current:
                            await channel.close()
                            async_channels.remove((loop, channel))

            return real_asyncio_run(_wrapped())

        server.start()
        try:
            monkeypatch.setattr(session_token.clu, 'caller_credentials', caller_credentials)
            monkeypatch.setattr(session_token.kms_v1, 'KeyManagementServiceClient', build_sync_client)
            monkeypatch.setattr(session_token.derive.kms_v1, 'KeyManagementServiceAsyncClient', build_async_client)
            monkeypatch.setattr(session_token.asyncio, 'run', _run_and_close_channels)
            yield mac_requests
        finally:
            for channel in sync_channels:
                channel.close()
            server.stop(None).wait()


def test_one_mac_key_names_its_pinned_version(monkeypatch: pytest.MonkeyPatch) -> None:
    with _kms_loopback(monkeypatch, mac_keys=[_SIGNING_KEY]):
        assert session_token.signing_key_version('p') == f'{_SIGNING_KEY}/cryptoKeyVersions/1'


@pytest.mark.parametrize(
    'mac_keys',
    [[], [f'{_KEY_RING}/cryptoKeys/one', f'{_KEY_RING}/cryptoKeys/two']],
)
def test_anything_but_one_mac_key_is_refused(monkeypatch: pytest.MonkeyPatch, mac_keys: list[str]) -> None:
    with _kms_loopback(monkeypatch, mac_keys=mac_keys), pytest.raises(session_token.TokenError, match='--key-version'):
        session_token.signing_key_version('p')


@pytest.mark.parametrize(
    'status',
    [
        (grpc.StatusCode.PERMISSION_DENIED, 'cloudkms.cryptoKeys.list denied'),
        (grpc.StatusCode.FAILED_PRECONDITION, 'cloudkms.cryptoKeys.list denied'),
    ],
)
def test_a_kms_failure_is_reported_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch, status: tuple[grpc.StatusCode, str]
) -> None:
    with (
        _kms_loopback(monkeypatch, list_status=status),
        pytest.raises(session_token.clu.GoogleCallError, match=r'cloudkms\.cryptoKeys\.list denied'),
    ):
        session_token.signing_key_version('p')


def test_a_login_that_cannot_refresh_is_reported_in_its_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        _kms_loopback(monkeypatch, login=_Login(exceptions.RefreshError('Reauthentication is needed'))),
        pytest.raises(session_token.clu.GoogleCallError, match='Reauthentication is needed'),
    ):
        session_token.signing_key_version('p')


def test_main_derives_the_session_token_over_kms_and_writes_an_owner_only_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            self.queries.append((sql, params))

        def fetchone(self) -> tuple[str]:
            return ('sess-42',)

        def close(self) -> None:
            pass

    cursor = _Cursor()
    impersonated_signer = _Login()
    expected_sa = 'themis-clu@p.iam.gserviceaccount.com'

    @contextlib.contextmanager
    def fake_connect(project: str, service_account: str) -> Iterator[object]:
        assert (project, service_account) == ('p', expected_sa)

        class _Conn:
            def cursor(self) -> _Cursor:
                return cursor

        yield _Conn()

    def fake_impersonated(service_account: str) -> _Login:
        assert service_account == expected_sa
        return impersonated_signer

    token_file = tmp_path / 'sub' / 'token.json'
    monkeypatch.setattr(session_token.clu_database, 'connect', fake_connect)
    monkeypatch.setattr(session_token.clu, 'impersonated', fake_impersonated)
    monkeypatch.setattr(
        'sys.argv',
        [
            'session_token',
            '--project',
            'p',
            '--analysis-id',
            'analysis-99',
            '--session-token-file',
            str(token_file),
        ],
    )

    with _kms_loopback(monkeypatch, mac_keys=[_SIGNING_KEY], expected_signer=impersonated_signer) as mac_requests:
        session_token.main()

    assert len(cursor.queries) == 1
    assert cursor.queries[0][1] == ('analysis-99',)
    [req] = mac_requests
    assert req.name == f'{_SIGNING_KEY}/cryptoKeyVersions/1'
    assert req.data == b'sess-42'
    assert req.data_crc32c == google_crc32c.value(b'sess-42')
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert remote_mod.read_credentials(token_file) == remote_mod.Credentials(
        session_token=_EXPECTED_ENCODED,
        bearer=None,
        calling_as=sandbox_options_pb2.CALLING_AS_SELF,
    )
