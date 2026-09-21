"""What the evidence servicer tests add to the shared authorization fixture (`themis.testing.auth`).

Every rpc is gated by the auth interceptor, so a behaviour test needs the interceptor on its server and an
admitting credential on the call. The fixture holds the callers and the interceptor; this names the
credential each suite is driven with — the agent for the database-backed interfaces, through `gated`,
and the universal caller for literature — and the context to bind around a backend called directly.
"""

from __future__ import annotations

import contextlib

import grpc.aio

from themis.clients.auth import context as auth_context
from themis.rpc import sandbox_options_pb2
from themis.testing import auth as auth_fixture
from themis.testing import in_process_grpc

WEB = auth_fixture.WEB
CLU = auth_fixture.CLU
SANDBOX_JOB = auth_fixture.SANDBOX_JOB
# The production caller of every database-backed evidence rpc, so a contract that stops naming it fails
# that interface's suite; the universal caller below would be admitted whatever the contract says.
AGENT = auth_fixture.AGENT
AGENT_BAD_SESSION = auth_fixture.AGENT_BAD_SESSION
WORKER = auth_fixture.WORKER
# Admitted to every literature rpc, whose contracts name web or agent per rpc: the maintainer's account,
# the universal caller, calling as itself. Injected so a behaviour test reaches the servicer without
# stating a caller.
ADMITTED: auth_fixture.Metadata = auth_fixture.CLU

# A context to bind around a servicer method or backend called directly, off any server.
AUTH = auth_context.AuthContext(
    caller=auth_fixture.CLU_EMAIL, calling_as=sandbox_options_pb2.CALLING_AS_SELF, session=auth_fixture.SESSION
)

authorizer = auth_fixture.authorizer
server_interceptors = auth_fixture.server_interceptors
InjectMetadata = auth_fixture.InjectMetadata


def gated(register: in_process_grpc.Register) -> contextlib.AbstractAsyncContextManager[grpc.aio.Channel]:
    """The gated in-process server `register` populates, and a channel whose every call is the agent's."""
    return in_process_grpc.serving(
        register, interceptors=(InjectMetadata(AGENT),), server_interceptors=server_interceptors()
    )
