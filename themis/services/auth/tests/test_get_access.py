"""``GetAccess`` over an in-process server as deployed: behind the gate, answering from the current ACL."""

from __future__ import annotations

import asyncio

import grpc
import grpc.aio
import pytest

from themis.clients.auth import claim as claim_mod
from themis.rpc import auth_pb2, sandbox_options_pb2
from themis.services.auth.tests import harness
from themis.testing import auth as auth_fixture


async def _get_access(
    served: harness.Served, email: str, metadata: auth_fixture.Metadata = auth_fixture.WEB
) -> auth_pb2.GetAccessResponse:
    return await served.stub.GetAccess(auth_pb2.GetAccessRequest(email=email), metadata=claim_mod.pairs(metadata))


async def _code(served: harness.Served, email: str, metadata: auth_fixture.Metadata) -> grpc.StatusCode:
    try:
        await _get_access(served, email, metadata)
    except grpc.aio.AioRpcError as e:
        return e.code()
    return grpc.StatusCode.OK


def _access(email: str) -> auth_pb2.GetAccessResponse:
    async def run() -> auth_pb2.GetAccessResponse:
        async with harness.serving() as served:
            return await _get_access(served, email)

    return asyncio.run(run())


def test_a_person_is_granted_each_project_they_hold_a_role_on_with_those_roles() -> None:
    assert _access(harness.JOHN) == auth_pb2.GetAccessResponse(
        projects=[auth_pb2.ProjectGrant(id='asd', roles=[auth_pb2.ROLE_MANAGER])]
    )


def test_the_operator_account_holds_both_roles_on_every_project_in_the_acl() -> None:
    both = [auth_pb2.ROLE_MANAGER, auth_pb2.ROLE_CURATOR]
    assert _access(harness.OPERATOR) == auth_pb2.GetAccessResponse(
        projects=[auth_pb2.ProjectGrant(id='asd', roles=both), auth_pb2.ProjectGrant(id='epi', roles=both)]
    )


def test_a_person_taken_off_their_last_project_is_not_admitted_after_the_next_refresh() -> None:
    async def run() -> list[bool]:
        async with harness.serving() as served:
            before = bool((await _get_access(served, harness.JOHN)).projects)
            served.backend.acl = harness.acl_version(
                {**harness.ACL, 'projects': {'asd': {'managers': [], 'curators': [harness.JANE]}}}
            )
            await served.acl.refresh()
            return [before, bool((await _get_access(served, harness.JOHN)).projects)]

    assert asyncio.run(run()) == [True, False]


def test_a_person_not_admitted_is_an_answer_not_an_error() -> None:
    assert _access('mallory@example.net') == auth_pb2.GetAccessResponse()


def test_the_email_is_matched_whatever_its_case() -> None:
    assert _access('John.Doe@Example.ORG') == _access(harness.JOHN)


@pytest.mark.parametrize(
    ('metadata', 'expected'),
    [
        pytest.param(auth_fixture.WEB, grpc.StatusCode.OK, id='web'),
        pytest.param(auth_fixture.CLU, grpc.StatusCode.OK, id='developer'),
        pytest.param(auth_fixture.AGENT, grpc.StatusCode.PERMISSION_DENIED, id='agent'),
        pytest.param(auth_fixture.WORKER, grpc.StatusCode.PERMISSION_DENIED, id='worker'),
        pytest.param((), grpc.StatusCode.UNAUTHENTICATED, id='no-credential'),
    ],
)
def test_only_the_web_tier_and_the_developer_reach_get_access(
    metadata: auth_fixture.Metadata, expected: grpc.StatusCode
) -> None:
    async def run() -> grpc.StatusCode:
        async with harness.serving() as served:
            return await _code(served, harness.JOHN, metadata)

    assert asyncio.run(run()) is expected


def test_the_gate_resolves_a_claimed_session_through_the_service_itself() -> None:
    bad_session = (*auth_fixture.CLU, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_SELF, 'bad'))

    async def run() -> list[grpc.StatusCode]:
        async with harness.serving() as served:
            return [
                await _code(served, harness.JOHN, auth_fixture.CLU_WITH_SESSION),
                await _code(served, harness.JOHN, bad_session),
            ]

    assert asyncio.run(run()) == [grpc.StatusCode.OK, grpc.StatusCode.PERMISSION_DENIED]


@pytest.mark.parametrize('email', [harness.JOHN, harness.OPERATOR])
def test_every_call_is_unavailable_once_the_copy_is_stale(email: str) -> None:
    async def run() -> list[grpc.StatusCode]:
        async with harness.serving() as served:
            served.backend.acl = b'{"format": 2}'
            served.clock.advance(harness.STALENESS_LIMIT_SECONDS + 1)
            await served.acl.refresh()
            stale = await _code(served, email, auth_fixture.WEB)
            served.backend.acl = harness.acl_version()
            await served.acl.refresh()
            return [stale, await _code(served, email, auth_fixture.WEB)]

    assert asyncio.run(run()) == [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.OK]


def test_a_request_naming_no_email_is_invalid() -> None:
    async def run() -> grpc.StatusCode:
        async with harness.serving() as served:
            return await _code(served, '', auth_fixture.WEB)

    assert asyncio.run(run()) is grpc.StatusCode.INVALID_ARGUMENT
