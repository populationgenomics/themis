"""Ready stubs for the internal Themis services, reached through the sandbox hatch (postern-sandbox-swap.md §4).

Guest-side: runs inside the postern sandbox, shipped into the guest rootfs as ``themis.agent.services`` (the Dockerfile
remaps it there so the agent's code-mode snippets import it under that stable name). Each function returns a gRPC stub
over a single, lazily-created channel to the hatch UDS at ``unix:$POSTERN_HATCH``. The trusted worker's hatch injects
the session token and forwards to the real service, so calling code holds no credentials and no service URL — get a
stub, keep it, and call it. Only the allowlisted methods are reachable; anything else is ``PERMISSION_DENIED``.

TODO: generate this module from the proto so the guest stubs stay in lockstep with the hatch allowlist
(``hatch.GUEST_METHODS``) by construction, rather than hand-authoring both.
"""

from __future__ import annotations

import functools
import os

import grpc

from themis.rpc import hello_pb2_grpc


@functools.cache
def _channel() -> grpc.Channel:
    return grpc.insecure_channel('unix:' + os.environ['POSTERN_HATCH'])


def hello() -> hello_pb2_grpc.HelloStub:
    """``hello``: echo a note against the Analysis the session is bound to (a connectivity check)."""
    return hello_pb2_grpc.HelloStub(_channel())
