"""The harness the remote-store tests share: the sheaf servicer served in-process, and a client over it.

The servicer runs on its own thread over `LocalBackend`, behind the auth interceptor with the session
resolver the servicer suite uses, so the client under test speaks to the real service code over a real
socket — status codes, metadata and streaming included — and a second writer can publish through
`themis.sheaf.Store` over the same backend to race it. The client sends its ID token over TLS only, so
over the loopback channel the gate is told to take every call as the sandbox job's; what the client
claims, and which session, still travels as metadata and is judged. Imported by the test modules as a
module, as `themis/sheaf/tests/conftest.py` is.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
from collections.abc import AsyncIterator, Iterator
from typing import override

import grpc
import grpc.aio
import pytest
from google.protobuf import empty_pb2

from themis import sheaf
from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth.tests import fixture_session
from themis.clients.sheaf import store as remote_mod
from themis.rpc import sandbox_options_pb2, sheaf_pb2, sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod
from themis.services.sheaf.tests import conftest as service_conftest
from themis.testing import auth as auth_fixture
from themis.testing import in_process_grpc

# The repository the fixture session names, which is also the name the client serves it under.
ANALYSIS_ID = fixture_session.ANALYSIS_ID
REF = 'refs/heads/main'


@contextlib.contextmanager
def serving(
    backend: sheaf.Backend,
    limits: servicer_mod.Limits = service_conftest.LIMITS,
    servicer_class: type[servicer_mod.Servicer] = servicer_mod.Servicer,
) -> Iterator[str]:
    """Serve `backend` through the sheaf servicer on a loopback port; yield the `host:port`.

    `servicer_class` lets a test serve a subclass that fails a call the way a deployment would.
    """
    servicer = servicer_class(backend, limits)
    authorizer = dataclasses.replace(
        service_conftest.authorizer(), verify_caller=auth_fixture.caller_presumed(auth_fixture.SANDBOX_JOB_EMAIL)
    )
    gate = interceptor_mod.AuthInterceptor(authorizer)
    with in_process_grpc.serving_in_thread(
        lambda server: sheaf_pb2_grpc.add_SheafServicer_to_server(servicer, server), server_interceptors=(gate,)
    ) as target:
        yield target


def write_token_file(
    path: pathlib.Path,
    session_token: str,
    bearer: str | None = None,
    calling_as: sandbox_options_pb2.CallingAs = sandbox_options_pb2.CALLING_AS_WORKER_SESSION,
) -> pathlib.Path:
    """Write the token file `RemoteStore` reads, as the worker writes it: the worker's claim on `session_token`."""
    credentials = remote_mod.Credentials(session_token=session_token, bearer=bearer, calling_as=calling_as)
    remote_mod.write_credentials(path, credentials)
    return path


@pytest.fixture
def backend(tmp_path: pathlib.Path) -> sheaf.LocalBackend:
    return sheaf.LocalBackend(tmp_path / 'store')


@pytest.fixture
def token_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """A token file carrying the fixture session's token and no bearer: the in-process channel is plain."""
    return write_token_file(tmp_path / 'token.json', fixture_session.GOOD_TOKEN)


@pytest.fixture
def target(backend: sheaf.LocalBackend) -> Iterator[str]:
    with serving(backend) as target:
        yield target


@pytest.fixture
def remote(target: str, token_file: pathlib.Path) -> Iterator[remote_mod.RemoteStore]:
    with remote_mod.RemoteStore(target, token_file, repo=ANALYSIS_ID) as store:
        yield store


class Flaky(servicer_mod.Servicer):
    """Answers UNAVAILABLE to the first `unavailable_reads` ReadRefDoc calls, then serves; counts every call."""

    unavailable_reads = 2

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.read_calls = 0

    @override
    async def ReadRefDoc(self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext) -> sheaf_pb2.RefDocSnapshot:
        self.read_calls += 1
        if self.read_calls <= self.unavailable_reads:
            await context.abort(grpc.StatusCode.UNAVAILABLE, 'instance recycling')
        return await super().ReadRefDoc(request, context)


class PublishDown(servicer_mod.Servicer):
    """Drains a publish and answers UNAVAILABLE: the service went away under the upload."""

    @override
    async def Publish(
        self, request_iterator: AsyncIterator[sheaf_pb2.PublishRequest], context: grpc.aio.ServicerContext
    ) -> sheaf_pb2.PublishResponse:
        async for _ in request_iterator:
            pass
        await context.abort(grpc.StatusCode.UNAVAILABLE, 'instance recycling')
        raise AssertionError('abort returns nothing')


def direct(backend: sheaf.Backend, analysis_id: str = ANALYSIS_ID) -> sheaf.Store:
    """The in-process store over the same repository: the second writer, and the oracle."""
    return sheaf.Store(backend, analysis_id)
