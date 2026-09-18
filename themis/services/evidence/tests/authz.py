"""What the evidence servicer tests add to the shared authorization fixture (`themis.testing.auth`).

Every rpc is gated by the auth interceptor, so a behaviour test needs the interceptor on its server and an
admitting credential on the call. The fixture holds the callers and the interceptor; this names the
credential a literature read is driven with by default, and the context to bind around a backend called
directly.
"""

from __future__ import annotations

from themis.clients.auth import context as auth_context
from themis.rpc import sandbox_options_pb2
from themis.testing import auth as auth_fixture

WEB = auth_fixture.WEB
CLU = auth_fixture.CLU
SANDBOX_JOB = auth_fixture.SANDBOX_JOB
AGENT = auth_fixture.AGENT
AGENT_BAD_SESSION = auth_fixture.AGENT_BAD_SESSION
WORKER = auth_fixture.WORKER
# Admitted to every literature rpc: the maintainer's account, which every literature contract names,
# calling as itself. Injected so a behaviour test reaches the servicer without stating a caller.
ADMITTED: auth_fixture.Metadata = auth_fixture.CLU

# A context to bind around a servicer method or backend called directly, off any server.
AUTH = auth_context.AuthContext(
    caller=auth_fixture.CLU_EMAIL, calling_as=sandbox_options_pb2.CALLING_AS_SELF, session=auth_fixture.SESSION
)

authorizer = auth_fixture.authorizer
server_interceptors = auth_fixture.server_interceptors
InjectMetadata = auth_fixture.InjectMetadata
