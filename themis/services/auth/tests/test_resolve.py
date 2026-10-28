"""``ResolveSession`` over an in-process server as deployed: ungated, and answering from the current ACL."""

from __future__ import annotations

import asyncio

import grpc
import grpc.aio
import pytest

from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2
from themis.services.auth import servicer as servicer_mod
from themis.services.auth.tests import harness

_ASD = auth_pb2.ProjectGrant(id='asd')


async def _resolve(served: harness.Served, token: str) -> auth_pb2.SessionContext:
    # No credential at all: the gate passes this rpc through, as admission is derived through it.
    return await served.stub.ResolveSession(auth_pb2.ResolveTokenRequest(session_token=token))


async def _code(served: harness.Served, token: str) -> grpc.StatusCode:
    try:
        await _resolve(served, token)
    except grpc.aio.AioRpcError as e:
        return e.code()
    return grpc.StatusCode.OK


def _acl_with(**projects: object) -> bytes:
    return harness.acl_version({**harness.ACL, 'projects': projects})


def test_a_token_resolves_to_its_analysis_and_its_project_s_grant() -> None:
    async def run() -> auth_pb2.SessionContext:
        async with harness.serving() as served:
            return await _resolve(served, harness.TOKEN)

    assert asyncio.run(run()) == auth_pb2.SessionContext(analysis_id=harness.ANALYSIS_ID, project=_ASD)


def test_an_unknown_token_is_permission_denied() -> None:
    async def run() -> grpc.StatusCode:
        async with harness.serving() as served:
            return await _code(served, 'nope')

    assert asyncio.run(run()) is grpc.StatusCode.PERMISSION_DENIED


def test_a_token_whose_project_left_the_acl_stops_resolving_at_the_next_refresh() -> None:
    async def run() -> list[grpc.StatusCode]:
        async with harness.serving() as served:
            before = await _code(served, harness.TOKEN)
            served.backend.acl = _acl_with(epi=harness.ACL['projects']['epi'])  # pyright: ignore[reportIndexIssue]
            await served.acl.refresh()
            return [before, await _code(served, harness.TOKEN)]

    assert asyncio.run(run()) == [grpc.StatusCode.OK, grpc.StatusCode.PERMISSION_DENIED]


def test_every_call_is_unavailable_once_the_copy_is_stale_and_resolves_again_once_a_version_loads() -> None:
    async def run() -> list[grpc.StatusCode]:
        async with harness.serving() as served:
            served.backend.acl = OSError('secret unreachable')
            served.clock.advance(harness.STALENESS_LIMIT_SECONDS + 1)
            await served.acl.refresh()
            stale = [await _code(served, harness.TOKEN), await _code(served, 'nope')]
            served.backend.acl = harness.acl_version()
            await served.acl.refresh()
            return [*stale, await _code(served, harness.TOKEN)]

    assert asyncio.run(run()) == [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.OK]


def test_the_gate_s_in_process_resolver_reports_a_gone_project_as_unresolved_and_a_stale_copy_as_unavailable() -> None:
    async def run() -> grpc.StatusCode:
        async with harness.serving() as served:
            resolver = servicer_mod.in_process_session_resolver(
                servicer_mod.Servicer(served.backend, served.acl, operator=harness.OPERATOR)
            )
            served.backend.acl = _acl_with(epi=harness.ACL['projects']['epi'])  # pyright: ignore[reportIndexIssue]
            await served.acl.refresh()
            with pytest.raises(session_mod.UnresolvedSessionError):
                await resolver(harness.TOKEN)
            served.clock.advance(harness.STALENESS_LIMIT_SECONDS + 1)
            with pytest.raises(interceptor_mod.StatusError) as stale:
                await resolver(harness.TOKEN)
            return stale.value.code

    assert asyncio.run(run()) is grpc.StatusCode.UNAVAILABLE
