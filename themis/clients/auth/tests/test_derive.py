"""Tests for ``kms_deriver`` over a loopback KMS gRPC server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

import google_crc32c
import grpc
import grpc.aio
import pytest
from google.cloud import kms_v1
from google.cloud.kms_v1.services.key_management_service import transports

from themis.clients.auth import derive

_KEY_VERSION = 'projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1'


@contextlib.asynccontextmanager
async def _kms_loopback(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[kms_v1.MacSignRequest], kms_v1.MacSignResponse],
) -> AsyncIterator[list[kms_v1.MacSignRequest]]:
    seen: list[kms_v1.MacSignRequest] = []

    async def mac_sign(request: kms_v1.MacSignRequest, _ctx: grpc.aio.ServicerContext) -> kms_v1.MacSignResponse:
        seen.append(request)
        return respond(request)

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                'google.cloud.kms.v1.KeyManagementService',
                {
                    'MacSign': grpc.unary_unary_rpc_method_handler(
                        mac_sign,
                        request_deserializer=kms_v1.MacSignRequest.deserialize,
                        response_serializer=kms_v1.MacSignResponse.serialize,
                    )
                },
            )
        ]
    )
    port = server.add_insecure_port('127.0.0.1:0')
    real_client = kms_v1.KeyManagementServiceAsyncClient
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            monkeypatch.setattr(
                derive.kms_v1,
                'KeyManagementServiceAsyncClient',
                lambda **_kwargs: real_client(
                    transport=transports.KeyManagementServiceGrpcAsyncIOTransport(channel=channel)
                ),
            )
            yield seen
    finally:
        await server.stop(None)


def test_kms_deriver_signs_session_id_with_crc_and_encodes_urlsafe_unpadded_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mac = b'\xfb\xff\x03\x04'

    async def run() -> tuple[str, list[kms_v1.MacSignRequest]]:
        async with _kms_loopback(
            monkeypatch,
            lambda _req: kms_v1.MacSignResponse(
                mac=mac,
                mac_crc32c=google_crc32c.value(mac),
                verified_data_crc32c=True,
            ),
        ) as seen:
            sign = derive.kms_deriver(_KEY_VERSION)
            return await sign('sess-1'), seen

    bearer, [req] = asyncio.run(run())

    assert bearer == '-_8DBA'
    assert req.name == _KEY_VERSION
    assert req.data == b'sess-1'
    assert req.data_crc32c == google_crc32c.value(b'sess-1')


def test_kms_deriver_rejects_unverified_request_crc(monkeypatch: pytest.MonkeyPatch) -> None:
    mac = b'\x01\x02\x03\x04'

    async def run() -> str:
        async with _kms_loopback(
            monkeypatch,
            lambda _req: kms_v1.MacSignResponse(
                mac=mac,
                mac_crc32c=google_crc32c.value(mac),
                verified_data_crc32c=False,
            ),
        ):
            return await derive.kms_deriver(_KEY_VERSION)('sess-1')

    with pytest.raises(RuntimeError, match='request-data CRC'):
        asyncio.run(run())


@pytest.mark.parametrize(
    'response',
    [
        kms_v1.MacSignResponse(mac=b'\x01\x02\x03\x04', mac_crc32c=12345, verified_data_crc32c=True),
        kms_v1.MacSignResponse(mac=b'\x01\x02\x03\x04', verified_data_crc32c=True),
    ],
    ids=['corrupted-mac-crc', 'absent-mac-crc'],
)
def test_kms_deriver_rejects_missing_or_mismatched_mac_crc(
    monkeypatch: pytest.MonkeyPatch, response: kms_v1.MacSignResponse
) -> None:
    async def run() -> str:
        async with _kms_loopback(monkeypatch, lambda _req: response):
            return await derive.kms_deriver(_KEY_VERSION)('sess-1')

    with pytest.raises(RuntimeError, match='MAC response CRC mismatch'):
        asyncio.run(run())
