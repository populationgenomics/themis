"""The harness the auth servicer tests share: an ACL, a token store, and the server as deployed.

The server runs behind the auth interceptor with the shared authorization fixture's callers, and its
gate resolves a claim's session through the servicer itself, as the entrypoint wires it. The ACL
is read from a backend whose next read a test sets, and its age is measured on a clock a test advances, so a test
drives refreshes and staleness without sleeping. Imported by the test modules as a module.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Mapping
from typing import override

from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import rules
from themis.rpc import auth_pb2_grpc
from themis.services.auth import acl_holder
from themis.services.auth import backend as auth_backend
from themis.services.auth import servicer as servicer_mod
from themis.testing import auth as auth_fixture
from themis.testing import in_process_grpc

STALENESS_LIMIT_SECONDS = 600
READ_TIMEOUT_SECONDS = 5

JOHN = 'john.doe@example.org'
JANE = 'jane.roe@example.com'
# The deployment's operator account in the shared fixture's project; the ACL never lists it.
OPERATOR = rules.operator_email(auth_fixture.PROJECT)

# Bound to an Analysis in Project `asd`; the second is the token the shared fixture's claims carry.
TOKEN = 'tok-asd'
CLAIMED_TOKEN = 'good'
ANALYSIS_ID = 'an_1'

ACL: dict[str, object] = {
    'format': 1,
    'projects': {
        'asd': {'managers': [JOHN], 'curators': [JANE]},
        'epi': {'managers': [], 'curators': [JANE]},
    },
}


def acl_version(document: object = None) -> bytes:
    """``document`` (``ACL`` when omitted) as the bytes of a version of the ACL."""
    return json.dumps(ACL if document is None else document).encode('utf-8')


class Clock:
    """A monotonic clock that moves only when a test advances it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Backend(auth_backend.AuthBackend):
    """A token store fixed at construction, and an ACL whose next read a test sets.

    Attributes:
        acl: What the next read returns, or the error it raises.
    """

    def __init__(self, bindings: Mapping[str, auth_backend.StoredSession], acl: bytes) -> None:
        self._by_hash = {auth_backend.hash_token(token): stored for token, stored in bindings.items()}
        self.acl: bytes | Exception = acl

    @override
    async def resolve(self, session_token: str) -> auth_backend.StoredSession:
        try:
            return self._by_hash[auth_backend.hash_token(session_token)]
        except KeyError:
            raise auth_backend.UnresolvedError from None

    @override
    async def read_acl(self) -> bytes:
        if isinstance(self.acl, Exception):
            raise self.acl
        return self.acl


@dataclasses.dataclass(frozen=True)
class Served:
    stub: auth_pb2_grpc.AuthAsyncStub
    backend: Backend
    clock: Clock
    acl: acl_holder.AclHolder


@contextlib.asynccontextmanager
async def serving() -> AsyncIterator[Served]:
    """The auth servicer behind the gate, over the token store above and an ACL that starts as ``ACL``."""
    clock = Clock()
    stored = auth_backend.StoredSession(analysis_id=ANALYSIS_ID, project_id='asd')
    backend = Backend({TOKEN: stored, CLAIMED_TOKEN: stored}, acl_version())
    acl = await acl_holder.AclHolder.load(
        backend.read_acl,
        read_timeout_seconds=READ_TIMEOUT_SECONDS,
        staleness_limit_seconds=STALENESS_LIMIT_SECONDS,
        clock=clock,
    )
    servicer = servicer_mod.Servicer(backend, acl, operator=OPERATOR)
    authorizer = dataclasses.replace(
        auth_fixture.authorizer(), session_resolver=servicer_mod.in_process_session_resolver(servicer)
    )
    async with in_process_grpc.serving(
        lambda server: auth_pb2_grpc.add_AuthServicer_to_server(servicer, server),
        server_interceptors=(interceptor_mod.AuthInterceptor(authorizer),),
    ) as channel:
        yield Served(stub=auth_pb2_grpc.AuthStub(channel), backend=backend, clock=clock, acl=acl)
