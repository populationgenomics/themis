"""The ACL read from Secret Manager, through the real client against an in-process Secret Manager server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator, Mapping
from concurrent import futures

import grpc
import pytest
from google.api_core import exceptions
from google.cloud import secretmanager
from google.cloud.secretmanager_v1.services.secret_manager_service.transports import grpc as secretmanager_grpc

from themis.services.auth import secret

_SECRET = 'projects/themis-proj/secrets/acl'


@contextlib.contextmanager
def _secret_manager(versions: Mapping[str, bytes]) -> Iterator[secretmanager.SecretManagerServiceClient]:
    """A client of a Secret Manager that serves each version name in ``versions`` and nothing else."""

    def access(
        request: secretmanager.AccessSecretVersionRequest, context: grpc.ServicerContext
    ) -> secretmanager.AccessSecretVersionResponse:
        if request.name not in versions:
            context.abort(grpc.StatusCode.NOT_FOUND, f'{request.name} not found')
        return secretmanager.AccessSecretVersionResponse(
            name=request.name, payload=secretmanager.SecretPayload(data=versions[request.name])
        )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                'google.cloud.secretmanager.v1.SecretManagerService',
                {
                    'AccessSecretVersion': grpc.unary_unary_rpc_method_handler(
                        access,
                        request_deserializer=secretmanager.AccessSecretVersionRequest.deserialize,
                        response_serializer=secretmanager.AccessSecretVersionResponse.serialize,
                    )
                },
            )
        ]
    )
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    channel = grpc.insecure_channel(f'127.0.0.1:{port}')
    try:
        yield secretmanager.SecretManagerServiceClient(
            transport=secretmanager_grpc.SecretManagerServiceGrpcTransport(channel=channel)
        )
    finally:
        channel.close()
        server.stop(None)


def test_the_latest_version_is_read() -> None:
    content = b'{"format": 1, "projects": {}}'
    with _secret_manager({f'{_SECRET}/versions/latest': content}) as client:
        assert asyncio.run(secret.AclSecret(_SECRET, client).read()) == content


def test_a_secret_with_no_version_raises_the_clients_error() -> None:
    with _secret_manager({}) as client, pytest.raises(exceptions.NotFound):
        asyncio.run(secret.AclSecret(_SECRET, client).read())


@pytest.mark.parametrize(
    'name',
    [
        pytest.param('acl', id='bare-id'),
        pytest.param(f'{_SECRET}/versions/latest', id='a-version'),
        pytest.param('projects//secrets/acl', id='no-project'),
        pytest.param('projects/themis-proj/secrets/', id='no-secret'),
    ],
)
def test_a_name_that_is_not_a_secret_is_refused(name: str) -> None:
    with _secret_manager({}) as client, pytest.raises(ValueError, match='projects/<project>/secrets/<secret>'):
        secret.AclSecret(name, client)
